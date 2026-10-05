"""Behavioral catalog integration regressions using generated synthetic inputs."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import uuid

import profile_catalog as integration
import profile_inventory as inventory
import profile_sync as sync
from catalog_fixture_factory import external, home, json_file, plugin, skill, write


class CatalogIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.gettempdir()) / ("catalog-fixture-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.addCleanup(shutil.rmtree, self.base)
        self.claude, self.codex, self.skills = home(self.base)

    def discover(self, **kwargs):
        return integration.discover_profile(self.claude, self.codex, self.skills, **kwargs)

    def test_external_manifest_requires_an_explicit_binding(self):
        with patch.dict("os.environ", {}, clear=True):
            request = integration.startup_request(self.claude, self.codex, self.skills)
        self.assertNotIn("external_skill_repos", request)

    def test_explicit_manifest_wins_over_config_environment(self):
        configured = self.base / "configured"
        selected = self.base / "selected.json"
        with patch.dict("os.environ", {"CLAUDE_CONFIG_REPO": str(configured)}):
            request = integration.startup_request(self.claude, self.codex, self.skills)
            override = integration.startup_request(self.claude, external_manifest=selected)
        self.assertEqual(request["external_skill_repos"], str(configured / "external-skill-repos.json"))
        self.assertEqual(override["external_skill_repos"], str(selected))

    def test_current_external_formulas_aliases_and_installer_survive_projection(self):
        manifest, checkout, vendored, installer = external(self.base)
        sync.make_link(self.claude / "skills/checkout-alias", checkout)
        sync.make_link(self.claude / "skills/installed-alias", vendored)
        sync.make_link(self.claude / "skills/cc-setup", installer)
        snapshot = self.discover(external_manifest=manifest)
        report = inventory.inventory_sources(self.claude, self.codex, self.skills, snapshot=snapshot)
        rows = {r["catalog_record"]["origin"].get("type"): r for r in report["skills"] if r["path"].endswith("checkout-alias")}
        record = rows["checkout"]["catalog_record"]
        self.assertEqual(Path(record["path"]), checkout)
        self.assertEqual(record["version"], "a" * 40)
        self.assertEqual(record["origin"]["branch"], "stable")
        self.assertEqual(record["aliases"], ["checkout-alias"])
        vendor = next(r for r in snapshot["records"] if r["aliases"] == ["installed-alias"])
        self.assertEqual(Path(vendor["path"]), vendored)
        self.assertEqual(vendor["origin"]["subPath"], "upstream/path")
        self.assertEqual(vendor["version"], "b" * 40)
        links, _, rows = sync.plan_skills(self.claude, self.skills, [], self.codex, catalog_snapshot=snapshot)
        self.assertIn(self.skills / "installed-alias", links)
        self.assertNotIn(self.skills / "cc-setup", links)
        self.assertTrue(any(r.get("reason") == "external_installer_required" for r in rows))

    def test_nested_native_entries_and_singular_discovery(self):
        skill(self.codex / "skills/codex-primary-runtime/spreadsheets", "Excel")
        skill(self.codex / "skills/.system/imagegen", "imagegen")
        skill(self.skills / "nested/shared", "shared")
        skill(self.claude / "skills/nested/claude", "claude")
        with patch.object(integration.catalog, "discover", wraps=integration.catalog.discover) as discover:
            _, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertEqual(discover.call_count, 1)
        self.assertEqual({r["name"] for r in report["inventory"]["skills"]}, {"Excel", "imagegen", "shared", "claude"})
        self.assertEqual(report["catalog"]["schema_version"], 1)
        self.assertIs(report["catalog"], report["inventory"]["catalog"])
        self.assertEqual(set(report["catalog"]["status"].values()), {"unknown"})

    def test_two_marketplaces_custom_paths_aliases_and_connector_auth(self):
        root = plugin(self.base, self.claude)
        plugin(self.base, self.claude, key="kit@market-b")
        _, report = sync.build_plan(self.claude, self.codex, self.skills)
        records = [r for r in report["catalog"]["records"] if r["kind"] == "plugin"]
        self.assertEqual(len({r["source_id"] for r in records}), 2)
        for record in records:
            entry = next(e for e in record["entrypoints"] if e["kind"] == "skill")
            self.assertEqual(entry["install_name"], "installed-alias")
            self.assertEqual(entry["name"], "different-frontmatter")
            self.assertEqual(entry["relative_path"], "skills/installed-alias/SKILL.md")
            self.assertEqual(record["status"]["discovered"], "unknown")
            self.assertNotIn("authenticated", record["status"])
        bindings = [r for r in report["catalog"]["records"] if r["kind"] == "mcp_binding"]
        self.assertEqual(len(bindings), 4)
        self.assertTrue(all(r["status"]["authenticated"] == "unknown" for r in bindings))
        self.assertEqual(report["agents"]["registered"], 2)
        adapted = [r["name"] for r in report["skills"] if r["status"] == "adapted"]
        self.assertEqual(len(adapted), len(set(adapted)))
        self.assertTrue(any(r["source"] == str(root / "custom/inspector.md") for r in report["agents"]["agents"]))

    def test_disabled_can_retire_but_unavailable_selection_cannot(self):
        root = plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        registry = self.claude / "plugins/installed_plugins.json"
        old = registry.read_bytes()
        data = json.loads(old)
        data["plugins"]["kit@market-a"][0]["installPath"] = str(root.parent / "missing-version")
        data["plugins"]["kit@market-a"][0]["version"] = "missing-version"
        json_file(registry, data)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertFalse(any(row["after"]["kind"] == "missing" for row in changes))
        provider = inventory.plugin_catalog(self.claude, report["catalog"])["kit@market-a"]
        self.assertEqual(provider["status"], "unavailable")
        self.assertEqual(provider["catalog_record"]["version"], "missing-version")
        registry.write_bytes(old)
        json_file(self.claude / "settings.json", {"enabledPlugins": {"kit@market-a": False}})
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertTrue(any(row["after"]["kind"] == "missing" for row in changes))

    def test_unreadable_settings_stays_unknown_and_withholds_retirement(self):
        plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        write(self.claude / "settings.json", "{")
        snapshot = self.discover()
        provider = inventory.plugin_catalog(self.claude, snapshot)["kit@market-a"]
        self.assertEqual(provider["status"], "unavailable")
        self.assertEqual(provider["catalog_record"]["status"]["enabled"], "unknown")
        self.assertEqual(snapshot["coverage"]["plugin_registries"]["status"], "partial")
        _, files, _ = sync.plan_skills(self.claude, self.skills, [], self.codex, catalog_snapshot=snapshot)
        self.assertFalse(any(value is None for value in files.values()))

    def test_snapshot_is_not_mutated_by_compatibility_consumers(self):
        plugin(self.base, self.claude)
        snapshot = self.discover()
        before = deepcopy(snapshot)
        providers = inventory.plugin_catalog(self.claude, snapshot)
        providers["kit@market-a"]["catalog_record"]["status"]["enabled"] = "no"
        sync.plan_skills(self.claude, self.skills, [], self.codex, catalog_snapshot=snapshot)
        inventory.inventory_sources(self.claude, self.codex, self.skills, snapshot=snapshot)
        self.assertEqual(snapshot, before)

    def test_incompatible_producer_is_visible_instead_of_empty_discovery(self):
        with patch.object(integration.catalog, "CONSUMER_FEATURES", set()), self.assertRaisesRegex(RuntimeError, "consumer interface missing"):
            self.discover()
        with patch.object(integration.catalog, "CONSUMER_FEATURES", set()), self.assertRaisesRegex(RuntimeError, "consumer interface missing"):
            inventory.declared_name(self.claude / "skills/example")

    def test_unknown_catalog_schema_cannot_authorize_adaptation(self):
        snapshot = self.discover()
        snapshot["schema_version"] = 2
        with self.assertRaisesRegex(ValueError, "catalog_schema_unsupported"):
            sync.plan_skills(self.claude, self.skills, [], self.codex, catalog_snapshot=snapshot)

    def test_legacy_skill_helpers_use_catalog_without_scanning_sibling_roots(self):
        root = skill(self.base / "one-skill", "declared")
        sibling = skill(self.base / "unrequested", "unrequested")
        with patch.object(integration.catalog, "discover", wraps=integration.catalog.discover) as discover:
            self.assertEqual(list(sync.skill_dirs(root)), [root])
            self.assertEqual(inventory.declared_name(root), "declared")
        self.assertTrue(all(call.args[0]["skill_roots"][0]["path"] == str(root) for call in discover.call_args_list))
        self.assertTrue((sibling / "SKILL.md").is_file())

    def test_broken_links_missing_sources_and_linked_worktree_remain_visible(self):
        manifest, checkout, _, _ = external(self.base)
        # A linked-worktree marker is observed, without claiming Git health.
        write(checkout.parents[1] / ".git", "gitdir: ../synthetic-git-metadata\n")
        vanished = skill(self.base / "vanished")
        sync.make_link(self.claude / "skills/broken", vanished)
        shutil.rmtree(vanished)
        data = json.loads(manifest.read_text())
        data["repos"][0]["skills"].append({"name": "missing", "subPath": "missing"})
        json_file(manifest, data)
        request = integration.startup_request(self.claude, self.codex, self.skills, external_manifest=manifest)
        request["repo_roots"] = [{"path": str(self.base / "checkouts")}]
        snapshot = integration.catalog.discover(request)
        report = inventory.inventory_sources(self.claude, self.codex, self.skills, snapshot=snapshot)
        row = next(r for r in report["skills"] if r["path"] == str(self.claude / "skills/broken"))
        self.assertTrue(row["broken"])
        self.assertEqual(row["catalog_record"]["status"]["resolved"], "no")
        self.assertTrue(any(r.get("git_kind") == "linked_worktree" for r in snapshot["records"]))
        missing = next(r for r in snapshot["records"] if "missing" in r["aliases"])
        self.assertEqual(missing["status"]["resolved"], "no")
        self.assertEqual(snapshot["coverage"]["external_skill_repos"]["status"], "partial")

    def test_same_install_name_does_not_transfer_owned_source_to_another_plugin(self):
        first = plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        plugin(self.base, self.claude, key="kit@market-b")
        json_file(self.claude / "settings.json", {"enabledPlugins": {"kit@market-a": False, "kit@market-b": True}})
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        target = self.skills / "installed-alias"
        self.assertFalse(any(Path(row["path"]) == target for row in changes))
        self.assertEqual(target.resolve(), (first / "skills/installed-alias").resolve())

    def test_unreadable_plugin_metadata_preserves_owned_artifacts(self):
        root = plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        write(root / ".claude-plugin/plugin.json", "{")
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertFalse(any(row["after"]["kind"] == "missing" for row in changes))
        self.assertEqual(inventory.plugin_catalog(self.claude, report["catalog"])["kit@market-a"]["reason"], "plugin_metadata_unavailable")

    def test_workflow_adapter_does_not_transfer_between_marketplaces(self):
        plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        target = self.skills / "claude-kit-nested-review/SKILL.md"
        original = target.read_bytes()
        plugin(self.base, self.claude, key="kit@market-b")
        registry = self.claude / "plugins/installed_plugins.json"
        entries = json.loads(registry.read_bytes())
        del entries["plugins"]["kit@market-a"]
        json_file(registry, entries)
        json_file(self.claude / "settings.json", {"enabledPlugins": {"kit@market-b": True}})
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertFalse(any(Path(row["path"]) == target for row in changes))
        self.assertEqual(target.read_bytes(), original)
        self.assertTrue(any(row.get("reason") == "owned_adapter_source_changed" for row in report["skills"]))

    def test_adapter_name_collision_cannot_change_entrypoint_within_same_source(self):
        root = plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        target = self.skills / "claude-kit-nested-review/SKILL.md"
        source = root / "commands/nested/review.md"
        source.rename(root / "commands/nested-review.md")
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertFalse(any(Path(row["path"]) == target for row in changes))
        self.assertTrue(any(row.get("reason") == "owned_adapter_source_changed" for row in report["skills"]))

    def test_retired_workflow_adapter_cannot_be_claimed_by_another_marketplace(self):
        plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        target = self.skills / "claude-kit-nested-review/SKILL.md"
        json_file(self.claude / "settings.json", {"enabledPlugins": {"kit@market-a": False}})
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertFalse(target.exists())
        plugin(self.base, self.claude, key="kit@market-b")
        registry = self.claude / "plugins/installed_plugins.json"
        entries = json.loads(registry.read_bytes())
        del entries["plugins"]["kit@market-a"]
        json_file(registry, entries)
        json_file(self.claude / "settings.json", {"enabledPlugins": {"kit@market-b": True}})
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertFalse(any(Path(row["path"]) == target for row in changes))
        self.assertTrue(any(row.get("reason") == "owned_adapter_source_changed" for row in report["skills"]))

    def test_same_workflow_identity_can_update_its_content(self):
        root = plugin(self.base, self.claude)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        source = root / "commands/nested/review.md"
        write(source, "---\ndescription: New review instructions.\n---\nUpdated synthetic command.\n")
        target = self.skills / "claude-kit-nested-review/SKILL.md"
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertTrue(any(Path(row["path"]) == target for row in changes))
        self.assertFalse(any(row.get("reason") == "owned_adapter_source_changed" for row in report["skills"]))

    def test_connector_observations_are_specific_and_runtime_dimensions_survive(self):
        skill(self.codex / "skills/native", "native")
        snapshot = self.discover()
        record, entry = next(integration.entries(snapshot))
        runtime = json_file(self.base / "runtime.json", {"entrypoints": [{
            "source_id": record["source_id"], "kind": entry["kind"], "name": entry["name"],
            "client": entry["client"], "scope": entry["scope"], "discovered": "yes", "compatible": "no"}]})
        bindings = json_file(self.base / "bindings.json", {"bindings": [
            {"id": "fixture-mail", "kind": "app_connector", "connector": "mail", "authenticated": "yes"},
            {"id": "fixture-drive", "kind": "app_connector", "connector": "drive"}]})
        snapshot = self.discover(runtime_discovery=runtime, private_bindings=bindings)
        report = inventory.inventory_sources(self.claude, self.codex, self.skills, snapshot=snapshot)
        entry = report["skills"][0]["catalog_entrypoint"]
        self.assertEqual(entry["status"]["discovered"], "yes")
        self.assertEqual(entry["status"]["compatible"], "no")
        observed = {r["connector"]: r["status"]["authenticated"] for r in snapshot["records"] if r["kind"] == "app_connector"}
        self.assertEqual(observed, {"mail": "yes", "drive": "unknown"})
