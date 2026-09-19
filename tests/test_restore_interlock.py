"""Every public writer refuses an incomplete CONFIG restore, even stale plans."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

import profile_memory
import profile_sync
import run_profile_sync
from profile_lock import backup_lock, profile_locks, restore_recovery_locks
from profile_bridge import memory_outbox as outbox
from profile_bridge.memory.compat import plan_files, apply_files
from profile_bridge.restore_interlock import marker_path, recovery_status, RestoreRecoveryRequired


def state(root):
    return {p.relative_to(root).as_posix(): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_ino)
            for p in root.rglob('*') if p.is_file()}


@pytest.fixture
def stale(tmp_path):
    home = tmp_path / 'home'
    claude, codex, skills = home / '.claude', home / '.codex', home / '.agents/skills'
    source = claude / 'projects/synthetic/memory/MEMORY.md'
    source.parent.mkdir(parents=True)
    source.write_text('Synthetic fact\n')
    contract = codex / 'memories/extensions/ad_hoc/instructions.md'
    contract.parent.mkdir(parents=True)
    contract.write_text('Synthetic contract\n')
    plans, _ = profile_memory.plan_memory(claude, codex, request_id='synthetic', scope=['synthetic'])
    intents, _ = plan_files(codex, claude / 'projects', 'synthetic',
        [dict(project='synthetic', path='MEMORY.md', text='Synthetic fact')], request_id='compat')
    marker = marker_path(codex)
    marker.parent.mkdir(parents=True)
    marker.write_bytes(b'{"orphaned":"synthetic"}')
    return home, claude, codex, skills, plans, intents


@pytest.mark.parametrize('writer', ['profile', 'rollback', 'archive', 'authorize', 'revoke',
                                    'prepare', 'deliver', 'conflict', 'compat'])
def test_stale_writer_refuses_without_touching_files(stale, writer):
    home, claude, codex, skills, plans, intents = stale
    operations = {
        'profile': lambda: profile_sync.apply_plan([], {}, codex, skills),
        'rollback': lambda: profile_sync.rollback(home / 'absent-backup', codex, skills),
        'archive': lambda: profile_memory.apply_memory_plan(plans, claude, codex, skills=skills),
        'authorize': lambda: outbox.authorize(codex, claude / 'projects', request_id='new', scope=['synthetic'], skills=skills),
        'revoke': lambda: outbox.revoke(codex, 'synthetic', skills=skills),
        'prepare': lambda: outbox.prepare(plans.delivery, skills=skills),
        'deliver': lambda: outbox.deliver(plans.delivery, skills=skills),
        'conflict': lambda: outbox.preserve_conflict(codex, {'reason': 'synthetic'}, skills=skills),
        'compat': lambda: apply_files(intents, skills=skills),
    }
    before = state(home)
    with pytest.raises(RestoreRecoveryRequired):
        operations[writer]()
    assert before == state(home)


def test_readers_and_runner_do_not_repair_or_publish_status(stale):
    home, claude, codex, skills, _, _ = stale
    before = state(home)
    plans, report = profile_memory.plan_memory(claude, codex)
    assert report['status'] == 'recovery_required' and not plans
    assert recovery_status(codex)['status'] == 'recovery_required'
    for kwargs in ({}, {'apply': True}, {'write_status': True}, {'apply': True, 'write_status': True}):
        result = run_profile_sync.run(claude, codex, skills, **kwargs)
        assert result['status'] == 'recovery_required'
        assert result['reason'] == 'incomplete_restore' and result['exit_code'] == 2
    with pytest.raises(RestoreRecoveryRequired):
        outbox._read_history(codex)
    assert before == state(home)


def test_no_general_recovery_bypass(stale):
    home, _, codex, skills, _, _ = stale
    backup = home / 'capture'
    options = dict(backup=backup, journal_path=home / 'foreign.json', plan_sha256='0' * 64)
    with pytest.raises(RuntimeError, match='restore_requires_backup_lock'):
        with restore_recovery_locks(codex, skills, **options):
            pytest.fail('unlocked recovery')
    with backup_lock(backup.parent), pytest.raises(RestoreRecoveryRequired):
        with restore_recovery_locks(codex, skills, **options):
            pytest.fail('foreign recovery')


def test_workflow_context_writer_refuses_before_dispatch(stale, monkeypatch):
    from profile_bridge import workflows
    home, _, codex, skills, _, _ = stale
    # Descriptor validation belongs to T10. No model library/client is needed to
    # verify that a valid context cannot enter its first mutation or dispatch.
    monkeypatch.setattr(workflows.overlays, 'validate', lambda descriptor: True)
    class NoDispatch:
        def call(self, *args, **kwargs):
            pytest.fail('dispatch under incomplete restore')
    built = {'status': 'ready', 'artifact_hash': 'synthetic', 'recipe': {'kind': 'review'}}
    workflow = workflows.DurableWorkflow(codex, skills, 'synthetic-workflow', built, client=NoDispatch())
    before = state(home)
    with pytest.raises(RestoreRecoveryRequired):
        workflow.run('synthetic-request', context='review', operation='start', prompt='Synthetic input')
    assert state(home) == before


@pytest.mark.parametrize('dry', [False, True])
def test_legacy_cli_fresh_process_refuses(stale, dry):
    home, claude, codex, _, _, _ = stale
    before = state(home)
    command = [sys.executable, '-B', '-m', 'profile_bridge.memory.compat',
               '-ProjectPath', str(home), '-ClaudeProjectsRoot', str(claude / 'projects'),
               '-ClaudeProjectKey', 'synthetic', '-CodexMemoriesRoot', str(codex / 'memories'),
               '-OutputFormat', 'Json']
    if dry:
        command.append('-DryRun')
    child = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert child.returncode == 2, child.stderr
    assert json.loads(child.stdout)['status'] == 'recovery_required'
    assert state(home) == before


@pytest.mark.skipif(os.name != 'nt', reason='Windows compatibility wrappers')
@pytest.mark.parametrize('wrapper', ['ps1', 'cmd'])
def test_real_legacy_wrappers_refuse(stale, wrapper):
    home, claude, codex, _, _, _ = stale
    root = Path(__file__).resolve().parents[1]
    before = state(home)
    args = ['-ProjectPath', str(home), '-ClaudeProjectsRoot', str(claude / 'projects'),
            '-ClaudeProjectKey', 'synthetic', '-CodexMemoriesRoot', str(codex / 'memories'),
            '-OutputFormat', 'Json']
    command = (['powershell.exe', '-NoProfile', '-File', str(root / 'sync-claude-memory-to-codex.ps1')]
               if wrapper == 'ps1' else ['cmd.exe', '/d', '/c', str(root / 'sync-memory.cmd')])
    child = subprocess.run(command + args, capture_output=True, text=True, timeout=30,
                           env=dict(os.environ, CCMS_PYTHON=sys.executable))
    assert child.returncode == 2, child.stderr
    assert json.loads(child.stdout)['status'] == 'recovery_required'
    assert state(home) == before
