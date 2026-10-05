"""Profile ownership formats and dependency validation, independent of storage.

Only verified original groups may receive replacement hashes. Callers provide
bytes and explicit path mappings; no filesystem or catalog reads occur here.
"""
import base64
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import tomllib


AGENTS = "claude-codex-profile-sync-agents"
HOOKS = "claude-codex-profile-sync-stop-hooks"
OWNER = "claude-codex-profile-sync"
ZERO = "0" * 64
BLOCK = re.compile(rb"(?:^|(?<=\A\xef\xbb\xbf))# BEGIN (?P<owner>claude[-a-z:]+) (?P<name>[a-z0-9-]+) sha256=(?P<hash>[a-f0-9]{64})\r?\n(?P<body>.*?)^# END (?P=owner) (?P=name)(?:\r?\n|$)", re.M | re.S)
INSTRUCTION = re.compile(rb"<!-- claude-profile-sync:begin sha256=(?P<hash>[a-f0-9]{64}) -->\n(?P<body>.*?)<!-- claude-profile-sync:end -->", re.S)
ADAPTER = re.compile(rb"\A(?P<body>.*)\n<!-- claude-profile-sync:adapter sha256=(?P<hash>[a-f0-9]{64}) -->\n\Z", re.S)
ROLE = re.compile(rb"\A# Managed by claude-codex-profile-sync-agents; source_sha256=(?P<source>[a-f0-9]{64})\n# instructions_sha256=(?P<hash>[a-f0-9]{64})\n(?P<body>.*)\Z", re.S)
PATTERNS = (BLOCK, INSTRUCTION, ADAPTER, ROLE)
COMMENT = re.compile(r"# (?:(?:BEGIN|END) claude[-a-z:]+ [a-z0-9-]+(?: sha256=[a-f0-9]{64})?|Managed by claude[-a-z:]+; source_sha256=[a-f0-9]{64}|instructions_sha256=[a-f0-9]{64})\Z")


def comment_metadata(comment):
    """Classify a preservable ownership comment for structural redaction."""
    if COMMENT.fullmatch(comment):
        return True, None
    if comment.startswith("# claude-profile-source: "):
        return True, json.loads(comment.split(": ", 1)[1])
    return False, None


def _marker_document(path):
    return path == "document" or PurePosixPath(path).name in {"AGENTS.md", "AGENTS.override.md", "SKILL.md"} or path.endswith(".toml")


def opaque_path(path):
    """Memory evidence is outside ordinary profile ownership and transformation."""
    return path.startswith((".codex/memories/", ".codex/claude-sync/memory-"))


class OwnershipConflict(ValueError):
    pass


def digest(data):
    return hashlib.sha256(data).hexdigest()


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def object_hash(value):
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":")).encode())


def map_paths(content, paths):
    """Map complete explicit path prefixes once, including escaped Windows paths."""
    if not isinstance(paths, dict):
        raise OwnershipConflict("invalid_path_mapping")
    pairs = {}
    for source, target in paths.items():
        if not isinstance(source, str) or not isinstance(target, str) or not source or not target:
            raise OwnershipConflict("invalid_path_mapping")
        source, target = source.replace("\\", "/").rstrip("/"), target.replace("\\", "/").rstrip("/")
        for path in (source, target):
            if (not (path.startswith("/") or re.match(r"^[A-Za-z]:/", path)) or
                    any(part in {".", ".."} for part in path.split("/")) or
                    any(char in path for char in ('\x00', '"', "'"))):
                raise OwnershipConflict("mapping_requires_absolute_path_prefixes")
        if source == target or os.name == "nt" and source.casefold() == target.casefold():
            continue
        for a, b in ((source, target), (source.replace("/", "\\"), target.replace("/", "\\")),
                     (source.replace("/", "\\\\"), target.replace("/", "\\\\"))):
            pairs[a.encode()] = b.encode()
    if not pairs:
        return content
    pattern = re.compile(b"(?:" + b"|".join(re.escape(k) for k in sorted(pairs, key=len, reverse=True)) + rb")(?=[/\\\"'\s]|$)", re.I if os.name == "nt" else 0)
    lookup = {k.lower() if os.name == "nt" else k: v for k, v in pairs.items()}
    return pattern.sub(lambda m: lookup[m[0].lower() if os.name == "nt" else m[0]], content)


