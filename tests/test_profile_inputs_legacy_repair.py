"""Synthetic real-boundary regressions; no runtime or model calls."""
from copy import deepcopy
import contextlib
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import profile_catalog
import profile_sync as sync
import run_profile_sync as runner
from catalog_fixture_factory import home, json_file, write


GOOD = dict(runtime_policy={'schema_version': 1, 'entries': []},
            capabilities={'capabilities': {}}, resource_roots={}, role_equivalence={})
FLAGS = dict(runtime_policy='--runtime-policy', capabilities='--capability-snapshot',
             resource_roots='--overlay-resource-roots', role_equivalence='--role-equivalence')
RULE = {'id': 'synthetic', 'selector': {'kind': 'command'}, 'tasks': [],
        'formats': [], 'requires': [], 'tier': 'main'}
INVALID = [(name, value) for name in FLAGS for value in (None, [], 'synthetic-secret')]
INVALID += [
    ('runtime_policy', {'schema_version': 999, 'entries': []}),
    ('runtime_policy', {'schema_version': True, 'entries': []}),
    ('runtime_policy', {'schema_version': 1}),
    ('runtime_policy', {'schema_version': 1, 'entries': {}}),
    ('runtime_policy', {'schema_version': 1, 'entries': [RULE, RULE]}),
    *[('runtime_policy', {'schema_version': 1, 'entries': [{**RULE, key: value}]})
      for key, value in [('id', []), ('selector', {}), ('selector', {'kind': []}),
                         ('tasks', 'synthetic'), ('formats', [1]), ('requires', {}), ('tier', 'unknown')]],
    ('capabilities', {'capabilities': []}),
    ('capabilities', {'capabilities': {'synthetic': {'status': True, 'evidence': []}}}),
    ('capabilities', {'capabilities': {'synthetic': {'status': 'supported', 'evidence': {}}}}),
    ('resource_roots', {'synthetic': None}),
    ('resource_roots', {'synthetic': []}),
    ('role_equivalence', {'synthetic:agent.md': []}),
    ('role_equivalence', {'synthetic:agent.md': {'status': 'verified'}}),
    ('role_equivalence', {'synthetic:agent.md': {'status': 'verified', 'artifact_hash': [], 'evidence': []}}),
]


def state(roots):
    return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
            for root in roots for p in root.rglob('*') if p.is_file()}


def invoke(cli, roots, args):
    out, err = io.StringIO(), io.StringIO()
    argv = [v for name, root in zip(('claude', 'codex', 'skills'), roots)
            for v in ('--' + name + '-home', str(root))]
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv + args)
    return code, out.getvalue() + err.getvalue()


def schema_case(base, name, value, boundary):
    roots = home(base)
    for name_part in ('commands', 'agents', 'projects'):
        (roots[0] / name_part).mkdir()
    write(roots[0] / 'CLAUDE.md', 'Synthetic original instructions.\n')
    first = runner.run(*roots, apply=True, write_status=True, **deepcopy(GOOD))
    assert first['status'] == 'healthy', first
    write(roots[0] / 'CLAUDE.md', 'Synthetic changed instructions.\n')
    before = state(roots[1:])
    values = deepcopy(GOOD)
    values[name] = value
    if boundary == 'parsed':
        result = runner.run(*roots, apply=True, write_status=True, **values)
        assert result['exit_code'] == 1, result
        assert result['error_type'] == 'ValueError'
        output = json.dumps(result)
    elif boundary == 'planner':
        with pytest.raises(ValueError):
            sync.build_plan(*roots, **values)
        output = ''
    else:
        args = []
        for key, document in values.items():
            path = json_file(base / (key + '.json'), document)
            args += [FLAGS[key], str(path)]
        cli = runner if boundary == 'scheduled' else sync
        code, output = invoke(cli, roots, args + ['--apply'] +
                              (['--write-status'] if cli is runner else ['--json']))
        assert code == 1, output
        assert 'ValueError' in output
    assert 'synthetic-secret' not in output
    assert state(roots[1:]) == before


@pytest.mark.parametrize('boundary', ['scheduled', 'direct', 'parsed', 'planner'])
@pytest.mark.parametrize('name,value', INVALID)
def test_invalid_schema_preserves_real_destination(tmp_path, name, value, boundary):
    schema_case(tmp_path, name, value, boundary)


