"""Canonical resource resolution for linked sources, using generated fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import profile_sync as sync
from tools.make_fixtures import make_resource_tree, make_wrapper_resource_home


@pytest.fixture
def resource_tree(tmp_path):
    tree = make_resource_tree(tmp_path)
    links = [tree['installed'], tree['root'] / 'escape']
    sync.make_link(links[0], tree['entrypoint'].parent)
    sync.make_link(links[1], tree['outside'].parent)
    try:
        yield tree
    finally:
        for path in links:
            if sync.linked(path):
                if os.name == 'nt' and not path.is_symlink():
                    os.rmdir(path)
                else:
                    path.unlink()


def invoke(tree, reference, *, entrypoint=None, source_root=None):
    return subprocess.run([sys.executable, '-m', 'profile_bridge.resources',
        '--entrypoint', str(entrypoint or tree['installed'] / 'SKILL.md'),
        '--source-root', str(source_root or tree['root']), '--reference', reference],
        capture_output=True, text=True, timeout=30)


def test_cli_resolves_from_canonical_source_instead_of_alias(resource_tree):
    tree = resource_tree
    result = invoke(tree, '../../shared-references/rules.md#checks')
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert Path(payload['path']) == tree['reference'].resolve()
    assert Path(payload['canonical_entrypoint']) == tree['entrypoint'].resolve()
    assert payload['fragment'] == 'checks'
    assert tree['reference'].read_text() != (tree['root'].parent / 'shared-references/rules.md').read_text()


@pytest.mark.parametrize('reference,reason', [
    ('../../../outside/rules.md', 'resource_outside_source_root'),
    ('../../escape/rules.md', 'resource_outside_source_root'),
    ('../../missing.md', 'resource_missing'),
    ('../../shared-references', 'resource_not_file'),
    ('https://example.com/rules.md', 'resource_reference_not_relative'),
    ('C:/example/rules.md', 'resource_reference_not_relative'),
    ('/example/rules.md', 'resource_reference_not_relative'),
    ('#checks', 'resource_reference_not_relative'),
    ('%2e%2e/%2e%2e/%2e%2e/outside/rules.md', 'resource_outside_source_root'),
])
def test_cli_fails_without_returning_a_candidate(resource_tree, reference, reason):
    result = invoke(resource_tree, reference)
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload == {'status': 'blocked', 'reason': reason}
    assert not result.stderr


def test_cli_rejects_source_outside_selected_root(resource_tree):
    result = invoke(resource_tree, 'rules.md', source_root=resource_tree['outside'].parent)
    assert result.returncode == 2
    assert json.loads(result.stdout)['reason'] == 'entrypoint_outside_source_root'


def test_cli_rejects_missing_entrypoint(resource_tree):
    resource_tree['entrypoint'].unlink()
    result = invoke(resource_tree, '../../shared-references/rules.md')
    assert result.returncode == 2
    assert json.loads(result.stdout)['reason'] == 'entrypoint_missing'


def test_inventory_exposes_canonical_context_and_keeps_source_identity(resource_tree, tmp_path):
    from profile_inventory import inventory_sources
    claude, codex = tmp_path / 'claude', tmp_path / 'codex'
    claude.mkdir()
    report = inventory_sources(claude, codex, resource_tree['installed'].parent)
    row = next(item for item in report['skills'] if item['location'] == 'agents')
    context = row['resource_context']
    assert Path(context['canonical_entrypoint']) == resource_tree['entrypoint'].resolve()
    assert Path(context['base_directory']) == resource_tree['entrypoint'].parent.resolve()
    assert context['source_root_basis'] == 'catalog_source'
    assert row['source_id'] == row['catalog_record']['source_id']
    assert row['catalog_entrypoint']['source_hash'].startswith('sha256:')


def test_inventory_does_not_attribute_logical_source_labels_to_cwd_repository(resource_tree):
    from profile_inventory import repository_info
    result = repository_info(resource_tree['logical_source'], {})
    assert result == {'status': 'unavailable', 'reason': 'source_not_absolute_path'}


def install_wrapper_fixture(tmp_path, overlay=False):
    fixture = make_wrapper_resource_home(tmp_path, role=overlay)
    options = {}
    if overlay:
        options = {'runtime_policy': {'schema_version': 1, 'entries': []},
            'capabilities': {'capabilities': {name: {'status': 'supported', 'evidence': ['synthetic fixture']}
                for name in ('llmcall.contexts', 'llmcall.agent', 'llmcall.independent_review')}}}
    changes, report = sync.build_plan(fixture['claude'], fixture['codex'], fixture['skills'], **options)
    sync.apply_plan(changes, report, fixture['codex'], fixture['skills'])
    return fixture


def wrapper_row(fixture, original):
    from profile_inventory import inventory_sources
    report = inventory_sources(fixture['claude'], fixture['codex'], fixture['skills'])
    return next(row for row in report['skills'] if row['location'] == 'agents' and row['source'] == str(original))


def test_forwarding_wrapper_context_resolves_resources_from_verified_upstream(tmp_path):
    fixture = install_wrapper_fixture(tmp_path)
    row = wrapper_row(fixture, fixture['command'])
    context = row['resource_context']
    assert context['status'] == 'available'
    assert context['source_root_basis'] == 'managed_forwarding_source'
    assert Path(context['canonical_entrypoint']) == fixture['command']
    result = invoke(fixture, '../references/rules.md', entrypoint=context['canonical_entrypoint'],
                    source_root=context['source_root'])
    assert result.returncode == 0, result.stderr
    assert Path(json.loads(result.stdout)['path']) == fixture['reference']
    fixture['command'].write_text('Changed synthetic source.\n', encoding='utf-8')
    assert wrapper_row(fixture, fixture['command'])['resource_context'] == {
        'status': 'unavailable', 'reason': 'adapter_source_changed'}


def test_overlay_context_resolves_owned_payload_instead_of_upstream(tmp_path):
    fixture = install_wrapper_fixture(tmp_path, overlay=True)
    row = wrapper_row(fixture, fixture['entrypoint'])
    context = row['resource_context']
    assert context['status'] == 'available'
    assert context['source_root_basis'] == 'owned_overlay_payload'
    payload = Path(row['path']) / 'payload'
    assert Path(context['source_root']) == payload
    assert Path(context['canonical_entrypoint']) == payload / 'agents/code-reviewer.md'
    fixture['reference'].write_text('Changed upstream; installed bundle stays pinned.\n', encoding='utf-8')
    result = invoke(fixture, '../references/rules.md', entrypoint=context['canonical_entrypoint'],
                    source_root=context['source_root'])
    assert result.returncode == 0, result.stderr
    resolved = Path(json.loads(result.stdout)['path'])
    assert resolved == payload / 'references/rules.md'
    assert resolved.read_text(encoding='utf-8') == 'Synthetic pinned rules.\n'
    Path(context['canonical_entrypoint']).write_text('Changed installed payload.\n', encoding='utf-8')
    assert wrapper_row(fixture, fixture['entrypoint'])['resource_context'] == {
        'status': 'unavailable', 'reason': 'owned_resource_changed'}
