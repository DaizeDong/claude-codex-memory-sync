"""Plan reviewed native hooks without executing scripts or writing profiles.

One bridge and versioned ownership manifest cover all reviewed events. Native
or user-edited handlers remain outside the planner's ownership.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
from profile_config import _plain_member
from profile_bridge import ownership
from profile_bridge import hook_runtime


_OWNER = "claude-codex-profile-sync-stop-hooks"
_APPROVED = {"pw-auth.py": ["absorb"], "pw_export_guard.py": []}
_ENTRYPOINTS = {
    "SessionStart": {"pw_isolation_guard.py": []},
    "PostToolUse": {"doc-budget/doc_budget.py": []},
}
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


def _approved_job(command: str, source_home: Path, timeout: object, *, event="Stop") -> tuple[dict | None, str]:
    try:
        argv = _windows_split(command)
    except ValueError:
        return None, "command_is_not_simple_windows_argv"
    if len(argv) not in (2, 3):
        return None, "command_not_reviewed"
    script = Path(argv[1])
    name = script.name
    approved = _APPROVED if event == "Stop" else _ENTRYPOINTS.get(event, {})
    relative = next((key for key in approved if Path(key).name == name), None)
    if relative is None or argv[2:] != approved[relative]:
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
        if not script.is_absolute() or script.resolve() != (scripts / relative).resolve():
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


def plan_entrypoint_checks(claude_home: Path, codex_home: Path, plugin_roots=()) -> list[dict]:
    """Describe explicit checks; these are NOT registrations in hooks.json.

    The same sources are also used by plan_hooks for native event registration.
    No source script, model, browser, or credential store is run/read here.
    """
    claude_home, codex_home = Path(claude_home).resolve(), Path(codex_home).resolve()
    try:
        settings = _read_object(claude_home / "settings.json")
        if settings.get("disableAllHooks"):
            return []
        hooks = settings.get("hooks", {})
    except (OSError, ValueError, UnicodeError):
        return [{"status": "unavailable", "reason": "source_settings_unreadable"}]
    if not isinstance(hooks, dict):
        return [{"status": "unavailable", "reason": "source_hooks_invalid"}]
    checks = []
    for event in _ENTRYPOINTS:
        groups = hooks.get(event, [])
        if not isinstance(groups, list):
            checks.append({"event": event, "status": "unavailable", "reason": "source_hook_groups_invalid"})
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                checks.append({"event": event, "status": "unavailable", "reason": "source_hook_entries_invalid"})
                continue
            allowed_matchers = (None, "", "*", "Edit|Write") if event == "PostToolUse" else (None, "", "*", "startup|clear|compact")
            if group.get("matcher") not in allowed_matchers:
                continue
            for entry in group["hooks"]:
                if not isinstance(entry, dict) or entry.get("type") != "command" or entry.get("async") or entry.get("disabled"):
                    continue
                job, reason = _approved_job(entry.get("command"), claude_home, entry.get("timeout", 30), event=event)
                if job is None:
                    unavailable = reason in {"python_executable_missing", "reviewed_script_or_python_missing",
                                             "reviewed_script_or_python_unreadable", "invalid_or_excessive_timeout"}
                    checks.append({"event": event, "status": "unavailable" if unavailable else "unsupported", "reason": reason})
                    continue
                job.update(event=event, status="entrypoint_available", registered=False,
                           sources=["startup", "clear", "compact"] if group.get("matcher") == "startup|clear|compact" else None)
                if event == "SessionStart":
                    source = Path(job["argv"][1]).read_bytes()
                    if b"--check-codex-config" not in source:
                        job.update(status="unavailable", reason="source_guard_check_mode_missing")
                    if b'"pw-auth.py"' in source or b"'pw-auth.py'" in source:
                        dependency = Path(job["argv"][1]).with_name("pw-auth.py")
                        try:
                            job["dependencies"] = [{"source": str(dependency), "source_sha256": _digest(dependency.read_bytes())}]
                        except OSError:
                            job.update(status="unavailable", reason="source_guard_dependency_missing")
                    job["argv"].extend(["--check-codex-config", str(codex_home / "config.toml")])
                    job["use"] = "read_only_no_pruning; SessionStart_not_guaranteed_before_mcp_bootstrap"
                else:
                    job["use"] = "after_explicit_file_edit; source_claude_document_globs_only"
                    job["input"] = "tool_input.file_path supplied by caller"
                checks.append(job)
    for raw_root in plugin_roots:
        root = Path(raw_root).resolve()
        try:
            manifest = _read_object(root / ".claude-plugin/plugin.json")
            if manifest.get("name") != "superpowers":
                continue
        except (OSError, ValueError, UnicodeError):
            continue
        enabled = settings.get("enabledPlugins", {})
        if isinstance(enabled, dict) and any(key.split("@")[0] == "superpowers" and value is False for key, value in enabled.items()):
            continue
        try:
            skill = root / "skills/using-superpowers/SKILL.md"
            data = skill.read_bytes()
        except (OSError, ValueError, UnicodeError):
            checks.append({"event": "SessionStart", "status": "unavailable", "reason": "plugin_entrypoint_unreadable"})
            continue
        checks.append({"event": "SessionStart", "name": "superpowers", "status": "instruction_entrypoint_available",
                       "registered": False, "source": str(skill), "source_sha256": _digest(data),
                       "use": "load_existing_skill_via_instruction_entrypoint; llmcall_platform_adaptation"})
    enabled = settings.get("enabledPlugins", {})
    if (isinstance(enabled, dict) and
            any(key.split("@")[0] == "superpowers" and value is True for key, value in enabled.items()) and
            not any(check.get("name") == "superpowers" for check in checks)):
        checks.append({"event": "SessionStart", "name": "superpowers", "status": "unavailable",
                       "reason": "enabled_superpowers_entrypoint_missing"})
    return checks


def run_entrypoint_check(claude_home: Path, codex_home: Path, name: str, *, file_path: Path | None = None) -> dict:
    """Run one reviewed source check with explicit inputs and fixed output only.

    This API never prepares state or starts a browser. The document reporter
    retains its source watermark behavior. A launcher must honor failed checks.
    """
    jobs = [job for job in plan_entrypoint_checks(claude_home, codex_home)
            if job.get("name") == name and job.get("status") == "entrypoint_available"]
    if len(jobs) != 1:
        return {"status": "unavailable", "reason": "reviewed_entrypoint_not_unique_or_available"}
    job = jobs[0]
    payload = None
    if job["event"] == "PostToolUse":
        if file_path is None or not Path(file_path).is_absolute():
            return {"status": "unavailable", "reason": "explicit_absolute_file_path_required"}
        payload = json.dumps({"tool_input": {"file_path": str(file_path)}}).encode("utf-8")
    try:
        if _digest(Path(job["argv"][1]).read_bytes()) != job["source_sha256"]:
            return {"status": "unavailable", "reason": "source_changed"}
        result = hook_runtime.run_source(job, payload if payload is not None else b"")
    except Exception:
        return {"status": "failed", "reason": "reviewed_entrypoint_failed"}
    if result.returncode or result.stderr:
        return {"status": "failed" if job["event"] == "SessionStart" else "warning",
                "reason": "browser_policy_or_saved_union_invalid" if job["event"] == "SessionStart" else "source_document_budget_feedback"}
    return {"status": "passed", "reason": "reviewed_entrypoint_completed"}


def _render_bridge(jobs: list[dict]) -> bytes:
    # Ship a self-contained bridge; no import from a moving active generation.
    source = Path(hook_runtime.__file__).read_text(encoding="utf-8")
    return (source + "\nJOBS = " + repr(jobs) +
            "\nif __name__ == '__main__':\n    raise SystemExit(main(JOBS))\n").encode("utf-8")


def _handler(bridge: Path, jobs: list[dict], event="Stop") -> dict:
    argv = [str(Path(sys.executable).resolve()), str(bridge), event]
    # This launcher works whether the enclosing Windows hook shell is cmd or
    # PowerShell. Neither shell receives the original Claude command string.
    powershell = "& " + " ".join("'" + value.replace("'", "''") + "'" for value in argv)
    encoded = base64.b64encode(powershell.encode("utf-16-le")).decode("ascii")
    # Pin the native Windows shell too, independent of a later session's PATH.
    launcher = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe") if os.name == "nt" else "powershell.exe"
    return {"hooks": [{
        "type": "command",
        "command": shlex.join(argv),
        "commandWindows": subprocess.list2cmdline([launcher, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded]),
        # hooks.json uses `timeout`; app-server DTOs report it as timeoutSec.
        # Exact 0.154.0 hooks/list ignores timeoutSec in the input file.
        "timeout": math.ceil(sum(job["timeout"] for job in jobs)) + 5,
    }]}


def plan_hooks(claude_home: Path, codex_home: Path, *, plugin_roots=()) -> tuple[dict[Path, bytes | None], dict]:
    """Return artifacts (`None` retires a file) without executing hooks."""
    claude_home, codex_home = Path(claude_home).resolve(), Path(codex_home).resolve()
    report = {"hooks": [], "warnings": [], "changed": False, "registered": 0, "requires_hooks_feature": False}
    report["protocol_boundaries"] = [
        "hooks.json timeout is reported as timeoutSec by app-server; timeoutSec in the input file is ignored in 0.154.0",
        "SessionStart is not guaranteed before MCP bootstrap; native trust and a new session remain runtime requirements",
        "PostToolUse follows core success gating; apply_patch tool_response is a string, and local file destinations come from tool_input.command",
        "shell-intercepted patches, remote environment selection and other edit tools are not adapted",
    ]
    report["entrypoint_checks"] = plan_entrypoint_checks(claude_home, codex_home, plugin_roots)
    settings_path = claude_home / "settings.json"
    try:
        settings = _read_object(settings_path) if settings_path.exists() else {}
    except (OSError, UnicodeError, ValueError):
        report["warnings"].append({"reason": "invalid_or_unreadable_claude_settings"})
        return {}, report
    source_hooks = {} if settings.get("disableAllHooks") else settings.get("hooks", {})
    if not isinstance(source_hooks, dict):
        report["warnings"].append({"reason": "invalid_claude_hooks_object"})
        return {}, report
    jobs, seen = [], set()
    unavailable = False
    for event, groups in source_hooks.items():
        if not isinstance(groups, list):
            unavailable = unavailable or event in {"Stop", *_ENTRYPOINTS}
            report["warnings"].append({"event": event, "reason": "invalid_claude_hook_groups"})
            continue
        for group in groups:
            entries = group.get("hooks", []) if isinstance(group, dict) else []
            if not isinstance(entries, list):
                unavailable = unavailable or event in {"Stop", *_ENTRYPOINTS}
                report["warnings"].append({"event": event, "reason": "invalid_claude_hook_entries"})
                continue
            for entry in entries:
                row = {"event": event, "status": "unsupported"}
                report["hooks"].append(row)
                if event != "Stop":
                    row["reason"] = {
                        "SessionStart": "source_command_not_reviewed_for_native_adapter",
                        "PostToolUse": "source_command_not_reviewed_for_native_adapter",
                    }.get(event, "event_not_reviewed")
                    continue
                if not isinstance(entry, dict) or entry.get("type") != "command":
                    row["reason"] = "only_reviewed_command_hooks_supported"
                    continue
                if group.get("matcher", "*") not in (None, "", "*") or entry.get("async") or entry.get("disabled"):
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
                job["event"] = "Stop"
                jobs.append(job)
    # Absorb must precede the export reminder even if source groups were reordered.
    jobs.sort(key=lambda job: 0 if job["name"] == "pw-auth.py" else 1)
    for check in report["entrypoint_checks"]:
        status = check.get("status")
        if status == "unavailable":
            unavailable = True
            continue
        if status not in {"entrypoint_available", "instruction_entrypoint_available"}:
            continue
        key = (check["event"], check["name"])
        if key in seen:
            existing = next(job for job in jobs if (job["event"], job["name"]) == key)
            if existing.get("sources") is None or check.get("sources") is None:
                existing["sources"] = None
            else:
                existing["sources"] = sorted(set(existing["sources"]) | set(check["sources"]))
            continue
        seen.add(key)
        if check["name"] == "superpowers":
            job = {"name": "superpowers", "event": "SessionStart", "source": check["source"],
                   "source_sha256": check["source_sha256"], "timeout": 1}
        else:
            job = {k: check[k] for k in ("name", "event", "argv", "timeout", "source_sha256", "sources")}
            if "dependencies" in check:
                job["dependencies"] = check["dependencies"]
        jobs.append(job)
        report["hooks"].append({"event": check["event"], "script": check["name"],
                                "status": "pending", "reason": "reviewed_native_event_adapter"})
    if unavailable:
        report["warnings"].append({"status": "unavailable", "reason": "declared_hook_source_unavailable; managed_wrapper_preserved"})
        return {}, report

    hooks_path = codex_home / "hooks.json"
    folder = codex_home / "imports" / "claude-hooks"
    bridge_path, manifest_path = folder / "stop_bridge.py", folder / "manifest.json"
    if not jobs and not manifest_path.exists() and not bridge_path.exists():
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
        hooks_bytes = _plain_member(hooks_path)
        document = json.loads(hooks_bytes) if hooks_bytes is not None else {}
        if not isinstance(document, dict):
            return conflict("existing_codex_hooks_shape_unsupported")
        hooks = document.get("hooks", {})
        if not isinstance(hooks, dict) or any(not isinstance(value, list) for value in hooks.values()):
            return conflict("existing_codex_hooks_shape_unsupported")
        stops = hooks.get("Stop", [])
        if any(not isinstance(group, dict) or not isinstance(group.get("hooks"), list) for group in stops):
            return conflict("existing_codex_stop_shape_unsupported")
        members = {}
        for path in (hooks_path, bridge_path, manifest_path):
            data = hooks_bytes if path == hooks_path else _plain_member(path)
            if data is not None:
                members[".codex/" + path.relative_to(codex_home).as_posix()] = data
        verification = ownership.verify_members(members, home=codex_home.parent)
        if verification["status"] != "verified":
            reasons = {item["reason"] for item in verification["conflicts"]}
            reason = ("managed_handler_modified_removed_or_duplicated"
                      if "hook_group_modified_missing_or_duplicate" in reasons else
                      "managed_hook_group_modified_by_user_or_incomplete")
            return conflict(reason)
    except (OSError, UnicodeError, ValueError):
        return conflict("existing_hook_artifacts_invalid_or_unreadable")
    groups = [group for group in verification["groups"] if group[2] == "hooks"]
    detail = groups[0][3] if groups else {}
    indexes = {"Stop": detail} if isinstance(detail, int) else detail
    if not jobs and not indexes:
        return {}, report

    merged = copy.deepcopy(document)
    merged_hooks = merged.setdefault("hooks", {})
    events = {job["event"] for job in jobs}
    for event, index in indexes.items():
        if event not in events:
            del merged_hooks[event][index]
            report["hooks"].append({"event": event, "status": "retired", "reason": "source_commands_removed"})
    if not jobs:
        report["changed"] = True
        return {hooks_path: _json_bytes(merged), bridge_path: None, manifest_path: None}, report

    bridge = _render_bridge(jobs)
    handlers = {}
    for event in ("Stop", "SessionStart", "PostToolUse"):
        event_jobs = [job for job in jobs if job["event"] == event]
        if not event_jobs:
            continue
        handler = _handler(bridge_path, event_jobs, event)
        if event == "PostToolUse":
            handler["matcher"] = "apply_patch"
        event_groups = merged_hooks.setdefault(event, [])
        if event not in indexes:
            if any(group == handler for group in event_groups):
                return conflict("existing_handler_not_owned")
            event_groups.append(handler)
        else:
            event_groups[indexes[event]] = handler
        handlers[event] = _fingerprint(handler)
    new_manifest = {
        "owner": _OWNER, "version": 2,
        "bridge_sha256": _digest(bridge), "handlers": handlers,
        "source": str(settings_path),
        "jobs": [{"source": job.get("source") or job["argv"][1],
                  "source_sha256": job["source_sha256"]} for job in jobs],
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
            row["status"] = "added" if row["event"] not in indexes else "updated" if outputs else "unchanged"
    report.update(changed=bool(outputs), registered=len(jobs), requires_hooks_feature=True)
    return outputs, report
