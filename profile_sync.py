"""Local Claude profile bridge. Python 3.11+, no network or third-party packages."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import tomllib

from profile_config import plan_config
from profile_memory import plan_memory
from profile_hooks import plan_hooks
from profile_agents import plan_agents
from profile_lock import acquire as acquire_profile_lock
from profile_inventory import (OWNER, inventory_sources, is_link, link_target,
                               plugin_catalog, source_identity, source_disposition)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}


def linked(path: Path) -> bool:
    return is_link(path)


def assert_plain_path(path: Path) -> None:
    for part in (path, *path.parents):
        if linked(part):
            raise ValueError(f"Refusing linked output path: {part}")


def ensure_external(path: Path) -> None:
    repo = Path(__file__).resolve().parent
    if path.resolve().is_relative_to(repo):
        raise ValueError("Live profile data and backups must stay outside the tool repository")


def enabled_plugins(claude: Path) -> tuple[list[tuple[str, Path]], list[dict]]:
    roots, skipped = [], []
    for name, provider in plugin_catalog(claude).items():
        if provider["status"] != "available":
            skipped.append({"name": name, "status": provider["status"], "reason": provider["reason"]})
            continue
        roots.append((name.split("@")[0], Path(provider["roots"][0]).resolve()))
    return roots, skipped


def skill_dirs(root: Path, depth: int = 0, seen: set | None = None):
    """Follow explicitly installed directory links, without walking whole repos."""
    seen = seen if seen is not None else set()
    if not root.is_dir() or depth > 4:
        return
    resolved = root.resolve()
    if resolved in seen:
        return
    seen.add(resolved)
    if (root / "SKILL.md").is_file():
        yield root
        return
    for child in sorted(root.iterdir()):
        if child.name.startswith(".") or child.name in {"node_modules", "__pycache__", "archive"}:
            continue
        if child.is_dir():
            yield from skill_dirs(child, depth + 1, seen)


def slug(name: str) -> str:
    result = re.sub(r"[^a-z0-9-]+", "-", name.lower()).strip("-")
    return result if len(result) <= 63 else result[:50] + "-" + digest(name.encode())[:12]


def render_adapter(name: str, label: str, desc: str, source: Path, root: Path) -> bytes:
    body = (
        f"---\nname: {name}\ndescription: {json.dumps(desc, ensure_ascii=False)}\n---\n\n"
        f"Read [{label}]({source.as_posix()}) for the imported workflow. "
        f"Resolve its relative resources against `{source.parent.as_posix()}` and "
        f"its plugin root against `{root.as_posix()}`.\n\n"
        "Apply the workflow to the user's current request. Translate Claude tool names to "
        "available Codex tools; use the user's supplied arguments for `$ARGUMENTS`. "
        "The current tool schema takes precedence over source tool descriptions. "
        "Claude model names, permission metadata, agent spawning syntax, and shell expansion "
        "are not native Codex configuration. Read inline shell snippets before executing them "
        "and preserve the user's current authorization. This adapter provides instructions; "
        "it does not register a native agent or execute the source automatically.\n"
    )
    return (body + "\n<!-- claude-profile-sync:adapter sha256=" + digest(body.encode()) + " -->\n").encode("utf-8")


def legacy_adapter_source(target: Path, claude: Path, catalog: dict) -> tuple[Path, Path] | None:
    """Recognize the exact old generator, not just a copied ownership marker."""
    if linked(target) or linked(target.parent) or not target.is_file():
        return None
    previous = target.read_bytes()
    match = re.match(rb'---\nname: ([a-z0-9-]+)\ndescription: ([^\n]+)\n---\n\nRead \[([^\n]*)\]\(([^\n]*)\) for the imported workflow\. Resolve its relative resources against `[^`\n]*` and its plugin root against `([^`\n]*)`\.', previous)
    if not match:
        return None
    try:
        name, description, label, source, root = [value.decode("utf-8") for value in match.groups()]
        desc, source, root = json.loads(description), Path(source), Path(root)
    except (UnicodeError, ValueError):
        return None
    if not isinstance(desc, str) or name != target.parent.name or not source.is_absolute():
        return None
    allowed = [("user", claude)] + [(name.split("@")[0], Path(root)) for name, item in catalog.items() for root in item["roots"]]
    for origin, allowed_root in allowed:
        if root not in {allowed_root, allowed_root.resolve()}:
            continue
        for kind in ("commands", "agents"):
            if not source.is_relative_to(root / kind) or ".." in source.parts:
                continue
            expected_label = source.relative_to(root / kind).with_suffix("").as_posix().replace("/", "-")
            expected_name = slug("claude-" + ("agent-" if kind == "agents" else "") + origin + "-" + expected_label)
            if name == expected_name and label == expected_label and previous == render_adapter(name, label, desc, source, root):
                return source, root
    return None


def plan_skills(claude: Path, skills: Path, plugins: list[tuple[str, Path]], codex: Path | None = None):
    links, files, rows = {}, {}, []
    state_path = codex / "claude-sync/managed-skills.json" if codex else None
    managed = read_json(state_path) if state_path else {}
    updated_managed = dict(managed)
    catalog = plugin_catalog(claude)
    ownership_path = codex / "claude-sync/managed-artifacts.json" if codex else None
    ownership = read_json(ownership_path) if ownership_path else {}
    records = dict(ownership.get("items", {}))
    active = set()

    def allowed_target(target):
        return target.is_relative_to(skills) or (codex is not None and target.is_relative_to(codex / "skills"))

    def own(path, source, expected, root=None):
        record = {"owner": OWNER, **source_identity(source, claude, catalog, root=root), "original": expected}
        if source.is_file():
            record["source_sha256"] = digest(source.read_bytes())
        records[str(path)] = record

    # Keep the legacy target -> source map readable by older installations.
    for raw_target, raw_source in managed.items():
        if isinstance(raw_source, str) and raw_target not in records:
            target = Path(raw_target)
            if allowed_target(target) and linked(target) and target.resolve() == Path(raw_source).resolve():
                own(target, Path(raw_source), snapshot(target))
    # Older adapters carry self hashes but had no ownership sidecar. Recover
    # provenance only when the complete old template and source layout match.
    for target in sorted(skills.glob("claude-*/SKILL.md")):
        if str(target) not in records:
            legacy = legacy_adapter_source(target, claude, catalog)
            if legacy:
                own(target, legacy[0], snapshot(target), legacy[1])
    sources = [("user", x) for x in skill_dirs(claude / "skills")]
    for name, root in plugins:
        sources.extend((name, x) for x in skill_dirs(root / "skills"))
    reserved = set()
    for origin, source in sources:
        name = source.name
        target = skills / name
        source_entry = source.absolute()
        source = source.resolve()
        active.add(str(target))
        text = (source / "SKILL.md").read_text(encoding="utf-8-sig")
        row = {"name": name, "source": str(source), "origin": origin}
        if not text.startswith("---") or not re.search(r"(?m)^name:\s*\S", text) or not re.search(r"(?m)^description:\s*\S", text):
            row.update(status="unsupported", reason="missing_skill_frontmatter")
        elif codex and (codex / "skills" / name / "SKILL.md").is_file() and not target.exists():
            row.update(status="conflict", reason="existing_legacy_codex_skill_preserved")
        elif target in reserved:
            row.update(status="conflict", reason="duplicate_source_name")
        elif target.exists() or linked(target):
            row["status"] = "shared" if target.resolve() == source else "conflict"
            if linked(target) and managed.get(str(target)) == str(target.resolve()) and target.resolve() != source:
                links[target] = source
                updated_managed[str(target)] = str(source)
                row["status"] = "relink"
            if row["status"] == "conflict":
                row["reason"] = "existing_codex_skill_preserved"
        else:
            links[target] = source
            updated_managed[str(target)] = str(source)
            row["status"] = "link"
        reserved.add(target)
        if row["status"] in {"link", "relink"}:
            own(target, source_entry / "SKILL.md", planned_link(source))
        elif row["status"] == "shared" and str(target) in records and not records[str(target)].get("retired") and snapshot(target) == records[str(target)]["original"]:
            own(target, source_entry / "SKILL.md", snapshot(target))
        rows.append(row)

    # Commands and roles are instruction adapters, not native Claude runtimes.
    instruction_roots = [("user", claude)] + plugins
    for origin, root in instruction_roots:
        for kind in ("commands", "agents"):
            for source in sorted((root / kind).rglob("*.md")) if (root / kind).is_dir() else []:
                label = source.relative_to(root / kind).with_suffix("").as_posix().replace("/", "-")
                name = slug("claude-" + ("agent-" if kind == "agents" else "") + origin + "-" + label)
                target = skills / name / "SKILL.md"
                active.add(str(target))
                if target.parent in reserved or target in files:
                    rows.append({"name": name, "status": "conflict", "reason": "duplicate_adapter_name"})
                    continue
                reserved.add(target.parent)
                text = source.read_text(encoding="utf-8-sig")
                match = re.search(r"(?m)^description:\s*(.+)$", text)
                desc = match.group(1).strip("\"'") if match else f"Use the imported {origin} {label} {kind[:-1]} workflow when requested."
                if desc in {"|", ">", "|-", ">-"}:
                    desc = f"Use the imported {origin} {label} {kind[:-1]} workflow when requested."
                desc = desc[:400]
                generated = render_adapter(name, label, desc, source, root)
                intact = False
                if target.is_file() and not linked(target.parent) and not linked(target):
                    previous = target.read_bytes()
                    found = re.fullmatch(rb"(.*)\n<!-- claude-profile-sync:adapter sha256=([0-9a-f]{64}) -->\n", previous, re.S)
                    intact = bool(found and digest(found[1]).encode() == found[2])
                    if str(target) in records:
                        intact = intact and not records[str(target)].get("retired") and snapshot(target) == records[str(target)].get("original")
                retired = records.get(str(target), {}).get("retired")
                restore_retired = retired and snapshot(target)["kind"] == "missing" and not linked(target.parent)
                if target.parent.exists() and not (intact or restore_retired):
                    rows.append({"name": name, "status": "conflict", "reason": "existing_codex_skill_preserved"})
                else:
                    files[target] = generated
                    own(target, source, {"kind": "file", "sha256": digest(generated)}, root)
                    rows.append({"name": name, "source": str(source), "status": "adapted", "kind": kind})

    for raw_target, record in list(records.items()):
        if raw_target in active:
            continue
        target = Path(raw_target)
        row = {"name": target.parent.name if target.name == "SKILL.md" else target.name,
               "path": raw_target, "source": record.get("source")}
        if record.get("owner") != OWNER or not allowed_target(target) or ".." in target.parts:
            row.update(status="conflict", reason="invalid_managed_ownership")
        elif record.get("retired"):
            if snapshot(target)["kind"] == "missing":
                row.update(status="retired", reason="managed_artifact_already_retired")
            else:
                row.update(status="conflict", reason="managed_artifact_recreated_after_retirement")
        elif snapshot(target) != record.get("original"):
            if snapshot(target)["kind"] == "missing":
                records.pop(raw_target)
                updated_managed.pop(raw_target, None)
                row.update(status="retired", reason="managed_artifact_already_missing")
            else:
                row.update(status="conflict", reason="managed_artifact_modified_by_user")
        else:
            status, reason = source_disposition(record, catalog)
            # Legacy link records name the directory rather than SKILL.md.
            if status == "present" and record["original"]["kind"] == "link" and not (Path(record["source"]) / "SKILL.md").is_file() and Path(record["source"]).is_dir():
                status, reason = "retired", "source_removed"
            row.update(status=status, reason=reason)
            if status == "retired":
                files[target] = None
                # Retain the source and original hash as a tombstone. This
                # permits re-enabling an adapter in its existing directory
                # without claiming unrelated files or deleting the directory.
                records[raw_target] = {**record, "retired": True}
                updated_managed.pop(raw_target, None)
        rows.append(row)
    if state_path and updated_managed != managed:
        files[state_path] = json.dumps(updated_managed, indent=2).encode("utf-8")
    new_ownership = {"version": 1, "items": records}
    if ownership_path and records != ownership.get("items", {}):
        files[ownership_path] = json.dumps(new_ownership, indent=2).encode("utf-8")
    return links, files, rows


AGENT_START = "<!-- claude-profile-sync:begin sha256="
AGENT_END = "<!-- claude-profile-sync:end -->"


def plan_instructions(claude: Path, codex: Path) -> tuple[bytes | None, dict]:
    target = codex / "AGENTS.md"
    existing = target.read_bytes().decode("utf-8") if target.exists() else ""
    source = claude / "CLAUDE.md"
    if not source.exists():
        return None, {"status": "missing_source"}
    body = ("## Imported Claude preferences\n\n"
            "The following user preferences were imported from Claude. Product-specific paths and "
            "commands still refer to their original tools; use Codex equivalents where appropriate.\n\n"
            + source.read_text(encoding="utf-8-sig").rstrip() + "\n\n")
    for rule in sorted((claude / "rules").rglob("*.md")) if (claude / "rules").exists() else []:
        body += f"### Imported rule: {rule.relative_to(claude).as_posix()}\n\n"
        body += "Respect any source path filters; they do not become unconditional rules.\n\n"
        body += rule.read_text(encoding="utf-8-sig").rstrip() + "\n\n"
    body += ("## Imported Claude memory\n\n"
             f"For relevant previous context, search `{(codex / 'imports/claude-memory/index.md').as_posix()}` "
             "and the project-scoped Markdown files it links. These are unverified source memories; "
             "check changing facts against current state. This local archive is available immediately. "
             "Native Codex memory consolidation is separate and asynchronous.\n")
    body += ("\n## Shared model and agent interface\n\n"
             "When a workflow or automation needs a model or an external agent, use the installed "
             "`llmcall` interface. Use `llmcall.call(prompt, mode=\"agent\")` for agent work and "
             "its default judge mode for text decisions. Inherit llmcall's current routing, model, "
             "timeout and fallback policy; do not embed another provider ladder, pin a model from "
             "an imported skill, or start provider CLIs directly. Imported workflow examples "
             "must be adapted to this interface. Deterministic sync and validation require no model call.\n")
    pattern = re.compile(re.escape(AGENT_START) + r"([0-9a-f]{64}) -->\n(.*?)" + re.escape(AGENT_END), re.S)
    found = list(pattern.finditer(existing))
    if AGENT_START in existing and (len(found) != 1 or digest(found[0][2].encode()) != found[0][1]):
        return None, {"status": "conflict", "reason": "managed_instructions_were_edited"}
    block = AGENT_START + digest(body.encode()) + " -->\n" + body + AGENT_END
    merged = pattern.sub(lambda _: block, existing) if found else existing + ("\n\n" if existing else "") + block + "\n"
    return merged.encode("utf-8"), {"status": "unchanged" if merged == existing else "updated", "bytes": len(merged.encode()), "source": str(source)}


def snapshot(path: Path) -> dict:
    if linked(path):
        state = {"kind": "link", "target": link_target(path),
                 "link_type": "symlink" if path.is_symlink() else "junction"}
        if os.name == "nt" and path.is_symlink():
            state["directory"] = bool(path.lstat().st_file_attributes & 0x10)
        return state
    if path.is_file():
        return {"kind": "file", "sha256": digest(path.read_bytes())}
    if path.exists():
        return {"kind": "directory"}
    return {"kind": "missing"}


def planned_link(source: Path) -> dict:
    return {"kind": "link", "target": str(source), "link_type": "junction" if os.name == "nt" else "symlink"}


def remove_entry(path: Path):
    """Remove a single file/link; directory contents are never traversed."""
    assert_plain_path(path.parent)
    current = snapshot(path)
    if current["kind"] == "link" and current.get("link_type") == "junction":
        os.rmdir(path)
    elif current["kind"] in {"link", "file"}:
        path.unlink()
    elif current["kind"] != "missing":
        raise ValueError("Refusing to delete a directory")


def restore_link(path: Path, state: dict):
    if state.get("link_type", "junction" if os.name == "nt" else "symlink") == "symlink" and os.name == "nt":
        assert_plain_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(state["target"], target_is_directory=state.get("directory", True))
    else:
        make_link(path, Path(state["target"]))


def atomic_write(path: Path, data: bytes):
    assert_plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".claude-sync-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def make_link(path: Path, source: Path):
    assert_plain_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.symlink_to(source, target_is_directory=True)
        return
    # Both New-Item and Python's CreateJunction require an existing target.
    # Write the mount-point reparse record directly so rollback can restore an
    # originally broken junction without creating anything at its destination.
    import ctypes
    from ctypes import wintypes
    import struct
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    destination = str(source.absolute())
    substitute = ("\\??\\UNC\\" + destination[2:] if destination.startswith("\\\\") else "\\??\\" + destination).encode("utf-16le")
    printable = destination.encode("utf-16le")
    names = substitute + b"\0\0" + printable + b"\0\0"
    payload = struct.pack("<IHHHHHH", 0xA0000003, len(names) + 8, 0, 0, len(substitute), len(substitute) + 2, len(printable)) + names
    path.mkdir()
    handle = kernel.CreateFileW(str(path), 0x40000000, 0, None, 3, 0x02200000, None)
    try:
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        size = wintypes.DWORD()
        buffer = ctypes.create_string_buffer(payload)
        if not kernel.DeviceIoControl(handle, 0x000900A4, buffer, len(payload), None, 0, ctypes.byref(size), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except Exception:
        if handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)
            handle = ctypes.c_void_p(-1).value
        os.rmdir(path)  # Only the newly created empty entry, never a target.
        raise
    finally:
        if handle != ctypes.c_void_p(-1).value:
            kernel.CloseHandle(handle)


def enable_hooks(config: bytes) -> tuple[bytes, str]:
    text = config.decode("utf-8")
    parsed = tomllib.loads(text.removeprefix("\ufeff"))
    features = parsed.get("features", {})
    if features.get("hooks") is True:
        return config, "enabled"
    if features.get("hooks") is False:
        return config, "explicitly_disabled_preserved"
    newline = "\r\n" if "\r\n" in text else "\n"
    header = re.search(r"(?m)^\[features\][ \t]*(?:#[^\r\n]*)?\r?\n", text)
    if header:
        merged = text[:header.end()] + "hooks = true" + newline + text[header.end():]
    elif "features" not in parsed:
        merged = text + newline + "[features]" + newline + "hooks = true" + newline
    else:
        return config, "feature_table_requires_manual_merge"
    expected = json.loads(json.dumps(parsed))
    expected.setdefault("features", {})["hooks"] = True
    if tomllib.loads(merged.removeprefix("\ufeff")) != expected:
        return config, "feature_table_requires_manual_merge"
    return merged.encode("utf-8"), "enabled"


def destination_baseline(codex: Path, skills: Path) -> dict:
    """Capture every writable destination before any planner reads ownership."""
    result = {}
    def visit(path):
        state = snapshot(path)
        result[path] = state
        if state["kind"] == "directory":
            for child in path.iterdir():
                visit(child)
    for name in ("config.toml", "AGENTS.md", "hooks.json"):
        visit(codex / name)
    for name in ("agents", "imports/claude-memory", "imports/claude-hooks", "memories/extensions/ad_hoc/notes"):
        visit(codex / name)
    state_root = codex / "claude-sync"
    if state_root.is_dir() and not linked(state_root):
        for path in state_root.glob("managed-*.json"):
            visit(path)
    for skill_root in {skills, codex / "skills"}:
        if skill_root.is_dir() and not linked(skill_root):
            for path in skill_root.iterdir():
                result[path] = snapshot(path)
                if result[path]["kind"] == "directory":
                    visit(path / "SKILL.md")
    return result


def build_plan(claude: Path, codex: Path, skills: Path, *, repair_links=False,
               approved_repos=(), external_manifest: Path | None = None):
    """Plan without writes. Optional recovery uses only approved local sources.

    The original three positional arguments remain supported. `None` file
    payloads from subplanners mean single-entry retirement, never rmtree.
    """
    for output in (codex, skills):
        ensure_external(output)
        assert_plain_path(output)
    if not claude.is_dir():
        raise ValueError("Claude configuration directory does not exist")
    baseline = destination_baseline(codex, skills)
    plugins, skipped_plugins = enabled_plugins(claude)
    links, files, skill_report = plan_skills(claude, skills, plugins, codex)
    config, config_report = plan_config(claude, codex, [root for _, root in plugins])
    hook_files, hook_report = plan_hooks(claude, codex)
    if hook_report["requires_hooks_feature"]:
        config, hook_report["feature"] = enable_hooks(config)
        for item in config_report["settings"]:
            if item.get("source") == str(claude / "settings.json") and item.get("key") == "hooks" and item.get("event") == "Stop":
                item.update(status="adapted", reason="reviewed_stop_commands_handled_by_hooks_adapter; see_hooks_report")
    files.update(hook_files)
    agent_files, config, agent_report = plan_agents(claude, codex, plugins, config)
    files.update(agent_files)
    agent_delete_before = {Path(path): {"kind": "file", "sha256": sha256}
                           for path, sha256 in agent_report["deletion_hashes"].items()}
    files.update({path: None for path in agent_delete_before})
    files[codex / "config.toml"] = config
    instructions, instruction_report = plan_instructions(claude, codex)
    if instructions is not None:
        files[codex / "AGENTS.md"] = instructions
    memory_files, memory_report = plan_memory(claude, codex)
    files.update(memory_files)
    inventory = inventory_sources(claude, codex, skills, approved_repos=approved_repos,
                                  external_manifest=external_manifest)
    source_checks = {}
    if repair_links:
        state_path = codex / "claude-sync/managed-skills.json"
        managed = json.loads(files[state_path]) if state_path in files else read_json(state_path)
        ownership_path = codex / "claude-sync/managed-artifacts.json"
        ownership = json.loads(files[ownership_path]) if ownership_path in files else read_json(ownership_path)
        owned = ownership.setdefault("items", {})
        for row in inventory["skills"]:
            recovery = row.get("recovery", {})
            target = Path(row["path"])
            if recovery.get("status") != "recoverable" or row["location"] not in {"agents", "legacy_codex"}:
                continue
            if target in links or target in files:
                continue
            source = Path(recovery["candidates"][0]["source"])
            links[target] = source
            source_checks[target] = {"path": str(source / "SKILL.md"), "sha256": digest((source / "SKILL.md").read_bytes()),
                                     "resolved": str((source / "SKILL.md").resolve())}
            managed[str(target)] = str(source)
            owned[str(target)] = {"owner": OWNER, "source": str(source / "SKILL.md"), "provider": "user",
                                  "provider_root": recovery["candidates"][0]["repository"]["root"],
                                  "original": planned_link(source)}
            recovery.update(status="planned", reason="explicit_link_repair_requested")
        if links and managed != read_json(state_path):
            files[state_path] = json.dumps(managed, indent=2).encode()
            files[ownership_path] = json.dumps({"version": 1, "items": owned}, indent=2).encode()
    report = {"tool": "claude-codex-profile-sync", "version": 1,
              "claude_home": str(claude), "codex_home": str(codex), "skills_home": str(skills),
              "skills": skill_report, "plugins_skipped": skipped_plugins,
              "config": config_report, "instructions": instruction_report, "memory": memory_report,
              "hooks": hook_report, "agents": agent_report,
              "inventory": inventory,
              "limitations": ["Claude login tokens and model/provider settings are not portable.",
                              "Only the two reviewed local browser Stop scripts are adapted; other hooks require porting.",
                              "Directory links and instruction adapters depend on their source installation.",
                              "Native memory consolidation is asynchronous; the searchable archive is immediate."]}
    changes = []
    for path, content in files.items():
        ensure_external(path)
        assert_plain_path(path.parent if content is None else path)
        before = snapshot(path)
        if before != baseline.get(path, {"kind": "missing"}):
            raise ValueError("Destination changed while planning; rerun sync")
        if path in agent_delete_before and before != agent_delete_before[path]:
            raise ValueError("Managed agent changed during planning")
        after = {"kind": "missing"} if content is None else {"kind": "file", "sha256": digest(content)}
        if before != after:
            if before["kind"] not in ({"missing", "file", "link"} if content is None else {"missing", "file"}):
                raise ValueError(f"Output is not a regular file: {path}")
            changes.append({"path": str(path), "before": before, "after": after, "data": content,
                            "append_only": path.is_relative_to(codex / "memories")})
    for path, source in links.items():
        before = snapshot(path)
        if before != baseline.get(path, {"kind": "missing"}):
            raise ValueError("Destination changed while planning; rerun sync")
        change = {"path": str(path), "before": before, "after": planned_link(source), "append_only": False}
        if path in source_checks:
            change["source_check"] = source_checks[path]
        changes.append(change)
    report["changes"] = [{k: v for k, v in row.items() if k != "data"} for row in changes]
    report["change_count"] = len(changes)
    return changes, report


@contextmanager
def destination_lock(codex: Path):
    root = codex / "claude-sync"
    assert_plain_path(root)
    with acquire_profile_lock(root):
        yield


def snapshot_matches(path: Path, current: dict, expected: dict) -> bool:
    """Version-one backups recorded resolved link targets without a link type."""
    if current == expected:
        return True
    if (current.get("kind") == expected.get("kind") == "link"
            and set(expected) == {"kind", "target"}
            and current.get("link_type") == ("junction" if os.name == "nt" else "symlink")):
        target = Path(current["target"])
        if not target.is_absolute():
            target = path.parent / target
        return target.resolve() == Path(expected["target"]).resolve()
    return False


def rollback(backup: Path, codex: Path, skills: Path) -> dict:
    base = codex / "claude-sync/backups"
    if not backup.resolve().is_relative_to(base.resolve()):
        raise ValueError("Backup must be inside this Codex home's claude-sync/backups")
    manifest = read_json(backup / "manifest.json")
    if manifest.get("codex_home") != str(codex) or manifest.get("skills_home") != str(skills):
        raise ValueError("Backup destination roots do not match this invocation")
    result = {"restored": [], "preserved": []}
    for row in reversed(manifest["changes"]):
        path = Path(row["path"])
        if not (path.is_relative_to(codex) or path.is_relative_to(skills)) or ".." in path.parts:
            raise ValueError("Unsafe path in backup manifest")
        if row.get("append_only") or path.is_relative_to(codex / "memories"):
            result["preserved"].append({"path": str(path), "reason": "append_only_memory_note"})
            continue
        current = snapshot(path)
        if snapshot_matches(path, current, row["before"]):
            continue
        if not snapshot_matches(path, current, row["after"]):
            result["preserved"].append({"path": str(path), "reason": "changed_since_sync"})
            continue
        assert_plain_path(path.parent)
        if row["before"]["kind"] == "missing":
            remove_entry(path)
        elif row["before"]["kind"] == "link":
            remove_entry(path)
            restore_link(path, row["before"])
        else:
            stored = backup / row["backup_file"]
            if not stored.resolve().is_relative_to(backup.resolve()):
                raise ValueError("Unsafe payload path in backup manifest")
            data = stored.read_bytes()
            if digest(data) != row["before"]["sha256"]:
                raise ValueError("Backup payload integrity check failed")
            atomic_write(path, data)
        if not snapshot_matches(path, snapshot(path), row["before"]):
            raise ValueError("Post-rollback verification failed")
        result["restored"].append(str(path))
    return result


def verify_source(row: dict):
    check = row.get("source_check")
    if check:
        path = Path(check["path"])
        if str(path.resolve()) != check["resolved"] or not path.is_file() or digest(path.read_bytes()) != check["sha256"]:
            raise ValueError("Recovery source changed during planning; rerun sync")


def apply_plan(changes: list, report: dict, codex: Path, skills: Path):
    if not changes:
        report["status"] = "no_changes"
        return report
    # Validate the complete plan before writing even the backup. Public callers
    # cannot turn the changes engine into recursive deletion or arbitrary writes.
    seen = set()
    for row in changes:
        path = Path(row["path"])
        ensure_external(path)
        if path in seen or ".." in path.parts or not (path.is_relative_to(codex) or path.is_relative_to(skills)):
            raise ValueError("Unsafe or duplicate path in plan")
        seen.add(path)
        assert_plain_path(path.parent)
        verify_source(row)
        before, after = row["before"]["kind"], row["after"]["kind"]
        if before not in {"file", "link", "missing"} or after not in {"file", "link", "missing"}:
            raise ValueError("Only regular files and links can be changed")
        if after == "missing" and (row.get("append_only") or path.is_relative_to(codex / "memories")):
            raise ValueError("Append-only memory cannot be deleted")
        if path.is_relative_to(codex / "memories") and before != "missing":
            raise ValueError("Existing append-only memory cannot be modified")
        if after == "file" and (before == "link" or digest(row["data"]) != row["after"]["sha256"]):
            raise ValueError("Invalid file publication")
        if after == "link" and before not in {"missing", "link"}:
            raise ValueError("Only links may be relinked")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = codex / "claude-sync/backups" / stamp
    assert_plain_path(backup)
    backup.mkdir(parents=True)
    manifest = {"codex_home": str(codex), "skills_home": str(skills), "changes": []}
    # Back up and verify every destination before changing any destination.
    for i, row in enumerate(changes):
        path = Path(row["path"])
        if snapshot(path) != row["before"]:
            raise ValueError("Destination changed during planning; rerun sync")
        entry = {k: v for k, v in row.items() if k != "data"}
        if row["before"]["kind"] == "file":
            entry["backup_file"] = f"{i:05d}.bin"
            data = path.read_bytes()
            if digest(data) != row["before"]["sha256"]:
                raise ValueError("Destination changed while backing up; rerun sync")
            atomic_write(backup / entry["backup_file"], data)
        manifest["changes"].append(entry)
    atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
    try:
        # The single append-only memory note is last, after reversible config changes.
        for row in sorted(changes, key=lambda x: x["append_only"]):
            path = Path(row["path"])
            if snapshot(path) != row["before"]:
                raise ValueError("Destination changed before publication; rerun sync")
            verify_source(row)
            if row["after"]["kind"] == "link":
                if row["before"]["kind"] == "link":
                    remove_entry(path)
                try:
                    make_link(path, Path(row["after"]["target"]))
                except Exception:
                    if row["before"]["kind"] == "link" and not path.exists() and not linked(path):
                        restore_link(path, row["before"])
                    raise
            elif row["after"]["kind"] == "missing":
                remove_entry(path)
            else:
                atomic_write(path, row["data"])
            if snapshot(path) != row["after"]:
                raise ValueError("Post-write verification failed")
        report.update(status="applied", backup=str(backup))
        atomic_write(backup / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception:
        rollback(backup, codex, skills)
        raise
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--skills-home", type=Path, default=Path.home() / ".agents/skills")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply changes after backup (default is zero-write preview)")
    mode.add_argument("--dry-run", action="store_true", help="Explicit zero-write preview")
    mode.add_argument("--rollback", type=Path, help="Restore unchanged config/files from a backup; never delete memory notes")
    parser.add_argument("--json", action="store_true", help="Print sanitized report with no source file contents")
    parser.add_argument("--repair-links", action="store_true", help="Plan recovery of legacy broken links from unique approved local sources")
    parser.add_argument("--approved-repo", action="append", type=Path, default=[], help="Additional authoritative local skill checkout (repeatable)")
    parser.add_argument("--external-skill-manifest", type=Path, help="External external-skill-repos.json source manifest")
    args = parser.parse_args(argv)
    claude, codex, skills = [p.absolute() for p in (args.claude_home, args.codex_home, args.skills_home)]
    try:
        for root in (codex, skills):
            ensure_external(root)
            assert_plain_path(root)
        if args.rollback:
            with destination_lock(codex):
                report = {"status": "rolled_back", **rollback(args.rollback.absolute(), codex, skills)}
        elif args.apply:
            with destination_lock(codex):
                changes, report = build_plan(claude, codex, skills, repair_links=args.repair_links, approved_repos=args.approved_repo, external_manifest=args.external_skill_manifest)
                report = apply_plan(changes, report, codex, skills)
        else:
            changes, report = build_plan(claude, codex, skills, repair_links=args.repair_links, approved_repos=args.approved_repo, external_manifest=args.external_skill_manifest)
            report["status"] = "preview" if changes else "no_changes"
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("Claude -> Codex:", report["status"])
            if "change_count" in report:
                print("Planned/verified changes:", report["change_count"])
                print("Skills:", dict((status, sum(r["status"] == status for r in report["skills"])) for status in sorted({r["status"] for r in report["skills"]})))
                print("Memory:", report["memory"]["status"], "files:", report["memory"]["selected_files"])
                print("MCP:", dict((status, sum(r["status"] == status for r in report["config"]["mcp"])) for status in sorted({r["status"] for r in report["config"]["mcp"]})))
                print("Use --json for conflicts, skipped sources, and compatibility details.")
            if "backup" in report:
                print("Backup/report:", report["backup"])
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        # JSON/TOML parse errors may quote secrets from the input: print type only.
        print(json.dumps({"status": "error", "error_type": type(exc).__name__, "message": "Sync failed; no source contents are printed. Inspect inputs and backup manifests locally."}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
