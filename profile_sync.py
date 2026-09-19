"""Local Claude profile bridge. Python 3.11+, catalog-backed discovery; no network."""
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
import tomllib

import profile_catalog as source_catalog

from profile_config import plan_config, plan_skill_routing, _plain_member, _profile_verification
from profile_memory import plan_memory
from profile_bridge import memory_outbox, ownership as bridge_ownership
from profile_bridge.memory import archive as memory_archive
from profile_bridge import overlays as runtime_overlays
from fleet_guards.filesystem import atomic_replace
from profile_hooks import plan_hooks
from profile_agents import plan_agents
from profile_lock import profile_locks
from profile_inventory import (OWNER, inventory_sources, is_link, link_target,
                               plugin_catalog, source_identity, source_disposition)

MEMORY_ARCHIVE_HYGIENE_VERSION = 1


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


def enabled_plugins(claude: Path, snapshot=None) -> tuple[list[tuple[str, Path]], list[dict]]:
    roots, skipped = [], []
    for name, provider in plugin_catalog(claude, snapshot).items():
        if provider["status"] != "available":
            skipped.append({"name": name, "status": provider["status"], "reason": provider["reason"]})
            continue
        roots.append((name, Path(provider["roots"][0]).resolve()))
    return roots, skipped


def skill_dirs(root: Path, depth: int = 0, seen: set | None = None, *, snapshot=None):
    """Compatibility view; traversal and metadata belong to the catalog."""
    from profile_inventory import skill_entries
    yield from (path for path in skill_entries(root, depth, seen, snapshot=snapshot)
                if (path / "SKILL.md").is_file())


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
    if bridge_ownership.verify(previous, "adapter")["status"] != "verified":
        return None
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
    # A short plugin name cannot establish marketplace ownership. Legacy
    # self-marked imports without sidecars need one available owning root.
    providers = [(key, Path(raw)) for key, item in catalog.items()
                 if item['status'] == 'available' for raw in item['roots']]
    owning = [(key, path) for key, path in providers if root in {path, path.resolve()}]
    if root != claude and len(owning) != 1:
        return None
    allowed = [("user", claude)] + [(key.rsplit('@', 1)[0], path) for key, path in owning
        if sum(other.rsplit('@', 1)[0] == key.rsplit('@', 1)[0] for other, _ in providers) == 1]
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


def _skill_ownership(codex: Path | None, skills: Path) -> tuple[dict, dict]:
    """Collect full original manifests, member bytes and immediate link facts."""
    members, links = {}, {}
    if codex is None:
        return _profile_verification(members), members
    home = codex.parent
    state_root = codex / "claude-sync"
    if linked(state_root):
        raise ValueError("linked_ownership_member")
    if state_root.exists():
        for path in state_root.glob("managed-*.json"):
            if path.name == "managed-agents.json":
                continue
            data = _plain_member(path)
            if data is None:
                raise ValueError("ownership_member_disappeared")
            members[".codex/claude-sync/" + path.name] = data
    for root in {skills, codex / "skills"}:
        assert_plain_path(root)
        if not root.exists():
            continue
        for target in root.iterdir():
            # Observe a directory link itself, never walk its source tree.
            paths = [target] if linked(target) or target.is_file() else [target / "SKILL.md"]
            for path in paths:
                key = path.relative_to(home).as_posix()
                if linked(path):
                    links[key] = snapshot(path)
                else:
                    data = _plain_member(path)
                    if data is not None:
                        members[key] = data
    # Enumerate declared members only; the bridge still exclusively validates
    # their schema, hashes and complete dependency groups below.
    artifact_bytes = members.get('.codex/claude-sync/managed-artifacts.json')
    if artifact_bytes is not None:
        for raw in json.loads(artifact_bytes).get('items', {}):
            path = Path(raw)
            if not path.is_absolute() or '..' in path.parts or not any(path.is_relative_to(r) for r in (skills, codex / 'skills')):
                raise ValueError('unsafe_managed_skill_member')
            if linked(path):
                links[path.relative_to(home).as_posix()] = snapshot(path)
            else:
                data = _plain_member(path)
                if data is not None:
                    members[path.relative_to(home).as_posix()] = data
    return _profile_verification(members, home=home, links=links), members


