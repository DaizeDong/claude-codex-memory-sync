"""Read-only source inventory and conservative lifecycle evidence.

All returned paths are profile data: callers persist reports only outside this
repository. No skill, installer, interpreter or dependency script is executed.
Recovery requires an exact previously owned source path in an approved source;
name matches are suggestions only. Missing provider mounts are not
proof of deletion. The caller owns publication, backups and opt-in recovery.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit, urlunsplit

import profile_catalog as sources


OWNER = "claude-codex-profile-sync"


def read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Expected JSON object")
    return value


def is_link(path: Path) -> bool:
    # lstat is essential: exists() is false for a broken Windows junction.
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def link_target(path: Path) -> str:
    """Keep the immediate target, including a missing or relative destination."""
    target = os.readlink(path)
    if target.startswith("\\\\?\\UNC\\"):
        return "\\\\" + target[8:]
    if target.startswith("\\\\?\\"):
        return target[4:]
    return target


def plugin_catalog(claude: Path, snapshot=None) -> dict[str, dict]:
    return sources.plugin_view(sources.get_snapshot(claude, snapshot))


def source_identity(source: Path, claude: Path, catalog: dict, *, root: Path | None = None, snapshot=None) -> dict:
    source = source.absolute()
    current = snapshot if snapshot is not None else sources.current_snapshot()
    match = sources.match_source(current, source) if current is not None else None
    provenance = sources.ownership_identity(*match) if match else {}
    for name, provider in catalog.items():
        for raw_root in provider["roots"]:
            provider_root = Path(raw_root)
            if source.is_relative_to(provider_root) or source.is_relative_to(provider_root.resolve()):
                return {**provenance, "source": str(source), "provider": "plugin", "plugin": name, "provider_root": str(provider_root)}
    if root is not None and root != claude:
        return {**provenance, "source": str(source), "provider": "plugin", "provider_root": str(root)}
    # Keep the source entry path, not just its resolved target. An intact broken
    # source junction means unavailable; removal of the entry means retirement.
    if source.is_relative_to(claude):
        relative = source.relative_to(claude)
        provider_root = claude / relative.parts[0] if len(relative.parts) > 1 else claude
        return {**provenance, "source": str(source), "provider": "user", "provider_root": str(provider_root)}
    return {**provenance, "source": str(source), "provider": "unknown"}


def source_disposition(identity: dict, catalog: dict, *, absent: bool = False) -> tuple[str, str]:
    """Classify observed absence; only `retired` authorizes intact-item removal."""
    source = Path(identity["source"])
    root = Path(identity["provider_root"]) if identity.get("provider_root") else None
    if identity.get("provider") == "plugin":
        provider = catalog.get(identity.get("plugin"))
        if provider:
            if provider["status"] == "disabled":
                return "retired", "disabled_in_claude"
            if provider["status"] != "available":
                return "unavailable", provider["reason"]
        elif identity.get("plugin"):
            return "unavailable", "plugin_install_metadata_unavailable"
        if root is None or not root.is_dir():
            return "unavailable", "plugin_install_unavailable"
    elif identity.get("provider") != "user":
        return "unavailable", "source_provider_unknown"
    if root is None or not root.is_dir():
        return "unavailable", "source_root_unavailable"
    # Do not interpret a missing mount behind any source link as a deletion.
    for part in (source, *source.parents):
        if is_link(part) and not part.exists():
            return "unavailable", "source_link_unavailable"
        if part == root:
            break
    if not source.exists() or absent:
        return "retired", "source_removed"
    return "present", "source_present"


def skill_entries(root: Path, depth: int = 0, seen: set | None = None, *, snapshot=None):
    """Legacy bounded walk projected from catalog entrypoints and mounts."""
    if snapshot is None:
        snapshot = sources.discover_snapshot({"skill_roots": [{"path": str(root),
            "include_root": True, "max_depth": max(1, min(16, 8 - depth))}]})
    yield from sources.skill_paths(snapshot, root, include_missing=True)


def declared_name(path: Path, *, snapshot=None) -> str | None:
    if snapshot is None:
        snapshot = sources.discover_snapshot({"skill_roots": [{"path": str(path), "include_root": True}]})
    match = sources.match_source(snapshot, path)
    return match[1].get("declared_name") if match else None


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True,
                                timeout=5, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def _safe_remote(value: str | None) -> str | None:
    if not value:
        return None
    if "://" in value:
        try:
            parsed = urlsplit(value)
            return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))
        except ValueError:
            return None
    return value if re.fullmatch(r"(?:git@)?[\w.-]+:[\w./-]+", value) else None


def repository_info(path: Path, cache: dict) -> dict:
    resolved = path.resolve()
    root = next((p for p in (resolved, *resolved.parents) if (p / ".git").exists()), None)
    if root is None:
        return {"status": "unavailable", "reason": "not_a_local_git_checkout"}
    if root not in cache:
        top = _git(root, "rev-parse", "--show-toplevel")
        if not top:
            cache[root] = {"status": "unavailable", "reason": "git_metadata_unreadable"}
        else:
            cache[root] = {"status": "available", "root": str(Path(top)),
                           "remote": _safe_remote(_git(root, "remote", "get-url", "origin")),
                           "version": _git(root, "rev-parse", "--verify", "HEAD")}
    result = dict(cache[root])
    if result.get("root") and resolved.is_relative_to(Path(result["root"])):
        result["relative_path"] = resolved.relative_to(Path(result["root"])).as_posix()
    return result


def dependencies(path: Path) -> dict:
    declarations, executables = [], set()
    for name in ("requirements.txt", "pyproject.toml", "package.json", "environment.yml", "Pipfile"):
        declaration = path / name
        if declaration.is_file():
            declarations.append({"path": str(declaration), "kind": name})
            executables.add("node" if name == "package.json" else "python")
    try:
        for script in (path / "scripts").glob("*"):
            interpreter = {".py": "python", ".js": "node", ".mjs": "node", ".sh": "bash", ".ps1": "powershell"}.get(script.suffix)
            if interpreter:
                executables.add(interpreter)
    except (OSError, UnicodeError):
        pass
    return {"check": "declarations_and_executables_only", "declarations": declarations,
            "executables": [{"name": name, "available": shutil.which(name) is not None} for name in sorted(executables)]}


def approved_roots(claude: Path, explicit, manifest: Path | None, warnings: list, *, snapshot=None) -> list[Path]:
    snapshot = sources.get_snapshot(claude, snapshot, approved_repos=explicit, external_manifest=manifest)
    roots = {Path(path).resolve() for path in explicit}
    cache = {}
    for record in snapshot["records"]:
        if record["origin"]["type"] == "checkout" and record.get("path"):
            repository = repository_info(Path(record["path"]), cache)
            if repository.get("root"):
                roots.add(Path(repository["root"]))
    for problem in snapshot["problems"]:
        if problem["stage"] == "external_skill_repos" and manifest is not None:
            warnings.append({"status": "unavailable", "reason": "external_skill_manifest_invalid",
                             "source": str(manifest), "catalog_problem": problem})
    return sorted(roots)


def inventory_sources(claude: Path, codex: Path, skills: Path, *, approved_repos=(), external_manifest: Path | None = None, snapshot=None) -> dict:
    snapshot = sources.get_snapshot(claude, snapshot, codex=codex, skills=skills,
                                   approved_repos=approved_repos, external_manifest=external_manifest)
    report = {"version": 1, "skills": [], "warnings": [], "approved_repos": [], "catalog": snapshot}
    legacy = read_object(codex / "claude-sync/managed-skills.json")
    owned = read_object(codex / "claude-sync/managed-artifacts.json").get("items", {})
    cache, candidates = {}, {}
    roots = approved_roots(claude, approved_repos, external_manifest, report["warnings"], snapshot=snapshot)
    for root in roots:
        repo = repository_info(root, cache)
        if repo.get("root") != str(root):
            report["warnings"].append({"source": str(root), "status": "unavailable", "reason": "approved_repository_unavailable"})
            continue
        report["approved_repos"].append(str(root))
    for record, entry in sources.entries(snapshot):
        path = Path(entry["path"]).parent
        repository = repository_info(path, cache)
        approved = repository.get("root") in report["approved_repos"]
        declared = record["status"]["declared"] == "yes"
        if (not approved and not declared) or record["status"]["resolved"] != "yes":
            continue
        if approved and not path.resolve().is_relative_to(Path(repository["root"])):
            continue
        candidate = {"source": str(path.resolve()), "repository": repository,
                     "reason": "catalog_source_candidate", **sources.projection(record, entry)}
        for name in set(record["aliases"] + [entry.get("declared_name")]):
            if name:
                candidates.setdefault(name, {})[str(path.resolve())] = candidate
    locations = [("agents", skills), ("legacy_codex", codex / "skills"), ("claude", claude / "skills")]
    for name, provider in plugin_catalog(claude, snapshot).items():
        if provider["status"] == "available":
            locations.extend(("plugin:" + name, Path(root)) for root in provider["roots"])
    for location, root in locations:
        for entry in sources.skill_paths(snapshot, root, include_missing=True):
            match = sources.match_source(snapshot, entry)
            if match:
                source_record, ep = match
            else:
                source_record = next((r for r in snapshot["records"] if any(m["path"] == str(entry) for m in r["mounts"])), None)
                ep = None
            if source_record is None:
                continue
            broken = is_link(entry) and not entry.exists()
            name = ep.get("declared_name") if ep else None
            record = owned.get(str(entry), owned.get(str(entry / "SKILL.md"), {}))
            original_source = Path(record["source"]) if record.get("owner") == OWNER and record.get("source") else entry.resolve()
            row = {"name": name or entry.name, "declared_name": name, "path": str(entry), "location": location,
                   "ownership": "managed" if str(entry) in legacy or record.get("owner") == OWNER else "unmanaged",
                   "source": str(original_source), "broken": broken,
                   "status": "broken" if broken else "available" if ep else "unavailable",
                   "reason": "broken_skill_link" if broken else "skill_entry_present" if ep else "source_unavailable",
                   "repository": repository_info(original_source, cache) if ep else {"status": "unavailable", "reason": "catalog_source_unresolved"},
                   "dependencies": dependencies(entry) if ep else {"check": "not_checked", "declarations": [], "executables": []},
                   **sources.projection(source_record, ep)}
            if record.get("owner") == OWNER:
                row["provenance"] = {key: record[key] for key in ("provider", "plugin", "provider_root", "source_sha256", "original") if key in record}
            if is_link(entry):
                row["link_target"] = link_target(entry)
            if broken:
                options = list(candidates.get(entry.name, {}).values())
                expected = record.get("source") if record.get("owner") == OWNER else legacy.get(str(entry))
                expected = Path(expected) if isinstance(expected, str) else None
                if expected and expected.name == "SKILL.md":
                    expected = expected.parent
                exact = [item for item in options if expected and Path(item["source"]).resolve() == expected.resolve()]
                # A name match is a suggestion, even for the old installation layout.
                recoverable = len(exact) == 1 and exact[0]["catalog_record"]["ownership"] != "external-installer"
                status = "recoverable" if recoverable else "ambiguous" if len(options) > 1 else "unrecoverable"
                row["recovery"] = {"status": status, "reason": "exact_owned_source" if recoverable else "source_identity_unproven",
                                   "candidates": exact if recoverable else options}
                if status == "ambiguous":
                    row.update(status="ambiguous", reason="multiple_authoritative_local_sources")
            report["skills"].append(row)
    return report
