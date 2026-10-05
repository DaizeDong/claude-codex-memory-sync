"""Temporary-fixture tests for the pure profile configuration planner."""

import json
import os
from pathlib import Path
import tempfile
import tomllib
import unittest
from unittest.mock import patch

from profile_config import plan_config


class ConfigPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / ".claude"
        self.codex = self.base / ".codex"
        self.claude.mkdir()
        self.codex.mkdir()

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def global_servers(self, servers):
        self.write_json(self.claude / ".claude.json", {"mcpServers": servers})

    def config(self, contents):
        (self.codex / "config.toml").write_bytes(contents)

    def plan(self, plugins=None):
        return plan_config(self.claude, self.codex, plugins or [])

    @staticmethod
    def parse(contents):
        return tomllib.loads(contents.decode("utf-8-sig"))

    def test_native_gateway_bytes_and_native_mcp_preserved(self):
        original = (
            b'# personal settings\r\nmodel = "gpt-existing"\r\n'
            b'model_provider = "gw"\r\napproval_policy = "never"\r\n'
            b'[profiles.gw]\r\nmodel = "different"\r\n'
            b'[model_providers.gw]\r\nbase_url = "http://127.0.0.1:8790/v1"\r\n'
            b'[mcp_servers.playwright]\r\ncommand = "native-browser"\r\n'
        )
        self.config(original)
        self.global_servers({"playwright": {"command": "claude-browser"}, "new": {"command": "node", "args": ["test.js"]}})
        merged, report = self.plan()
        self.assertIn(original, merged)
        self.assertEqual(self.parse(merged)["mcp_servers"]["playwright"]["command"], "native-browser")
        self.assertEqual([row["status"] for row in report["mcp"]], ["conflict", "added"])
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertEqual(self.parse(merged)["model_provider"], "gw")

    def test_active_home_json_wins_over_stale_parent_config(self):
        self.global_servers({"active": {"command": "active-server"}})
        self.write_json(self.base / ".claude.json", {"mcpServers": {"stale": {"command": "stale-server"}}})
        merged, report = self.plan()
        self.assertEqual(set(self.parse(merged)["mcp_servers"]), {"active"})
        self.assertEqual(report["claude_global_config"]["selection"], "claude_home_config_preferred")

    def test_parent_config_fallback(self):
        self.write_json(self.base / ".claude.json", {"mcpServers": {"legacy": {"command": "server"}}})
        merged, report = self.plan()
        self.assertIn("legacy", self.parse(merged)["mcp_servers"])
        self.assertEqual(report["claude_global_config"]["selection"], "parent_config_fallback")

    def test_stdio_env_cwd_and_plugin_root_expansion(self):
        plugin = self.base / "plugin"
        self.write_json(plugin / ".mcp.json", {"helper": {
            "command": "node", "args": ["${CLAUDE_PLUGIN_ROOT}/server.js", "${SYNC_TEST_ARG}"],
            "cwd": "${CLAUDE_PLUGIN_ROOT}", "env": {"SECRET": "${SYNC_TEST_SECRET}"},
        }})
        with patch.dict(os.environ, {"SYNC_TEST_ARG": "argument", "SYNC_TEST_SECRET": "do-not-report-me"}):
            merged, report = self.plan([plugin])
        server = self.parse(merged)["mcp_servers"]["helper"]
        self.assertEqual(server["args"], [str(plugin.resolve()) + "/server.js", "argument"])
        self.assertEqual(server["cwd"], str(plugin.resolve()))
        self.assertEqual(server["env"]["SECRET"], "do-not-report-me")
        self.assertNotIn("do-not-report-me", json.dumps(report))

    def test_http_headers_are_translated_without_reporting_secrets(self):
        self.global_servers({"remote": {"type": "http", "url": "https://example.invalid/mcp?key=private-url-value", "headers": {"Authorization": "Bearer ${SYNC_TEST_SECRET}"}}})
        with patch.dict(os.environ, {"SYNC_TEST_SECRET": "private-header-value"}):
            merged, report = self.plan()
        server = self.parse(merged)["mcp_servers"]["remote"]
        self.assertEqual(server["http_headers"]["Authorization"], "Bearer private-header-value")
        self.assertNotIn("private-header-value", json.dumps(report))
        self.assertNotIn("private-url-value", json.dumps(report))

    def test_unresolved_refs_and_sse_are_inventory_only(self):
        self.global_servers({
            "missing": {"command": "${CLAUDE_SYNC_MISSING_TEST_VAR}"},
            "sse": {"type": "sse", "url": "https://example.invalid/sse?private=secret"},
            "malformed": {"command": "${{bad-template}}"},
        })
        with patch.dict(os.environ, {}, clear=True):
            merged, report = self.plan()
        self.assertNotIn("mcp_servers", self.parse(merged))
        self.assertTrue(all(row["status"] == "unsupported" for row in report["mcp"]))
        self.assertEqual(report["mcp"][0]["variables"], ["CLAUDE_SYNC_MISSING_TEST_VAR"])
        self.assertNotIn("private=secret", json.dumps(report))

    def test_environment_defaults_support_empty_and_secret_values_without_reporting_them(self):
        self.global_servers({"defaults": {
            "command": "server",
            "args": ["${UNSET:-}", "${UNSET:-secret-default-value}", "${EMPTY:-fallback}", "${SET:-unused}"],
            "env": {"PLAIN_EMPTY": "${EMPTY}"},
        }})
        with patch.dict(os.environ, {"EMPTY": "", "SET": "present-value"}, clear=True):
            merged, report = self.plan()
        server = self.parse(merged)["mcp_servers"]["defaults"]
        self.assertEqual(server["args"], ["", "secret-default-value", "fallback", "present-value"])
        self.assertEqual(server["env"]["PLAIN_EMPTY"], "")
        self.assertNotIn("secret-default-value", json.dumps(report))
        self.assertNotIn("present-value", json.dumps(report))

    def test_http_header_empty_default_is_supported_for_optional_auth(self):
        self.global_servers({"context": {
            "type": "http", "url": "https://example.invalid/mcp",
            "headers": {"CONTEXT7_API_KEY": "${CONTEXT7_API_KEY:-}"},
        }})
        with patch.dict(os.environ, {}, clear=True):
            merged, report = self.plan()
        self.assertEqual(self.parse(merged)["mcp_servers"]["context"]["http_headers"], {"CONTEXT7_API_KEY": ""})
        self.assertEqual(report["mcp"][0]["status"], "added")

    def test_incremental_update_and_repeat_are_stable(self):
        self.global_servers({"example": {"command": "v1", "env": {"A": "one"}}})
        first, _ = self.plan()
        self.config(first)
        second, report = self.plan()
        self.assertEqual(first, second)
        self.assertFalse(report["changed"])
        self.assertEqual(report["mcp"][0]["status"], "unchanged")
        self.global_servers({"example": {"command": "v2", "args": ["new"]}})
        third, report = self.plan()
        self.assertEqual(self.parse(third)["mcp_servers"]["example"], {"command": "v2", "args": ["new"]})
        self.assertEqual(report["mcp"][0]["status"], "updated")
        self.assertEqual(third.count(b"# BEGIN claude-codex-profile-sync mcp-"), 1)

    def test_user_modified_managed_block_is_preserved(self):
        self.global_servers({"example": {"command": "v1"}})
        original, _ = self.plan()
        customized = original.replace(b'command = "v1"', b'command = "my-custom-server"')
        self.config(customized)
        self.global_servers({"example": {"command": "v2"}})
        merged, report = self.plan()
        self.assertEqual(merged, customized)
        self.assertEqual(report["mcp"][0]["reason"], "managed_block_modified_by_user")

    def test_user_extension_outside_managed_block_is_preserved(self):
        self.global_servers({"example": {"command": "v1", "env": {"A": "one"}}})
        original, _ = self.plan()
        extended = original + b'USER_ENV = "custom"\n'
        self.config(extended)
        self.global_servers({"example": {"command": "v2"}})
        merged, report = self.plan()
        self.assertEqual(merged, extended)
        self.assertEqual(report["mcp"][0]["reason"], "managed_server_extended_outside_block_by_user")

    def test_source_removal_retires_previously_managed_server(self):
        self.global_servers({"example": {"command": "v1"}})
        original, _ = self.plan()
        self.config(original)
        self.global_servers({})
        merged, report = self.plan()
        self.assertNotIn("example", self.parse(merged).get("mcp_servers", {}))
        self.assertEqual(report["mcp"][0]["status"], "retired")
        self.assertEqual(report["mcp"][0]["reason"], "source_removed")

    def test_removed_server_with_user_extension_is_preserved(self):
        self.global_servers({"example": {"command": "v1", "env": {"A": "one"}}})
        original, _ = self.plan()
        extended = original + b'USER_ENV = "custom"\n'
        self.config(extended)
        self.global_servers({})
        merged, report = self.plan()
        self.assertEqual(merged, extended)
        self.assertEqual(report["mcp"][0]["status"], "conflict")

    def test_explicitly_disabled_managed_server_is_retired(self):
        self.global_servers({"example": {"command": "v1"}})
        original, _ = self.plan()
        self.config(original)
        self.global_servers({"example": {"command": "v1", "disabled": True}})
        merged, report = self.plan()
        self.assertNotIn("example", self.parse(merged).get("mcp_servers", {}))
        self.assertEqual(report["mcp"][0]["reason"], "explicitly_disabled_source")

    def test_disabled_server_retires_after_credential_is_removed(self):
        server = {"command": "server", "env": {"TOKEN": "${PROFILE_REVIEW_TOKEN}"}}
        self.global_servers({"example": server})
        with patch.dict(os.environ, {"PROFILE_REVIEW_TOKEN": "fixture-secret"}, clear=True):
            original, _ = self.plan()
        self.config(original)
        self.global_servers({"example": {**server, "disabled": True}})
        with patch.dict(os.environ, {}, clear=True):
            merged, report = self.plan()
        self.assertNotIn("example", self.parse(merged).get("mcp_servers", {}))
        self.assertEqual(report["mcp"][0]["status"], "retired")
        self.assertEqual(report["mcp"][0]["reason"], "explicitly_disabled_source")

        customized = original.replace(b'command = "server"', b'command = "custom-server"')
        self.config(customized)
        with patch.dict(os.environ, {}, clear=True):
            preserved, report = self.plan()
        self.assertEqual(preserved, customized)
        self.assertEqual(report["mcp"][0]["reason"], "managed_block_modified_by_user")

    def test_legacy_managed_mcp_disable_uses_exact_known_plugin_definition(self):
        import hashlib
        plugin = self.base / "plugin"
        self.write_json(plugin / ".mcp.json", {"example": {"command": "fixture"}})
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        self.write_json(self.claude / "plugins/installed_plugins.json", {"plugins": {"fixture@local": [{"installPath": str(plugin)}]}})
        body = '[mcp_servers."example"]\ncommand = "fixture"\n'
        key = 'mcp-' + hashlib.sha256(b'example').hexdigest()[:16]
        block = f'# BEGIN claude-codex-profile-sync {key} sha256={hashlib.sha256(body.encode()).hexdigest()}\n{body}# END claude-codex-profile-sync {key}\n'
        self.config(block.encode())
        merged, report = self.plan()
        self.assertNotIn("example", self.parse(merged).get("mcp_servers", {}))
        self.assertEqual(report["mcp"][0]["reason"], "disabled_in_claude")

    def test_existing_multiline_fallback_preserves_other_names_and_tables(self):
        original = b'# keep before\nproject_doc_fallback_filenames = [\n "README.local.md",\n "TEAM.md",\n]\n[profiles.gw]\nmodel = "native"\n'
        self.config(original)
        merged, report = self.plan()
        self.assertTrue(merged.startswith(b"# keep before\n"))
        self.assertTrue(merged.endswith(b'[profiles.gw]\nmodel = "native"\n'))
        self.assertEqual(self.parse(merged)["project_doc_fallback_filenames"], ["README.local.md", "TEAM.md", "CLAUDE.md"])
        self.config(merged)
        repeated, report = self.plan()
        self.assertEqual(merged, repeated)
        self.assertFalse(report["changed"])

    def test_bom_and_crlf_preserved(self):
        original = b'\xef\xbb\xbfmodel = "native"\r\n'
        self.config(original)
        merged, _ = self.plan()
        self.assertTrue(merged.startswith(b"\xef\xbb\xbf"))
        self.assertIn(original[3:], merged)
        self.assertNotIn(b"\n", merged.replace(b"\r\n", b""))
        self.assertEqual(self.parse(merged)["model"], "native")

    def test_existing_inline_mcp_table_fails_closed_for_new_servers(self):
        self.config(b'mcp_servers = { native = { command = "keep" } }\n')
        self.global_servers({"new": {"command": "new"}})
        merged, report = self.plan()
        self.assertEqual(set(self.parse(merged)["mcp_servers"]), {"native"})
        self.assertEqual(report["mcp"][0]["reason"], "existing_toml_layout_prevents_safe_merge")

    def test_invalid_existing_toml_is_returned_unchanged(self):
        original = b'model = "unterminated\n'
        self.config(original)
        self.global_servers({"new": {"command": "new"}})
        merged, report = self.plan()
        self.assertEqual(original, merged)
        self.assertFalse(report["changed"])
        self.assertEqual(report["project_docs"]["status"], "conflict")

    def test_settings_and_hooks_are_inventoried_without_execution_or_values(self):
        self.write_json(self.claude / "settings.json", {
            "model": "private-model-value", "permissions": {"allow": ["Bash(secret command)"]},
            "env": {"PRIVATE": "private-env-value"},
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "private-hook-value"}]}]},
            "mcpServers": {"settings-server": {"command": "server"}},
        })
        merged, report = self.plan()
        self.assertIn("settings-server", self.parse(merged)["mcp_servers"])
        self.assertEqual({row["key"] for row in report["settings"]}, {"model", "permissions", "env", "hooks"})
        serialized = json.dumps(report)
        for secret in ("private-model-value", "secret command", "private-env-value", "private-hook-value"):
            self.assertNotIn(secret, serialized)
            self.assertNotIn(secret.encode(), merged)

    def test_recursive_codex_mcp_and_invalid_args_do_not_import_or_crash(self):
        self.global_servers({"codex": {"command": "codex", "args": ["mcp-server"]}, "invalid": {"command": "server", "args": None}})
        merged, report = self.plan()
        self.assertNotIn("mcp_servers", self.parse(merged))
        self.assertEqual(report["mcp"][0]["reason"], "recursive_codex_mcp_server_not_imported")
        self.assertEqual(report["mcp"][1]["reason"], "invalid_stdio_args")

    def test_plugin_manifest_inline_and_path_definitions(self):
        plugin = self.base / "plugin"
        self.write_json(plugin / ".claude-plugin" / "plugin.json", {"mcpServers": ["./servers.json", "../outside.json"]})
        self.write_json(plugin / "servers.json", {"mcpServers": {"manifest-server": {"command": "server"}}})
        other = self.base / "inline"
        self.write_json(other / ".claude-plugin" / "plugin.json", {"mcpServers": {"inline-server": {"command": "inline"}}})
        merged, report = self.plan([plugin, other, plugin])
        self.assertEqual(set(self.parse(merged)["mcp_servers"]), {"manifest-server", "inline-server"})
        self.assertEqual(len(report["mcp"]), 2)
        self.assertTrue(any(row["reason"] == "plugin_mcp_reference_outside_plugin_root" for row in report["warnings"]))

    def test_plugin_collision_with_native_server_keeps_native(self):
        self.config(b'[mcp_servers.playwright]\ncommand = "native"\n')
        plugin = self.base / "plugin"
        self.write_json(plugin / ".mcp.json", {"playwright": {"command": "plugin"}})
        merged, report = self.plan([plugin])
        self.assertEqual(set(self.parse(merged)["mcp_servers"]), {"playwright"})
        self.assertEqual(report["mcp"][0]["reason"], "native_codex_server_preserved")

    def test_duplicate_sources_do_not_replace_first_definition(self):
        self.global_servers({"same": {"command": "global"}})
        self.write_json(self.claude / "settings.json", {"mcpServers": {"same": {"command": "settings"}}})
        merged, report = self.plan()
        self.assertEqual(self.parse(merged)["mcp_servers"]["same"]["command"], "global")
        self.assertEqual(report["mcp"][1]["reason"], "duplicate_claude_server_name; first_source_preserved")

    def test_fake_fallback_assignment_in_multiline_string_is_preserved(self):
        original = b'other = """\nproject_doc_fallback_filenames = ["decoy"]\n"""\nproject_doc_fallback_filenames = ["real.md"]\n'
        self.config(original)
        merged, _ = self.plan()
        self.assertEqual(self.parse(merged)["other"], self.parse(original)["other"])
        self.assertEqual(self.parse(merged)["project_doc_fallback_filenames"], ["real.md", "CLAUDE.md"])


if __name__ == "__main__":
    unittest.main()
