"""Background-job admission and launch on the real engagement runtime."""
from __future__ import annotations

import asyncio
from pathlib import Path
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import background_jobs as jobs
import tools
from bus import MessageBus
from channels import ChannelManager
from channels.telegram import TelegramChannel
from drivers.in_casa_driver import InCasaDriver
from engagement_registry import EngagementRegistry
from mcp_registry import McpServerRegistry
from specialist_limits import SpecialistLimiter
from specialist_registry import SpecialistRegistry
from test_delegate_to_agent_interactive import _make_alex_cfg, _make_assistant_cfg
from test_in_casa_launch_terminal_artifact import ScriptedCompleteClient

pytestmark = pytest.mark.asyncio
DECL = jobs.JobDecl('ledger:classify', 'ledger', 'classify', 'ledger:classify',
                    'Classify entries', None)


class Client(ScriptedCompleteClient):
    instances = []

    def __init__(self, options):
        super().__init__(options)
        self.prompts = []
        type(self).instances.append(self)

    async def record_query(self, prompt):
        self.prompts.append(prompt)


async def drain_launch_turns(timeout: float = 5.0) -> None:
    """Wait until every job's queued writes are written and the turns they
    opened have ended: the launch returns once the brief is queued, and the
    job's delivery worker writes it afterwards."""
    driver = agent.active_engagement_driver
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        busy = (any(lane.queue for lane in driver._lanes.values())
                or any(s.turn_open for s in driver._sessions.values()))
        if not busy:
            break
        assert loop.time() < deadline, 'a job write or turn never finished'
        await asyncio.sleep(.005)
    for _ in range(10):
        await asyncio.sleep(0)


@pytest.fixture
async def runtime(tmp_path, monkeypatch):
    # A periodic loop wakeup also works in sandboxes that block self-pipe writes.
    loop = asyncio.get_running_loop()
    def tick():
        nonlocal timer
        timer = loop.call_later(.005, tick)
    timer = loop.call_later(.005, tick)
    bus = MessageBus()
    bus.register('assistant', None)
    bot = SimpleNamespace(
        create_forum_topic=AsyncMock(return_value=SimpleNamespace(message_thread_id=42)),
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        edit_message_text=AsyncMock(return_value=SimpleNamespace(message_id=100)),
        send_chat_action=AsyncMock(return_value=True),
    )
    # No forum group: a job needs none.
    channel = TelegramChannel(chat_id='1', bot=bot, bus=bus)
    channel._app = SimpleNamespace(bot=bot)
    registry = EngagementRegistry(tombstone_path=str(tmp_path / 'eng.json'))
    channel._engagement_registry = registry
    driver = InCasaDriver(persist_session_id=registry.persist_session_id,
                          record_lookup=registry.get)
    cm = ChannelManager()
    cm.register(channel)
    specialists = SpecialistRegistry(str(tmp_path / 'specialists'),
                                     tombstone_path=str(tmp_path / 'del.json'))
    cfg = _make_alex_cfg()
    cfg.cwd = str(tmp_path)
    specialists._configs['finance'] = cfg
    caller = _make_assistant_cfg()
    limiter = SpecialistLimiter(max_global=4)
    tools.init_tools(channel_manager=cm, bus=bus, specialist_registry=specialists,
                     mcp_registry=McpServerRegistry(), trigger_registry=None,
                     engagement_registry=registry, specialist_limiter=limiter,
                     agent_role_map={'assistant': caller, 'finance': cfg})
    monkeypatch.setattr(agent, 'active_engagement_driver', driver, raising=False)
    # The plugin snapshot is a module global: a file that ran earlier on the
    # same worker can leave one assigning a plugin to `specialist:finance`,
    # and the job record then names that plugin instead of this job's.
    import plugin_registry
    monkeypatch.setattr(plugin_registry, '_snapshot', None)
    monkeypatch.setattr('drivers.in_casa_driver.ClaudeSDKClient', Client)
    monkeypatch.setattr(tools, 'ClaudeSDKClient', Client)
    monkeypatch.setattr(Client, 'instances', [])
    # U1 owns discovery; these seams enforce its delegate-filtered interface.
    host = jobs.JobHost('specialist', 'finance', DECL, SimpleNamespace(name='ledger'))
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, roles:
                        host if name == DECL.qualified_name and 'finance' in roles else None)
    monkeypatch.setattr(jobs, 'startable_jobs', lambda caller, roles:
                        [host] if 'finance' in roles else [])
    yield SimpleNamespace(registry=registry, driver=driver, channel=channel, bot=bot,
                          cfg=cfg, caller=caller, limiter=limiter)
    await drain_launch_turns()
    await tools.drain_launch_death_reports()
    for rec in registry.active_and_idle():
        await driver.cancel(rec)
    # Keep the wakeup through pytest's executor shutdown; closing the loop
    # clears its scheduled handles.


