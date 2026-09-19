"""Plan native Codex roles without writing profiles or invoking any agents.

Contract verified against Codex CLI 0.154.0's AgentRoleToml and
AgentRoleOverrides deserializers. Config entries accept description and
config_file. The referenced override supports developer_instructions; permission
and tool allowlists are NOT role overrides in this runtime. Consequently only
roles without Claude execution restrictions are imported. Reviewer read-only
instructions are advisory, and all roles inherit native permissions and routing.

Only the caller persists this plan, including its separate ownership manifest
and verified deletion paths. All generated content is private profile data.
"""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import textwrap
import tomllib

import profile_catalog as source_catalog
from profile_bridge import overlays as runtime_overlays
from profile_config import _agent_members, _profile_verification, _Block


_OWNER = "claude-codex-profile-sync-agents"
_EXECUTION_FIELDS = {
    "permissionMode", "maxTurns", "skills", "mcpServers", "hooks", "memory",
    "background", "isolation", "permission_mode", "allowed-tools",
}
_REVIEW = re.compile(r"review|analy[sz]|audit|validat|hunter|inspect", re.I)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json(value: object) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse(text: str) -> dict:
    return tomllib.loads(text.removeprefix("\ufeff"))


def _plain(path: Path) -> bool:
    """Do not manage symlinks, junctions, or paths inside reparse points."""
    for part in (path, *path.parents):
        try:
            if part.is_symlink() or getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
                return False
        except FileNotFoundError:
            continue
    return True


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _object(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("expected_object")
    return value


def _name(category: str, plugin: str, relative: str) -> str:
    identity = _json([category, plugin, relative])
    label = (plugin.split("@")[0] if category == "plugin" else "user") + "-" + Path(relative).stem
    slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")[:44].rstrip("-")
    return "claude-" + slug + "-" + _hash(identity)[:12]


def _load_manifest(path: Path) -> dict:
    """Legacy facade: only a complete bridge-verified group grants records."""
    codex = path.parent.parent
    config = _read(codex / "config.toml") or b""
    members = _agent_members(codex, config)
    if _profile_verification(members)["status"] != "verified":
        raise ValueError("invalid_or_incomplete_agents_group")
    data = members.get(".codex/claude-sync/managed-agents.json")
    return json.loads(data)["roles"] if data is not None else {}


def _frontmatter(data: bytes) -> tuple[dict, str]:
    """Parse a deliberately small YAML subset; never execute YAML constructors.

    Scalars, indented continuations and literal/folded description blocks cover
    supported roles. Collections and execution metadata fail closed. A richer
    YAML document requires explicit translation, not a guessed interpretation.
    """
    text = data.decode("utf-8-sig").replace("\r\n", "\n")
    match = re.match(r"\A---\n(.*?)\n---(?:\n|$)(.*)\Z", text, re.DOTALL)
    if not match:
        raise ValueError("missing_or_invalid_frontmatter")
    groups: dict[str, list[str]] = {}
    key = None
    for line in match[1].splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        if line.startswith((" ", "\t")):
            if key is None:
                raise ValueError("unsupported_frontmatter_syntax")
            groups[key].append(line)
            continue
        field = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*):[ \t]*(.*)", line)
        if not field or field[1] in groups:
            raise ValueError("unsupported_frontmatter_syntax")
        key = field[1]
        groups[key] = [field[2]]
    if {"tools", "disallowedTools"} & groups.keys():
        raise ValueError("tool_restrictions_not_enforceable")
    if _EXECUTION_FIELDS & groups.keys():
        raise ValueError("claude_execution_metadata_not_supported")
    if groups.keys() - {"name", "description", "model", "color"}:
        raise ValueError("unknown_frontmatter_fields")
    metadata = {}
    for key, lines in groups.items():
        first = lines[0]
        if first in {"|", "|-", "|+", ">", ">-", ">+"}:
            value = textwrap.dedent("\n".join(lines[1:]))
            if first.startswith(">"):
                value = " ".join(value.splitlines())
        else:
            value = " ".join(part.strip() for part in lines)
            if value.startswith('"'):
                value = json.loads(value)
            elif value.startswith("'"):
                if not re.fullmatch(r"'(?:[^']|'')*'", value):
                    raise ValueError("unsupported_frontmatter_syntax")
                value = value[1:-1].replace("''", "'")
            elif value.startswith(("[", "{", "&", "*", "!", "|", ">")):
                raise ValueError("unsupported_frontmatter_syntax")
            else:
                value = value.split(" #", 1)[0].rstrip()
        if not isinstance(value, str):
            raise ValueError("unsupported_frontmatter_syntax")
        metadata[key] = value
    if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", metadata.get("name", ""))
            or not metadata.get("description", "").strip() or not match[2].strip()):
        raise ValueError("role_name_description_and_instructions_required")
    return metadata, match[2].lstrip("\n")