def adapter_home(base):
    roots = home(base)
    claude, codex, skills = roots
    root = base / 'upstream/kit'
    source = write(root / 'commands/review.md', '---\ndescription: Synthetic review.\n---\nReview synthetic input.\n')
    json_file(root / '.claude-plugin/plugin.json', {'name': 'kit'})
    json_file(claude / 'plugins/installed_plugins.json', {'plugins': {
        'kit@market-a': [{'scope': 'user', 'installPath': str(root), 'version': '1'}]}})
    json_file(claude / 'settings.json', {'enabledPlugins': {'kit@market-a': True}})
    changes, report = sync.build_plan(*roots)
    sync.apply_plan(changes, report, codex, skills)
    manifest = codex / 'claude-sync/managed-artifacts.json'
    target = skills / 'claude-kit-review/SKILL.md'
    return roots, root, source, manifest, target


def legacy_case(base, overlay=False):
    roots, root, source, manifest, target = adapter_home(base)
    saved = target.read_bytes()
    data = json.loads(manifest.read_text())
    record = data['items'][str(target)]
    original = deepcopy(record['original'])
    record.pop('source_id')
    record.pop('catalog_identity')
    json_file(manifest, data)
    kwargs = {}
    if overlay:
        kwargs = dict(runtime_policy={'schema_version': 1, 'entries': [{**RULE,
            'selector': {'kind': 'command'}}]}, capabilities={'capabilities': {
                'llmcall.contexts': {'status': 'supported', 'evidence': ['synthetic test']}}})
    changes, report = sync.build_plan(*roots, **kwargs)
    assert not [r for r in report['skills'] if r['status'] == 'conflict'], report['skills']
    assert changes
    sync.apply_plan(changes, report, roots[1], roots[2])
    modern = json.loads(manifest.read_text())['items'][str(target)]
    assert modern['source_id'] and modern['catalog_identity']
    if not overlay:
        assert modern['original'] == original
        assert target.read_bytes() == saved
    else:
        assert (target.parent / 'workflow.json').is_file()
    assert sync._skill_ownership(roots[1], roots[2])[0]['status'] == 'verified'
    again, _ = sync.build_plan(*roots, **kwargs)
    assert not again


@pytest.mark.parametrize('overlay', [False, True])
def test_verified_legacy_conversion_is_idempotent(tmp_path, overlay):
    legacy_case(tmp_path, overlay)


ADVERSARIAL = ['partial_source_id', 'partial_catalog_identity', 'null_source_id', 'target_edit',
    'source_edit', 'source_hash', 'target_hash', 'provider', 'provider_root', 'source_path',
    'plugin_key', 'missing_field', 'unknown_field', 'retired', 'marketplace', 'disabled', 'missing_provider']


def adversarial_case(base, mutation):
    roots, root, source, manifest, target = adapter_home(base)
    data = json.loads(manifest.read_text())
    record = data['items'][str(target)]
    modern = deepcopy(record)
    record.pop('source_id')
    record.pop('catalog_identity')
    if mutation.startswith('partial_'):
        field = mutation.removeprefix('partial_')
        record[field] = modern[field]
    elif mutation == 'null_source_id':
        record['source_id'] = None
    elif mutation == 'target_edit':
        target.write_bytes(target.read_bytes() + b'Synthetic user edit.\n')
    elif mutation == 'source_edit':
        source.write_bytes(source.read_bytes() + b'Synthetic changed source.\n')
    elif mutation in {'source_hash', 'target_hash'}:
        if mutation == 'source_hash':
            record['source_sha256'] = '0' * 64
        else:
            record['original']['sha256'] = '0' * 64
    elif mutation == 'provider':
        record['provider'] = 'unknown'
    elif mutation in {'provider_root', 'source_path'}:
        record['provider_root' if mutation == 'provider_root' else 'source'] = str(base / 'other')
    elif mutation == 'plugin_key':
        record['plugin'] = 'kit@market-b'
    elif mutation == 'missing_field':
        record.pop('source_sha256')
    elif mutation == 'unknown_field':
        record['unrecognized'] = True
    elif mutation == 'retired':
        record['retired'] = True
    elif mutation == 'marketplace':
        json_file(roots[0] / 'plugins/installed_plugins.json', {'plugins': {
            'kit@market-b': [{'scope': 'user', 'installPath': str(root), 'version': '1'}]}})
        json_file(roots[0] / 'settings.json', {'enabledPlugins': {'kit@market-b': True}})
    elif mutation == 'disabled':
        json_file(roots[0] / 'settings.json', {'enabledPlugins': {'kit@market-a': False}})
    elif mutation == 'missing_provider':
        json_file(roots[0] / 'plugins/installed_plugins.json', {'plugins': {}})
    json_file(manifest, data)
    before = state(roots[1:])
    changes, report = sync.build_plan(*roots)
    assert any(r['status'] == 'conflict' for r in report['skills']), report['skills']
    assert not any(r['path'] in {str(manifest), str(target)} for r in changes)
    assert state(roots[1:]) == before


