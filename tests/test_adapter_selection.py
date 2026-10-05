"""Runtime selection must verify the installed bundle it tells the host to read."""
from pathlib import Path

import pytest

import profile_catalog
import profile_sync as sync
from profile_bridge.adapters import select_entrypoint
from tools.make_fixtures import make_adapter_descriptions, make_wrapper_resource_home


@pytest.mark.parametrize('member', ['entrypoint', 'payload', 'resource'])
@pytest.mark.parametrize('mutation', ['missing', 'changed'])
def test_selected_bundle_members_must_match_the_planned_bytes(tmp_path, member, mutation):
    fixture = make_wrapper_resource_home(tmp_path)
    claude, codex, skills = (fixture[name] for name in ('claude', 'codex', 'skills'))
    snapshot = profile_catalog.discover_profile(claude, codex, skills)
    entry = next(ep for record in snapshot['records'] for ep in record['entrypoints']
                 if ep['kind'] == 'skill' and Path(ep['path']) == fixture['entrypoint'])
    policy = {'schema_version': 1, 'entries': [{'id': 'synthetic', 'tasks': ['review'],
        'selector': {key: entry[key] for key in ('source_id', 'kind', 'relative_path', 'client', 'scope')}}]}
    capabilities = {'capabilities': {'llmcall.contexts': {
        'status': 'supported', 'evidence': ['Synthetic host observation']}}}
    changes, report = sync.build_plan(claude, codex, skills, runtime_policy=policy, capabilities=capabilities)
    sync.apply_plan(changes, report, codex, skills)
    selection = skills / 'llmcall-tasks/selection.json'
    request = {'task': 'review'}
    result = select_entrypoint(selection, request)
    assert result['status'] == 'selected'
    entrypoint = Path(result['entrypoint_path'])
    target = {'entrypoint': entrypoint, 'payload': entrypoint.parent / 'payload/skills/example/SOURCE.md',
              'resource': entrypoint.parent / 'payload/references/rules.md'}[member]
    original = target.read_bytes()
    if mutation == 'missing':
        target.unlink()
    else:
        target.write_text(make_adapter_descriptions()['summary'], encoding='utf-8')
    modified = target.read_bytes() if target.exists() else None
    result = select_entrypoint(selection, request)
    assert result['status'] == 'blocked'
    assert result['reason'] == 'selected_adapter_' + mutation
    assert (target.read_bytes() if target.exists() else None) == modified
    target.write_bytes(original)
    assert select_entrypoint(selection, request)['status'] == 'selected'
