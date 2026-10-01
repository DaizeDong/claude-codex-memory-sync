"""Plan a conservative Claude-to-Codex config merge without writing any files.

Only MCP definitions and the CLAUDE.md project-document fallback are translated.
The caller selects enabled plugin roots and owns persistence/backups.  Reports
contain names, paths, and reason codes, never configuration values or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import tomllib
from typing import Any
from profile_inventory import OWNER, plugin_catalog, source_identity, source_disposition


_MARKER = "claude-codex-profile-sync"
_BLOCK_RE = re.compile(
    rf"^# BEGIN {_MARKER} (?P<key>[a-z0-9-]+) sha256=(?P<hash>[0-9a-f]{{64}})\r?\n"
    rf"(?P<body>.*?)^# END {_MARKER} (?P=key)(?:\r?\n|$)",
    re.MULTILINE | re.DOTALL,
)
_VARIABLE = re.compile(r"\$\{([^{}]+)\}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FALLBACK_KEY = "project_doc_fallback_filenames"
_SOURCE_PREFIX = "# claude-profile-source: "


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    body: str
    intact: bool


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _blocks(text: str) -> dict[str, _Block]:
    result = {}
    for match in _BLOCK_RE.finditer(text):
        body = match["body"]
        result[match["key"]] = _Block(
            match.start(), match.end(), body, _hash(body) == match["hash"]
        )
    return result


def _managed(key: str, body: str, newline: str) -> str:
    return (
        f"# BEGIN {_MARKER} {key} sha256={_hash(body)}{newline}"
        f"{body}# END {_MARKER} {key}{newline}"
    )


def _parse(text: str) -> dict[str, Any]:
    return tomllib.loads(text.removeprefix("\ufeff"))


def _read_json(path: Path, report: dict) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, ValueError):
        report["warnings"].append({"source": str(path), "reason": "invalid_or_unreadable_json"})
        return None


def _expand(value: str, plugin_root: Path | None) -> tuple[str, list[str]]:
    missing = []

    def replace(match: re.Match) -> str:
        name, separator, default = match[1].partition(":-")
        if not _ENV_NAME.fullmatch(name):
            missing.append("<unsupported-expression>")
            return match[0]
        if name == "CLAUDE_PLUGIN_ROOT" and plugin_root is not None:
            return str(plugin_root.resolve())
        value = os.environ.get(name)
        if value is not None and (value or not separator):
            return value
        if separator:
            return default
        # Report a variable name only if it really is a name.  A malformed
        # expression could itself contain a credential or default secret.
        missing.append(name)
        return match[0]

    expanded = _VARIABLE.sub(replace, value)
    if "${" in _VARIABLE.sub("", value):
        missing.append("<unsupported-expression>")
    return expanded, missing


def _translate(spec: Any, plugin_root: Path | None) -> tuple[dict | None, str | None, list[str]]:
    if not isinstance(spec, dict):
        return None, "invalid_server_definition", []
    kind = spec.get("type", "stdio" if "command" in spec else "http" if "url" in spec else "")
    if kind == "sse":
        return None, "sse_transport_not_supported_by_codex", []
    if kind not in ("stdio", "http"):
        return None, "unsupported_transport", []
    if "oauth" in spec:
        return None, "claude_oauth_configuration_requires_native_codex_setup", []
    result: dict[str, Any] = {}
    missing: list[str] = []

    def expand(value: str) -> str:
        expanded, names = _expand(value, plugin_root)
        missing.extend(names)
        return expanded

    def string_map(value: Any) -> dict[str, str] | None:
        if not isinstance(value, dict) or any(not isinstance(v, str) for v in value.values()):
            return None
        return {key: expand(val) for key, val in value.items()}

    if kind == "stdio":
        if not isinstance(spec.get("command"), str) or not spec["command"].strip():
            return None, "stdio_command_required", []
        args = spec.get("args", [])
        if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
            return None, "invalid_stdio_args", []
        result["command"] = expand(spec["command"])
        if "args" in spec:
            result["args"] = [expand(arg) for arg in args]
        if "cwd" in spec:
            if not isinstance(spec["cwd"], str):
                return None, "invalid_stdio_cwd", []
            result["cwd"] = expand(spec["cwd"])
        if "env" in spec:
            env = string_map(spec["env"])
            if env is None:
                return None, "invalid_stdio_env", []
            result["env"] = env
    else:
        if not isinstance(spec.get("url"), str) or not spec["url"].strip():
            return None, "http_url_required", []
        result["url"] = expand(spec["url"])
        if "headers" in spec:
            headers = string_map(spec["headers"])
            if headers is None:
                return None, "invalid_http_headers", []
            result["http_headers"] = headers
    if "disabled" in spec:
        if not isinstance(spec["disabled"], bool):
            return None, "invalid_disabled_flag", []
        result["enabled"] = not spec["disabled"]
    if missing:
        return None, "unresolved_environment_references", sorted(set(missing))
    return result, None, []


def _quote(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _render_mcp(name: str, spec: dict, newline: str) -> str:
    prefix = f"mcp_servers.{_quote(name)}"
    lines = [f"[{prefix}]"]
    for key, value in spec.items():
        if isinstance(value, dict):
            continue
        if isinstance(value, str):
            encoded = _quote(value)
        elif isinstance(value, bool):
            encoded = str(value).lower()
        else:
            encoded = "[" + ", ".join(_quote(v) for v in value) + "]"
        lines.append(f"{key} = {encoded}")
    for key, value in spec.items():
        if isinstance(value, dict):
            lines.append(f"[{prefix}.{key}]")
            lines.extend(f"{_quote(k)} = {_quote(v)}" for k, v in value.items())
    return newline.join(lines) + newline


def _inventory_settings(settings: dict, path: Path, report: dict) -> None:
    for key, value in settings.items():
        if key in ("mcpServers", "$schema"):
            continue
        if key == "hooks":
            if isinstance(value, dict):
                for event, hooks in value.items():
                    report["settings"].append({
                        "source": str(path), "key": "hooks", "event": event,
                        "count": len(hooks) if isinstance(hooks, list) else 1,
                        "status": "unsupported",
                        "reason": "claude_hook_events_and_payloads_require_manual_porting; hooks_not_executed",
                    })
            else:
                report["settings"].append({"source": str(path), "key": key, "status": "unsupported", "reason": "invalid_hook_configuration"})
            continue
        if key in ("model", "apiKeyHelper", "forceLoginMethod", "forceLoginOrgUUID"):
            reason = "codex_model_and_authentication_preserved"
        elif key in ("permissions", "sandbox", "defaultMode", "skipDangerousModePermissionPrompt"):
            reason = "codex_permissions_preserved; claude_permission_rules_are_not_equivalent"
        elif key == "env":
            reason = "global_environment_not_copied; use_explicit_server_environment_or_native_codex_setup"
        elif key == "enabledPlugins":
            reason = "enabled_plugin_selection_owned_by_caller"
        else:
            reason = "no_verified_codex_equivalent"
        report["settings"].append({"source": str(path), "key": key, "status": "unsupported", "reason": reason})


def _collect(claude_home: Path, plugin_roots: list[Path], report: dict) -> list[tuple[str, Any, Path, Path | None]]:
    entries: list[tuple[str, Any, Path, Path | None]] = []

    def add(data: Any, path: Path, root: Path | None, wrapped: bool = False) -> None:
        if not isinstance(data, dict):
            if data is not None:
                report["warnings"].append({"source": str(path), "reason": "expected_json_object"})
            return
        servers = data.get("mcpServers", {}) if wrapped else data.get("mcpServers", data)
        if not isinstance(servers, dict):
            report["warnings"].append({"source": str(path), "reason": "invalid_mcp_servers_object"})
            return
        entries.extend((name, spec, path, root) for name, spec in servers.items())

    preferred = claude_home / ".claude.json"
    legacy = claude_home.parent / ".claude.json"
    selected = preferred if preferred.is_file() else legacy
    report["claude_global_config"] = {
        "path": str(selected),
        "selection": "claude_home_config_preferred" if selected == preferred else "parent_config_fallback",
        "exists": selected.is_file(),
    }
    if selected.is_file():
        report["sources"].append(str(selected))
        add(_read_json(selected, report), selected, None, wrapped=True)
    settings_path = claude_home / "settings.json"
    settings = _read_json(settings_path, report)
    if settings is not None:
        report["sources"].append(str(settings_path))
        add(settings, settings_path, None, wrapped=True)
        if isinstance(settings, dict):
            _inventory_settings(settings, settings_path, report)

    seen_roots: set[Path] = set()
    for raw_root in plugin_roots:
        root = Path(raw_root).resolve()
        if root in seen_roots:
            continue
        seen_roots.add(root)
        paths = [root / ".mcp.json"]
        manifest_path = root / ".claude-plugin" / "plugin.json"
        manifest = _read_json(manifest_path, report)
        if isinstance(manifest, dict) and "mcpServers" in manifest:
            definitions = manifest["mcpServers"]
            if isinstance(definitions, dict):
                report["sources"].append(str(manifest_path))
                add(definitions, manifest_path, root)
            else:
                references = definitions if isinstance(definitions, list) else [definitions]
                for reference in references:
                    if not isinstance(reference, str):
                        report["warnings"].append({"source": str(manifest_path), "reason": "invalid_plugin_mcp_reference"})
                        continue
                    expanded, missing = _expand(reference, root)
                    if missing:
                        report["warnings"].append({"source": str(manifest_path), "reason": "unresolved_plugin_mcp_reference", "variables": missing})
                        continue
                    path = (root / expanded).resolve()
                    if not path.is_relative_to(root):
                        report["warnings"].append({"source": str(manifest_path), "reason": "plugin_mcp_reference_outside_plugin_root"})
                        continue
                    paths.append(path)
        seen_paths: set[Path] = set()
        for path in paths:
            if path in seen_paths or not path.is_file():
                continue
            seen_paths.add(path)
            report["sources"].append(str(path))
            add(_read_json(path, report), path, root)
        hook_path = root / "hooks" / "hooks.json"
        hooks = _read_json(hook_path, report)
        if isinstance(hooks, dict):
            _inventory_settings({"hooks": hooks.get("hooks", hooks)}, hook_path, report)
        if isinstance(manifest, dict) and "hooks" in manifest:
            report["settings"].append({"source": str(manifest_path), "key": "hooks", "status": "unsupported", "reason": "plugin_hooks_require_manual_porting; hooks_not_executed"})
    return entries


def _add_fallback(text: str, report: dict, newline: str) -> str:
    parsed = _parse(text)
    current = parsed.get(_FALLBACK_KEY, [])
    if not isinstance(current, list) or any(not isinstance(name, str) for name in current):
        report["project_docs"] = {"status": "conflict", "reason": "existing_fallback_is_not_a_string_array"}
        return text
    if "CLAUDE.md" in current:
        report["project_docs"] = {"status": "unchanged", "filenames": current}
        return text
    block = _blocks(text).get("project-docs")
    if block is not None and not block.intact:
        report["project_docs"] = {"status": "conflict", "reason": "managed_block_modified_by_user", "filenames": current}
        return text
    filenames = [*current, "CLAUDE.md"]
    body = f"{_FALLBACK_KEY} = [{', '.join(_quote(name) for name in filenames)}]{newline}"
    if block is not None:
        candidate = text[:block.start] + _managed("project-docs", body, newline) + text[block.end:]
    elif _FALLBACK_KEY not in parsed:
        offset = 1 if text.startswith("\ufeff") else 0
        candidate = text[:offset] + _managed("project-docs", body, newline) + text[offset:]
    else:
        # Locate a complete top-level assignment through successful TOML prefix
        # parsing; this handles multiline arrays and quoted keys without a TOML
        # rewrite that would reformat unrelated tables or expose their values.
        pattern = re.compile(rf"^[ \t]*(?:{_FALLBACK_KEY}|\"{_FALLBACK_KEY}\"|'{_FALLBACK_KEY}')[ \t]*=")
        lines = text.splitlines(keepends=True)
        offset = 0
        start = None
        end = None
        for line in lines:
            if start is None and pattern.match(line.removeprefix("\ufeff")):
                try:
                    _parse(text[:offset])
                except tomllib.TOMLDecodeError:
                    pass  # A matching line inside a multiline string is not a key.
                else:
                    start = offset + (1 if offset == 0 and line.startswith("\ufeff") else 0)
            offset += len(line)
            if start is not None:
                try:
                    prefix = _parse(text[:offset])
                except tomllib.TOMLDecodeError:
                    continue
                if prefix.get(_FALLBACK_KEY) == current:
                    end = offset
                    break
        if start is None or end is None:
            report["project_docs"] = {"status": "conflict", "reason": "fallback_assignment_could_not_be_safely_located", "filenames": current}
            return text
        candidate = text[:start] + body + text[end:]
    try:
        _parse(candidate)
    except tomllib.TOMLDecodeError:
        report["project_docs"] = {"status": "conflict", "reason": "fallback_merge_invalid", "filenames": current}
        return text
    report["project_docs"] = {"status": "updated" if current else "added", "filenames": filenames}
    return candidate


def _remove_mcp(text: str, key: str, name: str) -> str | None:
    block = _blocks(text)[key]
    candidate = text[:block.start] + text[block.end:]
    expected = _parse(text)
    del expected["mcp_servers"][name]
    try:
        parsed = _parse(candidate)
    except tomllib.TOMLDecodeError:
        return None
    if not expected["mcp_servers"] and "mcp_servers" not in parsed:
        del expected["mcp_servers"]
    return candidate if parsed == expected else None


def plan_config(claude_home: Path, codex_home: Path, plugin_roots: list[Path]) -> tuple[bytes, dict]:
    """Return merged config bytes and a credential-free inventory/change report.

    ``plugin_roots`` must contain only the plugins selected/enabled by the
    caller. Native Codex MCP names win every collision. Intact managed blocks
    can update incrementally. Hashed source metadata allows safe retirement;
    modified blocks and unavailable provider installations are kept.
    No model, provider, authentication, permission, hook, or global environment
    setting is translated. Process environment references are resolved at plan
    time, including Claude's ``${VAR:-default}`` (empty defaults are allowed).
    Unresolved servers are omitted and reported by variable name only.
    """
    claude_home, codex_home = Path(claude_home), Path(codex_home)
    config_path = codex_home / "config.toml"
    try:
        original = config_path.read_bytes()
    except FileNotFoundError:
        original = b""
    # Other read errors intentionally propagate: the caller must never treat an
    # unreadable existing configuration as an empty writable configuration.
    report: dict[str, Any] = {
        "changed": False, "sources": [], "mcp": [], "project_docs": {},
        "settings": [], "warnings": [],
    }
    entries = _collect(claude_home, plugin_roots, report)
    catalog = plugin_catalog(claude_home)
    try:
        text = original.decode("utf-8")
        _parse(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        report["warnings"].append({"source": str(config_path), "reason": "existing_codex_config_invalid; all_config_changes_withheld"})
        report["project_docs"] = {"status": "conflict", "reason": "existing_codex_config_invalid"}
        return original, report
    newline = "\r\n" if "\r\n" in text else "\n"
    text = _add_fallback(text, report, newline)
    seen: set[str] = set()
    active_keys: set[str] = set()
    for name, raw_spec, source, plugin_root in entries:
        item: dict[str, Any] = {"name": name, "source": str(source)}
        report["mcp"].append(item)
        if not name or not isinstance(name, str):
            item.update(status="unsupported", reason="invalid_server_name")
            continue
        key = "mcp-" + _hash(name)[:16]
        active_keys.add(key)
        if name in seen:
            item.update(status="conflict", reason="duplicate_claude_server_name; first_source_preserved")
            continue
        seen.add(name)
        raw_args = raw_spec.get("args") if isinstance(raw_spec, dict) else None
        if name.lower() == "codex" and isinstance(raw_args, list) and "mcp-server" in raw_args:
            item.update(status="unsupported", reason="recursive_codex_mcp_server_not_imported")
            continue
        if not isinstance(raw_spec, dict):
            item.update(status="unsupported", reason="invalid_server_definition")
            continue
        # Claude tool allow/deny lists and timeout settings have different
        # semantics. Inventory them without changing Codex permission policy.
        known = {"type", "command", "args", "env", "cwd", "url", "headers", "disabled"}
        extras = sorted(set(raw_spec) - known)
        if extras:
            item["untranslated_fields"] = extras
            item["untranslated_reason"] = "no_verified_codex_equivalent; native_codex_policy_preserved"
        current_mcp = _parse(text).get("mcp_servers", {})
        if not isinstance(current_mcp, dict):
            item.update(status="conflict", reason="existing_mcp_servers_is_not_a_table")
            continue
        block = _blocks(text).get(key)
        if block is not None and not block.intact:
            item.update(status="conflict", reason="managed_block_modified_by_user")
            continue
        if name in current_mcp and block is None:
            item.update(status="conflict", reason="native_codex_server_preserved")
            continue
        if block is not None and name in current_mcp:
            owned = _parse(block.body).get("mcp_servers", {}).get(name)
            if owned != current_mcp[name]:
                item.update(status="conflict", reason="managed_server_extended_outside_block_by_user")
                continue
        if raw_spec.get("disabled") is True:
            if block is None:
                item.update(status="skipped", reason="explicitly_disabled_source")
            else:
                candidate = _remove_mcp(text, key, name)
                if candidate is None:
                    item.update(status="conflict", reason="retirement_would_change_unmanaged_toml")
                else:
                    text = candidate
                    item.update(status="retired", reason="explicitly_disabled_source")
            continue
        # Retirement needs ownership, not working runtime credentials. A source
        # may disable a server at the same time that its token is revoked.
        spec, reason, missing = _translate(raw_spec, plugin_root)
        if spec is None:
            item.update(status="unsupported", reason=reason)
            if missing:
                item["variables"] = missing
            continue
        identity = {"owner": OWNER, **source_identity(source, claude_home, catalog, root=plugin_root)}
        body = _SOURCE_PREFIX + json.dumps(identity, sort_keys=True) + newline + _render_mcp(name, spec, newline)
        rendered = _managed(key, body, newline)
        if block is not None:
            if block.body == body:
                item["status"] = "unchanged"
                continue
            candidate = text[:block.start] + rendered + text[block.end:]
            status = "updated"
        else:
            spacer = "" if not text or text.endswith(("\n", "\r")) else newline
            candidate = text + spacer + rendered
            status = "added"
        try:
            _parse(candidate)
        except tomllib.TOMLDecodeError:
            item.update(status="conflict", reason="existing_toml_layout_prevents_safe_merge")
            continue
        text = candidate
        item["status"] = status
    # Old blocks lack source metadata. Only an exact definition in a known
    # disabled plugin can establish ownership for their initial retirement.
    disabled_definitions = {}
    disabled_roots = [Path(root) for provider in catalog.values() if provider["status"] == "disabled" for root in provider["roots"] if Path(root).is_dir()]
    if disabled_roots:
        ignored_report = {"sources": [], "settings": [], "warnings": []}
        for name, raw, source, root in _collect(claude_home, disabled_roots, ignored_report):
            if root is None:
                continue
            spec, reason, _ = _translate(raw, root)
            if spec is not None:
                disabled_definitions.setdefault("mcp-" + _hash(name)[:16], []).append((name, spec, source, root))
    for key in list(_blocks(text)):
        if key.startswith("mcp-") and key not in active_keys:
            block = _blocks(text)[key]
            owned = _parse(block.body).get("mcp_servers", {})
            names = list(owned) if isinstance(owned, dict) else []
            item = {"block": key, "name": names[0] if len(names) == 1 else key}
            report["mcp"].append(item)
            if not block.intact:
                item.update(status="conflict", reason="managed_block_modified_by_user")
                continue
            if len(names) != 1 or owned[names[0]] != _parse(text).get("mcp_servers", {}).get(names[0]):
                item.update(status="conflict", reason="managed_server_extended_outside_block_by_user")
                continue
            identity = None
            if block.body.startswith(_SOURCE_PREFIX):
                try:
                    identity = json.loads(block.body.splitlines()[0][len(_SOURCE_PREFIX):])
                except ValueError:
                    pass
            if not isinstance(identity, dict) or identity.get("owner") != OWNER:
                matches = [entry for entry in disabled_definitions.get(key, []) if _render_mcp(entry[0], entry[1], newline) == block.body]
                if len(matches) == 1:
                    _, _, source, root = matches[0]
                    identity = source_identity(source, claude_home, catalog, root=root)
                else:
                    item.update(status="unavailable", reason="legacy_mcp_source_ownership_unknown")
                    report["warnings"].append({"reason": "managed_server_source_absent; existing_block_preserved", "block": key})
                    continue
            item["source"] = identity["source"]
            # Invalid JSON, a missing environment reference, or a mount failure
            # is not proof that a server was intentionally removed.
            if any(w.get("source") == identity["source"] for w in report["warnings"]):
                item.update(status="unavailable", reason="source_configuration_unreadable")
                continue
            status, reason = source_disposition(identity, catalog, absent=True)
            item.update(status=status, reason=reason)
            if status == "retired":
                candidate = _remove_mcp(text, key, names[0])
                if candidate is None:
                    item.update(status="conflict", reason="retirement_would_change_unmanaged_toml")
                    continue
                text = candidate
    merged = text.encode("utf-8")
    report["changed"] = merged != original
    return merged, report
