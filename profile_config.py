"""Plan a conservative Claude-to-Codex config merge without writing any files.

Only MCP definitions and the CLAUDE.md project-document fallback are translated.
The caller selects enabled plugin roots and owns persistence/backups.  Reports
contain names, paths, and reason codes, never configuration values or exceptions.
"""

from __future__ import annotations

from dataclasses import dataclass
import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tomllib
from typing import Any
from profile_inventory import OWNER, plugin_catalog, source_identity, source_disposition
from profile_inventory import is_link
from profile_bridge import ownership


_MARKER = "claude-codex-profile-sync"
_VARIABLE = re.compile(r"\$\{([^{}]+)\}")
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_FALLBACK_KEY = "project_doc_fallback_filenames"
_SOURCE_PREFIX = "# claude-profile-source: "
_PLAYWRIGHT_BLOCK = "playwright-launch"
_PLAYWRIGHT_SESSION_FILES = ("pw-mcp-session.py", "pw-mcp-session.cjs", "pw-auth.py", "pw_storage_paths.py")
_PLAYWRIGHT_STARTUP_TIMEOUT = 120
_PLAYWRIGHT_CONFIG = "scripts/captcha-resident/mcp.json"
_PLAYWRIGHT_CONFIG_ARGS = {"--remote-debugging-port=0", "--captcha-resident=mcp"}
_BROWSER_FLAGS = ("--isolated", "--storage-state", "--output-dir")
_INCOMPATIBLE_BROWSER_FLAGS = {"--user-data-dir", "--cdp-endpoint", "--extension", "--config", "--shared-browser-context"}


@dataclass(frozen=True)
class _Block:
    start: int
    end: int
    body: str
    intact: bool


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _profile_verification(members: dict[str, bytes], *, home=None, links=None) -> dict:
    """Submit original bytes and filesystem facts to the ownership authority."""
    envelope = {"members": {key: data.hex() for key, data in members.items()},
                "home": str(home) if home is not None else "", "links": links or {}}
    return ownership.verify(json.dumps(envelope).encode(), "profile")


def _plain_member(path: Path) -> bytes | None:
    for part in (path, *path.parents):
        if is_link(part):
            raise ValueError("linked_ownership_member")
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _agent_members(codex: Path, config: bytes) -> dict[str, bytes]:
    """Collect the writer's fixed destination namespace without interpreting manifests."""
    members = {".codex/config.toml": config}
    manifest = codex / "claude-sync/managed-agents.json"
    data = _plain_member(manifest)
    if data is not None:
        members[".codex/claude-sync/managed-agents.json"] = data
    root = codex / "claude-sync/agents"
    if any(is_link(part) for part in (root, *root.parents)):
        raise ValueError("linked_ownership_member")
    if root.exists():
        # Walk without following directory links, including nonstandard files:
        # an orphaned role marker must not disappear from the evidence.
        for directory, dirs, files in os.walk(root, followlinks=False):
            for name in dirs:
                if is_link(Path(directory) / name):
                    raise ValueError("linked_ownership_member")
            for name in files:
                path = Path(directory) / name
                data = _plain_member(path)
                if data is None:
                    raise ValueError("ownership_member_disappeared")
                members[".codex/" + path.relative_to(codex).as_posix()] = data
    return members


def _blocks(text: str, members: dict[str, bytes] | None = None) -> dict[str, _Block]:
    """Compatibility view of bridge byte spans, converted for the text renderer."""
    data = text.encode("utf-8")
    report = _profile_verification({**(members or {}), ".codex/config.toml": data})
    if report["status"] != "verified":
        raise ValueError("config_ownership_conflict")
    return {span["name"]: _Block(
        len(data[:span["start"]].decode("utf-8")),
        len(data[:span["end"]].decode("utf-8")),
        data[span["body_start"]:span["body_end"]].decode("utf-8"), True)
        for span in report["markers"].get(".codex/config.toml", [])
        if span["kind"] == "config" and span["owner"] == _MARKER}


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


