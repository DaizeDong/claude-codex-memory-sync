"""Process-lifetime destination locks. Crashes release ownership automatically."""
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path


@contextmanager
def acquire(destination: Path):
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
                    raise RuntimeError('Another profile sync is running')
                raise ctypes.WinError(ctypes.get_last_error())
            yield
        finally:
            if held:
                kernel.ReleaseMutex(handle)
            kernel.CloseHandle(handle)
    else:
        import fcntl
        destination.mkdir(parents=True, exist_ok=True)
        # Never unlink an advisory lock inode: a waiting opener could retain the
        # old inode while a new caller creates a different lock at the same path.
        with (destination / '.profile-sync.lock').open('a+b') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError('Another profile sync is running') from exc
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)