def _markers(data):
    matches = [m for pattern in PATTERNS for m in pattern.finditer(data)]
    ordered = sorted(matches, key=lambda m: m.start())
    if any(a.end() > b.start() for a, b in zip(ordered, ordered[1:])):
        raise OwnershipConflict("overlapping_ownership_markers")
    names = [(m.re.pattern, m["name"] if m.re is BLOCK else b"") for m in matches]
    if len(names) != len(set(names)):
        raise OwnershipConflict("duplicate_ownership_markers")
    for match in matches:
        if match["hash"] == ZERO.encode() or digest(match["body"]).encode() != match["hash"]:
            raise OwnershipConflict("marker_modified")
        if match.re is BLOCK:
            if match["owner"].decode() not in {OWNER, AGENTS}:
                raise OwnershipConflict("unknown_marker_owner")
            # Markers inside a multiline TOML string are not ownership.
            tomllib.loads(data[:match.start()].decode("utf-8-sig"))
            tomllib.loads(match["body"].decode())
    residue = data
    for match in sorted(matches, key=lambda m: m.start(), reverse=True):
        residue = residue[:match.start()] + residue[match.end():]
    if re.search(rb"(?:<!-- claude-profile-sync:|# (?:BEGIN|END|Managed by) claude[-a-z:]+|# instructions_sha256=)", residue):
        raise OwnershipConflict("unknown_or_malformed_marker")
    return sorted(matches, key=lambda m: m.start())


def _marker_spans(matches):
    kinds = {BLOCK: "config", INSTRUCTION: "instruction", ADAPTER: "adapter", ROLE: "role"}
    spans = []
    for match in matches:
        item = {"kind": kinds[match.re], "start": match.start(), "end": match.end(),
                "body_start": match.start("body"), "body_end": match.end("body")}
        if match.re is BLOCK:
            item.update(owner=match["owner"].decode(), name=match["name"].decode())
        spans.append(item)
    return spans


def _member_path(value, home=None):
    if not isinstance(value, str) or not value or "\x00" in value:
        raise OwnershipConflict("invalid_dependency_path")
    value = value.replace("\\", "/")
    if home:
        prefix = str(home).replace("\\", "/").rstrip("/") + "/"
        if value.casefold().startswith(prefix.casefold()):
            value = value[len(prefix):]
    if value.startswith("/") or ":" in value or any(part in {"", ".", ".."} for part in value.split("/")):
        raise OwnershipConflict("dependency_path_outside_profile")
    return value


def _member_identity(value, home=None):
    member = _member_path(value, home)
    windows = os.name == "nt" or home and (re.match(r"^[A-Za-z]:", str(home)) or str(home).startswith("\\\\"))
    return member.casefold() if windows else member


def _member_index(members, links, home):
    index = {}
    for path in list(members) + list(links):
        identity = _member_identity(path, home)
        if identity in index:
            raise OwnershipConflict("ambiguous_dependency_path")
        index[identity] = path
    return index


def _artifact_rows(obj, home):
    if (not isinstance(obj, dict) or set(obj) != {"version", "items"} or
            type(obj["version"]) is not int or obj["version"] != 1 or not isinstance(obj["items"], dict)):
        raise OwnershipConflict("unknown_artifact_schema")
    rows = {}
    for target, record in obj["items"].items():
        identity = _member_identity(target, home)
        if identity in rows:
            raise OwnershipConflict("ambiguous_artifact_path")
        rows[identity] = (target, record)
    return rows