def _add_fallback(text: str, report: dict, newline: str, members=None) -> str:
    parsed = _parse(text)
    current = parsed.get(_FALLBACK_KEY, [])
    if not isinstance(current, list) or any(not isinstance(name, str) for name in current):
        report["project_docs"] = {"status": "conflict", "reason": "existing_fallback_is_not_a_string_array"}
        return text
    if "CLAUDE.md" in current:
        report["project_docs"] = {"status": "unchanged", "filenames": current}
        return text
    block = _blocks(text, members).get("project-docs")
    if block is not None and not block.intact:
        report["project_docs"] = {"status": "conflict", "reason": "managed_block_modified_by_user", "filenames": current}
        return text
    filenames = [*current, "CLAUDE.md"]
    body = f"{_FALLBACK_KEY} = [{', '.join(_quote(name) for name in filenames)}]{newline}"
    if block is not None:
        candidate = text[:block.start] + _managed("project-docs", body, newline) + text[block.end:]
    elif _FALLBACK_KEY not in parsed:
        offset = 1 if text.startswith("\ufeff") else 0
        candidate = text[:offset] + (newline if offset else "") + _managed("project-docs", body, newline) + text[offset:]
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


def _remove_mcp(text: str, key: str, name: str, members=None) -> str | None:
    block = _blocks(text, members)[key]
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


def _playwright_config(args, claude_home, source_checks):
    """Validate the one reviewed config and bind its exact bytes to the plan."""
    options = [index for index, arg in enumerate(args) if arg.split("=", 1)[0] == "--config"]
    if not options:
        return
    if len(options) != 1 or claude_home is None:
        raise ValueError("playwright_config_option_not_reviewed")
    index = options[0]
    _, equal, value = args[index].partition("=")
    if not equal:
        value = args[index + 1] if index + 1 < len(args) else ""
    canonical = (Path(claude_home) / _PLAYWRIGHT_CONFIG).absolute()
    path = Path(value)
    if (not value or not path.is_absolute() or path != canonical or
            not canonical.resolve().is_relative_to(Path(claude_home).resolve())):
        raise ValueError("playwright_config_path_not_reviewed")
    resolved = canonical.resolve()
    try:
        data = canonical.read_bytes()
    except FileNotFoundError:
        check = {"path": str(canonical), "resolved": str(resolved), "before": {"kind": "missing"}}
        if source_checks is not None and check not in source_checks:
            source_checks.append(check)
        raise ValueError("playwright_config_missing") from None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("playwright_config_duplicate_key")
            result[key] = value
        return result

    try:
        document = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_object)
    except (UnicodeError, ValueError):
        raise ValueError("playwright_config_content_not_reviewed") from None
    if (not isinstance(document, dict) or set(document) != {"browser"} or
            not isinstance(document["browser"], dict) or set(document["browser"]) != {"launchOptions"}):
        raise ValueError("playwright_config_content_not_reviewed")
    launch = document["browser"]["launchOptions"]
    if (not isinstance(launch, dict) or set(launch) != {"args"} or
            not isinstance(launch["args"], list) or len(launch["args"]) != len(_PLAYWRIGHT_CONFIG_ARGS) or
            any(not isinstance(arg, str) for arg in launch["args"]) or set(launch["args"]) != _PLAYWRIGHT_CONFIG_ARGS):
        raise ValueError("playwright_config_content_not_reviewed")
    if canonical.resolve() != resolved or canonical.read_bytes() != data:
        raise ValueError("playwright_config_changed_during_planning")
    check = {"path": str(canonical), "resolved": str(resolved),
             "before": {"kind": "file", "sha256": hashlib.sha256(data).hexdigest()}}
    if source_checks is not None:
        if any(old["path"] == check["path"] and old != check for old in source_checks):
            raise ValueError("playwright_config_changed_during_planning")
        if check not in source_checks:
            source_checks.append(check)


