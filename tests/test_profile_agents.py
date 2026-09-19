"""Synthetic profiles only: no installed agents, models, or external services."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import tomllib
import unittest
from unittest.mock import patch
import uuid

from profile_agents import plan_agents


class AgentPlanTests(unittest.TestCase):
    def setUp(self):
        # Python 3.13's Windows mkdtemp mode=0700 excludes the sandbox SID.
        self.base = Path(tempfile.gettempdir()) / ("profile-agents-test-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.claude = self.base / "source home"
        self.codex = self.base / "target home"
        self.claude.mkdir()
        self.codex.mkdir()
        self.plugin = self.base / "plugin v1"
        self.plugin.mkdir()
        self.plugins = [("review-kit@synthetic-market", self.plugin)]
        self.config = b'model = "native-default"\nmodel_provider = "existing"\napproval_policy = "never"\nsandbox_mode = "read-only"\n'

    def source(self, name="reviewer", metadata="", body="Review synthetic changes and report defects.\n", root=None):
        path = (root or self.plugin) / "agents" / (name + ".md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("---\nname: " + Path(name).name + "\ndescription: Review synthetic changes.\n" + metadata + "---\n\n" + body, encoding="utf-8")
        return path

    def settings(self, enabled):
        (self.claude / "settings.json").write_text(json.dumps({"enabledPlugins": enabled}), encoding="utf-8")

    def plan(self, plugins=None, config=None):
        return plan_agents(self.claude, self.codex, self.plugins if plugins is None else plugins, self.config if config is None else config)

    def apply(self, result):
        files, self.config, report = result
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        for path in report["deletions"]:
            Path(path).unlink()

    def role(self, result):
        files, config, _ = result
        roles = tomllib.loads(config.decode("utf-8-sig"))["agents"]
        name, entry = next((n, e) for n, e in roles.items() if n.startswith("claude-"))
        path = self.codex / entry["config_file"]
        return name, path, tomllib.loads(files.get(path, path.read_bytes() if path.exists() else b"").decode())

    def test_native_role_preserves_body_and_defaults_without_fake_permissions(self):
        self.source(metadata="model: opus\ncolor: green\n")
        result = self.plan()
        name, path, role = self.role(result)
        self.assertTrue(path.is_absolute())
        self.assertIn("Review synthetic changes and report defects.", role["developer_instructions"])
        self.assertIn('mode="agent"', role["developer_instructions"])
        self.assertIn("llmcall.call", role["developer_instructions"])
        self.assertIn("read-only", role["developer_instructions"])
        self.assertEqual(set(role), {"developer_instructions"})
        self.assertTrue(result[1].startswith(self.config))
        self.assertEqual(result[2]["registered"], 1)
        self.assertIn("review_permissions_are_advisory", result[2]["agents"][0]["caveats"])
        manifest = json.loads(result[0][self.codex / "claude-sync/managed-agents.json"])
        record = manifest["roles"][name]
        self.assertEqual(record["role_sha256"], hashlib.sha256(result[0][path]).hexdigest())
        self.assertEqual(len(record["config_sha256"]), 64)
        self.assertNotIn("Review synthetic", json.dumps(result[2]))

    def test_incremental_no_change_and_source_update(self):
        source = self.source()
        self.apply(self.plan())
        files, config, report = self.plan()
        self.assertEqual(files, {})
        self.assertEqual(config, self.config)
        self.assertFalse(report["changed"])
        source.write_text(source.read_text() + "Check boundary behavior.\n")
        result = self.plan()
        self.assertEqual(result[2]["agents"][0]["status"], "updated")
        self.assertIn("Check boundary behavior.", self.role(result)[2]["developer_instructions"])

    def test_explicit_disable_retires_intact_owned_pair(self):
        self.source()
        first = self.plan()
        _, path, _ = self.role(first)
        self.apply(first)
        self.settings({"review-kit@synthetic-market": False})
        result = self.plan(plugins=[])
        self.assertEqual(result[2]["deletions"], [str(path)])
        self.assertNotIn("agents", tomllib.loads(result[1].decode()))
        self.apply(result)
        self.assertEqual(self.plan(plugins=[])[0], {})
        self.assertFalse(path.exists())

    def test_unavailable_install_or_missing_source_is_preserved(self):
        source = self.source()
        self.apply(self.plan())
        self.settings({"review-kit@synthetic-market": True})
        for plugins in ([], self.plugins):
            source.unlink(missing_ok=True)
            files, config, report = self.plan(plugins=plugins)
            self.assertEqual(files, {})
            self.assertEqual(config, self.config)
            self.assertEqual(report["deletions"], [])

    def test_modified_role_and_config_are_preserved_on_update_and_disable(self):
        source = self.source()
        result = self.plan()
        _, path, _ = self.role(result)
        self.apply(result)
        path.write_bytes(path.read_bytes() + b"# manual edit\n")
        source.write_text(source.read_text() + "New source instructions.\n")
        for plugins in (self.plugins, []):
            self.settings({"review-kit@synthetic-market": bool(plugins)})
            result = self.plan(plugins=plugins)
            self.assertEqual(result[0], {})
            self.assertEqual(result[1], self.config)
            self.assertEqual(result[2]["deletions"], [])
            self.assertTrue(any(x["status"] == "conflict" for x in result[2]["agents"]))

    def test_modified_config_body_and_external_extensions_are_preserved(self):
        self.source()
        self.apply(self.plan())
        for config in (self.config.replace(b"Review synthetic changes.", b"Manual description."), self.config + b'nickname_candidates = ["Manual"]\n'):
            result = self.plan(config=config)
            self.assertEqual(result[0], {})
            self.assertEqual(result[1], config)
            self.assertEqual(result[2]["deletions"], [])

    def test_manual_native_entry_and_existing_file_win(self):
        self.source()
        first = self.plan()
        name, path, _ = self.role(first)
        config = self.config + f'[agents."{name}"]\ndescription="Manual"\nconfig_file="manual.toml"\n'.encode()
        result = self.plan(config=config)
        self.assertEqual(result[1], config)
        self.assertEqual(result[2]["registered"], 0)
        path.parent.mkdir(parents=True)
        path.write_text('developer_instructions="Manual"\n')
        self.assertEqual(self.plan()[2]["registered"], 0)
        self.assertEqual(path.read_text(), 'developer_instructions="Manual"\n')

    def test_tool_allowlists_and_other_execution_metadata_are_unsupported(self):
        for i, metadata in enumerate(('tools: ["Read", "Grep", "Glob"]\n', 'tools: Read, Bash\n', 'tools: []\n', 'disallowedTools: ["Write"]\n', 'permissionMode: plan\n', 'mcpServers: ["synthetic"]\n', 'skills: ["synthetic"]\n', 'hooks: {}\n', 'memory: user\n', 'background: true\n')):
            self.source(name=f"restricted-{i}", metadata=metadata)
        result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)
        self.assertEqual(len(result[2]["agents"]), 10)
        self.assertTrue(all(x["status"] == "unsupported" for x in result[2]["agents"]))

    def test_names_are_stable_across_root_versions_and_distinct_by_scope(self):
        self.source()
        other = self.base / "plugin v2"
        self.source(root=other)
        first = self.role(self.plan())[0]
        second = self.role(self.plan(plugins=[(self.plugins[0][0], other)]))[0]
        self.assertEqual(first, second)
        result = self.plan(plugins=[*self.plugins, ("review-kit@other-market", other)])
        self.assertEqual(len(tomllib.loads(result[1].decode())["agents"]), 2)

    def test_user_agents_and_relative_resources(self):
        root = self.claude
        (root / "references").mkdir()
        (root / "references/rules.md").write_text("Synthetic rules.")
        self.source(root=root, body="Read [rules](../references/rules.md).\nUse actual available tools.\n")
        result = self.plan(plugins=[])
        self.assertEqual(result[2]["agents"][0]["category"], "user")
        self.assertIn((root / "references/rules.md").as_posix(), self.role(result)[2]["developer_instructions"])

    def test_plugin_root_expansion_does_not_expand_environment_secrets(self):
        self.source(body="Read ${CLAUDE_PLUGIN_ROOT}/rules.md.\nKeep ${SYNTHETIC_SECRET} symbolic.\n")
        with patch.dict("os.environ", {"SYNTHETIC_SECRET": "never-copy-this-synthetic-value"}):
            result = self.plan()
        instructions = self.role(result)[2]["developer_instructions"]
        self.assertIn(self.plugin.as_posix() + "/rules.md", instructions)
        self.assertIn("${SYNTHETIC_SECRET}", instructions)
        self.assertNotIn("never-copy-this-synthetic-value", str(result))

    def test_invalid_config_or_manifest_is_fail_closed(self):
        self.source()
        result = self.plan(config=b'model = "unterminated')
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], b'model = "unterminated')
        self.assertNotIn("unterminated", json.dumps(result[2]))
        manifest = self.codex / "claude-sync/managed-agents.json"
        manifest.parent.mkdir()
        manifest.write_text('{"owner":"unowned","roles":{}}')
        self.assertEqual(self.plan()[0], {})

    def test_native_standalone_role_wins_without_config_entry(self):
        self.source()
        name = self.role(self.plan())[0]
        path = self.codex / "agents/manual.toml"
        path.parent.mkdir()
        path.write_text(f'name="{name}"\ndeveloper_instructions="Manual role."\n')
        result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)

    def test_corrupt_manifest_cannot_claim_an_outside_file(self):
        self.source()
        self.apply(self.plan())
        manifest_path = self.codex / "claude-sync/managed-agents.json"
        manifest = json.loads(manifest_path.read_bytes())
        name = next(iter(manifest["roles"]))
        manifest["roles"]["../../outside"] = manifest["roles"].pop(name)
        manifest_path.write_text(json.dumps(manifest))
        self.settings({"review-kit@synthetic-market": False})
        result = self.plan(plugins=[])
        self.assertEqual(result[0], {})
        self.assertEqual(result[2]["deletions"], [])
        self.assertEqual(result[1], self.config)

    def test_removed_config_marker_or_missing_file_is_not_recreated(self):
        self.source()
        result = self.plan()
        _, path, _ = self.role(result)
        self.apply(result)
        altered = self.config.replace(b"# BEGIN", b"# REMOVED BEGIN")
        self.assertEqual(self.plan(config=altered)[0], {})
        path.unlink()
        self.assertEqual(self.plan()[0], {})

    def test_restrictions_added_to_source_withdraw_an_intact_old_role(self):
        self.source()
        first = self.plan()
        _, path, _ = self.role(first)
        self.apply(first)
        self.source(metadata='tools: ["Read"]\n')
        result = self.plan()
        self.assertEqual(result[2]["deletions"], [str(path)])
        self.assertNotIn("agents", tomllib.loads(result[1].decode()))

    def test_explicit_disable_does_not_remove_edited_config(self):
        self.source()
        self.apply(self.plan())
        self.config = self.config.replace(b"Review synthetic changes.", b"Manual description.")
        self.settings({"review-kit@synthetic-market": False})
        result = self.plan(plugins=[])
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)
        self.assertEqual(result[2]["deletions"], [])

    def test_short_plugin_name_is_qualified_for_later_disable(self):
        self.source()
        self.settings({"review-kit@synthetic-market": True})
        self.apply(self.plan(plugins=[("review-kit", self.plugin)]))
        self.settings({"review-kit@synthetic-market": False})
        result = self.plan(plugins=[])
        self.assertEqual(len(result[2]["deletions"]), 1)

    def test_ambiguous_marketplace_or_installation_does_not_choose_first(self):
        self.source()
        self.settings({"review-kit@market-a": True, "review-kit@market-b": False})
        result = self.plan(plugins=[("review-kit", self.plugin)])
        self.assertEqual(result[0], {})
        other = self.base / "other-installation"
        self.source(root=other)
        result = self.plan(plugins=[*self.plugins, (self.plugins[0][0], other)])
        self.assertEqual(result[0], {})
        self.assertEqual(result[2]["registered"], 0)

    def test_install_registry_disambiguates_marketplace_and_scope(self):
        self.source()
        self.settings({"review-kit@market-a": True, "review-kit@market-b": True})
        registry = self.claude / "plugins/installed_plugins.json"
        registry.parent.mkdir()
        registry.write_text(json.dumps({"plugins": {
            "review-kit@market-a": [{"installPath": str(self.plugin), "scope": "user"}],
            "review-kit@market-b": [{"installPath": str(self.plugin), "scope": "project"}],
        }}))
        result = self.plan(plugins=[("review-kit", self.plugin)])
        self.assertEqual(result[2]["agents"][0]["plugin"], "review-kit@market-a")

    def test_invalid_enablement_preserves_previous_registration(self):
        self.source()
        self.apply(self.plan())
        (self.claude / "settings.json").write_text('{"enabledPlugins":"synthetic-private-value"}')
        result = self.plan(plugins=[])
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)
        self.assertNotIn("synthetic-private-value", json.dumps(result[2]))

    def test_custom_plugin_manifest_paths_and_traversal(self):
        source = self.source()
        custom = self.plugin / "custom/inspector.md"
        custom.parent.mkdir()
        source.rename(custom)
        manifest = self.plugin / ".claude-plugin/plugin.json"
        manifest.parent.mkdir()
        manifest.write_text(json.dumps({"agents": ["./custom/inspector.md", "../outside.md"]}))
        result = self.plan()
        self.assertEqual(result[2]["registered"], 1)
        self.assertTrue(any(x["reason"] == "agent_reference_outside_plugin" for x in result[2]["warnings"]))

    def test_yaml_folded_literal_and_quoted_descriptions(self):
        descriptions = ['>-\n  Review synthetic\n  examples.', '|\n  Review examples.\n  Then report.', '"Review examples with \\"quotes\\"."', "'Review examples with ''quotes''.'"]
        for i, description in enumerate(descriptions):
            source = self.source(name=f"reviewer-{i}")
            source.write_text(source.read_text().replace("Review synthetic changes.", description))
        result = self.plan()
        self.assertEqual(result[2]["registered"], len(descriptions))
        self.assertTrue(all(x["status"] == "added" for x in result[2]["agents"]))

    def test_malformed_frontmatter_does_not_leak_parser_input(self):
        source = self.source()
        source.write_text('---\nname: reviewer\ndescription: "synthetic-private-prose\n---\nReview.\n')
        result = self.plan()
        self.assertEqual(result[0], {})
        self.assertNotIn("synthetic-private-prose", json.dumps(result[2]))

    def test_bom_crlf_and_other_tables_survive_round_trip(self):
        self.source()
        self.config = b'\xef\xbb\xbfmodel = "native"\r\n[agents]\r\nmax_threads = 3\r\n[mcp_servers.synthetic]\r\ncommand = "synthetic"\r\n'
        original = tomllib.loads(self.config.decode("utf-8-sig"))
        self.apply(self.plan())
        parsed = tomllib.loads(self.config.decode("utf-8-sig"))
        self.assertEqual(parsed["mcp_servers"], original["mcp_servers"])
        self.assertEqual(parsed["agents"]["max_threads"], 3)
        result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)

    def test_unreadable_output_is_a_conflict_with_no_partial_files(self):
        self.source()
        first = self.plan()
        _, role_path, _ = self.role(first)
        self.apply(first)
        real_read = Path.read_bytes

        def read(path):
            if path == role_path:
                raise PermissionError("synthetic-secret-in-error")
            return real_read(path)

        with patch.object(Path, "read_bytes", read):
            result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)
        self.assertNotIn("synthetic-secret-in-error", json.dumps(result[2]))

    def test_linked_output_is_preserved(self):
        self.source()
        result = self.plan()
        _, role_path, _ = self.role(result)
        self.apply(result)
        with patch("profile_agents._plain", side_effect=lambda path: path != role_path):
            result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[2]["deletions"], [])

    def test_disabled_selection_does_not_import_even_if_caller_includes_it(self):
        self.source()
        self.settings({"review-kit@synthetic-market": False})
        self.assertEqual(self.plan()[0], {})

    def test_duplicate_input_root_is_idempotent(self):
        self.source()
        # Discovery timestamps are observations; compare one frozen snapshot.
        from profile_catalog import discover_profile
        snapshot = discover_profile(self.claude, plugins=self.plugins * 2)
        first = plan_agents(self.claude, self.codex, self.plugins, self.config, catalog_snapshot=snapshot)
        second = plan_agents(self.claude, self.codex, self.plugins * 2, self.config, catalog_snapshot=snapshot)
        self.assertEqual(second, first)

    def test_manual_reference_to_managed_file_prevents_retirement(self):
        self.source()
        first = self.plan()
        _, path, _ = self.role(first)
        self.apply(first)
        self.config += ('[agents.manual]\ndescription="Manual alias"\nconfig_file='
                        + json.dumps(path.as_posix()) + '\n').encode()
        self.settings({"review-kit@synthetic-market": False})
        result = self.plan(plugins=[])
        self.assertEqual(result[2]["deletions"], [])
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)

    def test_known_project_scope_is_not_imported_as_a_user_role(self):
        self.source()
        self.settings({"review-kit@synthetic-market": True})
        registry = self.claude / "plugins/installed_plugins.json"
        registry.parent.mkdir()
        registry.write_text(json.dumps({"plugins": {self.plugins[0][0]: [
            {"installPath": str(self.plugin), "scope": "project"}
        ]}}))
        result = self.plan()
        self.assertEqual(result[0], {})
        self.assertEqual(result[1], self.config)

    @unittest.skipUnless(os.environ.get("PROFILE_AGENTS_CODEX_EXE"), "optional installed-runtime parser check")
    def test_installed_runtime_parses_generated_roles_without_model_calls(self):
        """Opt in with PROFILE_AGENTS_CODEX_EXE; initialize only, never a turn."""
        executable = Path(os.environ["PROFILE_AGENTS_CODEX_EXE"])
        self.source()
        self.config = b""
        result = self.plan()
        name, role_path, _ = self.role(result)
        self.apply(result)
        config_path = self.codex / "config.toml"
        config_path.write_bytes(self.config)
        initialize = json.dumps({"id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "synthetic_role_schema_check", "version": "1"}
        }}) + "\n"

        def parse():
            # This temporary home contains only synthetic data. No model turn,
            # authentication command, MCP server or external agent is started.
            return subprocess.run([str(executable), "app-server", "--strict-config"],
                                  input=initialize, text=True, capture_output=True,
                                  env=dict(os.environ, CODEX_HOME=str(self.codex)),
                                  cwd=self.base, timeout=20)

        valid = parse()
        self.assertEqual(valid.returncode, 0, valid.stderr)
        self.assertNotIn("malformed agent role", valid.stderr)
        self.assertNotIn("unknown field", valid.stderr)
        # Negative controls prove the installed deserializers saw both the
        # config entry and the referenced file, rather than skipping them.
        config_path.write_text(f'[agents."{name}"]\ndescription=42\n')
        invalid_entry = parse()
        self.assertNotEqual(invalid_entry.returncode, 0)
        self.assertIn("expected a string", invalid_entry.stderr)
        config_path.write_bytes(self.config)
        role_path.write_text("developer_instructions=42\n")
        invalid_role = parse()
        self.assertIn("malformed agent role", invalid_role.stderr)
        self.assertIn("expected a string", invalid_role.stderr)


if __name__ == "__main__":
    unittest.main()
