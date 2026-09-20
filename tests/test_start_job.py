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
                    'Classify entries', None, None, 30)


class Client(ScriptedCompleteClient):
    instances = []
    gate = None

    def __init__(self, options):
        super().__init__(options)
        self.prompts = []
        self.owner_counts = []
        type(self).instances.append(self)

    async def query(self, prompt):
        self.prompts.append(prompt)
        self.owner_counts.append(sum(jobs._turn_owners.values()))

    async def receive_response(self):
        if self.gate is not None:
            await self.gate.wait()
        async for frame in super().receive_response():
            yield frame


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
        edit_forum_topic=AsyncMock(return_value=True),
        close_forum_topic=AsyncMock(return_value=True),
        send_chat_action=AsyncMock(return_value=True),
    )
    channel = TelegramChannel(chat_id='1', bot=bot, bus=bus,
                              engagement_supergroup_id=-1001)
    channel._app = SimpleNamespace(bot=bot)
    channel.engagement_permission_ok = True
    registry = EngagementRegistry(tombstone_path=str(tmp_path / 'eng.json'), bus=bus)
    channel._engagement_registry = registry
    driver = InCasaDriver(topic_stream_factory=channel.create_topic_stream,
                          persist_session_id=registry.persist_session_id,
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
    monkeypatch.setattr('drivers.in_casa_driver.ClaudeSDKClient', Client)
    monkeypatch.setattr(tools, 'ClaudeSDKClient', Client)
    monkeypatch.setattr(Client, 'instances', [])
    monkeypatch.setattr(Client, 'gate', None)
    calls = []
    async def after(rec, ch):
        calls.append((rec.id, jobs.turn_owners(rec.id), ch))
    monkeypatch.setattr(jobs, 'job_after_turn', after)
    # U1 owns discovery; these seams enforce its delegate-filtered interface.
    host = jobs.JobHost('specialist', 'finance', DECL, SimpleNamespace(name='ledger'))
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, roles:
                        host if name == DECL.qualified_name and 'finance' in roles else None)
    monkeypatch.setattr(jobs, 'startable_jobs', lambda caller, roles:
                        [host] if 'finance' in roles else [])
    yield SimpleNamespace(registry=registry, driver=driver, channel=channel, bot=bot,
                          cfg=cfg, caller=caller, limiter=limiter, calls=calls)
    await tools.drain_launch_turns()
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
    assert result['message'] == "Started Classify entries. Progress appears in Alex's topic."
    await tools.drain_launch_turns()
    client = Client.instances[0]
    assert cli_limit(client.options) == '30'
    assert client.prompts == [jobs.launch_prompt(DECL, 'Classify the ledger', 'all rows', 30)]
    assert DECL.title in runtime.bot.create_forum_topic.call_args.kwargs['name']
    rec = runtime.registry.get(result['engagement_id'])
    assert rec.origin['job'] == jobs.initial_job_state(DECL)
    rows = json.loads(Path(runtime.registry._tombstone_path).read_text())
    assert rows[0]['origin']['job'] == jobs.initial_job_state(DECL)
    assert jobs.JOB_CASA_GRANTS[0] in client.options.allowed_tools
    assert 'report_job_progress' in tools.engagement_casa_grant_names(rec)


async def test_launch_hook_runs_once_after_owner_finishes(runtime):
    result = await call()
    await tools.drain_launch_turns()
    assert Client.instances[0].owner_counts == [1]
    assert runtime.calls == [(result['engagement_id'], 0, runtime.channel)]


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
    await tools.drain_launch_turns()
    if second_job:
        result = await call(chat='2')
    else:
        result = await call(tools.delegate_to_agent, chat='2', agent='finance', mode='interactive')
    assert result['engagement_id'] == first['engagement_id']
    assert result['topic_id'] == 42
    if second_job:
        assert result['kind'] == 'job_busy'
        assert result['plugin'] == 'ledger'
        assert DECL.title in result['message'] and 'already has a running job' in result['message']
    else:
        assert result['kind'] == 'engagement_busy'
        assert result['agent'] == 'finance'
        assert DECL.title in result['message'] and 'Alex' in result['message']
    assert runtime.bot.create_forum_topic.await_count == 1


async def test_sync_delegation_while_job_open(runtime):
    first = await call()
    await tools.drain_launch_turns()
    result = await call(tools.delegate_to_agent, agent='finance', mode='sync')
    assert result['status'] == 'ok', result
    assert runtime.registry.get(first['engagement_id']).status == 'active'
    assert len(Client.instances) == 2
    assert cli_limit(Client.instances[1].options) == '20'
    assert jobs.JOB_CASA_GRANTS[0] not in Client.instances[1].options.allowed_tools


@pytest.mark.parametrize('limit', [30, None])
async def test_resume_keeps_limit_and_job_grant(runtime, limit):
    result = await call()
    await tools.drain_launch_turns()
    rec = runtime.registry.get(result['engagement_id'])
    rec.origin['job']['turns_per_batch'] = limit
    opts = tools.build_engagement_resume_options(rec, 'resumed-session')
    assert cli_limit(opts) == str(limit or 20)
    assert opts.resume == 'resumed-session'
    assert jobs.JOB_CASA_GRANTS[0] in opts.allowed_tools


async def test_launch_cancel_drops_owner_without_scheduling(runtime, monkeypatch):
    monkeypatch.setattr(Client, 'gate', asyncio.Event())
    result = await call()
    await asyncio.sleep(.02)
    assert jobs.turn_owners(result['engagement_id']) == 1
    await tools.stop_engagement_launches(runtime.registry)
    assert jobs.turn_owners(result['engagement_id']) == 0
    assert runtime.calls == []


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