def plan_skills(claude: Path, skills: Path, plugins: list[tuple[str, Path]], codex: Path | None = None, *, catalog_snapshot=None, runtime=None):
    catalog_snapshot = source_catalog.get_snapshot(claude, catalog_snapshot, codex=codex, skills=skills, plugins=plugins)
    links, files, rows = {}, {}, []
    state_path = codex / "claude-sync/managed-skills.json" if codex else None
    ownership_path = codex / "claude-sync/managed-artifacts.json" if codex else None
    try:
        verification, members = _skill_ownership(codex, skills)
    except (OSError, ValueError, UnicodeError):
        return {}, {}, [{"status": "conflict", "reason": "managed_skill_members_unreadable_or_outside_profile"}]
    if verification["status"] != "verified":
        conflicted = []
        for member in verification.get("invalid", []):
            target = codex.parent / member
            if target.is_relative_to(skills) or target.is_relative_to(codex / "skills"):
                conflicted.append({"name": target.parent.name if target.name == "SKILL.md" else target.name,
                                   "path": str(target), "status": "conflict",
                                   "reason": "managed_skill_group_modified_missing_or_unknown"})
        return {}, {}, conflicted or [{"name": "managed-skills", "status": "conflict",
                                       "reason": "managed_skill_group_modified_missing_or_unknown"}]
    state_bytes = members.get(".codex/claude-sync/managed-skills.json")
    ownership_bytes = members.get(".codex/claude-sync/managed-artifacts.json")
    managed = json.loads(state_bytes) if state_bytes is not None else {}
    ownership = json.loads(ownership_bytes) if ownership_bytes is not None else {}
    updated_managed = dict(managed)
    catalog = plugin_catalog(claude, catalog_snapshot)
    records = dict(ownership.get("items", {}))
    initial_import = state_bytes is None and ownership_bytes is None
    active = set()
    legacy_converted = set()
    for raw, previous in list(records.items()):
        if (Path(raw).name != 'SKILL.md' or previous.get('original', {}).get('kind') != 'file'
            or previous.get('source_id') == 'runtime-policy:tasks'):
            continue
        modern = 'source_id' in previous and 'catalog_identity' in previous
        if modern:
            provider = catalog.get(previous.get('plugin'), {})
            if (previous.get('retired') or provider.get('status') != 'available'
                or provider.get('roots') == [previous.get('provider_root')]):
                continue
            entry = {**previous['catalog_identity'].get('entrypoint', {}),
                     'source_id': previous['source_id']}
            if (str(Path(raw).with_name('workflow.json')) in records
                and runtime is not None and runtime_overlays.key(entry) in runtime['entries']):
                # Installed modern overlays use the complete ownership and
                # replacement checks below, not the legacy template check.
                continue
        # Invalid legacy and partial records cannot authorize retirement either.
        active.add(raw)
        target = Path(raw)
        legacy_record = {k: v for k, v in previous.items() if k not in {'source_id', 'catalog_identity'}} if modern else previous
        identity = source_catalog.legacy_adapter_identity(legacy_record, catalog_snapshot)
        if modern and identity is not None:
            old_entry = previous['catalog_identity'].get('entrypoint', {})
            new_entry = identity['catalog_identity']['entrypoint']
            if (previous['source_id'] != identity['source_id'] or
                any(old_entry.get(k) != new_entry.get(k) for k in ('kind', 'relative_path', 'client', 'scope'))):
                identity = None
        # The current catalog has already proved an exact origin/layout/hash
        # match. Verify the original adapter against its recorded old root.
        exact_provider = {previous['plugin']: {'status': 'available',
            'roots': [previous['provider_root']]}} if identity is not None else {}
        legacy_source = legacy_adapter_source(target, claude, exact_provider) if identity is not None else None
        if (identity is None or snapshot(target) != previous.get('original')
            or legacy_source != (Path(previous['source']), Path(previous['provider_root']))
            or bridge_ownership.verify(target.read_bytes(), 'adapter')['status'] != 'verified'):
            rows.append({'name': target.parent.name, 'path': raw, 'status': 'conflict',
                         'reason': 'legacy_adapter_identity_unverified'})
            continue
        records[raw] = {**previous, **identity}
        legacy_converted.add(raw)
        selected = [(record, entry) for record, entry in source_catalog.entries(
            catalog_snapshot, ('command', 'agent_template'), importable=True)
            if source_catalog.ownership_identity(record, entry) == identity]
        if len(selected) != 1:
            raise ValueError('legacy_adapter_selection_changed')
        current_record, current_entry = selected[0]
        if current_entry['path'] != previous['source']:
            current_source, current_root = Path(current_entry['path']), Path(current_record['path'])
            kind = 'agents' if current_entry['kind'] == 'agent_template' else 'commands'
            label = current_source.relative_to(current_root / kind).with_suffix('').as_posix().replace('/', '-')
            description = json.loads(target.read_text('utf-8').split('\ndescription: ', 1)[1].split('\n', 1)[0])
            generated = render_adapter(target.parent.name, label, description, current_source, current_root)
            files[target] = generated
            records[raw].update(source=str(current_source), provider_root=str(current_root),
                original={'kind': 'file', 'sha256': digest(generated)})
        if runtime is not None:
            runtime.setdefault('legacy_source_checks', []).append({
                'path': previous['source'], 'resolved': str(Path(previous['source']).resolve()),
                'sha256': previous['source_sha256']})
            runtime['legacy_source_checks'].append({
                'path': current_entry['path'], 'resolved': current_entry['resolved_path'],
                'sha256': previous['source_sha256']})
            # Freeze the provider selection evidence used by this conversion.
            for path in (claude / 'plugins/installed_plugins.json', claude / 'settings.json'):
                if path.is_file():
                    runtime['legacy_source_checks'].append({
                        'path': str(path), 'resolved': str(path.resolve()), 'sha256': digest(path.read_bytes())})

    def allowed_target(target):
        return target.is_relative_to(skills) or (codex is not None and target.is_relative_to(codex / "skills"))

    def own(path, source, expected, root=None, origin_record=None, entrypoint=None):
        record = {"owner": OWNER, **source_identity(source, claude, catalog, root=root, snapshot=catalog_snapshot), "original": expected}
        if origin_record is not None:
            record.update(source_catalog.ownership_identity(origin_record, entrypoint))
            if origin_record["kind"] == "plugin":
                record.update(provider="plugin", plugin=origin_record["registry_key"], provider_root=origin_record["path"])
        if source.is_file():
            record["source_sha256"] = digest(source.read_bytes())
        records[str(path)] = record

    def deploy_overlay(name, target, item):
        record, entry, overlay = item['record'], item['entrypoint'], item['overlay']
        active.add(str(target))
        row = {'name': name, 'source': entry.get('path'), 'status': overlay['status'],
               'reasons': overlay.get('reasons', []), **source_catalog.projection(record, entry)}
        if overlay['status'] != 'ready':
            rows.append(row)
            return
        generated = runtime_overlays.bundle(name, entry.get('description') or name, target, overlay)
        incoming = source_catalog.ownership_identity(record, entry)
        for path in generated:
            previous = records.get(str(path))
            if previous is not None:
                old_entry = previous.get('catalog_identity', {}).get('entrypoint', {})
                new_entry = incoming['catalog_identity']['entrypoint']
                if (previous.get('source_id') != incoming['source_id'] or
                    any(old_entry.get(k) != new_entry.get(k) for k in ('kind', 'relative_path', 'client', 'scope'))):
                    row.update(status='conflict', reason='owned_adapter_source_changed')
                    rows.append(row)
                    return
            if linked(path.parent) or linked(path) or (path.exists() and previous is None):
                row.update(status='conflict', reason='existing_codex_skill_preserved')
                rows.append(row)
                return
        if target.parent.exists() and str(target) not in records:
            row.update(status='conflict', reason='existing_codex_skill_preserved')
            rows.append(row)
            return
        for path, content in generated.items():
            active.add(str(path))
            files[path] = content
            own(path, Path(entry['path']), {'kind': 'file', 'sha256': digest(content)},
                Path(record['path']), record, entry)
            runtime.setdefault('source_checks', {})[path] = overlay
        runtime.setdefault('replacement_groups', []).append({
            'descriptor': overlay, 'members': {str(path): {'kind': 'file', 'sha256': digest(content)}
                                               for path, content in generated.items()}})
        # Retire only bridge-owned old links with this exact stable identity,
        # after the complete replacement bundle has passed ownership checks.
        if entry['kind'] == 'skill':
            for raw, previous in list(records.items()):
                old_entry = previous.get('catalog_identity', {}).get('entrypoint', {})
                if (previous.get('source_id') == incoming['source_id'] and not previous.get('retired')
                    and previous['original']['kind'] == 'link'
                    and all(old_entry.get(k) == incoming['catalog_identity']['entrypoint'].get(k)
                            for k in ('kind', 'relative_path', 'client', 'scope'))):
                    old_path = Path(raw)
                    active.add(raw)
                    files[old_path] = None
                    updated_managed.pop(raw, None)
                    records[raw] = {**previous, 'retired': True}
                    runtime['source_checks'][old_path] = overlay
        if entry['kind'] == 'agent_template':
            runtime['roles'][runtime_overlays.key(entry)] = {
                'status': 'ready', 'equivalent': item.get('equivalent', False),
                'artifact_hash': overlay['artifact_hash'], 'destination': str(target)}
        row.update(status='adapted', destination=str(target), artifact_hash=overlay['artifact_hash'])
        rows.append(row)

    if runtime is not None:
        alternatives = []
        selection_root = skills / 'llmcall-tasks'
        for item in runtime['entries'].values():
            if item['entrypoint']['kind'] != 'skill':
                continue
            entry = item['entrypoint']
            identity = json.dumps(runtime_overlays.key(entry)).encode()
            name = slug('llmcall-' + entry['name'] + '-' + digest(identity)[:12])
            # Only the policy router is discoverable. Alternatives keep their
            # exact source ownership in separate non-trigger payloads.
            target = selection_root / 'alternatives' / name / 'ENTRYPOINT.md'
            deploy_overlay(name, target, item)
            if target in files:
                alternatives.append({'identity': list(runtime_overlays.key(entry)),
                    'entrypoint': target.relative_to(selection_root).as_posix(),
                    'descriptor': (target.parent / 'workflow.json').relative_to(selection_root).as_posix(),
                    'artifact_hash': item['overlay']['artifact_hash']})
                # Remove only earlier generated discoverable adapters belonging
                # to this exact source. User-owned originals remain untouched.
                incoming = source_catalog.ownership_identity(item['record'], entry)
                for raw, previous in list(records.items()):
                    old = Path(raw)
                    old_entry = previous.get('catalog_identity', {}).get('entrypoint', {})
                    new_entry = incoming['catalog_identity']['entrypoint']
                    if (old.is_relative_to(skills / name) and not previous.get('retired')
                        and previous.get('source_id') == incoming['source_id']
                        and all(old_entry.get(k) == new_entry.get(k) for k in ('kind', 'relative_path', 'client', 'scope'))):
                        active.add(raw)
                        files[old] = None
                        records[raw] = {**previous, 'retired': True}
        # A removed policy or lost capability must replace the installed envelope
        # even when no replacement is runnable. Its old grant cannot remain live.
        router_owned = any(record.get('source_id') == 'runtime-policy:tasks'
                           for record in records.values())
        if alternatives or router_owned:
            envelope = {'schema_version': 1, 'policy': runtime['selection_policy'],
                'capabilities': runtime['selection_capabilities'], 'snapshot': runtime['selection_snapshot'],
                'alternatives': alternatives}
            envelope['selection_hash'] = runtime_overlays.conflicts.fingerprint(envelope)
            tasks = sorted({task for rule in (envelope['policy'] or {}).get('entries', []) for task in rule.get('tasks', [])})
            description = 'Select the configured primary or format specialist for ' + (', '.join(tasks) or 'configured tasks') + '. Explicit alternatives require a request override.'
            body = ('---\nname: llmcall-tasks\ndescription: ' + json.dumps(description) + '\n---\n\n'
                'Before executing a configured task, call `profile_bridge.adapters.select_entrypoint` with '
                f'`{(selection_root / "selection.json").as_posix()}` and the current request '
                '(task, format, requires, and any explicit override policy ID or exact source selector). '
                'Proceed only when status is selected. Read the returned entrypoint_path and use its pinned '
                'descriptor for execution. Preserve explicit user model and permission options. '
                'Fallbacks are explicit alternatives, never an automatic retry after execution starts.\n')
            router_files = {selection_root / 'SKILL.md': (body + '\n<!-- claude-profile-sync:adapter sha256=' + digest(body.encode()) + ' -->\n').encode(),
                            selection_root / 'selection.json': (json.dumps(envelope, sort_keys=True, indent=2) + '\n').encode()}
            if not alternatives:
                router_files[selection_root / 'SKILL.md'] = None
            for path, content in router_files.items():
                previous = records.get(str(path))
                if linked(path) or linked(path.parent) or (path.exists() and
                    (previous is None or previous.get('source_id') != 'runtime-policy:tasks')):
                    raise ValueError('existing_selection_entrypoint_preserved')
                active.add(str(path))
                files[path] = content
                if content is None:
                    if previous is not None:
                        records[str(path)] = {**previous, 'retired': True}
                    continue
                records[str(path)] = {'owner': OWNER, 'source_id': 'runtime-policy:tasks',
                    'source': 'explicit-runtime-policy', 'provider': 'user',
                    'original': {'kind': 'file', 'sha256': digest(content)}}
            # The selection file is executable policy evidence and belongs to
            # every alternative's dependency group.
            for group in runtime.get('replacement_groups', []):
                group['members'].update({str(p): {'kind': 'file', 'sha256': digest(b)}
                                         for p, b in router_files.items() if b is not None})

    # Keep the legacy target -> source map readable by older installations.
    for raw_target, raw_source in managed.items():
        if isinstance(raw_source, str) and raw_target not in records:
            target = Path(raw_target)
            if allowed_target(target) and linked(target) and target.resolve() == Path(raw_source).resolve():
                own(target, Path(raw_source), snapshot(target))
    # Older adapters carry self hashes but had no ownership sidecar. Recover
    # provenance only when the complete old template and source layout match.
    for target in sorted(skills.glob("claude-*/SKILL.md")):
        if initial_import and str(target) not in records:
            legacy = legacy_adapter_source(target, claude, catalog)
            if legacy:
                own(target, legacy[0], snapshot(target), legacy[1])
    sources = list(source_catalog.entries(catalog_snapshot, importable=True))
    reserved = set()
    for source_record, entrypoint in sources:
        if runtime is not None and runtime_overlays.key(entrypoint) in runtime['entries']:
            continue
        origin = source_record.get("registry_key") or "user"
        source = Path(entrypoint["path"]).parent
        name = entrypoint["install_name"]
        target = skills / name
        source_entry = source.absolute()
        source = source.resolve()
        active.add(str(target))
        row = {"name": name, "source": str(source), "origin": origin,
               **source_catalog.projection(source_record, entrypoint)}
        if source_record["ownership"] == "external-installer":
            row.update(status="unsupported", reason="external_installer_required")
        elif not entrypoint.get("declared_name") or not entrypoint.get("description"):
            row.update(status="unsupported", reason="missing_skill_frontmatter")
        elif codex and (codex / "skills" / name / "SKILL.md").is_file() and not target.exists():
            row.update(status="conflict", reason="existing_legacy_codex_skill_preserved")
        elif target in reserved:
            row.update(status="conflict", reason="duplicate_source_name")
        elif target.exists() or linked(target):
            row["status"] = "shared" if target.resolve() == source else "conflict"
            same_source = records.get(str(target), {}).get("source_id") == source_record["source_id"]
            if linked(target) and str(target) in managed and target.resolve() != source and same_source:
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
            own(target, source_entry / "SKILL.md", planned_link(source), origin_record=source_record, entrypoint=entrypoint)
        elif row["status"] == "shared" and str(target) in records and not records[str(target)].get("retired"):
            own(target, source_entry / "SKILL.md", snapshot(target), origin_record=source_record, entrypoint=entrypoint)
        rows.append(row)

    # Commands and roles are instruction adapters, not native Claude runtimes.
    providers = source_catalog.plugin_view(catalog_snapshot)
    short_counts = {}
    for key in providers:
        short = key.rsplit("@", 1)[0]
        short_counts[short] = short_counts.get(short, 0) + 1
    for source_record, entrypoint in source_catalog.entries(catalog_snapshot, ("command", "agent_template"), importable=True):
        source = Path(entrypoint["path"])
        plugin = source_record.get("registry_key")
        root = Path(source_record["path"]) if plugin else claude
        origin = plugin.rsplit("@", 1)[0] if plugin else "user"
        if plugin and short_counts[origin] > 1:
            origin = origin + "-" + digest(plugin.encode())[:12]
        kind = "agents" if entrypoint["kind"] == "agent_template" else "commands"
        relative = source.relative_to(root)
        if relative.parts[0] == kind:
            relative = Path(*relative.parts[1:])
        label = relative.with_suffix("").as_posix().replace("/", "-")
        name = slug("claude-" + ("agent-" if kind == "agents" else "") + origin + "-" + label)
        target = skills / name / "SKILL.md"
        active.add(str(target))
        if target.parent in reserved or (target in files and str(target) not in legacy_converted):
            rows.append({"name": name, "status": "conflict", "reason": "duplicate_adapter_name"})
            continue
        reserved.add(target.parent)
        if runtime is not None and runtime_overlays.key(entrypoint) in runtime['entries']:
            deploy_overlay(name, target, runtime['entries'][runtime_overlays.key(entrypoint)])
            continue
        previous_record = records.get(str(target))
        if previous_record is not None:
            incoming_identity = source_catalog.ownership_identity(source_record, entrypoint)
            old_entry = previous_record.get("catalog_identity", {}).get("entrypoint", {})
            new_entry = incoming_identity["catalog_identity"]["entrypoint"]
            identity_fields = ("kind", "relative_path", "client", "scope")
            same_identity = (previous_record.get("source_id") == incoming_identity["source_id"]
                             and all(old_entry.get(key) == new_entry.get(key) for key in identity_fields))
            if not same_identity:
                rows.append({"name": name, "status": "conflict", "reason": "owned_adapter_source_changed",
                             **source_catalog.projection(source_record, entrypoint)})
                continue
        if str(target) in legacy_converted:
            rows.append({'name': name, 'source': str(source), 'status': 'adapted',
                         'reason': 'verified_legacy_identity_converted',
                         **source_catalog.projection(source_record, entrypoint)})
            continue
        desc = entrypoint.get("description") or f"Use the imported {origin} {label} {kind[:-1]} workflow when requested."
        desc = desc[:400]
        intact = False
        if target.is_file() and not linked(target.parent) and not linked(target):
            previous = target.read_bytes()
            intact = bridge_ownership.verify(previous, "adapter")["status"] == "verified"
            intact = intact and str(target) in records and not records[str(target)].get("retired")
        retired = records.get(str(target), {}).get("retired")
        restore_retired = retired and snapshot(target)["kind"] == "missing" and not linked(target.parent)
        if target.parent.exists() and not (intact or restore_retired):
            rows.append({"name": name, "status": "conflict", "reason": "existing_codex_skill_preserved"})
        else:
            generated = render_adapter(name, label, desc, source, root)
            files[target] = generated
            own(target, source, {"kind": "file", "sha256": digest(generated)}, root, source_record, entrypoint)
            rows.append({"name": name, "source": str(source), "status": "adapted", "kind": kind, **source_catalog.projection(source_record, entrypoint)})

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
    original = _plain_member(target) or b""
    verification = _profile_verification({".codex/AGENTS.md": original})
    spans = verification.get("markers", {}).get(".codex/AGENTS.md", [])
    if verification["status"] != "verified" or any(span["kind"] != "instruction" for span in spans):
        return None, {"status": "conflict", "reason": "managed_instructions_were_edited"}
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
    block = (AGENT_START + digest(body.encode()) + " -->\n" + body + AGENT_END).encode("utf-8")
    if spans:
        span = spans[0]
        merged = original[:span["start"]] + block + original[span["end"]:]
    else:
        merged = original + (b"\n\n" if original else b"") + block + b"\n"
    return merged, {"status": "unchanged" if merged == original else "updated", "bytes": len(merged), "source": str(source)}


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
    atomic_replace(path, data)


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
    for name in ("agents", "claude-sync/agents", "imports/claude-memory", "imports/claude-hooks", "memories/extensions/ad_hoc/notes"):
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
                    visit(path)
    return result