def _instructions(body: str, source: Path, root: Path, reviewer: bool) -> str:
    body = body.replace("${CLAUDE_PLUGIN_ROOT}", root.as_posix()).replace("$CLAUDE_PLUGIN_ROOT", root.as_posix())

    def link(match: re.Match) -> str:
        target = match[2]
        if re.match(r"[A-Za-z][A-Za-z0-9+.-]*:|/|#", target):
            return match[0]
        path, separator, fragment = target.partition("#")
        resolved = (source.parent / path).resolve().as_posix()
        return match[1] + "<" + resolved + (separator + fragment) + ">" + match[3]

    # Explicit Markdown links have file-relative semantics. Ordinary code paths
    # and CLAUDE.md references remain relative to the user's working project.
    body = re.sub(r"(\]\()<?([^<>\s)]+)>?(\))", link, body)
    guidance = (
        "Codex adaptation rules for this imported role:\n"
        "Honor the current Codex model, provider, routing, approval and permission defaults.\n"
        'Any workflow step spawning or calling an external model agent must use llmcall.call(..., mode="agent"), '
        "inheriting llmcall routing; do not start provider CLIs directly.\n"
        "This does not create or rename internal tools. Use only tools actually available in this session; "
        "report unavailable capabilities rather than simulate them. Claude tool names in the source describe intent, "
        "not a native tool allowlist. These adaptation rules govern how the source workflow is carried out.\n"
        f"Source file: {source.as_posix()}\nSource resource directory: {source.parent.as_posix()}\n"
        f"Plugin/user resource root: {root.as_posix()}\n"
        "Resolve source resource references against those directories; project code paths stay relative to the working project.\n"
    )
    if reviewer:
        guidance += (
            "Perform this review as read-only: inspect and report, and do not edit files or run mutating commands. "
            "This is a behavioral instruction, not an enforced permission boundary; native permissions remain inherited.\n"
        )
    return guidance + "\nImported role instructions:\n\n" + body


def _collect(claude: Path, plugins: list[tuple[str, Path]], report: dict, *, catalog_snapshot=None) -> tuple[list[dict], dict]:
    snapshot = source_catalog.get_snapshot(claude, catalog_snapshot, plugins=plugins)
    providers = source_catalog.plugin_view(snapshot)
    settings = {key: item["catalog_record"]["status"]["enabled"] == "yes"
                for key, item in providers.items()
                if item["catalog_record"]["status"]["enabled"] in {"yes", "no"}}
    for problem in snapshot["problems"]:
        if problem["stage"] in {"plugin_registries", "plugin_descriptors", "workflow_roots"}:
            report["warnings"].append({"reason": problem["reason"], "source": problem.get("path")})
    sources = []
    for record, entry in source_catalog.entries(snapshot, ("agent_template",), importable=True):
        category = "plugin" if record["kind"] == "plugin" else "user"
        plugin = record["registry_key"] or ""
        root = Path(record["path"]) if category == "plugin" else claude
        source = Path(entry["path"])
        relative = source.relative_to(root).as_posix()
        sources.append(dict(name=_name(category, plugin, relative), category=category,
            plugin=plugin, relative=relative, source=source, root=root,
            **source_catalog.projection(record, entry)))
    return sources, settings


