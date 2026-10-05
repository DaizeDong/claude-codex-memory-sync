"""T10 planner/apply tests use synthetic sources, capabilities and role evidence."""
from copy import deepcopy
import json
from pathlib import Path
import tomllib

import pytest

import profile_catalog
import profile_sync as sync
from profile_bridge import overlays as integration
from skill_smith import role_entrypoints
from catalog_fixture_factory import home, json_file, skill, write as text_file


def caps():
    return {'capabilities': {name: {'status': 'supported', 'evidence': ['synthetic contract test']}
            for name in ('llmcall.agent', 'llmcall.contexts', 'llmcall.independent_review', 'image.native', 'image.api')}}


def policy_for(entries):
    return {'schema_version': 1, 'entries': [
        {'id': 'synthetic-' + str(i), 'selector': integration.selector(ep), 'tasks': ['image'],
         'requires': ['image.native' if i == 0 else 'image.api'],
         'tier': 'main' if i == 0 else 'specialist', 'formats': [] if i == 0 else ['png']}
        for i, ep in enumerate(entries)]}


def all_roles(base):
    claude, codex, shared = home(base)
    registry, settings = {}, {}
    for plugin, name in role_entrypoints.NATIVE_ROLES + role_entrypoints.RESTRICTED_ROLES:
        key = plugin + '@claude-plugins-official'
        root = base / 'upstream' / plugin
        json_file(root / '.claude-plugin/plugin.json', {'name': plugin})
        metadata = 'tools: Read, Grep\n' if (plugin, name) in role_entrypoints.RESTRICTED_ROLES else ''
        text_file(root / 'agents' / (name + '.md'),
            '---\nname: ' + name + '\ndescription: Synthetic role.\nmodel: source-hint\n' + metadata +
            '---\nInspect [rules](../references/rules.md). Delegate nested work with the shared interface.\n')
        text_file(root / 'references/rules.md', 'Synthetic resource instructions.\n')
        registry[key] = [{'scope': 'user', 'installPath': str(root), 'version': '1'}]
        settings[key] = True
    json_file(claude / 'plugins/installed_plugins.json', {'plugins': registry})
    json_file(claude / 'settings.json', {'enabledPlugins': settings})
    text_file(claude / 'agents/custom.md', '---\nname: custom\ndescription: User role.\n---\nPreserve this role.\n')
    return claude, codex, shared


def test_seven_native_roles_retire_only_with_equivalent_planned_templates(tmp_path, monkeypatch):
    claude, codex, shared = all_roles(tmp_path)
    actual_known_role = integration.known_role
    monkeypatch.setattr(integration, 'known_role', lambda *args: False)
    changes, report = sync.build_plan(claude, codex, shared)
    assert report['agents']['registered'] == 8
    sync.apply_plan(changes, report, codex, shared)
    before = tomllib.loads((codex / 'config.toml').read_text())['agents']
    user = {name: value for name, value in before.items() if 'user-custom' in name}
    monkeypatch.setattr(integration, 'known_role', actual_known_role)
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    policy = {'schema_version': 1, 'entries': []}
    prepared = integration.prepare(snapshot, policy, caps())
    equivalence = {item['record']['source_id'] + ':' + item['entrypoint']['relative_path']:
        {'status': 'verified', 'artifact_hash': item['overlay']['artifact_hash'], 'evidence': ['fake llmcall equivalence']}
        for item in prepared['entries'].values() if item['overlay']['status'] == 'ready'}
    assert len(equivalence) == 7
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps())
    assert not report['agents']['deletions']
    assert sum(row['status'] == 'preserved' for row in report['agents']['agents']) == 7
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy,
                                      capabilities=caps(), role_equivalence=equivalence)
    assert len(report['agents']['deletions']) == 7, {
        'skills': [(r.get('name'), r['status'], r.get('reason')) for r in report['skills']],
        'overlays': report['runtime_selection']['entries'], 'agents': report['agents']}
    assert len(changes.overlay_checks) == 7
    sync.apply_plan(changes, report, codex, shared)
    after = tomllib.loads((codex / 'config.toml').read_text())['agents']
    assert after == user
    assert sync._skill_ownership(codex, shared)[0]['status'] == 'verified'
    descriptors = list(shared.glob('claude-agent-*/workflow.json'))
    assert len(descriptors) == 7
    for path in descriptors:
        data = json.loads(path.read_text())
        assert 'llmcall.call' in data['execution_instructions']
        assert (path.parent / 'payload/references/rules.md').is_file()
        assert (path.parent / 'payload' / data['entrypoint']['relative_path']).is_file()
    again, report = sync.build_plan(claude, codex, shared, runtime_policy=policy,
                                    capabilities=caps(), role_equivalence=equivalence)
    assert not again
    restricted = [row for row in report['runtime_selection']['entries'] if row['status'] == 'blocked']
    assert len(restricted) == 3


