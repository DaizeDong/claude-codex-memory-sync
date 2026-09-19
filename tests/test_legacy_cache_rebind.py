"""Exact cache moves retain source identity without adopting edited content."""
import json
from pathlib import Path
import shutil

import pytest

import profile_sync as sync
from catalog_fixture_factory import json_file
from test_profile_inputs_legacy_repair import adapter_home, RULE


def moved(base, modern=False):
    roots, old_root, source, manifest, target = adapter_home(base)
    data = json.loads(manifest.read_bytes())
    if not modern:
        data['items'][str(target)].pop('source_id')
        data['items'][str(target)].pop('catalog_identity')
    json_file(manifest, data)
    new_root = old_root.parent / 'new-cache'
    shutil.copytree(old_root, new_root)
    json_file(roots[0] / 'plugins/installed_plugins.json', {'plugins': {
        'kit@market-a': [{'scope': 'user', 'installPath': str(new_root), 'version': '2'}]}})
    return roots, source, new_root / 'commands/review.md', manifest, target


@pytest.mark.parametrize('modern',[False,True])
def test_same_bytes_new_cache_rebinds_atomically_and_converges(tmp_path,modern):
    roots, old, new, manifest, target = moved(tmp_path,modern)
    changes, report = sync.build_plan(*roots)
    assert not [r for r in report['skills'] if r['status'] == 'conflict']
    assert str(new) not in target.read_text('utf-8')
    sync.apply_plan(changes, report, roots[1], roots[2])
    assert new.as_posix() in target.read_text('utf-8')
    assert old.as_posix() not in target.read_text('utf-8')
    record = json.loads(manifest.read_bytes())['items'][str(target)]
    assert Path(record['source']) == new
    assert sync._skill_ownership(roots[1], roots[2])[0]['status'] == 'verified'
    assert not sync.build_plan(*roots)[0]


def test_modern_overlay_cache_move_has_no_legacy_conflict(tmp_path):
    roots, old_root, source, manifest, target = adapter_home(tmp_path)
    options = dict(runtime_policy={'schema_version': 1, 'entries': [RULE]},
        capabilities={'capabilities': {'llmcall.contexts': {
            'status': 'supported', 'evidence': ['synthetic test']}}})
    changes, report = sync.build_plan(*roots, **options)
    sync.apply_plan(changes, report, roots[1], roots[2])
    descriptor = target.parent / 'workflow.json'
    assert Path(json.loads(descriptor.read_bytes())['source_file']) == source
    assert sync._skill_ownership(roots[1], roots[2])[0]['status'] == 'verified'

    new_root = old_root.parent / 'new-cache'
    shutil.copytree(old_root, new_root)
    json_file(roots[0] / 'plugins/installed_plugins.json', {'plugins': {
        'kit@market-a': [{'scope': 'user', 'installPath': str(new_root), 'version': '2'}]}})
    new_source = new_root / 'commands/review.md'
    assert source.read_bytes() == new_source.read_bytes()

    changes, report = sync.build_plan(*roots, **options)
    assert changes
    assert not [r for r in report['skills'] if r['status'] == 'conflict'], report['skills']
    sync.apply_plan(changes, report, roots[1], roots[2])
    assert Path(json.loads(descriptor.read_bytes())['source_file']) == new_source
    records = json.loads(manifest.read_bytes())['items']
    for member in (target, descriptor, target.parent / 'payload/commands/review.md'):
        assert Path(records[str(member)]['source']) == new_source
        assert Path(records[str(member)]['provider_root']) == new_root
    assert sync._skill_ownership(roots[1], roots[2])[0]['status'] == 'verified'
    remaining, report = sync.build_plan(*roots, **options)
    assert not remaining
    assert not [r for r in report['skills'] if r['status'] == 'conflict']


@pytest.mark.parametrize('which', ['old', 'new'])
def test_move_requires_both_source_hashes(tmp_path, which):
    roots, old, new, manifest, target = moved(tmp_path)
    source = old if which == 'old' else new
    source.write_bytes(source.read_bytes() + b'\nSynthetic change.\n')
    changes, report = sync.build_plan(*roots)
    assert any(r['status'] == 'conflict' for r in report['skills'])
    assert not any(r['path'] in {str(target), str(manifest)} for r in changes)


@pytest.mark.parametrize('which', ['old', 'new'])
def test_move_rechecks_both_sources_before_apply(tmp_path, which):
    roots, old, new, manifest, target = moved(tmp_path)
    changes, report = sync.build_plan(*roots)
    before = target.read_bytes(), manifest.read_bytes()
    source = old if which == 'old' else new
    source.write_bytes(source.read_bytes() + b'\nSynthetic race.\n')
    with pytest.raises(ValueError):
        sync.apply_plan(changes, report, roots[1], roots[2])
    assert (target.read_bytes(), manifest.read_bytes()) == before