def build_plan(claude: Path, codex: Path, skills: Path, *, repair_links=False,
               approved_repos=(), external_manifest: Path | None = None,
               request_id=None, scope=None, periodic=False, runtime_policy=runtime_overlays.OMITTED,
               capabilities=runtime_overlays.OMITTED, selection_requests=(),
               resource_roots=runtime_overlays.OMITTED, role_equivalence=runtime_overlays.OMITTED,
               adopt_playwright=False, reviewed_archive=(), reviewed_scopes=(), route_configured_skills=False):
    values = runtime_overlays.validate_inputs({k: v for k, v in (
        ('runtime_policy', runtime_policy), ('capabilities', capabilities),
        ('resource_roots', resource_roots), ('role_equivalence', role_equivalence))
        if v is not runtime_overlays.OMITTED})
    from profile_bridge.restore_interlock import require_ready
    require_ready(codex)
    catalog_snapshot = source_catalog.discover_profile(claude, codex, skills,
        approved_repos=approved_repos, external_manifest=external_manifest)
    with source_catalog.using_snapshot(catalog_snapshot):
        return _build_plan(claude, codex, skills, repair_links=repair_links,
            approved_repos=approved_repos, external_manifest=external_manifest,
            request_id=request_id, scope=scope, periodic=periodic, catalog_snapshot=catalog_snapshot,
            selection_requests=selection_requests, adopt_playwright=adopt_playwright,
            reviewed_archive=reviewed_archive, reviewed_scopes=reviewed_scopes,
            route_configured_skills=route_configured_skills, **values)