def test_source_resource_drift_is_revalidated_inside_apply_lock(tmp_path):
    claude, codex, shared = all_roles(tmp_path)
    changes, report = sync.build_plan(claude, codex, shared,
        runtime_policy={'schema_version': 1, 'entries': []}, capabilities=caps())
    source = Path(changes.overlay_checks[0]['resources'][0]['files'][0]['resolved_path'])
    source.write_text('Resource changed after planning')
    with pytest.raises(ValueError, match='Overlay source, resource or transform'):
        sync.apply_plan(changes, report, codex, shared)
    assert not (codex / 'config.toml').exists()
    assert not (codex / 'claude-sync/backups').exists()


def test_missing_policy_capabilities_and_modified_resource_preserve_entries(tmp_path):
    claude, codex, shared = all_roles(tmp_path)
    _, report = sync.build_plan(claude, codex, shared)
    assert report['runtime_selection']['status'] == 'uninitialized'
    assert len(report['runtime_selection']['entries']) == 10
    assert not report['agents']['deletions']
    policy = {'schema_version': 1, 'entries': []}
    _, report = sync.build_plan(claude, codex, shared, runtime_policy=policy)
    assert all(row['status'] == 'blocked' for row in report['runtime_selection']['entries'])
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps())
    sync.apply_plan(changes, report, codex, shared)
    path = next(shared.glob('claude-agent-*/payload/references/rules.md'))
    path.write_text('User changed a deployed resource')
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps())
    assert any(row['status'] == 'conflict' for row in report['skills'])
    assert not report['agents']['deletions']
    assert path.read_text() == 'User changed a deployed resource'


def test_same_name_selection_preserves_primary_and_format_specialist(tmp_path, monkeypatch):
    claude, codex, shared = home(tmp_path)
    skill(codex / 'skills/.system/imagegen', 'imagegen')
    skill(codex / 'skills/imagegen', 'imagegen')
    snapshot = profile_catalog.discover_profile(claude, codex, shared)
    entries = [ep for rec in snapshot['records'] for ep in rec['entrypoints'] if ep['name'] == 'imagegen']
    entries.sort(key=lambda ep: ep['relative_path'])
    policy = policy_for(entries)
    original = deepcopy(snapshot)
    calls = []
    def discover(*args, **kwargs):
        calls.append(True)
        return snapshot
    monkeypatch.setattr(profile_catalog, 'discover_profile', discover)
    changes, report = sync.build_plan(claude, codex, shared, runtime_policy=policy, capabilities=caps(),
        selection_requests=[{'task': 'image'}, {'task': 'image', 'format': 'png'},
                            {'task': 'image', 'format': 'png', 'override': 'synthetic-0'}])
    decisions = report['runtime_selection']['selections'][:3]
    assert [item['decision']['selection']['policy_id'] for item in decisions] == ['synthetic-0', 'synthetic-1', 'synthetic-0']
    assert len(calls) == 1 and snapshot == original
    assert len([row for row in report['skills'] if row['status'] == 'adapted']) == 2
    assert all(row['after']['kind'] != 'missing' for row in changes)
    assert report['runtime_selection']['discovery'] == 'unchecked'