def _ownership_json(data):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise OwnershipConflict("duplicate_ownership_key")
            obj[key] = value
        return obj
    return json.loads(data, object_pairs_hook=unique)


def _mapped_artifact_record(record, mapping):
    # Only path-bearing fields can follow a path mapping. Source IDs, hashes,
    # provider names and all other provenance remain authority, not text.
    expected = json.loads(encoded(record))
    for field in ("source", "provider_root"):
        if field in expected:
            expected[field] = json.loads(map_paths(encoded(expected[field]), mapping))
    state = expected.get("original", {})
    if state.get("kind") == "link" and "target" in state:
        state["target"] = json.loads(map_paths(encoded(state["target"]), mapping))
    if "catalog_identity" in expected:
        catalog = expected["catalog_identity"]
        if isinstance(catalog, dict) and "scope_path" in catalog:
            catalog["scope_path"] = json.loads(map_paths(encoded(catalog["scope_path"]), mapping))
        if isinstance(catalog, dict) and isinstance(catalog.get("origin"), dict) and "root" in catalog["origin"]:
            catalog["origin"]["root"] = json.loads(map_paths(encoded(catalog["origin"]["root"]), mapping))
    return expected


def _artifact_pairs(original_obj, changed_obj, detail, home, transformed_home, mapping):
    changed_rows = _artifact_rows(changed_obj, transformed_home)
    expected_rows = {}
    for target, record in original_obj["items"].items():
        relative = _member_path(target, home)
        absolute = str(home).replace("\\", "/").rstrip("/") + "/" + relative if home else target
        mapped = map_paths(absolute.encode(), mapping).decode()
        identity = _member_identity(mapped, transformed_home)
        if identity in expected_rows:
            raise OwnershipConflict("ambiguous_artifact_mapping")
        expected_rows[identity] = (record, detail[target])
    if changed_rows.keys() != expected_rows.keys():
        raise OwnershipConflict("artifact_membership_changed")
    pairs = []
    for identity, (_, row) in changed_rows.items():
        record, member = expected_rows[identity]
        if row != _mapped_artifact_record(record, mapping):
            raise OwnershipConflict("artifact_authority_changed")
        pairs.append((row, member))
    return pairs


def _link_target(value):
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    value = value.replace("\\", "/")
    return value.casefold() if os.name == "nt" else value


def _remap_hook_launcher(command, mapping):
    """Relocate the encoded argv of an already-verified owned Windows hook."""
    if not isinstance(command, str) or not mapping:
        return command
    match = re.fullmatch(r"(.* -EncodedCommand )([A-Za-z0-9+/=]+)", command)
    if not match:
        return command
    try:
        decoded = base64.b64decode(match[2], validate=True).decode("utf-16-le")
        mapped = map_paths(decoded.encode("utf-8"), mapping).decode("utf-8")
        return match[1] + base64.b64encode(mapped.encode("utf-16-le")).decode("ascii")
    except (ValueError, UnicodeError) as exc:
        raise OwnershipConflict("invalid_encoded_hook_launcher") from exc