async def call(tool=None, *, channel='telegram', chat='1', **args):
    token = agent.origin_var.set(dict(role='assistant', execution_role='assistant',
                                    channel=channel, chat_id=chat, cid='test',
                                    user_text='classify', _operator_turn=True))
    try:
        envelope = await (tool or tools.start_job).handler(
            dict(job=DECL.qualified_name, task='Classify the ledger', context='all rows', **args))
        return json.loads(envelope['content'][0]['text'])
    finally:
        agent.origin_var.reset(token)


def cli_limit(options):
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    command = SubprocessCLITransport(prompt='test', options=options)._build_command()
    return command[command.index('--max-turns') + 1]


async def test_launch_limit_prompt_envelope_and_persistence(runtime):
    result = await call()
    assert result['status'] == 'pending'
    assert (result['job'], result['title'], result['agent']) == (DECL.qualified_name, DECL.title, 'finance')
    assert result['message'] == "Started Classify entries."      # no log group: no log named
    await drain_launch_turns()
    client = Client.instances[0]
    assert cli_limit(client.options) == str(jobs.JOB_MAX_TURNS)
    rec = runtime.registry.get(result['engagement_id'])
    # The launch prompt names the job by its engagement id (INV-BGJOB-016).
    # #1277: the harness origin is the operator's own turn.
    assert client.prompts == [jobs.launch_prompt(DECL, 'Classify the ledger', 'all rows',
                                                 job_id=rec.id, started_by='operator')]
    assert runtime.bot.create_forum_topic.await_count == 0
    expected = {**jobs.initial_job_state(DECL, started_by='operator', context='all rows'),
                'brief_uuid': rec.origin['job']['brief_uuid']}
    # The cost a turn end adds is pinned in test_job_cost.py.
    assert {k: v for k, v in rec.origin['job'].items() if k != 'cost_usd'} == expected
    rows = json.loads(Path(runtime.registry._tombstone_path).read_text())
    assert {k: v for k, v in rows[0]['origin']['job'].items() if k != 'cost_usd'} == expected
    assert jobs.JOB_CASA_GRANTS[0] in client.options.allowed_tools



@pytest.mark.parametrize('outside', [False, True])
async def test_undeclared_job_or_host_outside_delegates(runtime, outside):
    if outside:
        runtime.caller.delegates = []
        result = await call()
    else:
        token = agent.origin_var.set({'role': 'assistant'})
        try:
            result = json.loads((await tools.start_job.handler(
                {'job': 'missing:job', 'task': 't', 'context': ''}))['content'][0]['text'])
        finally:
            agent.origin_var.reset(token)
    assert result['kind'] == 'job_not_declared'
    assert ('none' if outside else DECL.qualified_name) in result['message']
    assert not Client.instances


async def test_voice_needs_text_channel(runtime):
    assert (await call(channel='voice'))['kind'] == 'job_needs_text_channel'
    assert not runtime.registry.active_and_idle()


@pytest.mark.parametrize('second_job', [False, True])
async def test_second_engagement_from_another_chat_names_open_job(runtime, second_job):
    first = await call()
    await drain_launch_turns()
    if second_job:
        result = await call(chat='2')
    else:
        # another plugin's job on the same specialist: past its own plugin's
        # claim, the per-specialist rule refuses it (one open engagement)
        other = jobs.JobDecl('budget:review', 'budget', 'review', 'budget:review',
                             'Review budget', None)
        origin = dict(role='assistant', execution_role='assistant', channel='telegram',
                      chat_id='2', cid='test', user_text='review', _operator_turn=True,
                      plugin_job={'plugin': 'budget'})
        result = json.loads((await tools._launch_interactive_engagement(
            'finance', 'Review it', '', origin, job=other))['content'][0]['text'])
    assert result['engagement_id'] == first['engagement_id']
    assert 'topic_id' not in result
    if second_job:
        assert result['kind'] == 'job_busy'
        assert result['plugin'] == 'ledger'
        assert result['message'] == (
            f"ledger already has a running job: {DECL.title}. Wait for it to "
            "finish, or cancel it with the Cancel button on its start line or "
            "by asking me.")
    else:
        assert result['kind'] == 'engagement_busy'
        assert result['agent'] == 'finance'
        assert DECL.title in result['message'] and 'Alex' in result['message']
        assert 'Cancel button' in result['message'] and 'topic' not in result['message']
    assert runtime.bot.create_forum_topic.await_count == 0