def _playwright_argv(spec: dict, claude_home=None, source_checks=None) -> list[str]:
    """Accept only the reviewed direct npx Playwright transport, never a wrapper."""
    command = re.split(r"[/\\]", spec.get("command", ""))[-1].lower()
    args = spec.get("args")
    if (command not in {"npx", "npx.cmd", "npx.exe"} or "url" in spec or
            not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)):
        raise ValueError("playwright_alignment_requires_direct_npx")
    packages = [i for i, arg in enumerate(args) if re.fullmatch(r"@playwright/mcp(?:@[^\s]+)?", arg)]
    if len(packages) != 1 or any(arg not in {"-y", "--yes"} for arg in args[:packages[0]]):
        raise ValueError("playwright_alignment_requires_direct_npx")
    if any(arg.split("=", 1)[0] in _INCOMPATIBLE_BROWSER_FLAGS - {"--config"} for arg in args):
        raise ValueError("playwright_incompatible_launch_option")
    _playwright_config(args, claude_home, source_checks)
    return args


def _playwright_transport(spec: dict, claude_home: Path, source_checks=None) -> tuple[dict, bool]:
    """Unwrap only the canonical session launcher; arbitrary wrappers stay native."""
    args = spec.get("args")
    wrapper = claude_home / "scripts/pw-mcp-session.py"
    command = spec.get("command", "")
    if (isinstance(args, list) and len(args) >= 4 and
            all(isinstance(arg, str) for arg in args) and args[1] == "--" and
            Path(args[0]).is_absolute() and Path(args[0]).resolve() == wrapper.resolve()):
        interpreter = Path(command)
        if ("url" in spec or any(not (wrapper.parent / name).is_file() for name in _PLAYWRIGHT_SESSION_FILES) or
                not interpreter.is_absolute() or
                not interpreter.is_file() or
                not re.fullmatch(r"python(?:[0-9]+(?:\.[0-9]+)?)?(?:\.exe)?", interpreter.name, re.I)):
            raise ValueError("playwright_session_launcher_unavailable")
        inner = {"command": args[2], "args": args[3:]}
        _playwright_argv(inner, claude_home, source_checks)
        return inner, True
    _playwright_argv(spec, claude_home, source_checks)
    return spec, False


def _browser_options(args: list[str]) -> tuple[list[str], dict[str, str | bool]]:
    rest, options = [], {}
    index = 0
    while index < len(args):
        arg = args[index]
        flag, equal, value = arg.partition("=")
        if flag not in _BROWSER_FLAGS:
            rest.append(arg)
        else:
            if flag in options:
                raise ValueError("playwright_duplicate_policy_option")
            if flag == "--isolated":
                if equal:
                    raise ValueError("playwright_invalid_isolated_option")
                options[flag] = True
            else:
                if not equal:
                    index += 1
                    if index == len(args) or args[index].startswith("--"):
                        raise ValueError("playwright_missing_policy_value")
                    value = args[index]
                if not value:
                    raise ValueError("playwright_missing_policy_value")
                options[flag] = value
        index += 1
    return rest, options


def _replace_playwright_assignment(text, key, replacement, expected):
    """Find an ordinary assignment by fragment syntax and full-document equality."""
    lines = text.splitlines(keepends=True)
    offset = 0
    for index, line in enumerate(lines):
        if re.match(r"\s*(?:" + key + r"|\"" + key + r"\"|'" + key + r"')\s*=", line):
            end = offset
            for tail in lines[index:]:
                end += len(tail)
                try:
                    fragment = _parse(text[offset:end])
                except tomllib.TOMLDecodeError:
                    continue
                if set(fragment) == {key}:
                    candidate = text[:offset] + replacement + text[end:]
                    try:
                        if _parse(candidate) == expected:
                            return candidate
                    except tomllib.TOMLDecodeError:
                        pass
                break
        offset += len(line)
    return None