def _groups(members, home, links):
    """Return (manifest, dependencies, kind, detail), validating original bytes."""
    groups = []
    for path, data in members.items():
        if not ((path.startswith(".codex/claude-sync/managed-") and path.endswith(".json")) or path == ".codex/imports/claude-hooks/manifest.json"):
            continue
        dependencies = {path}
        kind, detail, reason = "unknown", None, None
        try:
            obj = _ownership_json(data)
            if path.endswith("/managed-agents.json"):
                kind = "agents"
                dependencies.add(".codex/config.toml")
                if set(obj) != {"owner", "version", "roles", "roles_sha256"} or obj["owner"] != AGENTS or type(obj["version"]) is not int or obj["version"] != 1 or not isinstance(obj["roles"], dict) or obj["roles_sha256"] != digest(encoded(obj["roles"])):
                    raise OwnershipConflict("invalid_agents_manifest")
                config = members[".codex/config.toml"]
                parsed = tomllib.loads(config.decode("utf-8-sig"))
                blocks = {m["name"].decode(): m for m in _markers(config)
                          if m.re is BLOCK and m["owner"].decode() == AGENTS}
                if blocks.keys() != obj["roles"].keys():
                    raise OwnershipConflict("agents_manifest_role_set_mismatch")
                detail = {}
                for name, record in obj["roles"].items():
                    fields = {"category", "plugin", "relative", "source", "source_sha256", "role_sha256", "config_sha256"}
                    if set(record) != fields or not all(isinstance(v, str) for v in record.values()):
                        raise OwnershipConflict("unknown_role_schema")
                    block = blocks[name]
                    if digest(block[0]) != record["config_sha256"]:
                        raise OwnershipConflict("role_config_modified_or_missing")
                    entry = tomllib.loads(block["body"].decode())
                    if set(entry) != {"agents"} or set(entry["agents"]) != {name} or entry["agents"][name] != parsed.get("agents", {}).get(name):
                        raise OwnershipConflict("role_config_shape")
                    role_path = ".codex/" + _member_path(entry["agents"][name]["config_file"])
                    dependencies.add(role_path)
                    role = members[role_path]
                    marker = ROLE.fullmatch(role)
                    if not marker or digest(role) != record["role_sha256"] or marker["source"].decode() != record["source_sha256"]:
                        raise OwnershipConflict("role_modified_or_missing")
                    _markers(role)
                    if any(n != name and isinstance(v, dict) and v.get("config_file") == entry["agents"][name]["config_file"] for n, v in parsed["agents"].items()):
                        raise OwnershipConflict("role_has_manual_alias")
                    detail[name] = role_path
            elif path.endswith("/claude-hooks/manifest.json"):
                kind = "hooks"
                bridge = ".codex/imports/claude-hooks/stop_bridge.py"
                dependencies.update({bridge, ".codex/hooks.json"})
                body = {k: v for k, v in obj.items() if k != "manifest_sha256"}
                version = obj.get("version")
                fields = {"owner", "version", "bridge_sha256", "source", "jobs"}
                fields.add("handler_sha256" if version == 1 else "handlers")
                if obj.get("owner") != HOOKS or type(version) is not int or version not in (1, 2) or set(body) - fields:
                    raise OwnershipConflict("unknown_hook_schema")
                if version == 2 and (set(body) != fields or "manifest_sha256" not in obj):
                    raise OwnershipConflict("unknown_hook_schema")
                if version == 2 and (
                        not isinstance(obj["source"], str) or not isinstance(obj["jobs"], list) or
                        any(not isinstance(job, dict) or set(job) != {"source", "source_sha256"} or
                            not isinstance(job["source"], str) or not isinstance(job["source_sha256"], str) or
                            not re.fullmatch(r"[a-f0-9]{64}", job["source_sha256"]) for job in obj["jobs"])):
                    raise OwnershipConflict("unknown_hook_schema")
                if "manifest_sha256" in obj and object_hash(body) != obj["manifest_sha256"]:
                    raise OwnershipConflict("hook_manifest_modified")
                if digest(members[bridge]) != obj.get("bridge_sha256"):
                    raise OwnershipConflict("hook_bridge_modified")
                hooks = _ownership_json(members[".codex/hooks.json"])["hooks"]
                if not isinstance(hooks, dict) or any(not isinstance(v, list) for v in hooks.values()):
                    raise OwnershipConflict("unknown_hook_groups")
                handlers = {"Stop": obj.get("handler_sha256")} if version == 1 else obj["handlers"]
                if (not isinstance(handlers, dict) or not handlers or
                        set(handlers) - {"Stop", "SessionStart", "PostToolUse"} or
                        any(not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for v in handlers.values())):
                    raise OwnershipConflict("unknown_hook_schema")
                detail = {}
                for event, fingerprint in handlers.items():
                    indexes = [i for i, g in enumerate(hooks.get(event, [])) if isinstance(g, dict) and isinstance(g.get("hooks"), list) and object_hash(g) == fingerprint]
                    if len(indexes) != 1:
                        raise OwnershipConflict("hook_group_modified_missing_or_duplicate")
                    detail[event] = indexes[0]
                if version == 1:
                    detail = detail["Stop"]
            elif path.endswith("/managed-artifacts.json"):
                kind = "artifacts"
                _artifact_rows(obj, home)
                member_index = _member_index(members, links, home)
                detail = {}
                for target, record in obj["items"].items():
                    known_fields = {"owner", "source", "provider", "plugin", "provider_root", "source_sha256",
                                    "original", "retired", "source_id", "catalog_identity"}
                    if not isinstance(record, dict) or set(record) - known_fields:
                        raise OwnershipConflict("unknown_artifact_dependencies")
                    identity = _member_identity(target, home)
                    member = member_index.get(identity, _member_path(target, home))
                    dependencies.add(member)
                    expected = record.get("original", {})
                    if record.get("owner") != OWNER:
                        raise OwnershipConflict("unknown_artifact_owner")
                    if record.get("retired"):
                        if member in members or member in links:
                            raise OwnershipConflict("retired_artifact_recreated")
                        dependencies.remove(member)
                    elif expected.get("kind") == "file":
                        if set(expected) != {"kind", "sha256"} or digest(members[member]) != expected.get("sha256"):
                            raise OwnershipConflict("artifact_modified_or_missing")
                        _markers(members[member])
                    elif expected.get("kind") == "link":
                        actual = links[member]
                        if (set(expected) - {"kind", "target", "link_type", "directory"} or
                                set(actual) != set(expected) or any(actual[k] != expected[k] for k in expected if k != "target") or
                                _link_target(actual["target"]) != _link_target(expected["target"])):
                            raise OwnershipConflict("artifact_link_changed")
                    else:
                        raise OwnershipConflict("unknown_artifact_kind")
                    detail[target] = member
            elif path.endswith("/managed-skills.json") and isinstance(obj, dict):
                kind, detail = "legacy-links", {}
                for target, source in obj.items():
                    if not isinstance(source, str):
                        raise OwnershipConflict("unknown_legacy_link_schema")
                    member = _member_path(target, home)
                    dependencies.add(member)
                    if _link_target(links[member]["target"]) != _link_target(source):
                        raise OwnershipConflict("legacy_link_changed")
            else:
                raise OwnershipConflict("unknown_dependency_group")
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            reason = str(exc) if isinstance(exc, OwnershipConflict) else "malformed_or_missing_dependency"
        opaque = {dep for dep in dependencies if opaque_path(dep)}
        if opaque:
            reason = reason or "opaque_dependency_not_owned"
            # A rejected authority cannot mutate memory or use it to spread
            # invalidation into another ownership group.
            dependencies -= opaque
        groups.append((path, dependencies, kind, detail, reason))
    return groups