async def test_with_a_log_group_the_start_names_the_log_and_logs_it(runtime, monkeypatch):
    """With the agent log on, the launch logs the job's start in its
    specialist's log and the result says where its progress appears."""
    from agent_log_double import RecordingLogs
    from agent_logs import LogKey
    logs = RecordingLogs()
    monkeypatch.setattr(tools, '_agent_logs', logs)
    result = await call()
    assert result['message'] == "Started Classify entries. Progress appears in Alex's log."
    await drain_launch_turns()
    assert logs.lines[0] == (LogKey('agent', 'finance'), DECL.title, '▶ started')
    assert runtime.bot.create_forum_topic.await_count == 0


async def test_sync_delegation_while_job_open(runtime):
    first = await call()
    await drain_launch_turns()
    result = await call(tools.delegate_to_agent, agent='finance', mode='sync')
    assert result['status'] == 'ok', result
    assert runtime.registry.get(first['engagement_id']).status == 'active'
    assert len(Client.instances) == 2
    assert cli_limit(Client.instances[1].options) == '20'
    assert jobs.JOB_CASA_GRANTS[0] not in Client.instances[1].options.allowed_tools


async def test_resume_keeps_limit_and_job_grant(runtime):
    result = await call()
    await drain_launch_turns()
    rec = runtime.registry.get(result['engagement_id'])
    opts = tools.build_engagement_resume_options(rec, 'resumed-session')
    assert cli_limit(opts) == str(jobs.JOB_MAX_TURNS)
    assert opts.resume == 'resumed-session'
    assert jobs.JOB_CASA_GRANTS[0] in opts.allowed_tools



async def test_host_resolution_preserves_execution_callers_delegate_order(runtime, monkeypatch):
    from config import DelegateEntry
    runtime.caller.delegates = [DelegateEntry(agent=r, purpose='p', when='w')
                                for r in ['second', 'finance', 'first']]
    seen = []
    def find(name, caller, roles):
        seen.append((name, caller, list(roles)))
        return None
    monkeypatch.setattr(jobs, 'find_job_host', find)
    token = agent.origin_var.set({'role': 'finance', 'execution_role': 'assistant'})
    try:
        await tools.start_job.handler({'job': 'unknown', 'task': 't', 'context': ''})
    finally:
        agent.origin_var.reset(token)
    assert seen == [('unknown', 'assistant', ['second', 'finance', 'first'])]


async def test_start_job_is_registered_and_granted_only_to_assistant():
    import yaml
    defaults = Path(__file__).parents[1] / 'casa/rootfs/opt/casa/defaults'
    grant = 'mcp__casa-framework__start_job'
    for role in ['assistant', 'butler', 'concierge']:
        for name in [f'roles/resident/{role}/role.yaml', f'agents/{role}/runtime.yaml']:
            cfg = yaml.safe_load((defaults / name).read_text())
            allowed = cfg['tools']['allowed']
            assert (grant in allowed) == (role == 'assistant')
            assert ('start_job' in {t.name for t in tools.select_casa_tools(frozenset(allowed))}) == (role == 'assistant')


async def test_a_job_starts_acts_and_is_busy_refused_with_no_group(runtime):
    """No forum group is configured: the job starts, a protected call made
    inside it binds to the operator, and a second start of the same plugin's
    job is refused with the Cancel-button text — no topic anywhere."""
    from authz_grants import GrantIdentity, resolve_grant_identity
    assert not runtime.channel.engagement_supergroup_id
    token = agent.origin_var.set(dict(role='assistant', execution_role='assistant',
                                      channel='telegram', chat_id='1', user_id='7',
                                      cid='test', user_text='classify',
                                      _operator_turn=True))
    try:
        first = json.loads((await tools.start_job.handler(dict(
            job=DECL.qualified_name, task='Classify the ledger', context='')))['content'][0]['text'])
    finally:
        agent.origin_var.reset(token)
    assert first['status'] == 'pending', first
    await drain_launch_turns()
    rec = runtime.registry.get(first['engagement_id'])

    token_o = agent.origin_var.set({'role': 'finance', 'execution_role': 'finance',
                                    'channel': 'telegram', 'source': 'telegram',
                                    'message_type': 'channel_in', 'chat_id': 1,
                                    'user_id': 7})
    token_e = tools.engagement_var.set(rec)
    try:
        identity, why = resolve_grant_identity('finance')
    finally:
        tools.engagement_var.reset(token_e)
        agent.origin_var.reset(token_o)
    assert why is None
    assert identity == GrantIdentity(7, 1, 'finance', '', rec.id, target_role='finance')

    again = await call()
    assert again['kind'] == 'job_busy'
    assert again['message'].endswith(
        "Wait for it to finish, or cancel it with the Cancel button on its "
        "start line or by asking me.")
    assert runtime.bot.create_forum_topic.await_count == 0
