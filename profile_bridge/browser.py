"""Plan and apply only the native Playwright launch configuration."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import profile_catalog
import profile_config
import profile_sync
from .restore_interlock import require_ready


_HELPERS = (
    "pw-mcp-session.py", "pw-mcp-session.cjs", "pw-auth.py", "pw_storage_paths.py",
    "pw_isolation_guard.py",
)


def _capture(path):
    path = Path(path).absolute()
    resolved = path.resolve()
    before = profile_sync.snapshot(resolved)
    if before["kind"] not in {"file", "missing"}:
        raise ValueError("browser_source_is_not_a_regular_file")
    return {"path": str(path), "resolved": str(resolved), "before": before}


def _capture_paths(paths):
    return [_capture(path) for path in sorted(set(paths), key=str)]


def _verify(checks):
    profile_sync.verify_source({"source_checks": checks})


def _catalog_request(claude, codex, skills):
    request = profile_catalog.startup_request(claude, codex, skills)
    request["skill_roots"] = []
    request["workflow_roots"] = []
    request.pop("external_skill_repos", None)
    return request


def _selection(catalog):
    return {
        name: [provider["status"], provider["reason"],
               [[root, str(Path(root).resolve())] for root in provider["roots"]]]
        for name, provider in profile_catalog.plugin_view(catalog).items()
    }


def _base_inputs(claude):
    return {
        claude / "settings.json", claude / ".claude.json", claude.parent / ".claude.json",
        claude / "plugins/installed_plugins.json",
        claude / profile_config._PLAYWRIGHT_CONFIG,
        *(claude / "scripts" / name for name in _HELPERS),
    }


def _plugin_inputs(catalog):
    paths = set()
    for provider in profile_catalog.plugin_view(catalog).values():
        for raw_root in provider["roots"]:
            root = Path(raw_root).absolute()
            manifest_path = root / ".claude-plugin/plugin.json"
            paths.update((root / ".mcp.json", manifest_path))
            manifest_before = _capture(manifest_path)
            if manifest_before["before"]["kind"] == "missing":
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            except (UnicodeError, ValueError):
                # The config planner reports malformed input; retain its bytes
                # as a precondition even when it declares no usable references.
                manifest = None
            _verify([manifest_before])
            if not isinstance(manifest, dict):
                continue
            references = manifest.get("mcpServers")
            if isinstance(references, str):
                references = [references]
            if not isinstance(references, list):
                continue
            for reference in references:
                if not isinstance(reference, str):
                    continue
                expanded, missing = profile_config._expand(reference, root)
                if missing:
                    continue
                path = root / expanded
                if path.resolve().is_relative_to(root.resolve()):
                    paths.add(path.absolute())
    return paths


def verify_selection(check):
    """Recheck plugin availability without freezing unrelated plugin payloads."""
    if (not isinstance(check, dict) or set(check) != {"request", "selection", "plugin_inputs"} or
            not isinstance(check["request"], dict) or not isinstance(check["selection"], dict) or
            not isinstance(check["plugin_inputs"], list) or
            any(not isinstance(path, str) for path in check["plugin_inputs"]) or
            check["request"].get("skill_roots") != [] or
            check["request"].get("workflow_roots") != [] or
            "external_skill_repos" in check["request"]):
        raise ValueError("invalid_browser_selection_precondition")
    catalog = profile_catalog.discover_snapshot(check["request"])
    if (_selection(catalog) != check["selection"] or
            sorted(map(str, _plugin_inputs(catalog))) != check["plugin_inputs"]):
        raise ValueError("browser_source_selection_changed_during_planning")


def build_playwright_plan(claude, codex, skills, *, adopt_playwright=False):
    """Return one config change or no changes, without writing any profile state.

    Source checks include absent selection files and MCP references so an
    intervening installation or newly preferred source invalidates the plan.
    Browser storage contents are not inputs to this transaction.
    """
    claude, codex, skills = (Path(path).absolute() for path in (claude, codex, skills))
    for root in (codex, skills):
        profile_sync.ensure_external(root)
        profile_sync.assert_plain_path(root)
    require_ready(codex)
    if not claude.is_dir():
        raise ValueError("claude_configuration_directory_missing")
    path = codex / "config.toml"
    profile_sync.assert_plain_path(path)
    before = profile_sync.snapshot(path)
    if before["kind"] not in {"file", "missing"}:
        raise ValueError("browser_destination_is_not_a_regular_file")

    base_paths = _base_inputs(claude)
    base_checks = _capture_paths(base_paths)
    request = _catalog_request(claude, codex, skills)
    catalog = profile_catalog.discover_snapshot(request)
    initial_selection = _selection(catalog)
    _verify(base_checks)
    plugin_inputs = _plugin_inputs(catalog)
    input_paths = base_paths | plugin_inputs
    source_checks = _capture_paths(input_paths)
    _verify(base_checks)

    # Refresh selection after capturing plugin metadata; the planner uses this
    # exact snapshot instead of implicitly discovering an entire profile.
    planned_catalog = profile_catalog.discover_snapshot(request)
    planned_selection = _selection(planned_catalog)
    if planned_selection != initial_selection:
        raise ValueError("browser_source_selection_changed_during_planning")
    _verify(source_checks)
    plugins, _ = profile_sync.enabled_plugins(claude, planned_catalog)
    with profile_catalog.using_snapshot(planned_catalog):
        config, config_report = profile_config.plan_playwright_config(
            claude, codex, [root for _, root in plugins], adopt_playwright=adopt_playwright)

    dependencies = config_report.get("source_dependencies", [])
    _verify(config_report.get("source_checks", []))
    known = {str(Path(check["path"]).resolve()) for check in source_checks}
    if any(str(Path(raw).resolve()) not in known for raw in dependencies):
        raise ValueError("untracked_browser_source_dependency")
    _verify(source_checks)
    selection_check = {"request": request, "selection": planned_selection,
                       "plugin_inputs": sorted(map(str, plugin_inputs))}
    verify_selection(selection_check)
    _verify(source_checks)
    if profile_sync.snapshot(path) != before:
        raise ValueError("browser_destination_changed_during_planning")
    require_ready(codex)

    after = {"kind": "file", "sha256": profile_sync.digest(config)}
    blocked = (len(config_report["mcp"]) != 1 or
               any(item.get("status") not in {"updated", "unchanged"}
                   for item in config_report["mcp"]))
    changes = []
    if not blocked and before != after:
        changes.append({"path": str(path), "before": before, "after": after,
                        "data": config, "append_only": False, "source_checks": source_checks,
                        "browser_selection_check": selection_check})
    report = {
        "status": "blocked" if blocked else "preview" if changes else "no_changes",
        "scope": "native_playwright_launch",
        "change_count": len(changes), "config": config_report,
        "changes": [{key: value for key, value in row.items()
                     if key not in {"data", "source_checks", "browser_selection_check"}}
                    for row in changes],
    }
    return changes, report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", type=Path,
                        default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")))
    parser.add_argument("--codex-home", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--skills-home", type=Path, default=Path.home() / ".agents/skills")
    parser.add_argument("--apply", action="store_true", help="Apply only the Playwright config change")
    parser.add_argument("--adopt-playwright", action="store_true",
                        help="Adopt the reviewed native Playwright command and arguments")
    args = parser.parse_args(argv)
    claude, codex, skills = (path.absolute() for path in
                            (args.claude_home, args.codex_home, args.skills_home))
    try:
        if args.apply:
            with profile_sync.destination_lock(codex, skills):
                changes, report = build_playwright_plan(
                    claude, codex, skills, adopt_playwright=args.adopt_playwright)
                if report["status"] != "blocked":
                    report = profile_sync.apply_plan(changes, report, codex, skills)
        else:
            changes, report = build_playwright_plan(
                claude, codex, skills, adopt_playwright=args.adopt_playwright)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if (report["status"] == "blocked" or
                     report.get("cleanup_pending") or report.get("recovery_required")) else 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Parser exceptions can contain source values. Keep failures structural.
        print(json.dumps({"status": "error", "reason": "playwright_plan_or_apply_failed",
                          **profile_sync.cleanup_error_context(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