def _build_plan(claude: Path, codex: Path, skills: Path, *, repair_links=False,
               approved_repos=(), external_manifest: Path | None = None,
               request_id=None, scope=None, periodic=False, catalog_snapshot=None, runtime_policy=None,
               capabilities=None, selection_requests=(), resource_roots=None, role_equivalence=None,
               adopt_playwright=False, reviewed_archive=(), reviewed_scopes=(), route_configured_skills=False):
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
    runtime = runtime_overlays.prepare(catalog_snapshot, runtime_policy, capabilities,
        requests=selection_requests, resource_roots=resource_roots, equivalence=role_equivalence)
    runtime_overlays.require_valid_selection({'runtime_selection': runtime})
    plugins, skipped_plugins = enabled_plugins(claude, catalog_snapshot)
    links, files, skill_report = plan_skills(claude, skills, plugins, codex, catalog_snapshot=catalog_snapshot, runtime=runtime)
    config, config_report = plan_config(claude, codex, [root for _, root in plugins], adopt_playwright=adopt_playwright)
    config, config_report['skill_routing'] = plan_skill_routing(config, codex, runtime, adopt=route_configured_skills)
    hook_files, hook_report = plan_hooks(claude, codex, plugin_roots=[root for _, root in plugins])
    if hook_report["requires_hooks_feature"]:
        config, hook_report["feature"] = enable_hooks(config)
        adapted_events = {row['event'] for row in hook_report['hooks']
                          if row.get('status') in {'added', 'updated', 'unchanged'}}
        for item in config_report["settings"]:
            if (item.get("source") == str(claude / "settings.json") and item.get("key") == "hooks"
                and item.get("event") in adapted_events):
                item.update(status="adapted", reason="reviewed_commands_handled_by_native_hooks_adapter; see_hooks_report_for_remaining_commands")
    files.update(hook_files)
    agent_files, config, agent_report = plan_agents(claude, codex, plugins, config, catalog_snapshot=catalog_snapshot,
        llmcall_roles=runtime['roles'])
    files.update(agent_files)
    agent_delete_before = {Path(path): {"kind": "file", "sha256": sha256}
                           for path, sha256 in agent_report["deletion_hashes"].items()}
    files.update({path: None for path in agent_delete_before})
    files[codex / "config.toml"] = config
    instructions, instruction_report = plan_instructions(claude, codex)
    if instructions is not None:
        files[codex / "AGENTS.md"] = instructions
    memory_files, memory_report = plan_memory(claude, codex, request_id=request_id, scope=scope, periodic=periodic,
                                            reviewed_archive=reviewed_archive, reviewed_scopes=reviewed_scopes)
    files.update(memory_files)
    inventory = inventory_sources(claude, codex, skills, approved_repos=approved_repos,
                                  external_manifest=external_manifest, snapshot=catalog_snapshot)
    source_checks = {}
    if repair_links:
        repair_verification, _ = _skill_ownership(codex, skills)
        if repair_verification["status"] != "verified":
            raise ValueError("Skill ownership conflict; link repair withheld")
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
            candidate = recovery["candidates"][0]
            owned[str(target)] = {"owner": OWNER, "source": str(source / "SKILL.md"), "provider": "user",
                                  "provider_root": candidate["repository"].get("root", str(source)),
                                  "original": planned_link(source),
                                  **source_catalog.ownership_identity(candidate["catalog_record"], candidate["catalog_entrypoint"])}
            recovery.update(status="planned", reason="explicit_link_repair_requested")
        if links:
            if managed != read_json(state_path):
                files[state_path] = json.dumps(managed, indent=2).encode()
            if {"version": 1, "items": owned} != read_json(ownership_path):
                files[ownership_path] = json.dumps({"version": 1, "items": owned}, indent=2).encode()
    report = {"tool": "claude-codex-profile-sync", "version": 1,
              "claude_home": str(claude), "codex_home": str(codex), "skills_home": str(skills),
              "skills": skill_report, "plugins_skipped": skipped_plugins,
              "config": config_report, "instructions": instruction_report, "memory": memory_report,
              "hooks": hook_report, "agents": agent_report,
              "inventory": inventory, "catalog": catalog_snapshot, "runtime_selection": runtime_overlays.report(runtime),
              "limitations": ["Claude login tokens and model/provider settings are not portable.",
                              "Reviewed Stop, SessionStart and apply_patch PostToolUse hooks use the native event bridge; unreviewed hooks remain explicit compatibility gaps.",
                              "Directory links and instruction adapters depend on their source installation.",
                              "Native memory consolidation is asynchronous; the searchable archive is immediate."]}
    changes = ProfileChanges()
    changes.memory_intent = memory_files.delivery
    changes.memory_conflict = memory_files.recovery_evidence
    changes.overlay_checks = list({value['artifact_hash']: value for value in runtime.get('source_checks', {}).values()}.values())
    dependencies = []
    for group in runtime.get('replacement_groups', []):
        dependencies.append({**group, 'before': {raw: baseline.get(Path(raw), {'kind': 'missing'})
                                                for raw in group['members']}})
    # These original manifests are part of the complete ownership evidence,
    # including on retirement-only plans with no replacement member changes.
    ownership_before = {str(path): baseline.get(path, {'kind': 'missing'}) for path in
                        (codex / 'claude-sync/managed-artifacts.json', codex / 'claude-sync/managed-skills.json')}
    for path, content in files.items():
        if path.is_relative_to(codex / "memories"):
            continue
        ensure_external(path)
        assert_plain_path(path.parent if content is None else path)
        before = snapshot(path)
        if before != baseline.get(path, {"kind": "missing"}):
            raise ValueError("Destination changed while planning; rerun sync")
        if path in memory_files.archive_before and before != memory_files.archive_before[path]:
            raise ValueError("Memory archive changed while planning; rerun sync")
        if path in agent_delete_before and before != agent_delete_before[path]:
            raise ValueError("Managed agent changed during planning")
        after = {"kind": "missing"} if content is None else {"kind": "file", "sha256": digest(content)}
        if before != after:
            if before["kind"] not in ({"missing", "file", "link"} if content is None else {"missing", "file"}):
                raise ValueError(f"Output is not a regular file: {path}")
            changes.append({"path": str(path), "before": before, "after": after, "data": content,
                            "append_only": path.is_relative_to(codex / "memories")})
            if path in memory_files.retirements:
                changes[-1]['memory_retirement'] = memory_files.retirements[path]
            if path in runtime.get('source_checks', {}):
                changes[-1]['overlay_check'] = runtime['source_checks'][path]
    for path, source in links.items():
        before = snapshot(path)
        if before != baseline.get(path, {"kind": "missing"}):
            raise ValueError("Destination changed while planning; rerun sync")
        change = {"path": str(path), "before": before, "after": planned_link(source), "append_only": False}
        if path in source_checks:
            change["source_check"] = source_checks[path]
        changes.append(change)
    if dependencies:
        required = [group['descriptor']['artifact_hash'] for group in dependencies]
        for row in changes:
            row['required_overlay_checks'] = required
            row['overlay_dependencies'] = dependencies
            row['overlay_ownership_before'] = ownership_before
        report['required_overlay_checks'] = {row['path']: required for row in changes}
    if runtime.get('legacy_source_checks'):
        for row in changes:
            row['legacy_source_checks'] = runtime['legacy_source_checks']
            row['legacy_ownership_before'] = ownership_before
    report["changes"] = [{k: v for k, v in row.items() if k not in {"data", "overlay_check", "overlay_dependencies"}} for row in changes]
    report['overlay_preconditions'] = [{k: item.get(k) for k in ('source_id', 'source_hash',
        'source_record_hash', 'transform_version', 'artifact_hash', 'resources')} for item in changes.overlay_checks]
    report["change_count"] = len(changes)
    return changes, report


