"""Process-lifetime destination locks. Crashes release ownership automatically."""
from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import stat
import threading


class LockBusyError(RuntimeError):
    """A cooperating writer currently owns this resource."""
    reason = 'profile_or_backup_writer_active'


class LockUnavailableError(LockBusyError):
    """A read-only caller cannot acquire an unprovisioned lock resource."""
    reason = 'profile_or_backup_lock_unavailable'


_local = threading.local()


def resource_key(destination):
    return os.path.normcase(str(Path(destination).resolve(strict=False)))


@contextmanager
def acquire(destination: Path, *, create=True):
    """Acquire a resource without nesting conflicts; reads never create disk state."""
    identity = resource_key(destination)
    if getattr(_local, 'pid', None) != os.getpid():
        _local.pid, _local.held = os.getpid(), set()
    if identity in _local.held:
        yield
        return
    with _acquire_native(Path(identity), create=create):
        _local.held.add(identity)
        try:
            yield
        finally:
            _local.held.remove(identity)


@contextmanager
def acquire_many(destinations, *, create=True):
    with ExitStack() as stack:
        for key in sorted({resource_key(path) for path in destinations}):
            stack.enter_context(acquire(Path(key), create=create))
        yield


def profile_resources(codex, skills):
    # Preserve the existing Windows mutex identity for the profile writer.
    return (Path(codex) / 'claude-sync', Path(skills))


@contextmanager
def profile_locks(codex, skills, *, create=True):
    with acquire_many(profile_resources(codex, skills), create=create):
        # Check even when these locks are already held: a previously built plan
        # and a nested writer must not pass an incomplete restore.
        from profile_bridge.restore_interlock import require_ready
        require_ready(codex)
        yield


@contextmanager
def restore_recovery_locks(codex, skills, *, backup, journal_path, plan_sha256):
    """CONFIG-only recovery access, bound to its journal and held backup lock."""
    backup_resource = resource_key(Path(backup).absolute().parent / '.profile-backup-resource')
    if (getattr(_local, 'pid', None) != os.getpid() or
            backup_resource not in getattr(_local, 'held', set())):
        raise RuntimeError('restore_requires_backup_lock')
    with acquire_many(profile_resources(codex, skills)):
        from profile_bridge.restore_interlock import validate_owner
        validate_owner(codex, backup, journal_path, plan_sha256)
        yield


def backup_lock(repo, *, create=True):
    # The lock identity is outside replaceable snapshot and transaction directories.
    return acquire(Path(repo) / '.profile-backup-resource', create=create)


def assert_restore_ownership(codex, skills, backup):
    """Require the existing backup and complete profile lock set for publication."""
    required = {resource_key(p) for p in profile_resources(codex, skills)}
    required.add(resource_key(Path(backup).absolute().parent / '.profile-backup-resource'))
    if (getattr(_local, 'pid', None) != os.getpid() or
            not required <= getattr(_local, 'held', set())):
        raise RuntimeError('restore_requires_owner_locks')


@contextmanager
def _acquire_native(destination: Path, *, create=True):
    identity = os.path.normcase(str(destination.absolute()))
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        name = 'Global\\CodexClaudeProfileSync-' + hashlib.sha256(identity.encode()).hexdigest()
        handle = kernel.CreateMutexW(None, False, name)
        if not handle:
            raise ctypes.WinError(ctypes.get_last_error())
        held = False
        try:
            verdict = kernel.WaitForSingleObject(handle, 0)
            held = verdict in (0, 0x80)  # Normal or abandoned ownership.
            if not held:
                if verdict == 0x102:
                    raise LockBusyError('Another profile sync is running')
                raise ctypes.WinError(ctypes.get_last_error())
            yield
        finally:
            if held:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        # Keep lock inodes outside the protected tree. Never unlink them: doing
        # so would let a new caller lock another inode while an old holder lives.
        # Resolve the account home through the OS account database, not HOME,
        # XDG_STATE_HOME or TMPDIR. Resolution itself never probes with writes.
        import pwd
        account_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        if not account_home.is_absolute():
            raise RuntimeError('Unsafe profile lock account home')
        root = account_home / '.local/state/claude-codex-memory-sync/locks'
        if create:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            info = root.lstat()
        except FileNotFoundError as exc:
            raise LockUnavailableError('Profile lock resource is not provisioned') from exc
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise RuntimeError('Unsafe profile lock directory')
        lock_path = root / (hashlib.sha256(identity.encode()).hexdigest() + '.lock')
        flags = (os.O_RDWR | os.O_CREAT if create else os.O_RDONLY) | getattr(os, 'O_NOFOLLOW', 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except FileNotFoundError as exc:
            if create:
                raise
            raise LockUnavailableError('Profile lock resource is not provisioned') from exc
        with os.fdopen(fd, 'r+b' if create else 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
                raise RuntimeError('Unsafe profile lock file')
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockBusyError('Another profile sync is running') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