def _without_role(parsed: dict, name: str) -> dict:
    result = deepcopy(parsed)
    roles = result.get("agents")
    if isinstance(roles, dict):
        roles.pop(name, None)
        if not roles:
            result.pop("agents", None)
    return result


def _owned_block(text: str, name: str, record: dict, path: Path, codex: Path,
                 *, verified=None, original: bytes | None = None) -> _Block | None:
    """Locate an authorized original block; never authorize from replacement hashes."""
    if verified is None:
        original = text.encode("utf-8")
        verified = _profile_verification(_agent_members(codex, original))
    if verified["status"] != "verified" or not _plain(path):
        return None
    spans = [span for span in verified["markers"].get(".codex/config.toml", [])
             if span["kind"] == "config" and span["owner"] == _OWNER and span["name"] == name]
    if len(spans) != 1 or original is None:
        return None
    span = spans[0]
    block = original[span["start"]:span["end"]].decode("utf-8")
    start = text.find(block)
    if start < 0 or text.find(block, start + 1) >= 0:
        return None
    # Destination identity and resolved aliases are writer policy. The bridge
    # has already verified all original role/config/manifest bytes together.
    entries = _parse(text).get("agents", {})
    reference = entries.get(name, {}).get("config_file")
    if not isinstance(reference, str) or (codex / reference).resolve() != path.resolve():
        return None
    for other, entry in entries.items():
        alias = entry.get("config_file") if isinstance(entry, dict) else None
        if other != name and isinstance(alias, str) and (codex / alias).resolve() == path.resolve():
            return None
    return _Block(start, start + len(block),
                  original[span["body_start"]:span["body_end"]].decode("utf-8"), True)