class ProfileChanges(list):
    memory_intent = None
    memory_conflict = None
    overlay_checks = ()


@contextmanager
def destination_lock(codex: Path, skills: Path | None = None, *, create=True):
    root = codex / "claude-sync"
    assert_plain_path(root)
    skills = skills if skills is not None else codex.parent / '.agents/skills'
    assert_plain_path(skills)
    with profile_locks(codex, skills, create=create):
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
    with destination_lock(codex, skills):
        return _rollback_locked(backup, codex, skills)


def _rollback_locked(backup: Path, codex: Path, skills: Path) -> dict:
    base = codex / "claude-sync/backups"
    if not backup.resolve().is_relative_to(base.resolve()):
        raise ValueError("Backup must be inside this Codex home's claude-sync/backups")
    manifest = read_json(backup / "manifest.json")
    if manifest.get("codex_home") != str(codex) or manifest.get("skills_home") != str(skills):
        raise ValueError("Backup destination roots do not match this invocation")
    result = {"restored": [], "preserved": []}
    for note in manifest.get("memory_notes", []):
        result["preserved"].append({"path": note, "reason": "append_only_memory_note"})
    for row in reversed(manifest["changes"]):
        path = Path(row["path"])
        if not (path.is_relative_to(codex) or path.is_relative_to(skills)) or ".." in path.parts:
            raise ValueError("Unsafe path in backup manifest")
        if memory_archive.preserve_on_rollback(row):
            result["preserved"].append({"path": str(path), "reason": "quarantined_memory_evidence"})
            continue
        if path.is_relative_to(memory_outbox.state_root(codex)) or path == memory_outbox.authorization_path(codex):
            result["preserved"].append({"path": str(path), "reason": "durable_memory_history"})
            continue
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
    for check in row.get('legacy_source_checks', []):
        verify_source({'source_check': check})
    overlay = row.get('overlay_check')
    if overlay is not None:
        from skill_smith.overlays import validate
        if not validate(overlay, target_runtime='codex'):
            raise ValueError('Overlay source, resource or transform changed; rerun sync')
    check = row.get("source_check")
    if check:
        path = Path(check["path"])
        if str(path.resolve()) != check["resolved"] or not path.is_file() or digest(path.read_bytes()) != check["sha256"]:
            raise ValueError("Recovery source changed during planning; rerun sync")