def _align_playwright(text, spec, claude_home, identity, newline, members, adopt, source_checks=None):
    """Own the reviewed launch and its startup budget, preserving other settings."""
    block = _blocks(text, members).get(_PLAYWRIGHT_BLOCK)
    if block is None and not adopt:
        return None, "native_codex_server_preserved"
    try:
        source, session_launcher = _playwright_transport(spec, claude_home, source_checks)
        _, policy = _browser_options(source["args"])
        if set(policy) != set(_BROWSER_FLAGS):
            raise ValueError("playwright_source_policy_incomplete")
        settings = json.loads((claude_home / "settings.json").read_text(encoding="utf-8-sig"))
        env = settings.get("env", {}) if isinstance(settings, dict) else {}
        if not isinstance(env, dict) or any(not isinstance(env.get(key), str) or not env[key]
                                           for key in ("PLAYWRIGHT_CONFIG", "PW_MCP_OUTPUT_DIR")):
            raise ValueError("playwright_storage_paths_not_configured")
        auth_root, output_root = (Path(env[key]).expanduser()
                                  for key in ("PLAYWRIGHT_CONFIG", "PW_MCP_OUTPUT_DIR"))
        if not auth_root.is_absolute() or not output_root.is_absolute():
            raise ValueError("playwright_storage_paths_must_be_absolute")
        store = auth_root / "store"
        selected_state = Path(policy["--storage-state"])
        if not selected_state.is_absolute() or selected_state.resolve() != store.resolve():
            raise ValueError("playwright_source_must_use_store")
        if not store.is_dir():
            raise ValueError("playwright_store_missing")
        output = Path(policy["--output-dir"])
        if not output.is_absolute() or output.resolve() != output_root.resolve():
            raise ValueError("playwright_source_output_directory_not_reviewed")
        expected = _parse(text)
        native = expected["mcp_servers"]["playwright"]
        native_transport, native_launcher = _playwright_transport(native, claude_home, source_checks)
        if native_launcher and not session_launcher:
            raise ValueError("playwright_session_launcher_downgrade_refused")
        rest, _ = _browser_options(native_transport["args"])
        if session_launcher:
            startup_timeout = native.get("startup_timeout_sec", _PLAYWRIGHT_STARTUP_TIMEOUT)
            if (type(startup_timeout) not in (int, float) or
                    isinstance(startup_timeout, float) and not math.isfinite(startup_timeout)):
                raise ValueError("playwright_native_startup_timeout_invalid")
            startup_timeout = max(_PLAYWRIGHT_STARTUP_TIMEOUT, startup_timeout)
    except (ValueError, OSError, TypeError, KeyError) as exc:
        return None, str(exc) if isinstance(exc, ValueError) else "playwright_policy_unreadable"
    args = [*rest, "--isolated", "--storage-state", policy["--storage-state"], "--output-dir", policy["--output-dir"]]
    expected = copy.deepcopy(expected)
    if session_launcher:
        args = [spec["args"][0], "--", native_transport["command"], *args]
        expected["mcp_servers"]["playwright"]["command"] = spec["command"]
        expected["mcp_servers"]["playwright"]["startup_timeout_sec"] = startup_timeout
    expected["mcp_servers"]["playwright"]["args"] = args
    body = (_SOURCE_PREFIX + json.dumps(identity, sort_keys=True) + newline +
            ("command = " + _quote(spec["command"]) + newline if session_launcher else "") +
            "args = [" + ", ".join(_quote(arg) for arg in args) + "]" + newline +
            ("startup_timeout_sec = " + repr(startup_timeout) + newline if session_launcher else ""))
    rendered = _managed(_PLAYWRIGHT_BLOCK, body, newline)

    def valid(candidate):
        try:
            return _parse(candidate) == expected
        except tomllib.TOMLDecodeError:
            return False

    owned = _parse(block.body) if block is not None else {}
    if block is not None:
        if set(owned) not in ({"args"}, {"command", "args"}, {"command", "args", "startup_timeout_sec"}):
            return None, "playwright_owned_args_layout_changed"
        without_owned = copy.deepcopy(_parse(text))
        for key in owned:
            without_owned["mcp_servers"]["playwright"].pop(key, None)
        try:
            if _parse(text[:block.start] + text[block.end:]) != without_owned:
                return None, "playwright_owned_args_layout_changed"
        except tomllib.TOMLDecodeError:
            return None, "playwright_owned_args_layout_changed"
    if session_launcher:
        for key in ("command", "startup_timeout_sec"):
            if key in owned or key not in native:
                continue
            without_key = copy.deepcopy(_parse(text))
            without_key["mcp_servers"]["playwright"].pop(key)
            text = _replace_playwright_assignment(text, key, "", without_key)
            if text is None:
                return None, f"playwright_native_{key}_layout_not_supported"
        if block is not None:
            block = _blocks(text, members)[_PLAYWRIGHT_BLOCK]
    if block is not None:
        candidate = text[:block.start] + rendered + text[block.end:]
        return (candidate, "playwright_launch_policy_updated") if valid(candidate) else (None, "playwright_owned_args_layout_changed")
    candidate = _replace_playwright_assignment(text, "args", rendered, expected)
    if candidate is not None:
        return candidate, "playwright_native_launch_adopted" if session_launcher else "playwright_native_args_adopted"
    return None, "playwright_native_args_layout_not_supported"


