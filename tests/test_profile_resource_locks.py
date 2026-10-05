"""Cooperating writers share locks even when their profile roots differ."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
import profile_lock


def test_read_only_lock_does_not_create_directories(tmp_path):
    target = tmp_path / 'missing' / 'profile'
    if os.name == 'nt':
        with profile_lock.acquire(target, create=False):
            assert not target.exists()
    else:
        with pytest.raises(profile_lock.LockUnavailableError):
            with profile_lock.acquire(target, create=False):
                pytest.fail('unprovisioned reader entered protected region')
    assert list(tmp_path.iterdir()) == []


def test_nested_resource_acquisition_is_reentrant(tmp_path):
    a, b = tmp_path / 'a', tmp_path / 'b'
    with profile_lock.acquire_many([b, a]):
        with profile_lock.acquire_many([a, b]):
            pass


def test_two_profiles_with_shared_skills_conflict(tmp_path):
    from profile_sync import destination_lock
    codex_a, codex_b, shared = tmp_path / 'one', tmp_path / 'two', tmp_path / 'shared'
    source = str(Path(__file__).resolve().parents[1])
    script = (f'import sys; sys.path.insert(0, {source!r}); from pathlib import Path; '
              'from profile_sync import destination_lock; import time; '
              f'lock=destination_lock(Path({str(codex_a)!r}),Path({str(shared)!r})); '
              'lock.__enter__(); print("locked",flush=True); time.sleep(45)')
    proc = subprocess.Popen([sys.executable, '-c', script], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        line = proc.stdout.readline().strip()
        assert line == 'locked', proc.stderr.read() if proc.poll() is not None else line
        with pytest.raises(RuntimeError):
            with destination_lock(codex_b, shared):
                pass
    finally:
        proc.kill()
        proc.communicate(timeout=10)


def test_public_apply_takes_resource_locks_even_without_wrapper(tmp_path, monkeypatch):
    import profile_sync
    from contextlib import contextmanager
    entered = []
    @contextmanager
    def lock(codex, skills=None):
        entered.append((codex, skills))
        yield
    monkeypatch.setattr(profile_sync, 'destination_lock', lock)
    codex, skills = tmp_path / 'codex', tmp_path / 'skills'
    profile_sync.apply_plan([], {}, codex, skills)
    assert entered == [(codex, skills)]
