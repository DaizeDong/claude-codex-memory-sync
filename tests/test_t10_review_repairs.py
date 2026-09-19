"""Behavior regressions for the six final-review findings; synthetic inputs only."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

import profile_catalog
import profile_sync as sync
from profile_bridge import adapters, overlays as bridge
from profile_bridge.workflows import DurableWorkflow
from skill_smith import overlays
from llmcall import process
from catalog_fixture_factory import home, json_file, plugin, skill, write
from test_t10_workflow_context import Fake, descriptor, author, workflow


CAPS = {'capabilities': {name: {'status': 'supported', 'evidence': ['synthetic observation']}
        for name in ('llmcall.agent', 'llmcall.contexts', 'llmcall.independent_review')}}


@pytest.mark.parametrize('transition', ['empty_policy', 'lost_capability', 'missing_requirement', 'unconfigured'])
def test_retracted_selection_cannot_execute_superseded_policy(tmp_path, transition):
    claude, codex, shared = home(tmp_path)
    skill(claude / 'skills/alpha', 'alpha')
    if transition == 'empty_policy':
        root = plugin(tmp_path, claude, key='pr-review-toolkit@claude-plugins-official')
        json_file(root / '.claude-plugin/plugin.json', {'name': 'pr-review-toolkit'})
        write(root / 'agents/code-reviewer.md', '---\nname: code-reviewer\ndescription: Synthetic reviewer.\n---\nReview synthetic input.\n')
    catalog = profile_catalog.discover_profile(claude, codex, shared)
    entry = next(ep for row in catalog['records'] for ep in row['entrypoints'] if ep['name'] == 'alpha')
    policy = {'schema_version': 1, 'entries': [{'id': 'alpha', 'selector': bridge.selector(entry),
              'tasks': ['image'], 'requires': ['llmcall.contexts']}]}
    caps = deepcopy(CAPS)
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps)
    sync.apply_plan(changes, report, codex, shared)
    selection = shared / 'llmcall-tasks/selection.json'
    assert adapters.select_entrypoint(selection, {'task': 'image'})['status'] == 'selected'
    if transition == 'empty_policy':
        policy['entries'] = []
    elif transition == 'lost_capability':
        caps['capabilities']['llmcall.contexts']['status'] = 'unsupported'
    elif transition == 'missing_requirement':
        policy['entries'][0]['requires'].append('synthetic.missing')
    else:
        policy, caps = None, None
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps)
    sync.apply_plan(list(changes), report, codex, shared)
    assert adapters.select_entrypoint(selection, {'task': 'image'})['status'] != 'selected'
    assert not (selection.parent / 'SKILL.md').exists()
    if transition == 'empty_policy':
        assert list(shared.glob('claude-agent-*/workflow.json'))
    repeated, _ = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps)
    assert not repeated


def retirement_plan(root, monkeypatch):
    claude, codex, shared = home(root)
    source = root / 'source/pr-review-toolkit'
    json_file(source / '.claude-plugin/plugin.json', {'name': 'pr-review-toolkit'})
    write(source / 'agents/code-reviewer.md', '---\nname: code-reviewer\ndescription: Synthetic review.\n---\nRead [rules](../references/rules.md).\n')
    write(source / 'references/rules.md', 'Synthetic rules.\n')
    json_file(claude / 'plugins/installed_plugins.json', {'plugins': {
        'pr-review-toolkit@claude-plugins-official': [{'scope': 'user', 'installPath': str(source), 'version': '1'}]}})
    json_file(claude / 'settings.json', {'enabledPlugins': {'pr-review-toolkit@claude-plugins-official': True}})
    with monkeypatch.context() as patch:
        patch.setattr(bridge, 'known_role', lambda *args: False)
        changes, report = sync.build_plan(claude, codex, shared)
        sync.apply_plan(changes, report, codex, shared)
    policy = {'schema_version': 1, 'entries': []}
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
    sync.apply_plan(changes, report, codex, shared)
    prepared = bridge.prepare(profile_catalog.discover_profile(claude, codex, shared), policy, CAPS)
    evidence = {item['record']['source_id'] + ':' + item['entrypoint']['relative_path']:
        {'status': 'verified', 'artifact_hash': item['overlay']['artifact_hash'], 'evidence': ['synthetic equivalence']}
        for item in prepared['entries'].values()}
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS, role_equivalence=evidence)
    assert report['agents']['deletions']
    assert not any(Path(row['path']).is_relative_to(shared) for row in changes)
    return codex, shared, source, changes, report


@pytest.mark.parametrize('ordinary_list', [False, True])
@pytest.mark.parametrize('drift', ['descriptor', 'resource', 'manifest', 'source', 'missing_evidence'])
def test_retirement_rejects_changed_complete_dependency_before_any_write(tmp_path, monkeypatch, ordinary_list, drift):
    codex, shared, source, changes, report = retirement_plan(tmp_path, monkeypatch)
    if ordinary_list:
        changes = list(changes)
    if drift == 'descriptor':
        next(shared.glob('claude-agent-*/workflow.json')).write_text('{}')
    elif drift == 'resource':
        next(shared.glob('claude-agent-*/payload/references/rules.md')).unlink()
    elif drift == 'manifest':
        (codex / 'claude-sync/managed-artifacts.json').write_text('{}')
    elif drift == 'source':
        (source / 'references/rules.md').write_text('Changed source')
    else:
        for row in changes:
            row.pop('overlay_dependencies', None)
    before_config = (codex / 'config.toml').read_bytes()
    before_roles = {p.name: p.read_bytes() for p in (codex / 'claude-sync/agents').glob('*.toml')}
    backups = set((codex / 'claude-sync/backups').iterdir())
    with pytest.raises(ValueError, match='[Oo]verlay'):
        sync.apply_plan(changes, report, codex, shared)
    assert (codex / 'config.toml').read_bytes() == before_config
    assert {p.name: p.read_bytes() for p in (codex / 'claude-sync/agents').glob('*.toml')} == before_roles
    assert set((codex / 'claude-sync/backups').iterdir()) == backups


@pytest.mark.parametrize('ordinary_list', [False, True])
def test_intact_retirement_only_plan_applies(tmp_path, monkeypatch, ordinary_list):
    codex, shared, source, changes, report = retirement_plan(tmp_path, monkeypatch)
    sync.apply_plan(list(changes) if ordinary_list else changes, report, codex, shared)
    assert not list((codex / 'claude-sync/agents').glob('*.toml'))
    assert sync._skill_ownership(codex, shared)[0]['status'] == 'verified'


def test_loaded_policy_selects_primary_specialist_fallback_and_override(tmp_path):
    claude, codex, shared = home(tmp_path)
    for name in ('alpha', 'beta', 'gamma'):
        skill(claude / 'skills' / name, name)
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    entries = sorted([ep for rec in snapshot['records'] for ep in rec['entrypoints']], key=lambda e: e['name'])
    policy = {'schema_version': 1, 'entries': [
        {'id': ep['name'], 'selector': bridge.selector(ep), 'tasks': ['image'],
         'tier': ['main', 'fallback', 'specialist'][i], 'formats': ['png'] if i == 2 else []}
        for i, ep in enumerate(entries)]}
    raw_sources = {ep['path']: Path(ep['path']).read_bytes() for ep in entries}
    def install():
        changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
        sync.apply_plan(changes, report, codex, shared)
        return shared / 'llmcall-tasks/selection.json'
    path = install()
    def selected(request):
        result = adapters.select_entrypoint(path, request)
        assert Path(result['entrypoint_path']).is_file()
        return result['descriptor']['entrypoint']['name']
    assert selected({'task': 'image'}) == 'alpha'
    assert selected({'task': 'image', 'format': 'png'}) == 'gamma'
    assert selected({'task': 'image', 'format': 'png', 'override': 'beta'}) == 'beta'
    assert selected({'task': 'image', 'override': bridge.selector(entries[0])}) == 'alpha'
    assert len(list(shared.rglob('SKILL.md'))) == 1
    policy['entries'][0]['tier'], policy['entries'][1]['tier'] = 'fallback', 'main'
    install()
    assert selected({'task': 'image'}) == 'beta'
    assert {raw: Path(raw).read_bytes() for raw in raw_sources} == raw_sources
    assert len(list(shared.rglob('SKILL.md'))) == 1
    changes, _ = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
    assert not changes


def test_disabled_sibling_never_builds_or_deploys(tmp_path):
    claude, codex, shared = home(tmp_path)
    plugin(tmp_path, claude, key='kit@market-a', enabled=True)
    plugin(tmp_path, claude, key='kit@market-b', enabled=False)
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    policy = {'schema_version': 1, 'entries': [{'id': 'choice', 'selector': {'kind': 'skill', 'name': 'different-frontmatter'}}]}
    state = bridge.prepare(snapshot, policy, CAPS)
    disabled = next(item for item in state['entries'].values() if item['record']['status']['enabled'] == 'no')
    assert disabled['overlay']['status'] == 'blocked'
    assert 'source_disabled' in disabled['overlay']['reasons']
    direct = overlays.build(disabled['record'], 'codex', CAPS, selector=bridge.selector(disabled['entrypoint']))
    assert direct['status'] == 'blocked' and direct['reasons'] == ['source_disabled']
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
    sync.apply_plan(changes, report, codex, shared)
    envelope = json.loads((shared / 'llmcall-tasks/selection.json').read_text())
    assert len(envelope['alternatives']) == 1
    assert json.loads((claude / 'settings.json').read_text())['enabledPlugins']['kit@market-b'] is False


def test_previous_transform_evidence_cannot_authorize_new_artifacts(tmp_path):
    built = descriptor(tmp_path)
    old = deepcopy(built)
    old['transform_version'] = 'llmcall-contexts-v2'
    old['artifact_hash'] = overlays.fingerprint({k: v for k, v in old.items() if k != 'artifact_hash'})
    assert old['artifact_hash'] != built['artifact_hash']
    assert not overlays.validate(old)


@pytest.mark.parametrize('lookalike', [
    {'type': 'object', 'fields': {'field1': 'string'}},
    {'type': 'Result', 'fields': {'provider': 'not-a-contract', 'text': 'opaque'}},
    {'encoding': 'contract', 'type': 'ExecutionRequirements', 'fields': {'access': 'workspace_write'}},
    {'encoding': 'dict', 'items': {'type': 'object', 'fields': {}}},
])
def test_arbitrary_user_json_survives_fresh_process_replay_and_followup(tmp_path, lookalike):
    fake, built = Fake(), descriptor(tmp_path)
    original_call = fake.call
    def respond(prompt, **options):
        result = original_call(prompt, **options)
        result.data = deepcopy(lookalike)
        return result
    fake.call = respond
    producer = author()
    producer.data = deepcopy(lookalike)
    options = dict(context='review', operation='start', prompt='Review schema', inputs=lookalike, producer=producer)
    assert workflow(tmp_path, fake, built).run('first', **options)
    (tmp_path / 'restart.json').write_text(json.dumps({'descriptor': built, 'input': lookalike}))
    script = '''import json, pathlib, sys
sys.path[:0] = json.loads(sys.argv[1])
from test_t10_workflow_context import Fake, author, workflow
root = pathlib.Path(sys.argv[2]); data = json.loads((root / 'restart.json').read_text())
fake = Fake(); fake.fail = True
producer = author(); producer.data = data['input']
session = workflow(root, fake, data['descriptor'])
result = session.run('first', context='review', operation='start', prompt='Review schema', inputs=data['input'], producer=producer)
assert result and not fake.calls
assert result.data == data['input']
fake.fail = False
assert session.run('second', context='review', operation='reply', prompt='Continue')
text, options = fake.calls[0]
history = json.loads(text.split('WORKFLOW CONTEXT\\n', 1)[1])
assert history['messages'][0]['content']['inputs'] == data['input']
print('fresh-process-json-preserved')
'''
    completed = subprocess.run([sys.executable, '-B', '-c', script, json.dumps(sys.path), str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert 'fresh-process-json-preserved' in completed.stdout


def test_relative_workspace_stays_anchored_and_transition_is_explicit(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    with process.use_context(process.CallContext(str(tmp_path / 'caller-a'), {})):
        assert workflow(tmp_path, fake, built).run_cli('first', ['exec', '-C', 'project', '--sandbox', 'workspace-write'], 'Edit')
    anchor = str(tmp_path / 'caller-a/project')
    with process.use_context(process.CallContext(str(tmp_path / 'caller-b'), {})):
        resumed = workflow(tmp_path, fake, built)
        assert resumed.run_cli('next', ['exec', 'resume', '--last'], 'Continue')
        assert fake.calls[-1][1]['cwd'] == anchor
        assert fake.calls[-1][1]['requirements'].workspace == anchor
        blocked = resumed.run_cli('move', ['exec', 'resume', '--last', '-C', 'other'], 'Move')
        assert blocked.outcome == 'workspace_transition_requires_explicit_authorization'
        assert len(fake.calls) == 2
        assert resumed.run_cli('move', ['exec', 'resume', '--last', '-C', 'other'], 'Move', allow_workspace_change=True)
        assert fake.calls[-1][1]['cwd'] == str(tmp_path / 'caller-b/other')
    requests = [json.loads(p.read_text())['request'] for p in resumed.root.glob('*.request.json')]
    assert all(Path(r['cwd']).is_absolute() for r in requests)
    assert len(requests) == 3


def test_cli_workspace_anchor_survives_a_new_process(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    with process.use_context(process.CallContext(str(tmp_path / 'caller-a'), {})):
        assert workflow(tmp_path, fake, built).run_cli('first', ['exec', '-C', 'project', '--sandbox', 'workspace-write'], 'Edit')
    (tmp_path / 'restart.json').write_text(json.dumps(built))
    script = '''import json, pathlib, sys
sys.path[:0] = json.loads(sys.argv[1])
from test_t10_workflow_context import Fake, workflow
from llmcall import process
root = pathlib.Path(sys.argv[2]); fake = Fake()
with process.use_context(process.CallContext(str(root / 'caller-b'), {})):
    assert workflow(root, fake, json.loads((root / 'restart.json').read_text())).run_cli('next', ['exec', 'resume', '--last'], 'Continue')
options = fake.calls[0][1]
assert options['cwd'] == str(root / 'caller-a/project')
assert options['requirements'].workspace == options['cwd']
print('fresh-process-workspace-preserved')
'''
    completed = subprocess.run([sys.executable, '-B', '-c', script, json.dumps(sys.path), str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    assert 'fresh-process-workspace-preserved' in completed.stdout


def test_legacy_records_offer_explicit_read_without_replay_or_guessed_context(tmp_path):
    from profile_bridge.workflows import _hash
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    session = workflow(tmp_path, fake, built)
    session.root.mkdir(parents=True)
    opaque = {'type': 'ExecutionRequirements', 'fields': {'workspace': 'relative-project'}}
    saved = {'sequence': 0, 'previous': None, 'request': {
        'request_id': 'legacy', 'artifact_hash': built['artifact_hash'], 'cwd': 'relative-project'}}
    receipt = {'request_hash': _hash(saved), 'state': 'completed',
        'result': {'type': 'Result', 'fields': {'text': 'Saved result', 'provider': 'fake', 'data': opaque}},
        'session': {'contexts': {}, 'completed': {}}}
    receipt['receipt_hash'] = _hash(receipt)
    (session.root / '00000000.request.json').write_text(json.dumps(saved))
    (session.root / '00000000.result.json').write_text(json.dumps(receipt))
    assert session.run_cli('next', ['exec', 'resume', '--last'], 'Continue').outcome == 'legacy_workflow_requires_explicit_migration'
    result = session.read_legacy_result('legacy')
    assert result.text == 'Saved result' and result.data == opaque
    assert not fake.calls
    assert len(list(session.root.glob('*.request.json'))) == 1


@pytest.mark.parametrize('ordinary_list', [False, True])
def test_skill_link_retirement_waits_for_loaded_selection_bundle(tmp_path, monkeypatch, ordinary_list):
    claude, codex, shared = home(tmp_path)
    skill(claude / 'skills/alpha', 'alpha')
    changes, report = sync.build_plan(claude, codex, shared)
    sync.apply_plan(changes, report, codex, shared)
    old_link = shared / 'alpha'
    assert sync.linked(old_link)
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    source_entry = next(ep for rec in snapshot['records'] for ep in rec['entrypoints']
                        if ep['name'] == 'alpha' and ep['client'] == 'claude')
    policy = {'schema_version': 1, 'entries': [{'id': 'alpha', 'selector': bridge.selector(source_entry)}]}
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
    original_remove = sync.remove_entry
    observed = []
    def remove(path):
        if path == old_link:
            result = adapters.select_entrypoint(shared / 'llmcall-tasks/selection.json', {})
            assert result['status'] == 'selected'
            assert Path(result['entrypoint_path']).is_file()
            observed.append(True)
        return original_remove(path)
    monkeypatch.setattr(sync, 'remove_entry', remove)
    sync.apply_plan(list(changes) if ordinary_list else changes, report, codex, shared)
    assert observed and not sync.linked(old_link)
    assert (claude / 'skills/alpha/SKILL.md').is_file()
    assert sync._skill_ownership(codex, shared)[0]['status'] == 'verified'


def test_projection_preserves_verified_legacy_link_groups(tmp_path):
    claude, codex, shared = home(tmp_path)
    skill(claude / 'skills/alpha', 'alpha')
    changes, report = sync.build_plan(claude, codex, shared)
    sync.apply_plan(changes, report, codex, shared)
    # A valid older installation has the legacy link map but no artifact sidecar.
    (codex / 'claude-sync/managed-artifacts.json').unlink()
    assert sync._skill_ownership(codex, shared)[0]['status'] == 'verified'
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    ep = next(ep for rec in snapshot['records'] for ep in rec['entrypoints']
              if ep['name'] == 'alpha' and ep['client'] == 'claude')
    policy = {'schema_version': 1, 'entries': [{'id': 'alpha', 'selector': bridge.selector(ep)}]}
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=CAPS)
    sync.apply_plan(changes, report, codex, shared)
    assert sync._skill_ownership(codex, shared)[0]['status'] == 'verified'
    assert sync.linked(shared / 'alpha')  # No exact source identity authorizes retirement.
    assert adapters.select_entrypoint(shared / 'llmcall-tasks/selection.json', {})['status'] == 'selected'