def verify_members(members, *, home=None, links=None):
    conflicts, invalid, matches = [], set(), {}
    for path, data in members.items():
        if opaque_path(path):
            continue
        if not _marker_document(path):
            continue
        try:
            matches[path] = _markers(data)
        except (ValueError, UnicodeError) as exc:
            invalid.add(path)
            conflicts.append({"path": path, "reason": str(exc) if isinstance(exc, OwnershipConflict) else "malformed_marker_document"})
    groups = _groups(members, home, links or {})
    hook_manifest = ".codex/imports/claude-hooks/manifest.json"
    hook_bridge = ".codex/imports/claude-hooks/stop_bridge.py"
    if hook_bridge in members and hook_manifest not in members:
        invalid.add(hook_bridge)
        conflicts.append({"path": hook_bridge, "reason": "missing_hook_dependency_group"})
    agents = [detail for _, _, kind, detail, reason in groups if kind == "agents" and not reason]
    for path, markers in matches.items():
        uncovered = any(
            m.re is ROLE and not any(path in detail.values() for detail in agents) or
            m.re is BLOCK and m["owner"].decode() == AGENTS and not any(
                path == ".codex/config.toml" and m["name"].decode() in detail for detail in agents)
            for m in markers)
        if uncovered:
            invalid.add(path)
            conflicts.append({"path": path, "reason": "missing_agents_dependency_group"})
    for path, dependencies, kind, detail, reason in groups:
        if reason or dependencies & invalid:
            invalid.update(dependencies)
            conflicts.append({"path": path, "reason": reason or "dependency_conflict"})
    # Shared dependencies propagate conflict through all groups.
    while True:
        previous = set(invalid)
        for _, deps, _, _, _ in groups:
            if deps & invalid:
                invalid.update(deps)
        if previous == invalid:
            break
    return {"status": "conflict" if conflicts else "verified", "conflicts": conflicts,
            "invalid": sorted(invalid), "groups": groups,
            "markers": {path: _marker_spans(items) for path, items in matches.items() if items}}