@pytest.mark.parametrize('mutation', ADVERSARIAL)
def test_legacy_refuses_changed_or_incomplete_evidence(tmp_path, mutation):
    adversarial_case(tmp_path, mutation)


def modern_case(base):
    roots, root, source, manifest, target = adapter_home(base)
    changes, _ = sync.build_plan(*roots)
    assert not changes
    data = json.loads(manifest.read_text())['items'][str(target)]
    write(source, '---\ndescription: Updated synthetic review.\n---\nUpdated synthetic input.\n')
    changes, report = sync.build_plan(*roots)
    assert changes and not any(r['status'] == 'conflict' for r in report['skills'])
    sync.apply_plan(changes, report, roots[1], roots[2])
    updated = json.loads(manifest.read_text())['items'][str(target)]
    assert updated['source_id'] == data['source_id']
    assert updated['source_sha256'] != data['source_sha256']
    assert b'Updated synthetic review' in target.read_bytes()
    assert not sync.build_plan(*roots)[0]


def test_modern_unchanged_and_source_update_control(tmp_path):
    modern_case(tmp_path)


def drift_case(base, member):
    roots, root, source, manifest, target = adapter_home(base)
    data = json.loads(manifest.read_text())
    record = data['items'][str(target)]
    record.pop('source_id')
    record.pop('catalog_identity')
    json_file(manifest, data)
    changes, report = sync.build_plan(*roots)
    path = {'source': source, 'target': target, 'manifest': manifest,
            'registry': roots[0] / 'plugins/installed_plugins.json'}[member]
    path.write_bytes(path.read_bytes() + b'\n')
    before = state(roots[1:])
    with pytest.raises(ValueError):
        sync.apply_plan(changes, report, roots[1], roots[2])
    assert state(roots[1:]) == before


@pytest.mark.parametrize('member', ['source', 'target', 'manifest', 'registry'])
def test_conversion_rechecks_source_and_original_group_before_apply(tmp_path, member):
    drift_case(tmp_path, member)


def ambiguity_case(base):
    roots, root, source, manifest, target = adapter_home(base)
    previous = json.loads(manifest.read_text())['items'][str(target)]
    previous.pop('source_id')
    previous.pop('catalog_identity')
    snapshot = profile_catalog.discover_profile(*roots)
    record = next(r for r in snapshot['records'] if r.get('registry_key') == 'kit@market-a')
    for field, value in [('kind', 'agent_template'), ('relative_path', 'agents/review.md'),
                         ('client', 'codex'), ('scope', 'project')]:
        changed = deepcopy(snapshot)
        changed_record = next(r for r in changed['records'] if r.get('registry_key') == 'kit@market-a')
        changed_record['entrypoints'][0][field] = value
        assert profile_catalog.legacy_adapter_identity(previous, changed) is None
    changed = deepcopy(snapshot)
    changed_record = next(r for r in changed['records'] if r.get('registry_key') == 'kit@market-a')
    changed_record['status']['enabled'] = 'unknown'
    changed_record['explicit_selection'] = True
    assert profile_catalog.legacy_adapter_identity(previous, changed) is None
    record['entrypoints'].append(deepcopy(record['entrypoints'][0]))
    assert profile_catalog.legacy_adapter_identity(previous, snapshot) is None
    catalog = {'kit@market-a': {'roots': [str(root)], 'status': 'available'},
               'kit@market-b': {'roots': [str(root)], 'status': 'available'}}
    assert sync.legacy_adapter_source(target, roots[0], catalog) is None
    catalog['kit@market-b']['roots'] = [str(base / 'other-market')]
    assert sync.legacy_adapter_source(target, roots[0], catalog) is None