def plan_playwright_config(claude_home: Path, codex_home: Path, plugin_roots: list[Path], *,
                           adopt_playwright: bool = False) -> tuple[bytes, dict]:
    """Plan only the existing native Playwright launch, without other profile work."""
    if type(adopt_playwright) is not bool:
        raise ValueError("adopt_playwright_must_be_boolean")
    claude_home, codex_home = Path(claude_home), Path(codex_home)
    original = (codex_home / "config.toml").read_bytes()
    report = {"changed": False, "sources": [], "settings": [], "warnings": [], "mcp": [],
              "source_dependencies": [str(claude_home / "settings.json")], "source_checks": []}
    entries = [entry for entry in _collect(claude_home, plugin_roots, report) if entry[0] == "playwright"]
    item = {"name": "playwright"}
    report["mcp"].append(item)
    if len(entries) != 1:
        item.update(status="conflict", reason="playwright_source_missing_or_ambiguous")
        return original, report
    _, raw_spec, source, plugin_root = entries[0]
    item["source"] = str(source)
    report["source_dependencies"].append(str(source))
    if not isinstance(raw_spec, dict) or raw_spec.get("disabled") is True:
        item.update(status="unavailable", reason="playwright_source_unavailable")
        return original, report
    if "url" in raw_spec:
        item.update(status="conflict", reason="playwright_alignment_requires_local_transport")
        return original, report
    spec, reason, _ = _translate(raw_spec, plugin_root)
    if spec is None:
        item.update(status="conflict", reason=reason)
        return original, report
    try:
        text = original.decode("utf-8")
        native = _parse(text).get("mcp_servers", {}).get("playwright")
        if not isinstance(native, dict):
            raise ValueError("playwright_native_server_missing")
        members = _agent_members(codex_home, original)
        _blocks(text, members)
        _, wrapped = _playwright_transport(spec, claude_home, report["source_checks"])
    except (OSError, UnicodeError, ValueError, TypeError):
        item.update(status="conflict", reason="playwright_transport_or_ownership_invalid")
        return original, report
    if wrapped:
        report["source_dependencies"].extend(str(claude_home / "scripts" / name) for name in _PLAYWRIGHT_SESSION_FILES)
    identity = {"owner": OWNER, **source_identity(source, claude_home, plugin_catalog(claude_home), root=plugin_root)}
    newline = "\r\n" if "\r\n" in text else "\n"
    candidate, reason = _align_playwright(text, spec, claude_home, identity, newline, members, adopt_playwright,
                                        report["source_checks"])
    report["source_dependencies"].extend(check["path"] for check in report["source_checks"])
    if candidate is None:
        item.update(status="conflict", reason=reason)
        return original, report
    merged = candidate.encode("utf-8")
    item.update(status="unchanged" if merged == original else "updated", reason=reason,
                ownership="playwright_command_args_startup_timeout" if wrapped else "playwright_args_only",
                backup="existing_config_plan_apply", before_sha256=hashlib.sha256(original).hexdigest())
    report["changed"] = merged != original
    return merged, report