def _invalidate(data, manifest=False, legacy_links=False):
    if manifest:
        try:
            def poison(obj):
                if isinstance(obj, dict):
                    return {k: ZERO if k.endswith("sha256") else "ownership-conflict" if k == "owner" else poison(v) for k, v in obj.items()}
                if isinstance(obj, list):
                    return [poison(v) for v in obj]
                return obj
            obj = poison(json.loads(data))
            if legacy_links and isinstance(obj, dict):
                # Legacy readers recognize strings as target-to-source grants.
                obj = {k: {"unverified_source": v} if isinstance(v, str) else v for k, v in obj.items()}
            if isinstance(obj, dict):
                obj["ownership_conflict"] = True
            return encoded(obj)
        except ValueError:
            return data
    return re.sub(rb"(?<=sha256=)[a-f0-9]{64}", ZERO.encode(), data)


def remap_members(original, changed, *, home=None, links=None, path_mapping=None, transformed_home=None):
    """Return transformed bytes and conflicts, poisoning invalid ownership.

    Invalid source markers are checked before redaction. Even if redaction
    removes a user edit, the resulting document cannot regain ownership.
    Member keys remain source-relative. Explicit path_mapping authorizes path
    fields only; transformed_home selects the namespace for changed targets.
    """
    report = verify_members(original, home=home, links=links)
    mapping = {} if path_mapping is None else path_mapping
    map_paths(b"", mapping)  # Validate even when the group has no path fields.
    if transformed_home is None:
        transformed_home = map_paths(str(home).encode(), mapping).decode() if home else home
    invalid = set(report["invalid"])
    result = dict(changed)
    for path, deps, _, _, _ in report["groups"]:
        if not deps <= (changed.keys() | (links or {}).keys()):
            invalid.update(deps)
            report["conflicts"].append({"path": path, "reason": "dependency_omitted"})
    for path, data in list(result.items()):
        if path not in original:
            raise OwnershipConflict("new_member_not_authorized")
        if opaque_path(path):
            result[path] = original[path]
            continue
        manifest = any(g[0] == path for g in report["groups"])
        if path in invalid:
            result[path] = _invalidate(data, manifest, path.endswith("/managed-skills.json"))
            continue
        if not _marker_document(path):
            continue
        old = _markers(original[path])
        new = [m for pattern in PATTERNS for m in pattern.finditer(data)]
        new.sort(key=lambda m: m.start())
        if len(old) != len(new) or any(
                a.re is not b.re or a.re is BLOCK and (a["owner"], a["name"]) != (b["owner"], b["name"])
                or a.re is ROLE and a["source"] != b["source"] for a, b in zip(old, new)):
            invalid.add(path)
            result[path] = _invalidate(data, manifest)
            report["conflicts"].append({"path": path, "reason": "marker_structure_changed"})
            continue
        for match in reversed(new):
            data = data[:match.start("hash")] + digest(match["body"]).encode() + data[match.end("hash"):]
        result[path] = data
    for path, deps, kind, detail, _ in report["groups"]:
        if path not in result:
            continue
        if deps & invalid:
            for dep in deps & result.keys():
                if not opaque_path(dep):
                    result[dep] = _invalidate(result[dep], dep == path, dep.endswith("/managed-skills.json"))
            continue
        obj = _ownership_json(result[path])
        if kind == "agents":
            for name, role_path in detail.items():
                block = next(m for m in BLOCK.finditer(result[".codex/config.toml"]) if m["name"].decode() == name)
                obj["roles"][name].update(role_sha256=digest(result[role_path]), config_sha256=digest(block[0]))
            obj["roles_sha256"] = digest(encoded(obj["roles"]))
        elif kind == "hooks":
            old_manifest = _ownership_json(original[path])
            if old_manifest["version"] == 2 and (
                    not isinstance(obj, dict) or set(obj) != set(old_manifest) or
                    any(obj.get(k) != old_manifest[k] for k in ("owner", "version", "handlers", "bridge_sha256")) or
                    not isinstance(obj.get("jobs"), list) or len(obj["jobs"]) != len(old_manifest["jobs"]) or
                    any(not isinstance(a, dict) or set(a) != set(b) or a.get("source_sha256") != b["source_sha256"]
                        for a, b in zip(obj["jobs"], old_manifest["jobs"]))):
                result[path] = _invalidate(result[path], True)
                report["conflicts"].append({"path": path, "reason": "hook_authority_changed"})
                continue
            old_hooks = json.loads(original[".codex/hooks.json"])["hooks"]
            new_document = json.loads(result[".codex/hooks.json"])
            new_hooks = new_document["hooks"]
            if old_hooks.keys() != new_hooks.keys() or any(len(old_hooks[k]) != len(new_hooks[k]) for k in old_hooks):
                result[path] = _invalidate(result[path], True)
                report["conflicts"].append({"path": path, "reason": "hook_membership_changed"})
                continue
            event_indexes = {"Stop": detail} if old_manifest["version"] == 1 else detail
            for event, index in event_indexes.items():
                for handler in new_hooks[event][index]["hooks"]:
                    if "commandWindows" in handler:
                        handler["commandWindows"] = _remap_hook_launcher(handler["commandWindows"], mapping)
            result[".codex/hooks.json"] = encoded(new_document)
            obj["bridge_sha256"] = digest(result[".codex/imports/claude-hooks/stop_bridge.py"])
            if obj["version"] == 1:
                obj["handler_sha256"] = object_hash(new_hooks["Stop"][detail])
            else:
                obj["handlers"] = {event: object_hash(new_hooks[event][index]) for event, index in detail.items()}
            if "manifest_sha256" in obj:
                obj["manifest_sha256"] = object_hash({k: v for k, v in obj.items() if k != "manifest_sha256"})
        elif kind == "artifacts":
            pairs = _artifact_pairs(json.loads(original[path]), obj, detail, home, transformed_home, mapping)
            for row, member in pairs:
                if not row.get("retired") and row["original"]["kind"] == "file":
                    row["original"]["sha256"] = digest(result[member])
        result[path] = encoded(obj)
    return result, report["conflicts"]


