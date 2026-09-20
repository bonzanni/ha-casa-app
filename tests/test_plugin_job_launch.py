"""Resident plugin workers on the real launch, persistence and SDK options path."""
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from claude_agent_sdk import ClaudeAgentOptions, PermissionResultDeny

import background_jobs as jobs
import plugin_registry
import tools
from config import CharacterConfig, HooksConfig
from engagement_registry import EngagementRegistry
from plugin_fixtures import entry, mk_artifact, mk_registry
from test_start_job import Client, DECL, call, cli_limit, runtime

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def worker(runtime, tmp_path, monkeypatch):
    e = entry('ledger', ['resident:assistant'])
    root = mk_artifact(tmp_path / 'store', 'ledger', e['artifact_id'],
                       mcp_servers={'ledger': {'env': {'KEY': '${LEDGER_JOB_KEY}'}}},
                       extra_manifest={'casa': {'protectedTools': ['classify']}},
                       extra_files={'skills/classify/SKILL.md': 'Classify entries.'})
    monkeypatch.setenv('LEDGER_JOB_KEY', 'test-value')
    plugin_registry.reload_snapshot(registry_path=mk_registry(tmp_path, [e]),
                                    store_root=tmp_path / 'store')
    plugin = plugin_registry.resolve_for('resident:assistant').plugins[0]
    host = jobs.JobHost('resident', 'assistant', DECL, plugin)
    monkeypatch.setattr(jobs, 'find_job_host', lambda name, caller, delegates: host)
    monkeypatch.setattr(tools, '_PLUGIN_JOB_ROOT', tmp_path / 'engagements')
    runtime.caller.character = CharacterConfig(name='Ellen', archetype='', card='', prompt='PERSONA SECRET')
    runtime.caller.system_prompt = 'PERSONA SECRET'
    runtime.caller.hooks = HooksConfig(pre_tool_use=[{'policy': 'resident-only-policy'}])
    runtime.caller.model = 'sonnet'
    runtime.caller.tools.max_turns = 17
    runtime.caller.tools.allowed = ['Bash', 'mcp__casa-framework__query_engager']
    runtime.caller.mcp_server_names = ['unrelated']
    runtime.caller.delegates = []
    runtime.caller.requires = SimpleNamespace(plugins=['unrelated'], tools=['missing'])
    selected = []
    def framework(role, grants):
        selected[:] = [t.name for t in tools.select_casa_tools(grants)]
        return tools.create_casa_tools(grants)
    tools._mcp_registry.register_sdk_factory('casa-framework', framework)
    tools._mcp_registry.register_http('unrelated', 'http://unused.invalid')
    return SimpleNamespace(**vars(runtime), host=host, root=root, selected=selected)


@pytest.mark.parametrize('limit', [30, None])
async def test_worker_options_record_and_topic(worker, monkeypatch, limit):
    host = replace(worker.host, decl=replace(DECL, turns_per_batch=limit))
    monkeypatch.setattr(jobs, 'find_job_host', lambda *args: host)
    result = await call()
    assert result['status'] == 'pending', result
    assert (result['agent'], result['job'], result['title']) == ('assistant', DECL.qualified_name, DECL.title)
    assert result['message'] == "Started Classify entries. Progress appears in Ellen's topic."
    await tools.drain_launch_turns()
    rec = worker.registry.get(result['engagement_id'])
    opts = Client.instances[0].options
    assert isinstance(opts, ClaudeAgentOptions)
    assert opts.allowed_tools == ['Skill', 'ToolSearch', 'mcp__plugin_ledger_ledger', *jobs.PLUGIN_JOB_CASA_GRANTS]
    assert opts.disallowed_tools == ['Agent', 'Task', 'AskUserQuestion']
    assert opts.permission_mode == 'default' and opts.setting_sources == []
    assert opts.skills == 'all' and opts.model == 'sonnet'
    assert opts.plugins == [{'type': 'local', 'path': str(worker.root)}]
    assert set(opts.mcp_servers) == {'casa-framework'}
    assert set(worker.selected) == {'report_job_progress', 'emit_completion'}
    assert cli_limit(opts) == str(limit or 17)
    assert Path(opts.cwd) == tools._PLUGIN_JOB_ROOT / rec.id / 'plugin-job'
    assert Path(opts.cwd).is_dir()
    assert opts.system_prompt == tools._PLUGIN_JOB_PROMPT
    assert 'PERSONA SECRET' not in opts.system_prompt
    for fragment in ('worker', 'bounded batches', 'park', 'report_job_progress', 'emit_completion', 'no delegation', 'resident memory'):
        assert fragment in opts.system_prompt
    assert isinstance(await opts.can_use_tool('Bash', {}, None), PermissionResultDeny)
    assert {'PreToolUse', 'PostToolUse', 'PostToolUseFailure'} <= set(opts.hooks)
    assert (rec.kind, rec.role_or_type, rec.driver) == ('plugin', 'assistant', 'in_casa')
    assert rec.origin['plugin_job'] == {'plugin': 'ledger', 'model': 'sonnet'}
    assert rec.origin['job']['turns_per_batch'] == (limit or 17)
    assert rec.origin['chat_id'] == '1' and rec.origin['_operator_turn'] is True
    assert rec.tools_allowed == ('mcp__plugin_ledger_ledger', *jobs.PLUGIN_JOB_CASA_GRANTS)
    assert rec.plugin_artifacts == ({'name': 'ledger', 'manifest_name': worker.host.plugin.manifest_name,
                                     'path': str(worker.root), 'artifact_id': worker.host.plugin.artifact_id},)
    assert rec.topic_title == 'Ellen · Classify entries'
    assert worker.bot.create_forum_topic.call_args.kwargs['name'] == '🟢 Ellen · Classify entries'
    await worker.channel.update_topic_state(engagement_id=rec.id, new_state='awaiting')
    assert 'Ellen · Classify entries' in worker.bot.edit_forum_topic.call_args.kwargs['name']
    loaded = EngagementRegistry(tombstone_path=worker.registry._tombstone_path, bus=None)
    await loaded.load()
    restored = loaded.get(rec.id)
    assert restored.origin == rec.origin
    assert restored.plugin_artifacts == rec.plugin_artifacts
    assert restored.topic_title == rec.topic_title
    assert worker.calls == [(rec.id, 0, worker.channel)]


