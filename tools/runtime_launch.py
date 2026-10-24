"""Stable entry into the active generation of one installed runtime root.

A generation directory is named by a random token and changes on every install, so nothing outside the
installer may name one. Consumers (scheduled-task launchers, MCP bridge entries) name this file instead,
at a path that never changes:

    <toolchain python> -I -B <root>/launch.py -m MODULE [ARGS...]
    <toolchain python> -I -B <root>/launch.py --script RELPATH [ARGS...]

The installer (runtime_artifacts.install) publishes it as <root>/launch.py after every install; this
same module is also the publisher (`publish --root DIR`). The toolchain interpreter is already a hard
dependency of every generation (each venv's pyvenv.cfg points its home there), so naming it adds none.

Each launch reads <root>/current.json, requires the receipt it names to hash to the recorded digest,
requires the generation interpreter (and, for --script, the script) to hash to the receipt's file entry,
and re-reads current.json so a concurrent publish cannot mix two generations. It does not re-hash the
whole generation: that full inventory takes 30-40 s on a real generation, longer than an MCP client
waits for a server to start, and `runtime-bootstrap.pyz --resolve` remains the full check.
The child always runs with -I -B (isolated, no bytecode writes into the receipt-checked tree).

The child runs inside a Windows job object that is closed when this process ends, so killing the
launcher (an MCP client closing its server, the Task Scheduler ending a run) also ends the child;
without it the intermediary would leave the real server running unowned. Processes may still break
away explicitly. A job that cannot be set up refuses the launch instead of running without it.

Every refusal exits 78 with one JSON line on stderr naming a fixed reason code; nothing is started.
Stdlib only: this file is copied out of the repository and runs before any package is importable.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
import uuid

LAUNCHER_NAME = "launch.py"
EXIT_REFUSED = 78
MAX_JSON_BYTES = 16 * 1024 * 1024
GENERATION = re.compile(r"generation-[0-9a-f]{32}")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
PYTHON_REL = "environment/Scripts/python.exe" if os.name == "nt" else "environment/bin/python"
RESERVED = {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}


class LaunchError(Exception):
    """A refusal. `reason` is a fixed code; nothing derived from file contents is reported."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _regular(path: Path, reason: str) -> None:
    if _is_link(path):
        raise LaunchError("link_rejected")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise LaunchError(reason) from None
    if not stat.S_ISREG(info.st_mode):
        raise LaunchError(reason)


def _read(path: Path, reason: str) -> bytes:
    _regular(path, reason)
    with path.open("rb") as stream:
        data = stream.read(MAX_JSON_BYTES + 1)
    if len(data) > MAX_JSON_BYTES:
        raise LaunchError("json_too_large")
    return data


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise LaunchError("duplicate_json_key")
        result[key] = value
    return result


def _json(data: bytes, reason: str):
    try:
        return json.loads(data, object_pairs_hook=_unique)
    except (UnicodeError, ValueError):
        raise LaunchError(reason) from None


def _file_digest(path: Path, reason: str) -> str:
    _regular(path, reason)
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def safe_relative(name) -> str:
    """The receipt's own path form: posix, relative, no dot segments, no Windows-reserved parts."""
    if not isinstance(name, str) or not name or "\\" in name or name.startswith("/"):
        raise LaunchError("unsafe_relative_path")
    parts = PurePosixPath(name).parts
    if not parts or "/".join(parts) != name or any(
            p in (".", "..") or p.endswith((" ", ".")) or re.search(r'[<>:"|?*\x00-\x1f]', p)
            or p.split(".")[0].upper() in RESERVED for p in parts):
        raise LaunchError("unsafe_relative_path")
    return name


def resolve(root) -> dict:
    """The verified active generation of `root`: its stage directory, interpreter and receipt."""
    root = Path(root).absolute()
    for part in (root, root / "generations"):
        if _is_link(part):
            raise LaunchError("link_rejected")
    pointer_bytes = _read(root / "current.json", "pointer_missing")
    pointer = _json(pointer_bytes, "pointer_invalid")
    if not isinstance(pointer, dict) or set(pointer) != {"generation", "receipt_sha256"}:
        raise LaunchError("pointer_invalid")
    generation = pointer["generation"]
    if not isinstance(generation, str) or not GENERATION.fullmatch(generation):
        raise LaunchError("pointer_invalid")
    stage = root / "generations" / generation
    if _is_link(stage):
        raise LaunchError("link_rejected")
    receipt_bytes = _read(stage / "receipt.json", "receipt_missing")
    if hashlib.sha256(receipt_bytes).hexdigest() != pointer["receipt_sha256"]:
        raise LaunchError("receipt_digest_mismatch")
    receipt = _json(receipt_bytes, "receipt_invalid")
    if (not isinstance(receipt, dict) or receipt.get("generation") != generation
            or not isinstance(receipt.get("files"), dict)):
        raise LaunchError("receipt_invalid")
    python = stage / PYTHON_REL
    recorded = receipt.get("python")
    if not isinstance(recorded, str) or os.path.normcase(recorded) != os.path.normcase(str(python)):
        raise LaunchError("interpreter_not_in_receipt")
    expected = receipt["files"].get(PYTHON_REL)
    if not isinstance(expected, str) or _file_digest(python, "interpreter_missing") != expected:
        raise LaunchError("interpreter_changed")
    # A publish between the two reads would pair this receipt with another pointer.
    if _read(root / "current.json", "pointer_missing") != pointer_bytes:
        raise LaunchError("pointer_changed")
    return {"root": root, "generation": generation, "stage": stage, "python": python, "receipt": receipt}


