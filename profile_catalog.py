"""Startup requests and lossless compatibility views of skill-smith snapshots.

Only this adapter chooses profile defaults. Discovery, source identity, plugin
selection and metadata parsing belong to skill_smith.catalog. A scoped snapshot
also serves legacy consumers that cannot yet accept an explicit snapshot argument.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import os
from pathlib import Path

from skill_smith import catalog
from skill_smith.paths import normalize


_CURRENT = ContextVar("profile_catalog_snapshot", default=None)
REQUIRED_FEATURES = {"workflow_roots", "plugin_descriptors", "plugin_metadata_paths"}


def startup_request(claude, codex=None, skills=None, *, approved_repos=(),
                    external_manifest=None, plugins=None, private_bindings=None,
                    runtime_discovery=None):
    claude = Path(claude).absolute()
    home = claude.parent
    codex = Path(codex).absolute() if codex is not None else home / ".codex"
    skills = Path(skills).absolute() if skills is not None else home / ".agents/skills"
    if external_manifest is None:
        config = os.environ.get("CLAUDE_CONFIG_REPO")
        if config:
            external_manifest = Path(config) / "external-skill-repos.json"
    approved = [str(home), *(str(Path(p).absolute()) for p in approved_repos)]
    request = {
        "profile_home": str(home),
        "approved_roots": approved,
        "skill_roots": [
            {"path": str(path), "namespace": namespace, "client": client,
             "scope": "user", "max_depth": 8}
            for path, namespace, client in ((claude / "skills", "claude-user", "claude"),
                (codex / "skills", "codex-user", "codex"), (skills, "shared-user", "shared"))
        ] + [{"path": str(Path(p).absolute()), "namespace": "approved:" + normalize(p),
              "max_depth": 8} for p in approved_repos],
        "plugin_registries": [{"path": str(claude / "plugins/installed_plugins.json"),
            "settings_path": str(claude / "settings.json"), "client": "claude", "scope": "user",
            "approved_roots": approved}],
        "workflow_roots": [{"path": str(claude / folder), "kind": kind,
                            "namespace": "claude-user:" + folder, "client": "claude", "scope": "user"}
                           for folder, kind in (("commands", "command"), ("agents", "agent_template"))],
    }
    if external_manifest is not None:
        request["external_skill_repos"] = str(external_manifest)
    if plugins is not None:
        request["plugin_descriptors"] = [{"name": name, "path": str(Path(path).absolute())}
                                          for name, path in sorted(set(plugins))]
    for field, path in (("private_bindings", private_bindings), ("runtime_discovery", runtime_discovery)):
        if path is not None:
            request[field] = str(path)
    return request


def discover_profile(claude, codex=None, skills=None, **kwargs):
    return discover_snapshot(startup_request(claude, codex, skills, **kwargs))


def discover_snapshot(request):
    missing = REQUIRED_FEATURES - getattr(catalog, "CONSUMER_FEATURES", set())
    if missing:
        raise RuntimeError("skill-smith consumer interface missing: " + ", ".join(sorted(missing)))
    return require_snapshot(catalog.discover(request))


def require_snapshot(snapshot):
    if type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise ValueError("catalog_schema_unsupported")
    return snapshot


@contextmanager
def using_snapshot(snapshot):
    token = _CURRENT.set(require_snapshot(snapshot))
    try:
        yield snapshot
    finally:
        _CURRENT.reset(token)


def get_snapshot(claude, snapshot=None, **kwargs):
    if snapshot is not None:
        return require_snapshot(snapshot)
    current = _CURRENT.get()
    return current if current is not None else discover_profile(claude, **kwargs)


def current_snapshot():
    return _CURRENT.get()


def projection(record, entrypoint=None):
    """Keep the complete producer record instead of flattening status to a bool."""
    result = {"source_id": record["source_id"], "catalog_record": deepcopy(record)}
    if entrypoint is not None:
        result["catalog_entrypoint"] = deepcopy(entrypoint)
    return result


def diagnostic_projection(record, entrypoint=None):
    """Preserve identity/status in the legacy prose-free agent diagnostic view.

    Complete records remain in the build plan's catalog envelope. Descriptions
    are intentionally excluded from this older diagnostic API.
    """
    result = projection(record, entrypoint)
    for entry in result["catalog_record"]["entrypoints"]:
        entry.pop("description", None)
    if "catalog_entrypoint" in result:
        result["catalog_entrypoint"].pop("description", None)
    return result


def ownership_identity(record, entry):
    """Persist stable source identity; observation timestamps stay in reports."""
    return {"source_id": record["source_id"], "catalog_identity": {
        **{key: deepcopy(record.get(key)) for key in
           ("kind", "origin", "registry_key", "version", "source_hash", "client", "scope", "scope_path")},
        "entrypoint": {key: entry.get(key) for key in
                       ("kind", "install_name", "relative_path", "source_hash", "client", "scope")},
    }}


def legacy_adapter_identity(previous, snapshot):
    """Match a complete legacy plugin record by exact origin and entry layout.

    The caller must first verify the original target and full ownership group.
    Missing modern fields are the only schema conversion allowed here.
    """
    fields = {'owner', 'source', 'provider', 'plugin', 'provider_root', 'source_sha256', 'original'}
    if (set(previous) != fields or previous.get('provider') != 'plugin'
        or not all(isinstance(previous.get(k), str) and previous[k] for k in fields - {'original'})
        or not isinstance(previous['original'], dict)
        or set(previous['original']) != {'kind', 'sha256'}
        or previous['original']['kind'] != 'file'):
        return None
    source, root = Path(previous['source']), Path(previous['provider_root'])
    if (not source.is_absolute() or not root.is_absolute() or '..' in source.parts
        or '..' in root.parts or not source.is_relative_to(root)
        or not source.resolve().is_relative_to(root.resolve())):
        return None
    relative = source.relative_to(root).as_posix()
    kind = {'commands': 'command', 'agents': 'agent_template'}.get(relative.split('/')[0])
    if kind is None:
        return None
    matches = [(record, entry) for record, entry in entries(snapshot, (kind,), importable=True)
        if record['kind'] == 'plugin' and record.get('registry_key') == previous['plugin']
        and record.get('status', {}).get('enabled') == 'yes'
        and record.get('origin', {}).get('type') == 'plugin'
        and record.get('origin', {}).get('marketplace') == previous['plugin'].rsplit('@', 1)[-1]
        and entry.get('relative_path') == relative
        and entry.get('source_hash') == 'sha256:' + previous['source_sha256']]
    if len(matches) != 1:
        return None
    if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != previous['source_sha256']:
        return None
    record, entry = matches[0]
    current_root, current = Path(record['path']), Path(entry['path'])
    # A cache generation can move, but neither marketplace identity nor entry
    # layout/content can change while adopting a legacy ownership record.
    if (not current_root.is_absolute() or not current.is_absolute()
        or '..' in current_root.parts or '..' in current.parts
        or not current.is_relative_to(current_root)
        or current.relative_to(current_root).as_posix() != relative
        or not current.resolve().is_relative_to(current_root.resolve())
        or record.get('resolved_path') != str(current_root.resolve())
        or entry.get('resolved_path') != str(current.resolve())
        or not current.is_file()
        or hashlib.sha256(current.read_bytes()).hexdigest() != previous['source_sha256']):
        return None
    return ownership_identity(*matches[0])


def plugin_view(snapshot):
    require_snapshot(snapshot)
    result = {}
    for record in snapshot["records"]:
        if record["kind"] != "plugin" or record.get("client") != "claude" or record.get("scope") != "user" or record.get("scope_path"):
            continue
        dimensions = record["status"]
        if dimensions["enabled"] == "no":
            status, reason = "disabled", "disabled_in_claude"
        elif dimensions["enabled"] != "yes" and not record.get("explicit_selection"):
            status, reason = "unavailable", "plugin_selection_unavailable"
        elif record.get("resolution") == "ambiguous":
            status, reason = "unavailable", "missing_or_ambiguous_user_install"
        elif dimensions["resolved"] != "yes":
            status, reason = "unavailable", "plugin_install_unavailable"
        elif any(p.get("source_id") == record["source_id"] and p["reason"] in {
                "invalid_or_unreadable_plugin_manifest", "entrypoint unreadable",
                "directory unreadable", "unreadable", "declared_plugin_path_unavailable"}
                 for p in snapshot["problems"]):
            status, reason = "unavailable", "plugin_metadata_unavailable"
        else:
            status = "available"
            reason = "enabled_user_install" if dimensions["enabled"] == "yes" else "explicit_user_install"
        result[record["registry_key"]] = {
            "name": record["registry_key"], "roots": [record["path"]] if record.get("path") else [],
            "status": status, "reason": reason, **projection(record),
        }
    return result


def entries(snapshot, kinds=("skill",), *, importable=False):
    providers = plugin_view(snapshot)
    for record in snapshot["records"]:
        if importable and record["kind"] == "plugin":
            provider = providers.get(record["registry_key"])
            if not provider or provider["source_id"] != record["source_id"] or provider["status"] != "available":
                continue
        for entrypoint in record["entrypoints"]:
            if entrypoint["kind"] not in kinds:
                continue
            if importable and (entrypoint.get("client") != "claude" or entrypoint.get("scope") != "user"):
                continue
            yield record, entrypoint


def match_source(snapshot, source):
    """Exact paths may transfer identity. Names never transfer ownership."""
    source = Path(source)
    paths = {normalize(source), normalize(source.resolve())}
    if source.name != "SKILL.md":
        paths.update((normalize(source / "SKILL.md"), normalize(source.resolve() / "SKILL.md")))
    matches = [(record, entry) for record, entry in entries(snapshot, ("skill", "command", "agent_template"))
               if paths.intersection(normalize(p) for p in (entry.get("path"), entry.get("resolved_path")) if p)]
    lexical = {normalize(source)}
    if source.name != "SKILL.md":
        lexical.add(normalize(source / "SKILL.md"))
    exact = [(record, entry) for record, entry in matches if normalize(entry["path"]) in lexical]
    if exact:
        matches = exact
    identities = {record["source_id"] for record, _ in matches}
    return matches[0] if len(identities) == 1 else None


def skill_paths(snapshot, root, *, include_missing=False):
    root = Path(root).absolute()
    found = {}
    for record, entry in entries(snapshot):
        path = Path(entry["path"]).parent
        if path.is_relative_to(root):
            found[str(path)] = path
    if include_missing:
        for record in snapshot["records"]:
            if record["kind"] == "skill":
                for mount in record["mounts"]:
                    path = Path(mount["path"])
                    if path.is_relative_to(root) and mount["resolved"] != "yes":
                        found[str(path)] = path
    return sorted(found.values())