def apply_plan(changes: list, report: dict, codex: Path, skills: Path):
    runtime_overlays.require_valid_selection(report)
    with destination_lock(codex, skills):
        return _apply_plan_locked(changes, report, codex, skills)


def _overlay_dependencies(changes, report, codex, skills):
    """Verify full original and projected groups using the T08 authority."""
    groups, expected = {}, {}
    for row in changes:
        required = report.get('required_overlay_checks', {}).get(row['path'], row.get('required_overlay_checks', []))
        if report.get('overlay_preconditions') and not required:
            raise ValueError('Required overlay dependency evidence missing')
        supplied = row.get('overlay_dependencies', [])
        if required and (row.get('required_overlay_checks') != required or
                         [g['descriptor']['artifact_hash'] for g in supplied] != required or
                         not row.get('overlay_ownership_before')):
            raise ValueError('Required overlay dependency evidence missing')
        for group in supplied:
            identity = group['descriptor']['artifact_hash']
            if identity in groups:
                if groups[identity] != group:
                    raise ValueError('Inconsistent overlay dependency evidence')
                continue
            verify_source({'overlay_check': group['descriptor']})
            if not group.get('members') or set(group['before']) != set(group['members']):
                raise ValueError('Required overlay dependency evidence missing')
            groups[identity] = group
            expected.update(group['before'])
        expected.update(row.get('overlay_ownership_before', {}))
        expected.update(row.get('legacy_ownership_before', {}))
    if not groups and not any(row.get('legacy_ownership_before') for row in changes):
        return {}, {}
    for raw, before in expected.items():
        path = Path(raw)
        if not path.is_absolute() or '..' in path.parts or not (path.is_relative_to(skills) or path.is_relative_to(codex)):
            raise ValueError('Unsafe overlay dependency path')
        assert_plain_path(path)
        if snapshot(path) != before:
            raise ValueError('Overlay replacement changed during planning; rerun sync')
    verification, members = _skill_ownership(codex, skills)
    if verification['status'] != 'verified':
        raise ValueError('Overlay replacement ownership conflict')
    projected = dict(members)
    # Use the already verified original manifest bytes, including legacy-only
    # installations. Link facts are evidence inputs, not new ownership rules.
    link_paths = set(json.loads(members.get('.codex/claude-sync/managed-skills.json', b'{}')))
    artifact = json.loads(members.get('.codex/claude-sync/managed-artifacts.json', b'{}'))
    for raw, record in artifact.get('items', {}).items():
        if not record.get('retired') and record.get('original', {}).get('kind') == 'link':
            link_paths.add(raw)
    links = {Path(raw).relative_to(codex.parent).as_posix(): snapshot(Path(raw)) for raw in link_paths}
    for row in changes:
        path = Path(row['path'])
        if not (path.is_relative_to(skills) or path.is_relative_to(codex / 'skills') or
                path in {codex / 'claude-sync/managed-artifacts.json', codex / 'claude-sync/managed-skills.json'}):
            continue
        member = path.relative_to(codex.parent).as_posix()
        projected.pop(member, None)
        links.pop(member, None)
        if row['after']['kind'] == 'file':
            projected[member] = row['data']
        elif row['after']['kind'] == 'link':
            links[member] = row['after']
    if _profile_verification(projected, home=codex.parent, links=links)['status'] != 'verified':
        raise ValueError('Overlay replacement projected ownership conflict')
    for group in groups.values():
        for raw, after in group['members'].items():
            member = Path(raw).relative_to(codex.parent).as_posix()
            if member not in projected or {'kind': 'file', 'sha256': digest(projected[member])} != after:
                raise ValueError('Overlay replacement dependency missing from plan')
    return groups, expected