def child_argv(resolved: dict, mode: str, target: str, args) -> list:
    if mode == "-m":
        if not isinstance(target, str) or not MODULE.fullmatch(target):
            raise LaunchError("invalid_module")
        return [str(resolved["python"]), "-I", "-B", "-m", target, *args]
    if mode == "--script":
        rel = safe_relative(target)
        expected = resolved["receipt"]["files"].get(rel)
        if not isinstance(expected, str):
            raise LaunchError("script_not_in_receipt")
        script = resolved["stage"] / rel
        if _file_digest(script, "script_missing") != expected:
            raise LaunchError("script_changed")
        return [str(resolved["python"]), "-I", "-B", str(script), *args]
    raise LaunchError("invalid_arguments")


def describe(resolved: dict) -> dict:
    """Expose installed entrypoints without repeating the full installation inventory."""
    stage, receipt = resolved["stage"], resolved["receipt"]
    entries = {}
    for name, entry in receipt.get("entrypoints", {}).items():
        item = dict(entry, path=str(stage / safe_relative(entry["path"])))
        if entry.get("import_root"):
            item["import_root"] = str(stage / safe_relative(entry["import_root"]))
        entries[name] = item
    return {"generation": resolved["generation"], "stage": str(stage),
            "python": str(resolved["python"]), "packages": receipt.get("packages", {}),
            "entrypoints": entries}


class _Job:
    """A job object that kills its processes when the last handle (ours) closes."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount",
                        "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise LaunchError("job_unavailable")
        info = EXTENDED()
        # KILL_ON_JOB_CLOSE | BREAKAWAY_OK: ending us ends the child; an explicit breakaway still works.
        info.BasicLimitInformation.LimitFlags = 0x2000 | 0x0800
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise LaunchError("job_unavailable")
        # The launcher joins first, so the child is a member from its first instruction (no window in
        # which it could start a grandchild outside the job). Nested jobs are supported since Windows 8.
        if not kernel32.AssignProcessToJobObject(handle, kernel32.GetCurrentProcess()):
            raise LaunchError("job_unavailable")
        self.handle = handle


def _exit_code(code: int) -> int:
    # A Windows exit status is a DWORD; sys.exit takes a C long, so pass the same bits signed.
    return code - (1 << 32) if code > 0x7FFFFFFF else code


def run(root, mode: str, target: str, args) -> int:
    resolved = resolve(root)
    argv = child_argv(resolved, mode, target, list(args))
    job = _Job() if os.name == "nt" else None
    try:
        process = subprocess.Popen(argv)
    except OSError:
        raise LaunchError("child_unavailable") from None
    while True:
        try:
            code = process.wait()
            break
        except KeyboardInterrupt:
            continue  # the console delivered Ctrl+C to the child too; its exit decides
    # The job handle stays open until this process exits; closing it then ends anything still inside.
    assert job is None or job.handle
    return _exit_code(code)


def source_bytes() -> bytes:
    """This module's own bytes, from a checkout or from inside the bootstrap .pyz."""
    return __loader__.get_data(__file__)  # noqa: F821  (set by every import system loader)


def publish(root) -> str:
    """Write this module to <root>/launch.py when it differs; returns 'published' or 'unchanged'."""
    root = Path(root).absolute()
    if _is_link(root) or not root.is_dir():
        raise LaunchError("root_missing")
    data = source_bytes()
    target = root / LAUNCHER_NAME
    if target.exists() or _is_link(target):
        _regular(target, "launcher_not_a_file")
        if target.read_bytes() == data:
            return "unchanged"
    temporary = root / ("launch-" + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    if target.read_bytes() != data:
        raise LaunchError("publish_readback_mismatch")
    return "published"


def _refuse(reason: str) -> int:
    sys.stderr.write(json.dumps({"launcher": "refused", "reason": reason}) + "\n")
    sys.stderr.flush()
    return EXIT_REFUSED


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    root = Path(__file__).absolute().parent
    try:
        if argv[:1] == ["publish"]:
            if len(argv) != 3 or argv[1] != "--root":
                raise LaunchError("invalid_arguments")
            print(json.dumps({"launcher": publish(argv[2])}))
            return 0
        if argv[:1] == ["resolve"]:
            if argv[1:] and (len(argv) != 3 or argv[1] != "--root"):
                raise LaunchError("invalid_arguments")
            found = resolve(argv[2] if argv[1:] else root)
            print(json.dumps(describe(found)))
            return 0
        if argv[:1] == ["--root"]:
            if len(argv) < 2:
                raise LaunchError("invalid_arguments")
            root, argv = Path(argv[1]), argv[2:]
        if len(argv) < 2 or argv[0] not in ("-m", "--script"):
            raise LaunchError("invalid_arguments")
        return run(root, argv[0], argv[1], argv[2:])
    except LaunchError as error:
        return _refuse(error.reason)
    except OSError:
        return _refuse("io_error")


if __name__ == "__main__":
    sys.exit(main())
