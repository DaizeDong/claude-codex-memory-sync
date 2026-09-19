"""No fake lock may authorize entry; native POSIX contention runs on POSIX."""
import os
from pathlib import Path
import subprocess
import sys

import pytest
import profile_lock


def child(code, env=None, **kwargs):
    environment = dict(os.environ, PYTHONPATH=str(Path(profile_lock.__file__).parent)
                       + os.pathsep + os.environ.get('PYTHONPATH', ''), PYTHONDONTWRITEBYTECODE='1')
    environment.update(env or {})
    return subprocess.Popen([sys.executable, '-B', '-c', code], env=environment, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kwargs)


def posix_without_lock_backend(tmp_path):
    # Exercise pre-acquisition behavior on every OS. Any attempt to take a
    # simulated flock is a failure; this adapter never grants ownership.
    return (
        'import os,sys,types\nfrom pathlib import Path\nimport profile_lock\n'
        'uid=getattr(os,"getuid",lambda:0)()\n'
        'profile_lock.os=types.SimpleNamespace(**dict(vars(os),name="posix",getuid=lambda:uid))\n'
        f'sys.modules["pwd"]=types.SimpleNamespace(getpwuid=lambda uid:types.SimpleNamespace(pw_dir={str(tmp_path)!r}))\n'
        'def forbidden(*args): raise AssertionError("unexpected lock backend")\n'
        'sys.modules["fcntl"]=types.SimpleNamespace(flock=forbidden,LOCK_EX=2,LOCK_NB=4,LOCK_UN=8)\n')


def test_cold_read_only_attempts_no_writes(tmp_path):
    code = posix_without_lock_backend(tmp_path) + (
        'import tempfile\ntempfile.tempdir=None\n'
        'def audit(event,args):\n'
        ' if event == "open" and args[2] & (os.O_CREAT|os.O_TRUNC|os.O_WRONLY|os.O_RDWR):\n'
        '  raise AssertionError("write attempt")\n'
        ' if event in {"os.mkdir","os.remove","os.rename","os.rmdir"}:\n'
        '  raise AssertionError("mutation attempt")\n'
        'sys.addaudithook(audit)\n'
        f'try:\n with profile_lock.acquire(Path({str(tmp_path / "fresh")!r}),create=False): print("acquired")\n'
        'except profile_lock.LockBusyError: print("unavailable")\n')
    proc = child(code)
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert out.strip() == 'unavailable'


@pytest.mark.parametrize('directory_exists', [False, True])
def test_missing_reader_lock_never_enters_or_marks_held(tmp_path, directory_exists):
    if directory_exists:
        (tmp_path / '.local/state/claude-codex-memory-sync/locks').mkdir(parents=True)
    code = posix_without_lock_backend(tmp_path) + (
        f'target=Path({str(tmp_path / "fresh")!r})\n'
        'try:\n with profile_lock.acquire(target,create=False): print("unprotected")\n'
        'except profile_lock.LockBusyError: print("unavailable")\n'
        'assert profile_lock.resource_key(target) not in profile_lock._local.held\n')
    proc = child(code, env={'TMPDIR': str(tmp_path), 'TEMP': str(tmp_path), 'TMP': str(tmp_path)})
    out, err = proc.communicate(timeout=10)
    assert proc.returncode == 0, err
    assert out.strip() == 'unavailable'


@pytest.mark.skipif(os.name == 'nt', reason='requires real POSIX flock and account database')
def test_different_tmpdir_writers_contend(tmp_path):
    one, two = tmp_path / 'one', tmp_path / 'two'
    one.mkdir()
    two.mkdir()
    target = tmp_path / 'profile'
    # Account lookup is isolated to a synthetic account directory. flock and
    # filesystem operations are real; the test cannot touch live lock state.
    prelude = ('import pwd,types\n'
               f'pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir={str(tmp_path)!r})\n')
    holder = child(prelude +
        f'from pathlib import Path\nfrom profile_lock import acquire\n'
        f'lock=acquire(Path({str(target)!r})); lock.__enter__()\n'
        'print("held",flush=True); input()\n', env={'TMPDIR': str(one)}, stdin=subprocess.PIPE)
    try:
        assert holder.stdout.readline().strip() == 'held'
        contender = child(prelude +
            'from pathlib import Path\nfrom profile_lock import acquire,LockBusyError\n'
            f'try:\n with acquire(Path({str(target)!r})): print("overlap")\n'
            'except LockBusyError: print("busy")\n', env={'TMPDIR': str(two)})
        out, err = contender.communicate(timeout=10)
        assert contender.returncode == 0, err
        assert out.strip() == 'busy'
    finally:
        holder.communicate('\n', timeout=10)


@pytest.mark.skipif(os.name == 'nt', reason='requires real POSIX flock')
def test_first_use_reader_then_writer(tmp_path):
    prelude = ('import pwd,types\n'
               f'pwd.getpwuid=lambda uid:types.SimpleNamespace(pw_dir={str(tmp_path)!r})\n'
               'from pathlib import Path\nfrom profile_lock import acquire,LockBusyError\n'
               f'target=Path({str(tmp_path / "fresh")!r})\n')
    reader = child(prelude +
        'try:\n with acquire(target,create=False): print("unprotected",flush=True)\n'
        'except LockBusyError: print("unavailable",flush=True)\ninput()\n'
        'try:\n with acquire(target,create=False): print("overlap",flush=True)\n'
        'except LockBusyError: print("busy",flush=True)\n', stdin=subprocess.PIPE)
    writer = None
    try:
        assert reader.stdout.readline().strip() == 'unavailable'
        writer = child(prelude + 'with acquire(target):\n print("held",flush=True)\n input()\n', stdin=subprocess.PIPE)
        assert writer.stdout.readline().strip() == 'held'
        out, err = reader.communicate('\n', timeout=10)
        assert reader.returncode == 0, err
        assert out.strip() == 'busy'
    finally:
        if reader.poll() is None:
            reader.kill()
            reader.communicate(timeout=10)
        if writer:
            writer.communicate('\n', timeout=10)