def verify(document: bytes, kind: str) -> dict:
    """Verify a standalone marker, or a UTF-8 profile envelope of hex bytes."""
    try:
        if kind == "profile":
            obj = json.loads(document)
            report = verify_members({k: bytes.fromhex(v) for k, v in obj["members"].items()}, home=obj.get("home"), links=obj.get("links"))
            result = {k: report[k] for k in ("status", "conflicts", "invalid", "markers")}
            result["groups"] = [{"manifest": path, "members": sorted(deps), "kind": group_kind,
                                 "status": "conflict" if deps & set(report["invalid"]) else "verified"}
                                for path, deps, group_kind, _, _ in report["groups"]]
            return result
        patterns = {"instruction": INSTRUCTION, "adapter": ADAPTER, "config": BLOCK}
        if kind not in patterns:
            raise OwnershipConflict("dependency_group_required_or_unknown_kind")
        markers = _markers(document)
        if not markers or any(m.re is not patterns[kind] for m in markers):
            raise OwnershipConflict("unexpected_marker_kind")
        if any(m.re is BLOCK and m["owner"].decode() == AGENTS for m in markers):
            raise OwnershipConflict("dependency_group_required")
        return {"status": "verified", "conflicts": [], "markers": _marker_spans(markers)}
    except (ValueError, KeyError, TypeError, UnicodeError, AttributeError):
        return {"status": "conflict", "conflicts": [{"reason": "invalid_or_incomplete_ownership"}]}


