"""Plan the two reviewed, local Stop hooks without executing or writing them.

The generated bridge has an argv-only subprocess boundary and never forwards
child output. Its sidecar owns only its bridge and one Stop handler; unfamiliar
or user-edited state is left alone. Other Claude hooks remain inventory entries.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import re
import shlex
import shutil
import sys


_OWNER = "claude-codex-profile-sync-stop-hooks"
_APPROVED = {"pw-auth.py": ["absorb"], "pw_export_guard.py": []}
_PYTHON = re.compile(r"python(?:\d+(?:\.\d+)*)?(?:\.exe)?\Z", re.I)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _fingerprint(value: object) -> str:
    return _digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def _windows_split(command: str) -> list[str]:
    """Decode Windows double quotes/backslashes without invoking a shell."""
    if not isinstance(command, str) or "\x00" in command or "\n" in command or "\r" in command:
        raise ValueError("invalid_command")
    arguments, index, length = [], 0, len(command)
    while index < length:
        while index < length and command[index] in " \t":
            index += 1
        if index == length:
            break
        current, quoted = [], False
        while index < length:
            if command[index] in " \t" and not quoted:
                break
            if command[index] == "\\":
                start = index
                while index < length and command[index] == "\\":
                    index += 1
                count = index - start
                if index < length and command[index] == '"':
                    current.extend("\\" * (count // 2))
                    if count % 2:
                        current.append('"')
                    else:
                        quoted = not quoted
                    index += 1
                else:
                    current.extend("\\" * count)
                continue
            if command[index] == '"':
                quoted = not quoted
            else:
                current.append(command[index])
            index += 1
        if quoted:
            raise ValueError("unbalanced_windows_quotes")
        arguments.append("".join(current))
    return arguments


def _read_object(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    return value


def _approved_job(command: str, source_home: Path, timeout: object) -> tuple[dict | None, str]:
    try:
        argv = _windows_split(command)
    except ValueError:
        return None, "command_is_not_simple_windows_argv"
    if len(argv) not in (2, 3):
        return None, "command_not_reviewed"
    script = Path(argv[1])
    name = script.name
    if name not in _APPROVED or argv[2:] != _APPROVED[name]:
        return None, "command_not_reviewed"
    executable = Path(argv[0])
    if not _PYTHON.fullmatch(executable.name):
        return None, "reviewed_hook_requires_python_executable"
    if not executable.is_absolute():
        found = shutil.which(argv[0])
        if not found:
            return None, "python_executable_missing"
        executable = Path(found)
    try:
        scripts = (source_home / "scripts").resolve()
        if not script.is_absolute() or script.resolve().parent != scripts:
            return None, "script_not_in_source_scripts_directory"
        if not script.is_file() or not executable.is_file():
            return None, "reviewed_script_or_python_missing"
        source_hash = _digest(script.read_bytes())
    except OSError:
        return None, "reviewed_script_or_python_unreadable"
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 120:
        return None, "invalid_or_excessive_timeout"
    return {
        "name": name,
        "argv": [str(executable.resolve()), str(script.resolve()), *argv[2:]],
        "timeout": timeout,
        "source_sha256": source_hash,
    }, "reviewed_local_stop_command"


def _render_bridge(jobs: list[dict]) -> bytes:
    source = '''# Managed by claude-codex-profile-sync-stop-hooks; see manifest.json.
"""Run reviewed hooks and emit only fixed, non-blocking Codex JSON statuses."""
import hashlib
import json
from pathlib import Path
import subprocess

JOBS = __JOBS__


def main():
    failed = []
    for job in JOBS:
        try:
            # A source edit requires a fresh sync before it can execute here.
            if hashlib.sha256(Path(job["argv"][1]).read_bytes()).hexdigest() != job["source_sha256"]:
                failed.append(job["name"] + ": failed")
                continue
            result = subprocess.run(
                job["argv"], shell=False, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=job["timeout"], check=False,
            )
            # The export guard signals its warning through stderr with exit 0.
            if result.returncode != 0 or result.stderr:
                failed.append(job["name"] + ": failed")
        except Exception:
            # Exceptions, stdout and stderr can contain browser credentials.
            failed.append(job["name"] + ": failed")
    output = {"systemMessage": "; ".join(failed), "suppressOutput": True} if failed else {}
    print(json.dumps(output, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''
    return source.replace("__JOBS__", repr(jobs)).encode("utf-8")


def _handler(bridge: Path, jobs: list[dict]) -> dict:
    argv = [sys.executable, str(bridge)]
    # This launcher works whether the enclosing Windows hook shell is cmd or
    # PowerShell. Neither shell receives the original Claude command string.
    powershell = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)
    encoded = base64.b64encode(powershell.encode("utf-16-le")).decode("ascii")
    return {"hooks": [{
        "type": "command",
        "command": shlex.join(argv),
        "commandWindows": "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded,
        "timeout": sum(job["timeout"] for job in jobs) + 5,
    }]}


def plan_hooks(claude_home: Path, codex_home: Path) -> tuple[dict[Path, bytes | None], dict]:
    """Return artifacts (`None` retires a file) without executing hooks."""
    claude_home, codex_home = Path(claude_home).resolve(), Path(codex_home).resolve()
    report = {"hooks": [], "warnings": [], "changed": False, "registered": 0, "requires_hooks_feature": False}
    settings_path = claude_home / "settings.json"
    try:
        settings = _read_object(settings_path) if settings_path.exists() else {}
    except (OSError, UnicodeError, ValueError):
        report["warnings"].append({"reason": "invalid_or_unreadable_claude_settings"})
        return {}, report
    source_hooks = settings.get("hooks", {})
    if not isinstance(source_hooks, dict):
        report["warnings"].append({"reason": "invalid_claude_hooks_object"})
        return {}, report
    jobs, seen = [], set()
    unavailable = False
    for event, groups in source_hooks.items():
        if not isinstance(groups, list):
            unavailable = unavailable or event == "Stop"
            report["warnings"].append({"event": event, "reason": "invalid_claude_hook_groups"})
            continue
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(entries, list):
                unavailable = unavailable or event == "Stop"
                report["warnings"].append({"event": event, "reason": "invalid_claude_hook_entries"})
                continue
            for entry in entries:
                row = {"event": event, "status": "unsupported"}
                report["hooks"].append(row)
                if event != "Stop":
                    row["reason"] = {
                        "SessionStart": "claude_plugin_cache_mutation_and_pruning_not_portable",
                        "PostToolUse": "claude_file_path_payload_and_document_globs_require_porting",
                    }.get(event, "event_not_reviewed")
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    row["reason"] = "only_reviewed_command_hooks_supported"
                    continue
                if group.get("matcher", "*") not in (None, "", "*") or entry.get("async"):
                    row["reason"] = "custom_matcher_or_async_semantics_not_ported"
                    continue
                job, reason = _approved_job(entry.get("command"), claude_home, entry.get("timeout", 30))
                row["reason"] = reason
                if job is None:
                    if reason in {"python_executable_missing", "reviewed_script_or_python_missing", "reviewed_script_or_python_unreadable"}:
                        row["status"] = "unavailable"
                        unavailable = True
                    continue
                row["script"] = job["name"]
                if job["name"] in seen:
                    row.update(status="skipped", reason="duplicate_source_hook")
                    continue
                seen.add(job["name"])
                row["status"] = "pending"
                jobs.append(job)
    if unavailable:
        report["warnings"].append({"status": "unavailable", "reason": "declared_hook_source_unavailable; managed_wrapper_preserved"})
        return {}, report

    hooks_path = codex_home / "hooks.json"
    folder = codex_home / "imports" / "claude-hooks"
    bridge_path, manifest_path = folder / "stop_bridge.py", folder / "manifest.json"
    if not jobs and not manifest_path.exists():
        return {}, report

    def conflict(reason: str):
        report["warnings"].append({"reason": reason})
        if not jobs:
            report["hooks"].append({"event": "Stop", "status": "conflict", "reason": reason})
        for row in report["hooks"]:
            if row["status"] == "pending":
                row.update(status="conflict", reason=reason)
        return {}, report

    try:
        document = _read_object(hooks_path) if hooks_path.exists() else {}
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict) or any(not isinstance(value, list) for value in hooks.values()):
            return conflict("existing_codex_hooks_shape_unsupported")
        stops = hooks.get("Stop", [])
        if any(not isinstance(group, dict) or not isinstance(group.get("hooks"), list) for group in stops):
            return conflict("existing_codex_stop_shape_unsupported")
        manifest = _read_object(manifest_path) if manifest_path.exists() else None
    except (OSError, UnicodeError, ValueError):
        return conflict("existing_hook_artifacts_invalid_or_unreadable")
    index = None
    if manifest is not None:
        if manifest.get("owner") != _OWNER or manifest.get("version") != 1:
            return conflict("existing_manifest_not_owned")
        manifest_body = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
        if ("manifest_sha256" in manifest and _fingerprint(manifest_body) != manifest["manifest_sha256"]) or set(manifest_body) - {"owner", "version", "bridge_sha256", "handler_sha256", "source", "jobs"}:
            return conflict("managed_manifest_modified_by_user")
        try:
            if _digest(bridge_path.read_bytes()) != manifest.get("bridge_sha256"):
                return conflict("managed_bridge_modified_by_user")
        except OSError:
            return conflict("managed_bridge_missing_or_unreadable")
        indexes = [i for i, group in enumerate(stops) if _fingerprint(group) == manifest.get("handler_sha256")]
        if len(indexes) != 1:
            return conflict("managed_handler_modified_removed_or_duplicated")
        index = indexes[0]
    elif bridge_path.exists():
        return conflict("existing_bridge_not_owned")

    if not jobs:
        merged = copy.deepcopy(document)
        del merged["hooks"]["Stop"][index]
        report["hooks"].append({"event": "Stop", "status": "retired", "reason": "source_commands_removed"})
        report["changed"] = True
        return {hooks_path: _json_bytes(merged), bridge_path: None, manifest_path: None}, report

    bridge = _render_bridge(jobs)
    handler = _handler(bridge_path, jobs)
    merged = copy.deepcopy(document)
    merged_stops = merged.setdefault("hooks", {}).setdefault("Stop", [])
    if index is None:
        if any(group == handler for group in merged_stops):
            return conflict("existing_handler_not_owned")
        merged_stops.append(handler)
    else:
        merged_stops[index] = handler
    new_manifest = {
        "owner": _OWNER, "version": 1,
        "bridge_sha256": _digest(bridge), "handler_sha256": _fingerprint(handler),
        "source": str(settings_path),
        "jobs": [{"source": job["argv"][1], "source_sha256": job["source_sha256"]} for job in jobs],
    }
    new_manifest["manifest_sha256"] = _fingerprint(new_manifest)
    proposed = {bridge_path: bridge, manifest_path: _json_bytes(new_manifest)}
    if merged != document:
        proposed[hooks_path] = _json_bytes(merged)
    outputs = {}
    try:
        for path, content in proposed.items():
            if not path.exists() or path.read_bytes() != content:
                outputs[path] = content
    except OSError:
        return conflict("existing_hook_artifacts_unreadable")
    for row in report["hooks"]:
        if row["status"] == "pending":
            row["status"] = "added" if index is None else "updated" if outputs else "unchanged"
    report.update(changed=bool(outputs), registered=len(jobs), requires_hooks_feature=True)
    return outputs, report