async def test_env_withheld_before_topic(worker, monkeypatch):
    monkeypatch.delenv('LEDGER_JOB_KEY')
    result = await call()
    assert result['kind'] == 'plugin_env_unresolved'
    worker.bot.create_forum_topic.assert_not_awaited()
    assert not worker.registry.active_and_idle()
    assert not Client.instances
    assert worker.limiter.in_flight == 0


async def test_resume_uses_pinned_launch_settings(worker):
    result = await call()
    await tools.drain_launch_turns()
    loaded = EngagementRegistry(tombstone_path=worker.registry._tombstone_path, bus=None)
    await loaded.load()
    rec = loaded.get(result['engagement_id'])
    worker.caller.model = 'opus'
    worker.caller.tools.max_turns = 99
    # No assignment or host config is needed to rebuild a pinned worker.
    plugin_registry.reload_snapshot(registry_path=Path(worker.registry._tombstone_path).parent / 'absent.json')
    opts = tools.build_engagement_resume_options(rec, 'resume-id')
    initial = Client.instances[0].options
    assert opts.resume == 'resume-id' and opts.model == 'sonnet'
    assert cli_limit(opts) == '30'
    assert opts.plugins == initial.plugins
    assert opts.allowed_tools == initial.allowed_tools
    assert opts.system_prompt == initial.system_prompt
    assert opts.cwd == initial.cwd


@pytest.mark.parametrize('lost', ['resolution', 'environment'])
async def test_resume_refuses_lost_plugin(worker, monkeypatch, lost):
    result = await call()
    await tools.drain_launch_turns()
    rec = worker.registry.get(result['engagement_id'])
    if lost == 'resolution':
        # The real recorded resolver excludes malformed protected declarations.
        manifest_path = worker.root / '.claude-plugin/plugin.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['casa']['protectedTools'] = 5
        manifest_path.write_text(json.dumps(manifest))
        from plugin_store import content_checksum, read_metadata, METADATA_FILENAME
        meta = read_metadata(worker.root)
        meta['content_checksum'] = content_checksum(worker.root)
        (worker.root / METADATA_FILENAME).write_text(json.dumps(meta))
    else:
        monkeypatch.delenv('LEDGER_JOB_KEY')
    with pytest.raises(RuntimeError, match='declaring plugin unavailable'):
        tools.build_engagement_resume_options(rec, 'resume-id')


async def test_options_failure_aborts_and_releases(worker):
    # A file in the workspace's place makes the real options builder fail.
    tools._PLUGIN_JOB_ROOT.write_text('not a directory')
    result = await call()
    assert result['kind'] == 'options_build_failed'
    assert not worker.registry.active_and_idle()
    assert worker.limiter.in_flight == 0
    worker.bot.close_forum_topic.assert_awaited()
    assert not Client.instances