def remap(document: bytes, kind: str, mapping: dict) -> bytes:
    if verify(document, kind)["status"] != "verified":
        raise OwnershipConflict("original_ownership_conflict")
    if kind == "profile":
        obj = json.loads(document)
        members = {k: bytes.fromhex(v) for k, v in obj["members"].items()}
        target_home = map_paths(obj.get("home", "").encode(), mapping).decode()
        def target_key(key):
            if not obj.get("home") or opaque_path(key):
                return key
            absolute = obj["home"].replace("\\", "/").rstrip("/") + "/" + _member_path(key, obj["home"])
            return _member_path(map_paths(absolute.encode(), mapping).decode(), target_home)
        changed = {k: map_paths(v, mapping) if not opaque_path(k) else v for k, v in members.items()}
        # Relative manifest targets need the same explicit absolute mapping as
        # their member keys when a mapping relocates a subtree within the home.
        for path, data in members.items():
            if not path.endswith(("/managed-artifacts.json", "/managed-skills.json")):
                continue
            manifest = _ownership_json(data)
            rows = manifest["items"] if path.endswith("/managed-artifacts.json") else manifest
            mapped_rows = {}
            for key, value in rows.items():
                relative = _member_path(key, obj.get("home"))
                target = target_key(key) if key.replace("\\", "/") == relative else map_paths(key.encode(), mapping).decode()
                if target in mapped_rows:
                    raise OwnershipConflict("ambiguous_artifact_mapping")
                mapped_rows[target] = json.loads(map_paths(encoded(value), mapping))
            if path.endswith("/managed-artifacts.json"):
                manifest["items"] = mapped_rows
            else:
                manifest = mapped_rows
            changed[path] = encoded(manifest)
        result, conflicts = remap_members(members, changed, home=obj.get("home"), links=obj.get("links"), path_mapping=mapping)
        if conflicts:
            raise OwnershipConflict("remap_dependency_conflict")
        mapped_members = {}
        for key, value in result.items():
            target = target_key(key)
            if target in mapped_members:
                raise OwnershipConflict("ambiguous_member_mapping")
            mapped_members[target] = value.hex()
        output = {"home": target_home, "members": mapped_members}
        if "links" in obj:
            mapped_links = {}
            for key, value in obj["links"].items():
                target = target_key(key)
                if target in mapped_links:
                    raise OwnershipConflict("ambiguous_link_mapping")
                mapped_links[target] = json.loads(map_paths(encoded(value), mapping))
            output["links"] = mapped_links
        _member_index(output["members"], output.get("links", {}), target_home)
        rendered = encoded(output)
        if verify(rendered, "profile")["status"] != "verified":
            raise OwnershipConflict("remap_dependency_conflict")
        return rendered
    result, conflicts = remap_members({"document": document}, {"document": map_paths(document, mapping)})
    if conflicts:
        raise OwnershipConflict("remap_marker_conflict")
    return result["document"]
