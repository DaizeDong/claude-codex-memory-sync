"""One scheduled run. No model calls, notifications, or provider selection."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tomllib

from profile_sync import (apply_plan, assert_plain_path, atomic_write, build_plan,
                          destination_lock, ensure_external)
from profile_health import assess, check_mcp


def run(claude, codex, skills, *, apply=False, write_status=False, probe=False):
    started = datetime.now(timezone.utc).isoformat()
    state = codex / "claude-sync"
    success = state / "last-success.json"
    for root in (codex, skills):
        ensure_external(root)
        assert_plain_path(root)
    if write_status:
        assert_plain_path(success)
    result = {"version": 1, "started_at": started, "mode": "apply" if apply else "check"}
    try:
        with destination_lock(codex):
            try:
                if write_status and success.exists():
                    success.unlink()
                changes, plan = build_plan(claude, codex, skills)
                if apply:
                    applied = apply_plan(changes, plan, codex, skills)
                    _, verified = build_plan(claude, codex, skills)
                else:
                    applied, verified = plan, plan
                config_file = codex / "config.toml"
                config = tomllib.loads(config_file.read_text(encoding="utf-8-sig")) if config_file.exists() else {}
                checks = check_mcp(config.get("mcp_servers", {}), probe=probe)
                result.update(assess(verified, checks))
                result.update(change_count=len(changes), remaining_changes=verified.get("change_count", 0),
                              backup=applied.get("backup"), report=verified,
                              exit_code=0 if result["status"] == "healthy" else 2)
            except Exception as exc:
                # Source parse exceptions may contain secrets. Store the type only.
                result.update(status="error", error_type=type(exc).__name__, exit_code=1)
            _publish(result, state, success, write_status)
    except Exception as exc:
        # Lock contention must not overwrite the active runner's status or marker.
        result.update(status="error", error_type=type(exc).__name__, exit_code=1)
        result["finished_at"] = datetime.now(timezone.utc).isoformat()
    return result


def _publish(result, state, success, enabled):
    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    if enabled:
        data = json.dumps(result, ensure_ascii=True, indent=2).encode("utf-8")
        inventory = result.get("report", {}).get("inventory")
        if inventory is not None:
            atomic_write(state / "skill-inventory.json", json.dumps(inventory, ensure_ascii=True, indent=2).encode())
        atomic_write(state / "last-run.json", data)
        if result["status"] == "healthy":
            atomic_write(success, json.dumps({key: result[key] for key in
                ("version", "status", "started_at", "finished_at", "remaining_changes")}).encode())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home()/".claude")))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home()/".codex")))
    parser.add_argument("--skills-home", type=Path, default=Path.home()/".agents/skills")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--write-status", action="store_true", help="Publish the task health artifacts in the Codex home")
    parser.add_argument("--probe-mcp", action="store_true", help="Initialize loopback HTTP MCP servers; no tool calls")
    args = parser.parse_args(argv)
    try:
        result = run(*(p.absolute() for p in (args.claude_home, args.codex_home, args.skills_home)),
                     apply=args.apply, write_status=args.write_status, probe=args.probe_mcp)
    except Exception as exc:
        result = {"status": "error", "error_type": type(exc).__name__, "exit_code": 1}
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result["exit_code"]


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
