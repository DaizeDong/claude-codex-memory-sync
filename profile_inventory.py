"""Read-only source inventory and conservative lifecycle evidence.

All returned paths are profile data: callers persist reports only outside this
repository. No skill, installer, interpreter or dependency script is executed.
Recovery trusts exact declared names in explicitly approved local git roots;
directory-name similarity is never evidence. Missing provider mounts are not
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


def plugin_catalog(claude: Path) -> dict[str, dict]:
    enabled = read_object(claude / "settings.json").get("enabledPlugins", {})
    installed = read_object(claude / "plugins/installed_plugins.json").get("plugins", {})
    if not isinstance(enabled, dict) or not isinstance(installed, dict):
        raise ValueError("Invalid plugin selection")
    result = {}
    for name in sorted(set(enabled) | set(installed)):
        raw = installed.get(name, [])
        entries = [x for x in raw if isinstance(x, dict) and x.get("scope", "user") == "user"] if isinstance(raw, list) else []
        roots = [str(Path(x["installPath"]).absolute()) for x in entries if isinstance(x.get("installPath"), str) and x["installPath"]]
        if enabled.get(name) is False:
            status, reason = "disabled", "disabled_in_claude"
        elif enabled.get(name) is not True:
            status, reason = "unavailable", "plugin_selection_unavailable"
        elif len(roots) != 1 or len(entries) != 1:
            status, reason = "unavailable", "missing_or_ambiguous_user_install"
        elif not Path(roots[0]).is_dir():
            status, reason = "unavailable", "plugin_install_unavailable"
        else:
            status, reason = "available", "enabled_user_install"
        result[name] = {"name": name, "roots": roots, "status": status, "reason": reason}
    return result


def source_identity(source: Path, claude: Path, catalog: dict, *, root: Path | None = None) -> dict:
    source = source.absolute()
    for name, provider in catalog.items():
        for raw_root in provider["roots"]:
            provider_root = Path(raw_root)
            if source.is_relative_to(provider_root) or source.is_relative_to(provider_root.resolve()):
                return {"source": str(source), "provider": "plugin", "plugin": name, "provider_root": str(provider_root)}
    if root is not None and root != claude:
        return {"source": str(source), "provider": "plugin", "provider_root": str(root)}
    # Keep the source entry path, not just its resolved target. An intact broken
    # source junction means unavailable; removal of the entry means retirement.
    if source.is_relative_to(claude):
        relative = source.relative_to(claude)
        provider_root = claude / relative.parts[0] if len(relative.parts) > 1 else claude
        return {"source": str(source), "provider": "user", "provider_root": str(provider_root)}
    return {"source": str(source), "provider": "unknown"}


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


def skill_entries(root: Path, depth: int = 0, seen: set | None = None):
    """Bounded entry-point walk, including broken links and legacy .system."""
    if is_link(root) and not root.exists():
        yield root
        return
    if not root.is_dir() or depth > 8:
        return
    seen = set() if seen is None else seen
    resolved = root.resolve()
    if resolved in seen:
        return
    seen.add(resolved)
    if (root / "SKILL.md").is_file():
        yield root
        return
    for child in sorted(root.iterdir()):
        if child.name in {"node_modules", "__pycache__", "archive", "venv", "dist", "build"} or (child.name.startswith(".") and child.name != ".system"):
            continue
        if is_link(child) and not child.exists():
            yield child
        elif child.is_dir():
            yield from skill_entries(child, depth + 1, seen.copy())


def declared_name(path: Path) -> str | None:
    try:
        text = (path / "SKILL.md").read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return None
    front = re.match(r"\A---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
    match = re.search(r"(?m)^name:\s*([^\r\n]+)", front[1]) if front else None
    if not match:
        return None
    name = match[1].strip().strip("\"'")
    return name if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]*", name) else None


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
    # Frontmatter declarations are hints, not installation commands. Never copy
    # arbitrary prose or dependency values (which may include authenticated URLs).
    try:
        text = (path / "SKILL.md").read_text(encoding="utf-8-sig")
        front = re.match(r"\A---\s*\n(.*?)\n---", text, re.S)
        for field in ("dependencies", "requires", "allowed-tools"):
            if front and re.search(rf"(?m)^\s*{field}:", front[1]):
                declarations.append({"path": str(path / "SKILL.md"), "kind": field})
        for script in (path / "scripts").glob("*"):
            interpreter = {".py": "python", ".js": "node", ".mjs": "node", ".sh": "bash", ".ps1": "powershell"}.get(script.suffix)
            if interpreter:
                executables.add(interpreter)
    except (OSError, UnicodeError):
        pass
    return {"check": "declarations_and_executables_only", "declarations": declarations,
            "executables": [{"name": name, "available": shutil.which(name) is not None} for name in sorted(executables)]}


def approved_roots(claude: Path, explicit, manifest: Path | None, warnings: list) -> list[Path]:
    roots = [Path(path).resolve() for path in explicit]
    if manifest is None:
        # No built-in guess at where a configuration repository lives. A wrong guess would
        # silently authorize recovery from a directory nobody approved, so the manifest is
        # named by $CLAUDE_CONFIG_REPO or by --external-skill-manifest, or there is none.
        config_repo = os.environ.get("CLAUDE_CONFIG_REPO")
        if not config_repo:
            return sorted(set(roots))
        manifest = Path(config_repo) / "external-skill-repos.json"
    if manifest.is_file():
        try:
            data = read_object(manifest)
            base = Path(data["skillRepoRoot"])
            if not base.is_absolute():
                base = claude.parent / base
            base = base.resolve()
            for item in data["repos"]:
                relative = Path(item["dir"])
                root = (base / relative).resolve()
                if relative.is_absolute() or not root.is_relative_to(base):
                    raise ValueError("Invalid repository path")
                roots.append(root)
        except (OSError, KeyError, TypeError, ValueError):
            warnings.append({"status": "unavailable", "reason": "external_skill_manifest_invalid", "source": str(manifest)})
            # No partially parsed manifest may authorize recovery.
            roots = [Path(path).resolve() for path in explicit]
    return sorted(set(roots))


def inventory_sources(claude: Path, codex: Path, skills: Path, *, approved_repos=(), external_manifest: Path | None = None) -> dict:
    report = {"version": 1, "skills": [], "warnings": [], "approved_repos": []}
    legacy = read_object(codex / "claude-sync/managed-skills.json")
    owned = read_object(codex / "claude-sync/managed-artifacts.json").get("items", {})
    cache, candidates = {}, {}
    roots = approved_roots(claude, approved_repos, external_manifest, report["warnings"])
    for root in roots:
        repo = repository_info(root, cache)
        if repo.get("root") != str(root):
            report["warnings"].append({"source": str(root), "status": "unavailable", "reason": "approved_repository_unavailable"})
            continue
        report["approved_repos"].append(str(root))
        for entry in skill_entries(root):
            # A symlink escaping an approved checkout does not inherit approval.
            if not entry.resolve().is_relative_to(root):
                continue
            name = declared_name(entry)
            candidate_repo = repository_info(entry, cache)
            if name and candidate_repo.get("root") == str(root):
                candidates.setdefault(name, {})[str(entry.resolve())] = {"source": str(entry.resolve()), "repository": candidate_repo, "reason": "declared_name_in_approved_repository"}
    locations = [("agents", skills), ("legacy_codex", codex / "skills"), ("claude", claude / "skills")]
    for name, provider in plugin_catalog(claude).items():
        if provider["status"] == "available":
            locations.extend(("plugin:" + name, Path(root) / "skills") for root in provider["roots"])
    for location, root in locations:
        try:
            entries = list(skill_entries(root))
        except OSError:
            report["warnings"].append({"source": str(root), "status": "unavailable", "reason": "skill_root_unreadable"})
            continue
        for entry in entries:
            broken = is_link(entry) and not entry.exists()
            name = declared_name(entry)
            record = owned.get(str(entry), owned.get(str(entry / "SKILL.md"), {}))
            original_source = Path(record["source"]) if record.get("owner") == OWNER and record.get("source") else entry.resolve()
            row = {"name": name or entry.name, "declared_name": name, "path": str(entry), "location": location,
                   "ownership": "managed" if str(entry) in legacy or str(entry) in owned or str(entry / "SKILL.md") in owned else "unmanaged",
                   "source": str(original_source), "broken": broken, "status": "broken" if broken else "available",
                   "reason": "broken_skill_link" if broken else "skill_entry_present",
                   "repository": repository_info(original_source, cache), "dependencies": dependencies(entry)}
            if record.get("owner") == OWNER:
                row["provenance"] = {key: record[key] for key in ("provider", "plugin", "provider_root", "source_sha256", "original") if key in record}
            if is_link(entry):
                row["link_target"] = link_target(entry)
            if broken:
                options = list(candidates.get(entry.name, {}).values())
                # Only the known legacy installation layout establishes that
                # the destination basename was the installed declared name.
                immediate = Path(row["link_target"])
                if not immediate.is_absolute():
                    immediate = entry.parent / immediate
                legacy_target = claude / "skills" / entry.name
                known_legacy = immediate.absolute() == legacy_target.absolute()
                if not known_legacy:
                    options = []
                status = "recoverable" if len(options) == 1 else "ambiguous" if options else "unrecoverable"
                reason = "unique_authoritative_local_source" if len(options) == 1 else "multiple_authoritative_local_sources" if options else "no_authoritative_local_source" if known_legacy else "unknown_link_origin"
                row["recovery"] = {"status": status, "reason": reason, "candidates": options}
                if status == "ambiguous":
                    row.update(status="ambiguous", reason=reason)
            report["skills"].append(row)
    return report