def plan_agents(claude: Path, codex: Path, plugins: list[tuple[str, Path]], config: bytes, *, catalog_snapshot=None, llmcall_roles=None) -> tuple[dict[Path, bytes], bytes, dict]:
    """Return changed files, merged config bytes, and a prose-free report.

    ``plugins`` are the caller's selected user installations, preferably keyed
    by the full plugin@marketplace identity. Legacy short names are qualified
    only with unambiguous settings/install evidence. No live profile is written.
    ``report['deletions']`` contains absolute, plain, hash-verified role paths;
    apply these with the same snapshot/race checks and rollback as other changes.
    A missing install or source never implies explicit disablement.
    """
    claude, codex = Path(claude).absolute(), Path(codex).absolute()
    manifest_path = codex / "claude-sync/managed-agents.json"
    report = dict(agents=[], warnings=[], deletions=[], deletion_hashes={}, registered=0, changed=False,
                  contract="codex-cli-0.154.0", counts={}, native_agent_tool_availability="inherited_not_enabled")

    def fail(reason: str):
        report["warnings"].append({"reason": reason})
        return {}, config, report

    try:
        text = config.decode("utf-8")
        parsed = _parse(text)
        if not isinstance(parsed.get("agents", {}), dict):
            return fail("existing_agents_is_not_a_table")
        if not _plain(codex / "config.toml"):
            return fail("linked_codex_config")
    except (OSError, ValueError, UnicodeError):
        return fail("existing_codex_config_invalid_or_unreadable")
    try:
        members = _agent_members(codex, config)
        verified = _profile_verification(members)
        if verified["status"] != "verified":
            report["agents"].append({"status": "conflict", "reason": "managed_role_or_entry_modified_missing_or_unowned"})
            return fail("managed_agents_dependency_conflict")
        manifest_bytes = members.get(".codex/claude-sync/managed-agents.json")
        records = json.loads(manifest_bytes)["roles"] if manifest_bytes is not None else {}
        # A verified record may only address this writer's deterministic output.
        for name, entry in records.items():
            if name != _name(entry["category"], entry["plugin"], entry["relative"]):
                return fail("managed_agents_destination_identity_conflict")
            path = codex / "claude-sync/agents" / (name + ".toml")
            if _owned_block(text, name, entry, path, codex, verified=verified, original=config) is None:
                report["agents"].append({"status": "conflict", "reason": "managed_role_destination_or_alias_conflict"})
                return fail("managed_agents_destination_or_alias_conflict")
    except (OSError, ValueError, UnicodeError):
        return fail("managed_agents_manifest_invalid_or_unreadable")
    # Native standalone role files are auto-discovered by current Codex. They
    # win collisions even when no [agents.<name>] table exists in config.toml.
    native_names = set()
    try:
        native_dir = codex / "agents"
        if native_dir.exists():
            for path in native_dir.rglob("*.toml"):
                native = _parse(path.read_text(encoding="utf-8-sig"))
                if isinstance(native.get("name"), str):
                    native_names.add(native["name"])
    except (OSError, ValueError, UnicodeError):
        return fail("native_role_inventory_unreadable")

    sources, settings = _collect(claude, plugins, report, catalog_snapshot=catalog_snapshot)
    next_records = deepcopy(records)
    files: dict[Path, bytes] = {}
    newline = "\r\n" if "\r\n" in text else "\n"
    visited = set()

    def retire(name: str, row: dict, reason: str) -> bool:
        nonlocal text
        path = codex / "claude-sync/agents" / (name + ".toml")
        try:
            block = _owned_block(text, name, records[name], path, codex, verified=verified, original=config)
            if block is None or name in native_names:
                row.update(status="conflict", reason="managed_role_or_entry_modified_missing_or_unowned")
                return False
            candidate = text[:block.start] + text[block.end:]
            if _without_role(_parse(candidate), name) != _without_role(_parse(text), name):
                raise ValueError("unsafe_removal")
        except (OSError, ValueError, UnicodeError):
            row.update(status="conflict", reason="managed_role_removal_could_not_be_verified")
            return False
        text = candidate
        del next_records[name]
        report["deletions"].append(str(path))
        report["deletion_hashes"][str(path)] = records[name]["role_sha256"]
        row.update(status="removed", reason=reason)
        return True

    for source in sources:
        name = source["name"]
        visited.add(name)
        row = {key: source[key] for key in ("name", "category", "plugin")}
        row["source"] = str(source["source"])
        row.update(source_catalog.diagnostic_projection(source["catalog_record"], source["catalog_entrypoint"]))
        report["agents"].append(row)
        if source.get("ambiguous"):
            row.update(status="conflict", reason="multiple_installations_for_role")
            continue
        entry = source['catalog_entrypoint']
        if source['category'] == 'plugin' and runtime_overlays.known_role(source['catalog_record'], entry):
            replacement = (llmcall_roles or {}).get(runtime_overlays.key(entry), {})
            if replacement.get('status') == 'ready' and replacement.get('equivalent'):
                row.update(status='llmcall', reason='verified_template_entrypoint',
                           destination=replacement['destination'], artifact_hash=replacement['artifact_hash'])
                if name in records:
                    retire(name, row, 'equivalent_llmcall_template_installed_in_same_plan')
            else:
                row.update(status='preserved' if name in records else 'unsupported',
                           reason='llmcall_template_equivalence_or_capability_unverified')
            continue
        try:
            data = source["source"].read_bytes()
        except OSError:
            row.update(status="preserved" if name in records else "unsupported", reason="agent_source_unreadable")
            continue
        try:
            metadata, body = _frontmatter(data)
        except (ValueError, UnicodeError) as exc:
            # Only our fixed reason codes may leave the parser. JSON and Unicode
            # exception messages may contain arbitrary private frontmatter.
            reason = str(exc) if type(exc) is ValueError else "unsupported_frontmatter_syntax"
            row.update(status="unsupported", reason=reason)
            if name in records and reason in {"tool_restrictions_not_enforceable", "claude_execution_metadata_not_supported", "unknown_frontmatter_fields"}:
                retire(name, row, "source_execution_restrictions_no_longer_supported")
            continue
        path = codex / "claude-sync/agents" / (name + ".toml")
        try:
            if not _plain(path):
                raise ValueError("linked_role_output")
            block = _owned_block(text, name, records[name], path, codex, verified=verified, original=config) if name in records else None
            if name in native_names:
                raise ValueError("native_codex_role_preserved")
            if name in records and block is None:
                raise ValueError("managed_role_or_entry_modified_missing_or_unowned")
            if name not in records and (name in _parse(text).get("agents", {}) or path.exists()):
                raise ValueError("native_codex_role_or_file_preserved")
            reviewer = bool(_REVIEW.search(source["catalog_entrypoint"]["name"]))
            instructions = _instructions(body, source["source"], source["root"], reviewer)
            role_body = "developer_instructions = " + _quote(instructions) + "\n"
            role = (f"# Managed by {_OWNER}; source_sha256={_hash(data)}\n"
                    f"# instructions_sha256={_hash(role_body.encode())}\n" + role_body).encode("utf-8")
            entry = newline.join([f"[agents.{_quote(name)}]",
                                  "description = " + _quote(metadata["description"]),
                                  "config_file = " + _quote(path.relative_to(codex).as_posix()), ""])
            rendered = (f"# BEGIN {_OWNER} {name} sha256={_hash(entry.encode())}{newline}"
                        + entry + f"# END {_OWNER} {name}{newline}")
            if block:
                candidate = text[:block.start] + rendered + text[block.end:]
            else:
                candidate = text + ("" if not text or text.endswith("\n") else newline) + rendered
            if _without_role(_parse(candidate), name) != _without_role(_parse(text), name):
                raise ValueError("unsafe_config_merge")
            _parse(role.decode())
            current = _read(path)
        except (OSError, ValueError, UnicodeError):
            row.update(status="conflict", reason="native_or_edited_role_preserved_or_merge_unavailable")
            continue
        status = "added" if name not in records else "updated" if role != current or candidate != text else "unchanged"
        if current != role:
            files[path] = role
        text = candidate
        next_records[name] = {
            "category": source["category"], "plugin": source["plugin"], "relative": source["relative"],
            "source": str(source["source"]), "source_sha256": _hash(data), "role_sha256": _hash(role),
            "config_sha256": _hash(rendered.encode()),
        }
        row.update(status=status, role_sha256=_hash(role), config_sha256=_hash(rendered.encode()),
                   caveats=["native_permissions_and_model_defaults_inherited", "claude_tool_names_are_not_native_capabilities"])
        if "model" in metadata:
            row["caveats"].append("claude_model_alias_not_imported")
        if reviewer:
            row["caveats"].append("review_permissions_are_advisory")
        report["registered"] += 1

    for name, record in sorted(records.items()):
        if name in visited:
            continue
        row = {key: record[key] for key in ("category", "plugin", "source")}
        row["name"] = name
        report["agents"].append(row)
        if record["category"] == "plugin" and settings.get(record["plugin"]) is False:
            retire(name, row, "plugin_explicitly_disabled")
        else:
            row.update(status="preserved", reason="source_or_install_unavailable_no_explicit_disable")
    if next_records != records:
        files[manifest_path] = _json({"owner": _OWNER, "version": 1, "roles": next_records,
                                      "roles_sha256": _hash(_json(next_records))})
    merged = text.encode("utf-8")
    report["changed"] = bool(files or report["deletions"] or merged != config)
    report["counts"] = {
        "by_status": dict(sorted(Counter(row["status"] for row in report["agents"]).items())),
        "by_category": dict(sorted(Counter(row["category"] for row in report["agents"]).items())),
    }
    return files, merged, report