def _apply_plan_locked(changes: list, report: dict, codex: Path, skills: Path):
    from skill_smith.overlays import validate
    for descriptor in getattr(changes, 'overlay_checks', ()):
        if not validate(descriptor, target_runtime='codex'):
            raise ValueError('Overlay source, resource or transform changed; rerun sync')
    dependency_groups, dependency_states = _overlay_dependencies(changes, report, codex, skills)
    intent = getattr(changes, 'memory_intent', None)
    conflict = getattr(changes, 'memory_conflict', None)
    if intent and intent.codex != codex:
        raise ValueError('Memory plan destination does not match apply destination')
    if not changes:
        if conflict:
            memory_outbox.preserve_conflict(codex, conflict, skills=skills)
        if intent:
            memory_outbox.prepare(intent, skills=skills)
            report['memory']['delivery'] = memory_outbox.deliver(intent, skills=skills)
            if report['memory']['delivery'].get('unresolved'):
                report['memory']['status'] = 'partial'
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
        if path.is_relative_to(codex / "memories"):
            raise ValueError("Native delivery requires a memory outbox intent")
        if after == "file" and (before == "link" or digest(row["data"]) != row["after"]["sha256"]):
            raise ValueError("Invalid file publication")
        if after == "link" and before not in {"missing", "link"}:
            raise ValueError("Only links may be relinked")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = codex / "claude-sync/backups" / stamp
    assert_plain_path(backup)
    backup.mkdir(parents=True)
    manifest = {"codex_home": str(codex), "skills_home": str(skills), "changes": []}
    if intent:
        manifest['memory_notes'] = [str(codex / 'memories/extensions/ad_hoc/notes' / f"claude-memory-{intent.record['increment_id']}.md")]

    # Back up and verify every destination before changing any destination.
    for i, row in enumerate(changes):
        path = Path(row["path"])
        if snapshot(path) != row["before"]:
            raise ValueError("Destination changed during planning; rerun sync")
        entry = {k: v for k, v in row.items() if k not in {
            "data", "overlay_check", "overlay_dependencies", "overlay_ownership_before", "required_overlay_checks"}}
        if row["before"]["kind"] == "file":
            entry["backup_file"] = f"{i:05d}.bin"
            data = path.read_bytes()
            if digest(data) != row["before"]["sha256"]:
                raise ValueError("Destination changed while backing up; rerun sync")
            atomic_write(backup / entry["backup_file"], data)
        manifest["changes"].append(entry)
    atomic_write(backup / "manifest.json", json.dumps(manifest, indent=2).encode())
    if intent:
        memory_outbox.prepare(intent, skills=skills)
    elif conflict:
        memory_outbox.preserve_conflict(codex, conflict, skills=skills)
    try:
        # Native delivery follows all reversible profile/archive changes.
        replacement_members = {raw for group in dependency_groups.values() for raw in group['members']}
        for row in sorted(changes, key=lambda x: (x["append_only"], x['path'] not in replacement_members)):
            path = Path(row["path"])
            if snapshot(path) != row["before"]:
                raise ValueError("Destination changed before publication; rerun sync")
            verify_source(row)
            if row['path'] not in replacement_members:
                for raw, expected in dependency_states.items():
                    if snapshot(Path(raw)) != expected:
                        raise ValueError('Overlay replacement changed before publication')
                for group in dependency_groups.values():
                    verify_source({'overlay_check': group['descriptor']})
                    if any(snapshot(Path(raw)) != after for raw, after in group['members'].items()):
                        raise ValueError('Overlay replacement must be installed before dependent mutation')
            for descriptor in getattr(changes, 'overlay_checks', ()):
                if not validate(descriptor, target_runtime='codex'):
                    raise ValueError('Overlay source, resource or transform changed before publication')
            if memory_archive.apply_retirement(row, codex, backup):
                pass
            elif row["after"]["kind"] == "link":
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
                if path == codex / 'imports/claude-memory/index.md':
                    memory_outbox.checkpoint('index')
            if snapshot(path) != row["after"]:
                raise ValueError("Post-write verification failed")
            if str(path) in dependency_states:
                dependency_states[str(path)] = row['after']
        if intent:
            report['memory']['delivery'] = memory_outbox.deliver(intent, skills=skills)
            if report['memory']['delivery'].get('unresolved'):
                report['memory']['status'] = 'partial'
        report.update(status="applied", backup=str(backup))
        atomic_write(backup / "report.json", json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"))
    except Exception:
        rollback(backup, codex, skills)
        raise
    return report


def add_runtime_input_arguments(parser):
    """Expose explicit runtime files shared by the direct and scheduled CLIs."""
    parser.add_argument('--runtime-policy', type=Path, help='Explicit private runtime selection policy')
    parser.add_argument('--capability-snapshot', type=Path, help='Explicit supported/unverified capability observations')
    parser.add_argument('--overlay-resource-roots', type=Path, help='Explicit source_id to approved resource-root mapping')
    parser.add_argument('--role-equivalence', type=Path, help='Verified exact-artifact role equivalence observations')
    parser.add_argument('--adopt-playwright', action='store_true', help='Adopt only native Playwright launch arguments into the isolated shared-state policy')
    parser.add_argument('--route-configured-skills', action='store_true', help='Route configured task skills through llmcall-tasks and withhold their raw discovery entries')
    parser.add_argument('--reviewed-memory-archive', type=Path, help='Exact-byte reviewed archive adoption/quarantine decisions')
    parser.add_argument('--reviewed-memory-scopes', type=Path, help='Evidence-backed historical project scope decisions')


def load_runtime_inputs(args):
    """Read only explicitly supplied files, once, before entering a writer."""
    values = runtime_overlays.validate_inputs({name: json.loads(path.read_text(encoding='utf-8-sig'),
        object_pairs_hook=_unique_json_object) for name, path in (
        ('runtime_policy', args.runtime_policy), ('capabilities', args.capability_snapshot),
        ('resource_roots', args.overlay_resource_roots), ('role_equivalence', args.role_equivalence)) if path is not None})
    if getattr(args, 'adopt_playwright', False):
        values['adopt_playwright'] = True
    if getattr(args, 'route_configured_skills', False):
        values['route_configured_skills'] = True
    for name, flag in (('reviewed_archive', 'reviewed_memory_archive'), ('reviewed_scopes', 'reviewed_memory_scopes')):
        path = getattr(args, flag, None)
        if path is not None:
            rows = json.loads(path.read_text(encoding='utf-8-sig'), object_pairs_hook=_unique_json_object)
            if not isinstance(rows, list):
                raise ValueError('reviewed_memory_input_must_be_array')
            values[name] = rows
    return values


def _unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate_input_key')
        result[key] = value
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--claude-home", type=Path, default=Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")))
    parser.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")))
    parser.add_argument("--skills-home", type=Path, default=Path.home() / ".agents/skills")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="Apply changes after backup (default is zero-write preview)")
    mode.add_argument("--dry-run", action="store_true", help="Explicit zero-write preview")
    mode.add_argument("--rollback", type=Path, help="Restore unchanged config/files from a backup; never delete memory notes")
    parser.add_argument("--json", action="store_true", help="Print catalog and compatibility metadata without workflow bodies")
    parser.add_argument("--repair-links", action="store_true", help="Plan recovery of broken links with exact previously owned approved sources")
    parser.add_argument("--approved-repo", action="append", type=Path, default=[], help="Additional authoritative local skill checkout (repeatable)")
    parser.add_argument("--external-skill-manifest", type=Path, help="External external-skill-repos.json source manifest")
    parser.add_argument('--request-id', help='Explicit memory ingress authorization identifier')
    parser.add_argument('--scope', action='append', help='Authorized project key, or * (repeatable)')
    parser.add_argument('--periodic', action='store_true', help='Persist explicit periodic memory authorization')
    add_runtime_input_arguments(parser)
    args = parser.parse_args(argv)
    claude, codex, skills = [p.absolute() for p in (args.claude_home, args.codex_home, args.skills_home)]
    from profile_bridge.restore_interlock import recovery_status
    recovery = recovery_status(codex)
    if recovery:
        print(json.dumps(recovery))
        return 2
    try:
        runtime_args = load_runtime_inputs(args)
        for root in (codex, skills):
            ensure_external(root)
            assert_plain_path(root)
        if args.rollback:
            with destination_lock(codex, skills):
                report = {"status": "rolled_back", **rollback(args.rollback.absolute(), codex, skills)}
        elif args.apply:
            with destination_lock(codex, skills):
                changes, report = build_plan(claude, codex, skills, repair_links=args.repair_links, approved_repos=args.approved_repo, external_manifest=args.external_skill_manifest, request_id=args.request_id, scope=args.scope, periodic=args.periodic, **runtime_args)
                report = apply_plan(changes, report, codex, skills)
        else:
            changes, report = build_plan(claude, codex, skills, repair_links=args.repair_links, approved_repos=args.approved_repo, external_manifest=args.external_skill_manifest, request_id=args.request_id, scope=args.scope, periodic=args.periodic, **runtime_args)
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
