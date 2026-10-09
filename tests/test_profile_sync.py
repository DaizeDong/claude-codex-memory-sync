"""End-to-end profile bridge tests using synthetic temporary homes only."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import tomllib
import unittest
from unittest.mock import Mock, patch

import profile_sync as sync
from tools.make_fixtures import (make_profile_apply_fixture, make_profile_hook_source, make_profile_invalid_backup_root,
                                make_profile_rollback_fixture, make_windows_short_path)


class ProfileSyncTests(unittest.TestCase):
    def test_short_home_alias_hook_plan_apply_and_replan(self):
        alias = make_windows_short_path(self.base)
        if alias is None:
            self.skipTest('Native Windows short paths are unavailable')
        make_profile_hook_source(self.claude, sys.executable)
        native = {'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'synthetic-native-hook'}]}]}}
        self.write_json(self.codex / 'hooks.json', native)
        before = (self.codex / 'hooks.json').read_bytes()
        claude, codex, skills = alias / 'claude', alias / 'codex', alias / 'agents/skills'
        changes, report = sync.build_plan(claude, codex, skills)
        self.assertEqual((self.codex / 'hooks.json').read_bytes(), before)
        self.assertEqual(report['hooks']['registered'], 1)
        sync.apply_plan(changes, report, codex, skills)
        self.assertEqual(json.loads((self.codex / 'hooks.json').read_bytes())['hooks']['Stop'][0], native['hooks']['Stop'][0])
        again, _ = sync.build_plan(claude, codex, skills)
        self.assertEqual(again, [])

    def test_destination_edit_during_inventory_rejects_stale_plan(self):
        original = self.existing_config()
        real_inventory = sync.inventory_sources
        def concurrent(*args, **kwargs):
            result = real_inventory(*args, **kwargs)
            self.write(self.codex / 'config.toml', original + b'\n# user edit during planning\n')
            return result
        with patch.object(sync, 'inventory_sources', concurrent), self.assertRaises(ValueError):
            sync.build_plan(self.claude, self.codex, self.skills)
        self.assertIn(b'user edit during planning', (self.codex/'config.toml').read_bytes())

    def test_retirement_edit_during_inventory_rejects_stale_deletion(self):
        adapter = self.command()
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        (self.claude/'commands/review.md').unlink()
        real_inventory = sync.inventory_sources
        def concurrent(*args, **kwargs):
            result = real_inventory(*args, **kwargs)
            adapter.write_bytes(adapter.read_bytes() + b'User edit during retirement.\n')
            return result
        with patch.object(sync, 'inventory_sources', concurrent), self.assertRaises(ValueError):
            sync.build_plan(self.claude, self.codex, self.skills)
        self.assertTrue(adapter.is_file())

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_legacy_backup_link_snapshot_still_rolls_back(self):
        source = self.source_skill()
        target = self.skills / "example"
        changes = [{"path": str(target), "before": {"kind": "missing"},
                    "after": sync.planned_link(source), "append_only": False}]
        report = sync.apply_plan(changes, {}, self.codex, self.skills)
        manifest_path = Path(report["cleanup_pending"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["changes"][0]["after"].pop("link_type")
        manifest_path.write_text(json.dumps(manifest))
        restored = sync.rollback(Path(report["cleanup_pending"]), self.codex, self.skills)
        self.assertEqual(restored["preserved"], [])
        self.assertFalse(sync.linked(target))

    def test_native_agent_plan_is_applied_and_converges(self):
        self.write(self.claude / "agents/auditor.md",
                   b'---\nname: auditor\ndescription: Review synthetic changes.\n---\nInspect code and report findings.\n')
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertEqual(report["agents"]["registered"], 1)
        sync.apply_plan(changes, report, self.codex, self.skills)
        again, result = sync.build_plan(self.claude, self.codex, self.skills)
        self.assertEqual(again, [])
        self.assertEqual(result["agents"]["registered"], 1)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="claude-profile-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / "claude"
        self.codex = self.base / "codex"
        self.skills = self.base / "agents" / "skills"
        self.claude.mkdir()
        self.write(self.claude / "CLAUDE.md", b"Use concise, factual replies.\n")
        self.write_json(self.claude / ".claude.json", {
            "mcpServers": {
                "native": {"command": "claude-native"},
                "imported": {"command": "synthetic-server", "args": ["--stdio"]},
            },
        })
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {}})
        self.write(self.claude / "projects" / "project-one" / "memory" / "MEMORY.md", b"# Project knowledge\nUse the documented test command.\n")

    @staticmethod
    def write(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def write_json(self, path, value):
        self.write(path, json.dumps(value).encode("utf-8"))

    def existing_config(self):
        original = (
            b'# keep native formatting\r\nmodel = "native-model"\r\n'
            b'model_provider = "gw"\r\napproval_policy = "never"\r\n'
            b'[model_providers.gw]\r\nbase_url = "http://127.0.0.1:8790/v1"\r\n'
            b'[mcp_servers.native]\r\ncommand = "native-server"\r\n'
        )
        self.write(self.codex / "config.toml", original)
        return original

    def native_skill(self, name="native"):
        path = self.skills / name / "SKILL.md"
        self.write(path, b"---\nname: native\ndescription: Keep this native skill.\n---\nNative instructions.\n")
        return path

    def source_skill(self, name="example", root=None):
        root = root or (self.claude / "skills" / name)
        self.write(root / "SKILL.md", f"---\nname: {name}\ndescription: Use the {name} fixture.\n---\nRead scripts/helper.py.\n".encode())
        self.write(root / "scripts" / "helper.py", b"print('fixture only')\n")
        return root

    def command(self):
        self.write(self.claude / "commands" / "review.md", b"---\ndescription: Review a fixture.\n---\nReview $ARGUMENTS.\n")
        return self.skills / "claude-user-review" / "SKILL.md"

    def ingress_contract(self):
        from profile_bridge.memory_outbox import authorize
        authorize(self.codex, self.claude / 'projects', request_id='synthetic-periodic', scope=['*'], periodic=True, skills=self.skills)
        self.write(self.codex / "memories" / "extensions" / "ad_hoc" / "instructions.md", b"Fixture ingress contract.\n")

    def tree(self, root=None):
        root = root or self.base
        result = {}

        def visit(path):
            name = path.relative_to(root).as_posix()
            if sync.linked(path):
                result[name] = ("link", str(path.resolve()))
            elif path.is_file():
                result[name] = ("file", path.read_bytes())
            elif path.is_dir():
                result[name] = ("directory",)
                for child in sorted(path.iterdir()):
                    visit(child)

        visit(root)
        return result

    def junction(self, path, source):
        sync.make_link(path, source)

        def unlink_only():
            # Cleanup removes only fixture junctions/symlinks, never their targets.
            if sync.linked(path):
                if os.name == "nt" and not path.is_symlink():
                    os.rmdir(path)
                else:
                    path.unlink()

        self.addCleanup(unlink_only)
        self.assertTrue(sync.linked(path))

    def plan(self):
        return sync.build_plan(self.claude, self.codex, self.skills)

    def apply(self):
        changes, report = self.plan()
        return sync.apply_plan(changes, report, self.codex, self.skills)

    def test_build_plan_and_cli_dry_run_create_no_files_or_directories(self):
        self.source_skill()
        self.command()
        before = self.tree()
        changes, report = self.plan()
        self.assertGreater(len(changes), 0)
        self.assertEqual(self.tree(), before)
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.skills.exists())
        output = io.StringIO()
        with redirect_stdout(output):
            code = sync.main(["--claude-home", str(self.claude), "--codex-home", str(self.codex), "--skills-home", str(self.skills), "--dry-run", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "preview")
        self.assertEqual(self.tree(), before)

    def test_apply_preserves_native_config_skill_and_source_then_is_idempotent(self):
        original = self.existing_config()
        native = self.native_skill()
        native_bytes = native.read_bytes()
        self.source_skill()
        self.command()
        self.ingress_contract()
        sources = self.tree(self.claude)
        report = self.apply()
        self.assertEqual(report["status"], "applied")
        self.assertIn(original, (self.codex / "config.toml").read_bytes())
        self.assertEqual(native.read_bytes(), native_bytes)
        self.assertEqual(self.tree(self.claude), sources)
        self.assertTrue(sync.linked(self.skills / "example"))
        after = self.tree()
        changes, report = self.plan()
        self.assertEqual(changes, [])
        self.assertEqual(report["change_count"], 0)
        self.assertEqual(sync.apply_plan(changes, report, self.codex, self.skills)["status"], "no_changes")
        self.assertEqual(self.tree(), after)

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_rollback_restores_config_and_keeps_memory_notes_and_source_junction(self):
        original = self.existing_config()
        native = self.native_skill()
        native_bytes = native.read_bytes()
        self.ingress_contract()
        origin = self.source_skill("linked", self.base / "skill-source")
        source_link = self.claude / "skills" / "linked"
        self.junction(source_link, origin)
        source_tree = self.tree(origin)
        report = self.apply()
        destination = self.skills / "linked"
        self.assertTrue(sync.linked(destination))
        self.assertEqual(destination.resolve(), origin.resolve())
        notes = list((self.codex / "memories" / "extensions" / "ad_hoc" / "notes").glob("*.md"))
        self.assertEqual(len(notes), 1)
        note_bytes = notes[0].read_bytes()
        result = sync.rollback(Path(report["cleanup_pending"]), self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertEqual(native.read_bytes(), native_bytes)
        self.assertFalse(destination.exists())
        self.assertTrue(sync.linked(source_link))
        self.assertEqual(self.tree(origin), source_tree)
        self.assertEqual(notes[0].read_bytes(), note_bytes)
        self.assertTrue(any(row["reason"] == "append_only_memory_note" for row in result["preserved"]))

    def test_existing_native_skill_collision_is_preserved(self):
        native = self.native_skill("same")
        original = native.read_bytes()
        self.source_skill("same")
        changes, report = self.plan()
        row = next(row for row in report["skills"] if row["name"] == "same")
        self.assertEqual(row["status"], "conflict")
        self.assertFalse(any(Path(row["path"]).is_relative_to(native.parent) for row in changes))
        sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual(native.read_bytes(), original)

    def test_existing_legacy_codex_skill_is_preserved(self):
        original = b"---\nname: example\ndescription: Native legacy skill.\n---\nKeep this.\n"
        target = self.codex / "skills" / "example" / "SKILL.md"
        self.write(target, original)
        self.source_skill("example")
        changes, report = self.plan()
        row = next(row for row in report["skills"] if row["name"] == "example")
        self.assertEqual(row["status"], "conflict")
        self.assertEqual(row["reason"], "existing_legacy_codex_skill_preserved")
        self.assertFalse(any(Path(row["path"]) == self.skills / "example" for row in changes))
        self.assertEqual(target.read_bytes(), original)

    def prepare_upgrade(self):
        first = self.source_skill("upgraded", self.base / "install-v1")
        second = self.source_skill("upgraded", self.base / "install-v2")
        self.write(second / "scripts" / "helper.py", b"print('version two')\n")
        source_link = self.claude / "skills" / "upgraded"
        self.junction(source_link, first)
        self.apply()
        state = self.codex / "claude-sync" / "managed-skills.json"
        before_state = state.read_bytes()
        if os.name == "nt" and not source_link.is_symlink():
            os.rmdir(source_link)
        else:
            source_link.unlink()
        self.junction(source_link, second)
        return first, second, source_link, state, before_state

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_managed_skill_upgrade_relinks_and_rollback_restores_previous_link(self):
        first, second, source_link, state, before_state = self.prepare_upgrade()
        first_contents, second_contents = self.tree(first), self.tree(second)
        destination = self.skills / "upgraded"
        changes, report = self.plan()
        row = next(row for row in report["skills"] if row["name"] == "upgraded")
        self.assertEqual(row["status"], "relink")
        result = sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual(destination.resolve(), second.resolve())
        self.assertEqual(self.plan()[0], [])
        sync.rollback(Path(result["cleanup_pending"]), self.codex, self.skills)
        self.assertTrue(sync.linked(destination))
        self.assertEqual(destination.resolve(), first.resolve())
        self.assertEqual(source_link.resolve(), second.resolve())
        self.assertEqual(state.read_bytes(), before_state)
        self.assertEqual(self.tree(first), first_contents)
        self.assertEqual(self.tree(second), second_contents)

    def test_managed_skill_relink_creation_failure_restores_previous_link(self):
        first, second, source_link, state, before_state = self.prepare_upgrade()
        destination = self.skills / "upgraded"
        changes, report = self.plan()
        real_link = sync.make_link

        def fail_upgrade(path, source):
            if path == destination and source.resolve() == second.resolve():
                raise OSError("Synthetic upgraded junction failure")
            real_link(path, source)

        with patch.object(sync, "make_link", side_effect=fail_upgrade):
            with self.assertRaises(OSError):
                sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertTrue(sync.linked(destination))
        self.assertEqual(destination.resolve(), first.resolve())
        self.assertEqual(source_link.resolve(), second.resolve())
        self.assertEqual(state.read_bytes(), before_state)

    def test_colliding_command_adapter_names_are_reported_without_overwrite(self):
        self.write(self.claude / "commands" / "a-b.md", b"First workflow.\n")
        self.write(self.claude / "commands" / "a" / "b.md", b"Second workflow.\n")
        changes, report = self.plan()
        rows = [row for row in report["skills"] if row["name"] == "claude-user-a-b"]
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(row["status"] == "conflict" for row in rows), 1)
        target = self.skills / "claude-user-a-b" / "SKILL.md"
        self.assertEqual(sum(Path(row["path"]) == target for row in changes), 1)

    def test_managed_instruction_edits_are_preserved(self):
        self.apply()
        path = self.codex / "AGENTS.md"
        edited = path.read_bytes().replace(b"Use concise, factual replies.", b"User edited imported instructions.")
        path.write_bytes(edited)
        changes, report = self.plan()
        self.assertEqual(report["instructions"]["status"], "conflict")
        self.assertFalse(any(Path(row["path"]) == path for row in changes))
        self.assertEqual(path.read_bytes(), edited)

    def test_managed_command_adapter_edits_are_preserved(self):
        target = self.command()
        self.apply()
        edited = target.read_bytes() + b"\nUser customization must survive.\n"
        target.write_bytes(edited)
        changes, report = self.plan()
        self.assertFalse(any(Path(row["path"]) == target for row in changes))
        row = next(row for row in report["skills"] if row["name"] == "claude-user-review")
        self.assertEqual(row["status"], "conflict")
        self.assertEqual(target.read_bytes(), edited)

    def test_unrelated_instruction_bytes_are_preserved(self):
        original = b"\xef\xbb\xbf# Existing personal instructions\r\nKeep this spacing.  \r\n\r\n"
        self.write(self.codex / "AGENTS.md", original)
        changes, _ = self.plan()
        planned = next(row["data"] for row in changes if Path(row["path"]) == self.codex / "AGENTS.md")
        self.assertTrue(planned.startswith(original))

    def test_destination_change_between_plan_and_apply_is_not_overwritten(self):
        self.existing_config()
        changes, report = self.plan()
        customized = b'model = "user-changed-after-preview"\n'
        self.write(self.codex / "config.toml", customized)
        with self.assertRaises(ValueError):
            sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), customized)
        self.assertFalse((self.codex / "AGENTS.md").exists())

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_rollback_preserves_files_changed_after_apply(self):
        self.existing_config()
        report = self.apply()
        changed = b'model = "post-apply-user-choice"\n'
        self.write(self.codex / "config.toml", changed)
        result = sync.rollback(Path(report["cleanup_pending"]), self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), changed)
        self.assertTrue(any(row["path"] == str(self.codex / "config.toml") and row["reason"] == "changed_since_sync" for row in result["preserved"]))

    def test_publication_failure_rolls_back_prior_reversible_writes(self):
        original = self.existing_config()
        adapter = self.command()
        self.ingress_contract()
        changes, report = self.plan()
        real_write = sync.atomic_write

        def fail_instructions_once(path, data):
            if path == self.codex / "AGENTS.md":
                raise OSError("Synthetic write failure")
            real_write(path, data)

        with patch.object(sync, "atomic_write", side_effect=fail_instructions_once):
            with self.assertRaises(OSError):
                sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertFalse(adapter.exists())
        self.assertFalse((self.codex / "AGENTS.md").exists())
        self.assertEqual(list((self.codex / "memories" / "extensions" / "ad_hoc" / "notes").glob("*.md")), [])

    def test_later_link_failure_removes_created_junction_without_touching_source(self):
        original = self.existing_config()
        first = self.source_skill("first")
        second = self.source_skill("second")
        before = self.tree(self.claude)
        changes, report = self.plan()
        real_link = sync.make_link

        def fail_second(path, source):
            if path.name == "second":
                raise OSError("Synthetic junction creation failure")
            real_link(path, source)

        with patch.object(sync, "make_link", side_effect=fail_second):
            with self.assertRaises(OSError):
                sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertFalse((self.skills / "first").exists())
        self.assertFalse((self.skills / "second").exists())
        self.assertTrue((first / "SKILL.md").exists())
        self.assertTrue((second / "SKILL.md").exists())
        self.assertEqual(self.tree(self.claude), before)

    def backup_root(self):
        return self.codex / "claude-sync" / "backups"

    def test_successful_apply_leaves_no_rollback_copy(self):
        # The owner keeps no backup copies: the copy exists only while the run can still fail.
        self.existing_config()
        report = self.apply()
        self.assertEqual(report["status"], "applied")
        self.assertNotIn("cleanup_pending", report)
        self.assertFalse(self.backup_root().exists())

    def test_failed_apply_rolls_back_then_removes_its_copy(self):
        original = self.existing_config()
        changes, report = self.plan()
        real_write = sync.atomic_write

        def fail_config(path, data):
            if path.name == "config.toml":
                raise OSError("Synthetic publication failure")
            real_write(path, data)

        with patch.object(sync, "atomic_write", side_effect=fail_config):
            with self.assertRaises(OSError):
                sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertFalse(self.backup_root().exists())

    def test_failed_rollback_keeps_the_copy_for_manual_recovery(self):
        self.existing_config()
        changes, report = self.plan()
        real_write = sync.atomic_write

        def fail_config(path, data):
            if path.name == "config.toml":
                raise OSError("Synthetic publication failure")
            real_write(path, data)

        def broken_rollback(*args, **kwargs):
            raise ValueError("Synthetic rollback failure")

        with patch.object(sync, "atomic_write", side_effect=fail_config), \
                patch.object(sync, "_rollback_locked", side_effect=broken_rollback):
            with self.assertRaises(ValueError):
                sync.apply_plan(changes, report, self.codex, self.skills)
        kept = list(self.backup_root().iterdir())
        self.assertEqual(len(kept), 1)
        self.assertTrue((kept[0] / "manifest.json").is_file())

    def test_manual_rollback_of_a_leftover_copy_removes_it(self):
        original = self.existing_config()
        with patch.object(sync, "_discard_backup", new=lambda backup: False):
            report = self.apply()
        backup = Path(report["cleanup_pending"])
        self.assertTrue(backup.is_dir())
        sync.rollback(backup, self.codex, self.skills)
        self.assertEqual((self.codex / "config.toml").read_bytes(), original)
        self.assertFalse(backup.exists())
        self.assertFalse(self.backup_root().exists())

    def test_discard_leaves_a_copy_with_unexpected_entries_in_place(self):
        backup = self.backup_root() / "synthetic-run"
        (backup / "nested").mkdir(parents=True)
        self.write(backup / "manifest.json", b"{}")
        self.assertFalse(sync._discard_backup(backup))
        self.assertTrue((backup / "manifest.json").is_file())
        self.assertTrue(sync._discard_backup(self.backup_root() / "absent"))

    def test_successful_apply_reports_pending_cleanup_without_changing_success(self):
        changes = make_profile_apply_fixture(self.codex)
        with patch.object(sync, "_discard_backup", return_value=False):
            report = sync.apply_plan(changes, {}, self.codex, self.skills)
        self.assertEqual(report["status"], "applied")
        self.assertTrue(Path(report["cleanup_pending"]).is_dir())
        self.assertNotIn("backup", report)
        self.assertNotIn("backup_kept", report)
        for row in changes:
            self.assertEqual(sync.snapshot(Path(row["path"])), row["after"])

    def test_partial_cleanup_reports_remnants_without_promising_rollback_copy(self):
        backup = self.backup_root() / "synthetic-run"
        destination = make_profile_rollback_fixture(backup, self.codex, self.skills)
        real_unlink, removed = os.unlink, []

        def fail_second_member(path, *args, **kwargs):
            if Path(path).parent == backup:
                removed.append(Path(path))
                if len(removed) == 2:
                    raise PermissionError("synthetic cleanup refusal")
            return real_unlink(path, *args, **kwargs)

        output = io.StringIO()
        with patch.object(sync.os, "unlink", side_effect=fail_second_member), redirect_stdout(output):
            code = sync.main(["--claude-home", str(self.claude), "--codex-home", str(self.codex),
                              "--skills-home", str(self.skills), "--rollback", str(backup)])
        self.assertEqual(code, 0)
        self.assertEqual(destination.read_bytes(), b"previous content")
        self.assertEqual(len(removed), 2)
        self.assertFalse(removed[0].exists())
        self.assertTrue(removed[1].is_file())
        self.assertIn("Cleanup pending; remaining files: " + str(backup), output.getvalue())
        self.assertNotIn("Rollback copy kept", output.getvalue())

    def test_failed_apply_rethrows_original_error_with_pending_cleanup_path(self):
        changes = make_profile_apply_fixture(self.codex)
        failure = OSError("synthetic private publication detail")
        real_write = sync.atomic_write

        def fail_second(path, data):
            if str(path) == changes[1]["path"]:
                raise failure
            return real_write(path, data)

        with patch.object(sync, "atomic_write", side_effect=fail_second), \
                patch.object(sync, "_discard_backup", return_value=False):
            with self.assertRaises(OSError) as caught:
                sync.apply_plan(changes, {}, self.codex, self.skills)
        self.assertIs(caught.exception, failure)
        backup = Path(sync.cleanup_error_context(failure)["cleanup_pending"])
        self.assertTrue((backup / "manifest.json").is_file())
        for row in changes:
            self.assertEqual(sync.snapshot(Path(row["path"])), row["before"])

    def test_failed_apply_cli_reports_cleanup_or_recovery_path_without_error_contents(self):
        for rollback_fails in (False, True):
            with self.subTest(rollback_fails=rollback_fails):
                changes = make_profile_apply_fixture(self.codex)
                failure = OSError("synthetic private publication detail")
                rollback_failure = ValueError("synthetic private recovery detail")
                real_write = sync.atomic_write

                def fail_second(path, data):
                    if str(path) == changes[1]["path"]:
                        raise failure
                    return real_write(path, data)

                output = io.StringIO()
                with patch.object(sync, "build_plan", return_value=(changes, {})), \
                        patch.object(sync, "atomic_write", side_effect=fail_second), \
                        patch.object(sync, "_rollback_locked", wraps=sync._rollback_locked) as restore, \
                        patch.object(sync, "_discard_backup", return_value=False) as discard, \
                        redirect_stderr(output):
                    if rollback_fails:
                        restore.side_effect = rollback_failure
                    code = sync.main(["--claude-home", str(self.claude), "--codex-home", str(self.codex),
                                      "--skills-home", str(self.skills), "--apply", "--json"])
                self.assertEqual(code, 1)
                error = json.loads(output.getvalue())
                expected = rollback_failure if rollback_fails else failure
                self.assertEqual(error["error_type"], type(expected).__name__)
                key = "recovery_required" if rollback_fails else "cleanup_pending"
                backup = Path(error[key])
                self.assertEqual(sync.cleanup_error_context(expected), {key: str(backup)})
                self.assertNotIn("synthetic private", output.getvalue())
                manifest = json.loads((backup / "manifest.json").read_bytes())
                for row in manifest["changes"]:
                    self.assertEqual(sync.snapshot(backup / row["backup_file"]), row["before"])
                if rollback_fails:
                    discard.assert_not_called()
                    self.assertEqual(sync.snapshot(Path(changes[0]["path"])), changes[0]["after"])
                else:
                    discard.assert_called_once()
                    self.assertEqual(sync.snapshot(Path(changes[0]["path"])), changes[0]["before"])

    def test_discard_rejects_linked_backup_root_and_preserves_target(self):
        target = self.base / "other-backup"
        make_profile_rollback_fixture(target, self.codex, self.skills)
        backup = self.backup_root() / "synthetic-run"
        self.junction(backup, target)
        before = self.tree(target)
        self.assertFalse(sync._discard_backup(backup))
        self.assertEqual(self.tree(target), before)
        self.assertTrue(sync.linked(backup))

    def test_discard_rejects_linked_ancestor_and_preserves_target(self):
        target = self.base / "other-backups"
        make_profile_rollback_fixture(target / "synthetic-run", self.codex, self.skills)
        self.junction(self.backup_root(), target)
        before = self.tree(target)
        self.assertFalse(sync._discard_backup(self.backup_root() / "synthetic-run"))
        self.assertEqual(self.tree(target), before)
        self.assertTrue(sync.linked(self.backup_root()))

    def test_discard_rejects_regular_file_as_backup_root(self):
        backup = self.backup_root() / "synthetic-run"
        make_profile_invalid_backup_root(backup)
        self.assertFalse(sync._discard_backup(backup))
        self.assertEqual(backup.read_bytes(), b"keep ordinary file")

    def test_discard_stat_failure_preflights_all_entries_before_deletion(self):
        backup = self.backup_root() / "synthetic-run"
        make_profile_rollback_fixture(backup, self.codex, self.skills)
        before = self.tree(backup)
        with os.scandir(backup) as scan:
            entries = list(scan)
        for error in (PermissionError("synthetic stat denied"),
                      FileNotFoundError("synthetic entry disappeared")):
            with self.subTest(error=type(error).__name__):
                with patch.object(sync.os, "scandir") as scan:
                    bad_entry = Mock(wraps=entries[-1])
                    bad_entry.stat.side_effect = error
                    checked = [*entries[:-1], bad_entry]
                    scan.return_value.__iter__.return_value = iter(checked)
                    scan.return_value.__enter__.return_value = iter(checked)
                    self.assertFalse(sync._discard_backup(backup))
                    bad_entry.stat.assert_called_once_with(follow_symlinks=False)
                self.assertEqual(self.tree(backup), before)

    def test_rollback_rejects_linked_backup_root_before_restoring(self):
        backup = self.backup_root() / "synthetic-run"
        destination = make_profile_rollback_fixture(backup, self.codex, self.skills)
        target = backup.with_name("other-run")
        backup.rename(target)
        self.junction(backup, target)
        before = self.tree(target)
        with self.assertRaisesRegex(ValueError, "linked"):
            sync.rollback(backup, self.codex, self.skills)
        self.assertEqual(destination.read_bytes(), b"current content")
        self.assertEqual(self.tree(target), before)
        self.assertTrue(sync.linked(backup))

    def test_rollback_rejects_linked_backup_ancestor_before_restoring(self):
        backup = self.backup_root() / "synthetic-run"
        destination = make_profile_rollback_fixture(backup, self.codex, self.skills)
        target = self.backup_root().with_name("other-backups")
        self.backup_root().rename(target)
        self.junction(self.backup_root(), target)
        before = self.tree(target)
        with self.assertRaisesRegex(ValueError, "linked"):
            sync.rollback(backup, self.codex, self.skills)
        self.assertEqual(destination.read_bytes(), b"current content")
        self.assertEqual(self.tree(target), before)
        self.assertTrue(sync.linked(self.backup_root()))

    def test_rollback_rejects_linked_manifest_and_payload(self):
        for member in ("manifest.json", "payload.bin"):
            with self.subTest(member=member):
                backup = self.backup_root() / member
                destination = make_profile_rollback_fixture(backup, self.codex, self.skills)
                link = backup / member
                target = backup / (member + ".original")
                link.rename(target)
                try:
                    link.symlink_to(target)
                except OSError as exc:
                    if getattr(exc, "winerror", None) == 1314:
                        self.skipTest("Windows file symlinks require a privilege unavailable here")
                    raise
                self.addCleanup(link.unlink, missing_ok=True)
                before = self.tree(backup)
                with self.assertRaisesRegex(ValueError, "linked"):
                    sync.rollback(backup, self.codex, self.skills)
                self.assertEqual(destination.read_bytes(), b"current content")
                self.assertEqual(self.tree(backup), before)

    def test_output_home_junction_is_rejected_without_writes(self):
        make_profile_hook_source(self.claude, sys.executable)
        outside = self.base / "outside"
        outside.mkdir()
        self.junction(self.codex, outside)
        before = self.tree()
        with self.assertRaises(ValueError):
            self.plan()
        self.assertEqual(self.tree(), before)

    def test_lock_directory_junction_is_rejected_before_lock_creation(self):
        outside = self.base / "outside"
        outside.mkdir()
        self.junction(self.codex / "claude-sync", outside)
        before = self.tree(outside)
        with self.assertRaises(ValueError):
            with sync.destination_lock(self.codex):
                pass
        self.assertEqual(self.tree(outside), before)

    def test_backup_directory_junction_is_rejected_before_mkdir(self):
        self.existing_config()
        outside = self.base / "outside"
        outside.mkdir()
        self.junction(self.codex / "claude-sync" / "backups", outside)
        before = self.tree(outside)
        changes, report = self.plan()
        with self.assertRaises(ValueError):
            sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual(self.tree(outside), before)

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_rollback_rejects_manifest_paths_outside_destination_roots(self):
        report = self.apply()
        backup = Path(report["cleanup_pending"])
        manifest = json.loads((backup / "manifest.json").read_text())
        outside = self.base / "outside.txt"
        self.write(outside, b"Do not touch.\n")
        manifest["changes"].append({
            "path": str(outside), "before": {"kind": "missing"},
            "after": sync.snapshot(outside), "append_only": False,
        })
        self.write_json(backup / "manifest.json", manifest)
        with self.assertRaises(ValueError):
            sync.rollback(backup, self.codex, self.skills)
        self.assertEqual(outside.read_bytes(), b"Do not touch.\n")

    def test_disabled_plugin_is_not_imported(self):
        plugin = self.base / "plugin"
        self.source_skill("plugin-example", plugin / "skills" / "plugin-example")
        self.write_json(plugin / ".mcp.json", {"plugin-server": {"command": "fixture"}})
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        self.write_json(self.claude / "plugins" / "installed_plugins.json", {"plugins": {"fixture@local": [{"scope": "user", "installPath": str(plugin)}]}})
        changes, report = self.plan()
        self.assertFalse(any(row["name"] == "plugin-example" for row in report["skills"]))
        self.assertFalse(any(row["name"] == "plugin-server" for row in report["config"]["mcp"]))
        self.assertEqual(report["plugins_skipped"][0]["reason"], "disabled_in_claude")

    def plugin_fixture(self):
        plugin = self.base / "plugin"
        self.source_skill("plugin-example", plugin / "skills" / "plugin-example")
        self.write(plugin / "commands" / "review.md", b"Review the synthetic input.\n")
        self.write_json(plugin / ".mcp.json", {"plugin-server": {"command": "fixture"}})
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": True}})
        self.write_json(self.claude / "plugins" / "installed_plugins.json", {"plugins": {"fixture@local": [{"scope": "user", "installPath": str(plugin)}]}})
        return plugin

    @patch.object(sync, "_discard_backup", new=lambda backup: False)  # an interrupted run left its copy
    def test_disabled_plugin_retires_owned_artifacts_and_rollback_restores_them(self):
        plugin = self.plugin_fixture()
        self.apply()
        link = self.skills / "plugin-example"
        adapter = self.skills / "claude-fixture-review" / "SKILL.md"
        previous = adapter.read_bytes()
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        changes, report = self.plan()
        self.assertEqual({Path(x["path"]) for x in changes if x["after"]["kind"] == "missing"}, {link, adapter})
        result = sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertFalse(sync.linked(link))
        self.assertFalse(adapter.exists())
        self.assertTrue((plugin / "skills/plugin-example/SKILL.md").is_file())
        self.assertNotIn("plugin-server", tomllib.loads((self.codex / "config.toml").read_text()).get("mcp_servers", {}))
        self.assertEqual(self.plan()[0], [])
        sync.rollback(Path(result["cleanup_pending"]), self.codex, self.skills)
        self.assertTrue(sync.linked(link))
        self.assertEqual(adapter.read_bytes(), previous)

    def test_missing_plugin_install_preserves_owned_artifacts_as_unavailable(self):
        self.plugin_fixture()
        self.apply()
        self.write_json(self.claude / "plugins/installed_plugins.json", {"plugins": {}})
        changes, report = self.plan()
        self.assertFalse(any(x["after"]["kind"] == "missing" for x in changes))
        self.assertTrue(any(x["status"] == "unavailable" for x in report["skills"]))
        self.assertTrue(any(x.get("status") == "unavailable" for x in report["config"]["mcp"]))

    def test_removed_source_retires_adapter_but_preserves_user_edits(self):
        adapter = self.command()
        self.source_skill()
        self.apply()
        (self.claude / "commands/review.md").unlink()
        (self.claude / "skills/example/SKILL.md").unlink()
        adapter.write_bytes(adapter.read_bytes() + b"User customization.\n")
        changes, report = self.plan()
        self.assertFalse(any(Path(x["path"]) in {self.skills / "example", adapter} for x in changes))
        self.assertTrue(any(x.get("reason") == "managed_skill_group_modified_missing_or_unknown" for x in report["skills"]))

    def test_legacy_link_manifest_supports_retirement(self):
        plugin = self.plugin_fixture()
        target = self.skills / "plugin-example"
        # The legacy producer used the same resolved source in the link and map.
        self.junction(target, (plugin / "skills/plugin-example").resolve())
        self.write_json(self.codex / "claude-sync/managed-skills.json", {str(target): str(target.resolve())})
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        changes, _ = self.plan()
        self.assertTrue(any(Path(x["path"]) == target and x["after"]["kind"] == "missing" for x in changes))

    def test_owned_link_in_legacy_codex_root_can_retire(self):
        plugin = self.plugin_fixture()
        target = self.codex / "skills/plugin-example"
        self.junction(target, (plugin / "skills/plugin-example").resolve())
        self.write_json(self.codex / "claude-sync/managed-skills.json", {str(target): str(target.resolve())})
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        changes, report = self.plan()
        self.assertTrue(any(Path(x["path"]) == target and x["after"]["kind"] == "missing" for x in changes))
        sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertFalse(sync.linked(target))

    def test_missing_artifact_manifest_does_not_reclaim_adapter_on_disable(self):
        self.plugin_fixture()
        self.apply()
        (self.codex / "claude-sync/managed-artifacts.json").unlink()
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        changes, report = self.plan()
        adapter = self.skills / "claude-fixture-review/SKILL.md"
        self.assertFalse(any(Path(x["path"]) == adapter for x in changes))
        self.assertTrue(adapter.is_file())

    def test_missing_plugin_cache_preserves_links_and_adapters(self):
        plugin = self.plugin_fixture()
        self.apply()
        plugin.rename(self.base / "cache-unmounted")
        changes, report = self.plan()
        self.assertFalse(any(x["after"]["kind"] == "missing" for x in changes))
        self.assertTrue(any(x["status"] == "unavailable" and x["reason"] == "plugin_install_unavailable" for x in report["skills"]))

    def test_disabled_plugin_preserves_modified_link_and_adapter(self):
        self.plugin_fixture()
        self.apply()
        target = self.skills / "plugin-example"
        sync.remove_entry(target)
        new_source = self.source_skill("custom", self.base / "custom")
        self.junction(target, new_source)
        adapter = self.skills / "claude-fixture-review/SKILL.md"
        adapter.write_bytes(adapter.read_bytes() + b"User edited this file.\n")
        self.write_json(self.claude / "settings.json", {"enabledPlugins": {"fixture@local": False}})
        changes, report = self.plan()
        self.assertFalse(any(Path(x["path"]) in {target, adapter} for x in changes))
        self.assertEqual(sum(x["status"] == "conflict" for x in report["skills"]), 2)

    def test_reenabled_plugin_restores_retired_adapter_without_removing_sibling_files(self):
        self.plugin_fixture()
        self.apply()
        adapter = self.skills / 'claude-fixture-review/SKILL.md'
        sibling = adapter.parent / 'custom.txt'
        self.write(sibling, b'Keep this sibling file.')
        self.write_json(self.claude / 'settings.json', {'enabledPlugins': {'fixture@local': False}})
        self.apply()
        self.assertFalse(adapter.exists())
        self.write_json(self.claude / 'settings.json', {'enabledPlugins': {'fixture@local': True}})
        self.apply()
        self.assertTrue(adapter.is_file())
        self.assertEqual(sibling.read_bytes(), b'Keep this sibling file.')
        self.assertEqual(self.plan()[0], [])

    def test_deletion_failure_restores_prior_deleted_file_and_link(self):
        source = self.source_skill()
        self.junction(self.skills / "example", source)
        file = self.codex / "owned.txt"
        final = self.codex / "later.txt"
        self.write(file, b"restore me")
        changes = [{"path": str(p), "before": sync.snapshot(p), "after": {"kind": "missing"}, "append_only": False} for p in (file, self.skills / "example")]
        changes.append({"path": str(final), "before": {"kind": "missing"}, "after": {"kind": "file", "sha256": sync.digest(b"new")}, "data": b"new", "append_only": False})
        real_write = sync.atomic_write
        def fail_last(path, data):
            if path == final:
                raise OSError("Synthetic write failure")
            real_write(path, data)
        with patch.object(sync, "atomic_write", side_effect=fail_last):
            with self.assertRaises(OSError):
                sync.apply_plan(changes, {}, self.codex, self.skills)
        self.assertEqual(file.read_bytes(), b"restore me")
        self.assertEqual((self.skills / "example").resolve(), source.resolve())
        self.assertTrue(sync.linked(self.skills / "example"))

    def test_deletion_refuses_real_directory_and_append_only_note(self):
        directory = self.codex / "directory"
        self.write(directory / "keep.txt", b"keep")
        note = self.codex / "memories/extensions/ad_hoc/notes/note.md"
        self.write(note, b"permanent note")
        for path in (directory, note):
            changes = [{"path": str(path), "before": sync.snapshot(path), "after": {"kind": "missing"}, "append_only": path == note}]
            with self.assertRaises(ValueError):
                sync.apply_plan(changes, {}, self.codex, self.skills)
        self.assertEqual((directory / "keep.txt").read_bytes(), b"keep")
        self.assertEqual(note.read_bytes(), b"permanent note")


if __name__ == "__main__":
    unittest.main()
