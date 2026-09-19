"""Explicit scheduled inputs use synthetic files and never dispatch providers."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

import profile_sync
import run_profile_sync as runner


FLAGS = {
    'runtime_policy': '--runtime-policy',
    'capabilities': '--capability-snapshot',
    'resource_roots': '--overlay-resource-roots',
    'role_equivalence': '--role-equivalence',
}


def healthy_report():
    return {'change_count': 0, 'skills': [], 'plugins_skipped': [],
            'config': {'mcp': [], 'settings': []}, 'hooks': {'hooks': []},
            'instructions': {'status': 'unchanged'}, 'memory': {'status': 'unchanged'}}


@pytest.fixture
def homes(tmp_path):
    roots = tuple(tmp_path / name for name in ('claude', 'codex', 'skills'))
    for root in roots:
        root.mkdir()
    return roots


def home_args(homes):
    return [arg for name, root in zip(('claude', 'codex', 'skills'), homes)
            for arg in (f'--{name}-home', str(root))]


def inputs(tmp_path):
    label = 'Synthetic caf\u00e9 \u96ea'
    return {
        'runtime_policy': {'schema_version': 1, 'entries': [], 'label': label},
        'capabilities': {'capabilities': {}, 'label': label},
        'resource_roots': {'synthetic:fixture': str(tmp_path / label)},
        'role_equivalence': {'synthetic:fixture:agent.md': {
            'status': 'unverified', 'artifact_hash': '', 'evidence': [], 'label': label}},
    }


def write_inputs(tmp_path, values, encoding):
    paths, args = {}, []
    for name, flag in FLAGS.items():
        path = tmp_path / f'{name}.json'
        path.write_bytes(json.dumps(values[name], ensure_ascii=False).encode(encoding))
        paths[name] = path
        args.extend((flag, str(path)))
    return paths, args


def files_state(roots):
    return {str(path): (path.read_bytes(), path.stat().st_mtime_ns)
            for root in roots for path in root.rglob('*') if path.is_file()}


@pytest.mark.parametrize('apply', [False, True])
@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
def test_scheduled_main_passes_one_snapshot_to_both_plans(homes, tmp_path, monkeypatch, capsys, apply, encoding):
    expected = inputs(tmp_path)
    paths, args = write_inputs(tmp_path, expected, encoding)
    read_text = Path.read_text
    reads = []

    def read_once(path, *args, **kwargs):
        if path in paths.values():
            reads.append(path)
            assert kwargs == {'encoding': 'utf-8-sig'}
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'read_text', read_once)
    calls = []

    def plan(*roots, **kwargs):
        assert roots == homes
        assert kwargs == dict(expected, request_id='synthetic', scope=['synthetic'], periodic=True)
        calls.append(kwargs)
        return [], healthy_report()

    applied = []

    def apply_plan(changes, report, codex, skills):
        applied.append(True)
        # A source file edit after preview cannot change verification's inputs.
        for path in paths.values():
            path.write_bytes(b'{"synthetic-secret": invalid}')
        return report

    monkeypatch.setattr(runner, 'build_plan', plan)
    monkeypatch.setattr(runner, 'apply_plan', apply_plan)
    argv = home_args(homes) + args + ['--request-id', 'synthetic', '--scope', 'synthetic', '--periodic']
    if apply:
        argv += ['--apply', '--write-status']
    assert runner.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'healthy'
    assert len(calls) == (2 if apply else 1)
    assert len(applied) == int(apply)
    assert reads == list(paths.values())
    if apply:
        for name in FLAGS:
            assert calls[0][name] is calls[1][name]
        assert (homes[1] / 'claude-sync/last-success.json').is_file()
    else:
        assert files_state(homes) == {}


def test_run_freezes_optional_parsed_keyword_inputs(homes, tmp_path, monkeypatch):
    supplied = inputs(tmp_path)
    expected = deepcopy(supplied)
    calls = []

    def plan(*roots, **kwargs):
        calls.append(kwargs)
        assert {name: kwargs[name] for name in FLAGS} == expected
        return [], healthy_report()

    def apply_plan(changes, report, *roots):
        for value in supplied.values():
            value.clear()
        return report

    monkeypatch.setattr(runner, 'build_plan', plan)
    monkeypatch.setattr(runner, 'apply_plan', apply_plan)
    assert runner.run(*homes, apply=True, **supplied)['exit_code'] == 0
    assert len(calls) == 2
    for name in FLAGS:
        assert calls[0][name] is calls[1][name]
        assert calls[0][name] is not supplied[name]


@pytest.mark.parametrize('flag', FLAGS.values())
@pytest.mark.parametrize('failure,error_type', [
    ('malformed', 'JSONDecodeError'), ('missing', 'FileNotFoundError'),
    ('invalid_utf8', 'UnicodeDecodeError'),
])
def test_scheduled_bad_input_fails_safely_before_any_write(homes, tmp_path, monkeypatch, capsys, flag, failure, error_type):
    path = tmp_path / 'synthetic-secret-filename.json'
    if failure == 'malformed':
        path.write_bytes(b'{"synthetic-secret-value": invalid}')
    elif failure == 'invalid_utf8':
        path.write_bytes(b'{"synthetic-secret-value": "\xff"}')
    success = homes[1] / 'claude-sync/last-success.json'
    success.parent.mkdir()
    success.write_bytes(b'{"status":"healthy","finished_at":"prior-run"}')
    before = files_state(homes)

    def forbidden(*args, **kwargs):
        pytest.fail('Invalid input entered a planner or writer')

    for name in ('destination_lock', 'build_plan', 'apply_plan', '_publish'):
        monkeypatch.setattr(runner, name, forbidden)
    assert runner.main(home_args(homes) + [flag, str(path), '--apply', '--write-status']) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {'status': 'error', 'error_type': error_type, 'exit_code': 1}
    assert 'synthetic-secret' not in captured.out + captured.err
    assert files_state(homes) == before


@pytest.mark.parametrize('apply', [False, True])
def test_scheduled_main_without_inputs_keeps_legacy_plan_kwargs(homes, monkeypatch, capsys, apply):
    calls = []

    def plan(*roots, **kwargs):
        calls.append(kwargs)
        assert kwargs == {'request_id': None, 'scope': None, 'periodic': False}
        return [], healthy_report()

    monkeypatch.setattr(runner, 'build_plan', plan)
    monkeypatch.setattr(runner, 'apply_plan', lambda changes, report, *roots: report)
    argv = home_args(homes) + (['--apply'] if apply else [])
    assert runner.main(argv) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'healthy'
    assert len(calls) == (2 if apply else 1)


@pytest.mark.parametrize('encoding', ['utf-8', 'utf-8-sig'])
def test_direct_cli_retains_shared_input_contract(homes, tmp_path, monkeypatch, capsys, encoding):
    expected = inputs(tmp_path)
    _, args = write_inputs(tmp_path, expected, encoding)
    calls = []

    def plan(*roots, **kwargs):
        calls.append(kwargs)
        assert {name: kwargs[name] for name in FLAGS} == expected
        return [], healthy_report()

    monkeypatch.setattr(profile_sync, 'build_plan', plan)
    assert profile_sync.main(home_args(homes) + args + ['--dry-run', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'no_changes'
    assert len(calls) == 1