def plan_skill_routing(config: bytes, codex: Path, runtime: dict, *, adopt=False):
    """Withhold raw policy-selected skills from automatic discovery.

    The policy router remains the executable entrypoint. This changes only
    Codex discovery preferences; it neither removes sources nor certifies a
    blocked implementation or changes native agent permissions.
    """
    key = 'llmcall-skill-routing'
    report = {'status': 'not_requested', 'paths': [], 'native_roles_changed': False}
    try:
        text = config.decode('utf-8')
        members = _agent_members(codex, config)
        blocks = _blocks(text, members)
        if not adopt and key not in blocks:
            return config, report
        if runtime.get('status') != 'ready' or runtime.get('selection_policy') is None:
            return config, {**report, 'status': 'conflict', 'reason': 'routing_policy_unavailable'}
        entries = [item['entrypoint'] for item in runtime['entries'].values()
                   if item['entrypoint']['kind'] == 'skill' and item.get('policy_ids')]
        paths = sorted({str(Path(entry['path']).resolve()) for entry in entries})
        old = blocks.get(key)
        base = text[:old.start] + text[old.end:] if old else text
        document = _parse(base)
        existing = document.get('skills', {}).get('config', [])
        if not isinstance(existing, list) or any(not isinstance(row, dict) for row in existing):
            raise ValueError('invalid_native_skill_preferences')
        existing_paths = {str(Path(row['path']).resolve()).casefold(): row for row in existing if isinstance(row.get('path'), str)}
        wanted = []
        for path in paths:
            native = existing_paths.get(path.casefold())
            if native is not None:
                if native.get('enabled') is not False:
                    raise ValueError('native_skill_preference_conflict')
                continue
            wanted.append({'path': path, 'enabled': False})
        newline = '\r\n' if '\r\n' in text else '\n'
        body = ''.join('[[skills.config]]' + newline + 'path = ' + _quote(row['path']) + newline
                       + 'enabled = false' + newline for row in wanted)
        rendered = _managed(key, body, newline) if body else ''
        candidate = (text[:old.start] + rendered + text[old.end:] if old else
                     base + (newline if base and not base.endswith(('\n', '\r')) else '') + rendered)
        expected = copy.deepcopy(document)
        if wanted:
            expected.setdefault('skills', {}).setdefault('config', []).extend(wanted)
        if _parse(candidate) != expected:
            raise ValueError('skill_preference_layout_changed')
        return candidate.encode('utf-8'), {**report, 'status': 'unchanged' if candidate == text else 'updated',
                                         'paths': paths, 'reason': 'configured_tasks_use_llmcall_router'}
    except (ValueError, KeyError, TypeError, OSError, UnicodeError):
        return config, {**report, 'status': 'conflict', 'reason': 'skill_routing_ownership_or_layout_conflict'}