def test_ambiguous_catalog_and_initial_import_market_collision_refuse(tmp_path):
    ambiguity_case(tmp_path)


def duplicate_case(base, boundary, name):
    roots = home(base)
    path = base / 'duplicate.json'
    payload = {'runtime_policy': '{"schema_version":1,"entries":[],"entries":[]}',
        'capabilities': '{"capabilities":{"synthetic":{"status":"unknown"},"synthetic":{"status":"supported"}}}',
        'resource_roots': '{"synthetic":"a","synthetic":"b"}',
        'role_equivalence': '{"synthetic:agent":{},"synthetic:agent":{}}'}[name]
    path.write_text(payload, encoding='utf-8-sig')
    before = state(roots)
    code, output = invoke(runner if boundary == 'scheduled' else sync, roots,
        [FLAGS[name], str(path), '--apply'])
    assert code == 1 and 'ValueError' in output
    assert state(roots) == before


@pytest.mark.parametrize('boundary', ['scheduled', 'direct'])
@pytest.mark.parametrize('name', FLAGS)
def test_duplicate_json_identity_keys_fail_before_write(tmp_path, boundary, name):
    duplicate_case(tmp_path, boundary, name)


def test_unknown_capabilities_remain_blocked():
    from skill_smith import conflicts
    for status in ('unknown', 'unverified', 'unsupported', 'future-status', 'supported'):
        capabilities = {'capabilities': {'synthetic': {'status': status}}}
        conflicts.validate_capabilities(capabilities)
        assert conflicts.capability_gaps(['synthetic'], capabilities) == ['synthetic']


@pytest.mark.parametrize('name,value', INVALID)
def test_parsed_validation_precedes_all_consumers(tmp_path, monkeypatch, name, value):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid explicit document entered a lock, planner or writer')
    for attribute in ('destination_lock', 'build_plan', 'apply_plan', '_publish'):
        monkeypatch.setattr(runner, attribute, forbidden)
    result = runner.run(tmp_path / 'claude', tmp_path / 'codex', tmp_path / 'skills',
                        apply=True, write_status=True, **{name: value})
    assert result == {'status': 'error', 'error_type': 'ValueError', 'exit_code': 1}


def test_invalid_selection_report_cannot_enter_apply(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Invalid selection entered destination locking')
    monkeypatch.setattr(sync, 'destination_lock', forbidden)
    with pytest.raises(ValueError):
        sync.apply_plan([], {'runtime_selection': {'status': 'unsupported'}},
                        tmp_path / 'codex', tmp_path / 'skills')


def snapshot_case(base):
    roots = home(base)
    for name in ('commands', 'agents', 'projects'):
        (roots[0] / name).mkdir()
    write(roots[0] / 'CLAUDE.md', 'Synthetic snapshot instructions.\n')
    paths, argv = [], []
    for name, value in GOOD.items():
        path = base / (name + '.json')
        path.write_text(json.dumps(value), encoding='utf-8-sig')
        paths.append(path)
        argv.extend([FLAGS[name], str(path)])
    reads, calls = [], []
    read_text, build_plan, apply_plan = Path.read_text, runner.build_plan, runner.apply_plan

    def observed_read(path, *args, **kwargs):
        if path in paths:
            reads.append(path)
        return read_text(path, *args, **kwargs)

    def observed_plan(*args, **kwargs):
        calls.append(kwargs)
        return build_plan(*args, **kwargs)

    def observed_apply(*args, **kwargs):
        for path in paths:
            path.write_text('null', encoding='utf-8')
        return apply_plan(*args, **kwargs)

    with patch.object(Path, 'read_text', observed_read), patch.object(runner, 'build_plan', observed_plan), patch.object(runner, 'apply_plan', observed_apply):
        code, output = invoke(runner, roots, argv + ['--apply', '--write-status'])
    assert code == 0, output
    assert reads == paths and len(calls) == 2
    assert all(calls[0][name] is calls[1][name] for name in GOOD)
    assert all(calls[0][name] == value for name, value in GOOD.items())
    assert runner.run(*roots)['status'] == 'healthy'
    for path, value in zip(paths, GOOD.values()):
        path.write_text(json.dumps(value), encoding='utf-8-sig')
    code, output = invoke(sync, roots, argv + ['--apply', '--json'])
    assert code == 0, output


def test_real_bom_frozen_inputs_and_omitted_controls(tmp_path):
    snapshot_case(tmp_path)
