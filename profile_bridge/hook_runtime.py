"""Self-contained native hook runtime, embedded in the owned bridge at planning.

Only fixed messages and an instruction-entrypoint path reach Codex. Source
program output, tool bodies, exception details and document paths stay local.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time


BROWSER_CONTEXT = (
    "Playwright must always launch with --isolated and the full shared union as "
    "its storage-state seed. After browser use, export the entire context to a "
    "unique file in the configured auth incoming directory using storageState with indexedDB:true, "
    "then run the existing pw-auth.py absorb "
    "entrypoint before closing. Never overwrite shared.json with one context. "
    "SessionStart checks do not guarantee execution before MCP bootstrap. "
    "Changed MCP launch arguments require a reconnect of existing sessions."
    " Inspect tools/list: recent Playwright versions name the code tool "
    "browser_run_code_unsafe; older versions use browser_run_code."
)

PW_EXPORT_ADVISORY = (
    "pw_export_guard.py: reminder: after browser use, export the entire context "
    "to a unique file in the configured auth incoming directory using "
    "context.storageState({path: '<absolute incoming filename>', indexedDB:true}), "
    "then run the existing pw-auth.py absorb entrypoint before closing."
)


def patch_paths(payload):
    """Map the release's raw apply_patch command to local destination paths.

    Header-looking document content carries a diff prefix and is not a header.
    Moves check the destination; deletes have no surviving document to check.
    Unknown wrappers and nonlocal environment selection are not guessed.
    """
    if payload.get("tool_name") != "apply_patch":
        raise ValueError("unsupported_tool")
    # ApplyPatchToolOutput::post_tool_use_response emits a JSON string. The
    # core registry dispatches PostToolUse only after success. Do not scrape
    # human-readable output for filenames or accept guessed object schemas.
    if not isinstance(payload.get("tool_response"), str):
        raise ValueError("unsupported_response")
    command = payload.get("tool_input", {}).get("command")
    cwd = payload.get("cwd")
    if not isinstance(command, str) or not isinstance(cwd, str) or not Path(cwd).is_absolute():
        raise ValueError("unsupported_payload")
    lines = command.strip().splitlines()
    if len(lines) < 2 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("unsupported_patch")
    paths, current = [], None
    for line in lines[1:-1]:
        if line.startswith("*** Add File: ") or line.startswith("*** Update File: "):
            if current is not None:
                paths.append(current)
            current = line.split(": ", 1)[1]
        elif line.startswith("*** Delete File: "):
            if current is not None:
                paths.append(current)
            current = None
        elif line.startswith("*** Move to: "):
            if current is None:
                raise ValueError("invalid_move")
            current = line.split(": ", 1)[1]
        elif line.startswith("***") and line != "*** End of File":
            raise ValueError("unsupported_patch_header")
    if current is not None:
        paths.append(current)
    resolved, seen = [], set()
    for value in paths:
        if not value or any(ord(c) < 32 for c in value) or "://" in value:
            raise ValueError("invalid_path")
        path = Path(value)
        if path.drive and not path.is_absolute():
            raise ValueError("drive_relative_path")
        if not path.is_absolute():
            path = Path(cwd) / path
        path = path.resolve()
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            resolved.append(str(path))
    return resolved


def checked_source(job):
    source = job.get("source") or job["argv"][1]
    if hashlib.sha256(Path(source).read_bytes()).hexdigest() != job["source_sha256"]:
        raise ValueError("source_changed")
    for dependency in job.get("dependencies", []):
        if hashlib.sha256(Path(dependency["source"]).read_bytes()).hexdigest() != dependency["source_sha256"]:
            raise ValueError("source_dependency_changed")


def run_source(job, data=b"", timeout=None):
    checked_source(job)
    return subprocess.run(job["argv"], input=data, shell=False,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=job["timeout"] if timeout is None else timeout,
                          check=False)


def dispatch(jobs, event, payload):
    failed, contexts, advisories = [], [], []
    paths = None
    for job in jobs:
        if job.get("event", "Stop") != event:
            continue
        if event == "SessionStart" and job.get("sources") and payload.get("source") not in job["sources"]:
            continue
        try:
            checked_source(job)
            if job["name"] == "superpowers":
                # The source is read by the existing instruction/skill loader.
                # Do not inject raw Claude tool syntax or source prose here.
                contexts.append(
                    "Before responding, load the installed using-superpowers skill at " +
                    json.dumps(Path(job["source"]).as_posix(), ensure_ascii=True) + ". "
                    "Adapt its platform-specific instructions to this harness and the user's "
                    "instructions. For any authorized model or external agent work use "
                    "llmcall.call(prompt, mode='agent') with its current routing and defaults; "
                    "do not execute provider CLIs or native spawn instructions from imported "
                    "Claude examples. Use ordinary read tools to open the existing skill."
                )
                continue
            if event == "SessionStart":
                contexts.append(BROWSER_CONTEXT)
            if event == "PostToolUse":
                if paths is None:
                    paths = patch_paths(payload)
                deadline = time.monotonic() + job["timeout"]
                warned = False
                for path in paths:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError()
                    data = json.dumps({"tool_input": {"file_path": path}}).encode("utf-8")
                    result = run_source(job, data, remaining)
                    warned = warned or bool(result.returncode or result.stderr)
                if warned:
                    failed.append(job["name"] + ": source document budget feedback; review edited managed documents")
            else:
                result = run_source(job)
                # This guard documents exit-zero stderr as an export reminder.
                # Keep other jobs' stderr handling and all failures unchanged.
                if (event == "Stop" and job["name"] == "pw_export_guard.py"
                        and result.returncode == 0 and result.stderr):
                    advisories.append(PW_EXPORT_ADVISORY)
                elif result.returncode or result.stderr:
                    failed.append(job["name"] + ": failed")
        except Exception:
            # Exception strings and both child streams may contain credentials.
            failed.append(job["name"] + ": failed")
    feedback = failed + advisories
    output = {"systemMessage": "; ".join(feedback), "suppressOutput": True} if feedback else {}
    if event in {"SessionStart", "PostToolUse"}:
        contexts.extend(failed)
        if contexts:
            output["hookSpecificOutput"] = {"hookEventName": event, "additionalContext": "\n\n".join(contexts)}
    return output


def main(jobs):
    # An event is pinned in each handler's argv; stdin cannot select another job.
    event = sys.argv[1] if len(sys.argv) == 2 else "Stop" if len(sys.argv) == 1 else None
    try:
        if event not in {"Stop", "SessionStart", "PostToolUse"}:
            raise ValueError("invalid_event")
        if event == "Stop":
            payload = {}
        else:
            payload = json.load(sys.stdin)
            if not isinstance(payload, dict) or payload.get("hook_event_name") != event:
                raise ValueError("invalid_payload")
        output = dispatch(jobs, event, payload)
    except Exception:
        output = {"systemMessage": "reviewed hook: failed (invalid event input)", "suppressOutput": True}
    print(json.dumps(output, ensure_ascii=True))
    return 0