def plan_config(claude_home: Path, codex_home: Path, plugin_roots: list[Path], *,
                adopt_playwright: bool = False) -> tuple[bytes, dict]:
    """Return merged config bytes and a credential-free inventory/change report.

    ``plugin_roots`` must contain only the plugins selected/enabled by the
    caller. Native Codex MCP names win collisions unless ``adopt_playwright``
    explicitly adopts the reviewed Playwright launch policy. Adoption owns args
    and the canonical session launcher's command, uses the existing full-config
    plan/apply backup, and persists through
    subsequent syncs without repeating the opt-in. Intact managed blocks
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
        "settings": [], "warnings": [], "source_checks": [],
    }
    entries = _collect(claude_home, plugin_roots, report)
    if type(adopt_playwright) is not bool:
        raise ValueError("adopt_playwright_must_be_boolean")
    catalog = plugin_catalog(claude_home)
    try:
        text = original.decode("utf-8")
        _parse(text)
    except (UnicodeError, tomllib.TOMLDecodeError):
        report["warnings"].append({"source": str(config_path), "reason": "existing_codex_config_invalid; all_config_changes_withheld"})
        report["project_docs"] = {"status": "conflict", "reason": "existing_codex_config_invalid"}
        return original, report
    # Whole tables another writer (the Codex desktop app) placed inside an owned block move out of it
    # first; otherwise one foreign table turns every managed block into a conflict.
    relocated, foreign = ownership.relocate_foreign_tables(original)
    if foreign:
        text = relocated.decode("utf-8")
        report["foreign_tables"] = [{**row, "status": "relocated", "reason": "foreign_table_inside_managed_block"}
                                    for row in foreign]
    try:
        members = _agent_members(codex_home, text.encode("utf-8"))
        _blocks(text, members)
    except (OSError, ValueError, UnicodeError):
        reason = "managed_block_modified_by_user"
        report["warnings"].append({"reason": reason})
        report["project_docs"] = {"status": "conflict", "reason": reason}
        report["mcp"] = [{"name": entry[0], "status": "conflict", "reason": reason} for entry in entries]
        return original, report
    newline = "\r\n" if "\r\n" in text else "\n"
    text = _add_fallback(text, report, newline, members)
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
        block = _blocks(text, members).get(key)
        if block is not None and not block.intact:
            item.update(status="conflict", reason="managed_block_modified_by_user")
            continue
        if name in current_mcp and block is None:
            if name == "playwright" and (adopt_playwright or _PLAYWRIGHT_BLOCK in _blocks(text, members)):
                if raw_spec.get("disabled") is True:
                    item.update(status="unavailable", reason="adopted_native_playwright_source_disabled; policy_preserved")
                    continue
                if "url" in raw_spec:
                    item.update(status="conflict", reason="playwright_alignment_requires_local_transport")
                    continue
                spec, reason, missing = _translate(raw_spec, plugin_root)
                if spec is None:
                    item.update(status="unavailable", reason=reason)
                    continue
                identity = {"owner": OWNER, **source_identity(source, claude_home, catalog, root=plugin_root)}
                candidate, reason = _align_playwright(text, spec, claude_home, identity, newline, members, adopt_playwright,
                                                    report["source_checks"])
                if candidate is None:
                    item.update(status="conflict", reason=reason)
                else:
                    item.update(status="unchanged" if candidate == text else "updated", reason=reason,
                                ownership=("playwright_command_args_startup_timeout" if _playwright_transport(spec, claude_home, report["source_checks"])[1]
                                           else "playwright_args_only"), backup="existing_config_plan_apply",
                                before_sha256=hashlib.sha256(original).hexdigest())
                    text = candidate
                continue
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
                candidate = _remove_mcp(text, key, name, members)
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
    for key in list(_blocks(text, members)):
        if key.startswith("mcp-") and key not in active_keys:
            block = _blocks(text, members)[key]
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
                candidate = _remove_mcp(text, key, names[0], members)
                if candidate is None:
                    item.update(status="conflict", reason="retirement_would_change_unmanaged_toml")
                    continue
                text = candidate
    if _PLAYWRIGHT_BLOCK in _blocks(text, members) and "playwright" not in seen:
        report["mcp"].append({"name": "playwright", "status": "unavailable",
                              "reason": "adopted_native_playwright_source_absent; policy_preserved"})
    merged = text.encode("utf-8")
    report["changed"] = merged != original
    return merged, report
