"""Only synthetic scripts run here; never import a real user's hooks."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from profile_hooks import _windows_split, plan_hooks


class HookPlanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude = self.base / "source home"
        self.codex = self.base / "target home"
        (self.claude / "scripts").mkdir(parents=True)
        self.codex.mkdir()

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def source(self, contents="print('private-cookie-value')\n", name="pw-auth.py", tail=None):
        script = self.claude / "scripts" / name
        script.write_text(contents, encoding="utf-8")
        if tail is None:
            tail = ["absorb"] if name == "pw-auth.py" else []
        command = subprocess.list2cmdline([sys.executable, str(script), *tail])
        return {"type": "command", "command": command, "timeout": 2}

    def configure(self, entries, other=None):
        hooks = {"Stop": [{"matcher": "*", "hooks": entries}]}
        hooks.update(other or {})
        self.write_json(self.claude / "settings.json", {"hooks": hooks})

    def plan(self):
        return plan_hooks(self.claude, self.codex)

    @staticmethod
    def apply(outputs):
        for path, contents in outputs.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(contents)

    def run_bridge(self):
        bridge = self.codex / "imports" / "claude-hooks" / "stop_bridge.py"
        result = subprocess.run([sys.executable, str(bridge)], capture_output=True, text=True, check=True)
        self.assertEqual(result.stderr, "")
        self.assertNotIn("private-cookie-value", result.stdout)
        return json.loads(result.stdout)

    def test_windows_quotes_and_backslashes_round_trip(self):
        args = [r"C:\Program Files\Python\python.exe", r"C:\user dir\.claude\scripts\pw-auth.py", "absorb"]
        self.assertEqual(_windows_split(subprocess.list2cmdline(args)), args)
        args = ["python", "", "a\\", 'a\\"quoted', "space \\"]
        self.assertEqual(_windows_split(subprocess.list2cmdline(args)), args)
        with self.assertRaises(ValueError):
            _windows_split('python "unterminated')

    def test_planning_has_no_writes_or_hook_execution(self):
        sentinel = self.base / "executed"
        self.configure([self.source(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n")])
        outputs, report = self.plan()
        self.assertEqual(len(outputs), 3)
        self.assertFalse(sentinel.exists())
        self.assertFalse((self.codex / "hooks.json").exists())
        self.assertTrue(report["requires_hooks_feature"])
        self.assertEqual(report["registered"], 1)

    def test_success_json_suppresses_original_stdout(self):
        self.configure([self.source()])
        outputs, report = self.plan()
        self.apply(outputs)
        self.assertEqual(self.run_bridge(), {})
        self.assertNotIn("private-cookie-value", json.dumps(report))

    def test_nonzero_and_guard_advisory_remain_distinct_nonblocking_statuses(self):
        failing = self.source("import sys\nprint('private-cookie-value')\nsys.exit(2)\n")
        warning = self.source("import sys\nsys.stderr.write('private-cookie-value')\n", "pw_export_guard.py")
        self.configure([failing, warning])
        self.apply(self.plan()[0])
        result = self.run_bridge()
        self.assertTrue(result["systemMessage"].startswith("pw-auth.py: failed; pw_export_guard.py: reminder:"))
        self.assertNotIn("pw_export_guard.py: failed", result["systemMessage"])
        self.assertTrue(result["suppressOutput"])
        self.assertNotIn("decision", result)

    def test_export_guard_advisory_is_fixed_sanitized_and_actionable(self):
        messages = []
        for raw in ("synthetic-private-path 12:34:56", "different-private-path 23:45:01"):
            self.configure([self.source(
                f"import sys\nprint({raw!r}, file=sys.stderr)\n", "pw_export_guard.py")])
            self.apply(self.plan()[0])
            result = self.run_bridge()
            message = result["systemMessage"]
            self.assertIn("reminder:", message)
            self.assertNotIn("failed", message)
            for instruction in ("entire context", "storageState", "indexedDB:true",
                                "unique", "auth incoming directory", "pw-auth.py absorb", "before closing"):
                self.assertIn(instruction, message)
            self.assertNotIn(raw, json.dumps(result))
            self.assertNotIn(str(self.base), json.dumps(result))
            self.assertEqual(set(result), {"systemMessage", "suppressOutput"})
            messages.append(message)
        self.assertEqual(*messages)

    def test_export_guard_nonzero_and_source_exception_remain_failures(self):
        for code in ("import sys\nsys.exit(2)\n",
                     "import sys\nprint('synthetic-secret', file=sys.stderr)\nsys.exit(2)\n",
                     "raise RuntimeError('synthetic-secret')\n"):
            with self.subTest(code=code):
                self.configure([self.source(code, "pw_export_guard.py")])
                self.apply(self.plan()[0])
                self.assertEqual(self.run_bridge(), {
                    "systemMessage": "pw_export_guard.py: failed", "suppressOutput": True})

    def test_export_guard_source_check_exception_remains_failure(self):
        self.configure([self.source("pass\n", "pw_export_guard.py")])
        self.apply(self.plan()[0])
        (self.claude / "scripts/pw_export_guard.py").unlink()
        self.assertEqual(self.run_bridge(), {
            "systemMessage": "pw_export_guard.py: failed", "suppressOutput": True})

    def test_export_guard_no_stderr_stays_silent(self):
        for code in ("pass\n", "print('synthetic-private-stdout')\n"):
            with self.subTest(code=code):
                self.configure([self.source(code, "pw_export_guard.py")])
                self.apply(self.plan()[0])
                self.assertEqual(self.run_bridge(), {})

    def test_other_stop_job_zero_return_stderr_remains_failure(self):
        self.configure([self.source("import sys\nprint('synthetic-secret', file=sys.stderr)\n")])
        self.apply(self.plan()[0])
        self.assertEqual(self.run_bridge(), {
            "systemMessage": "pw-auth.py: failed", "suppressOutput": True})

    def test_changed_source_does_not_execute_without_resync(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        sentinel = self.base / "executed"
        self.source(f"from pathlib import Path\nPath({str(sentinel)!r}).write_text('ran')\n")
        self.assertEqual(self.run_bridge()["systemMessage"], "pw-auth.py: failed")
        self.assertFalse(sentinel.exists())

    def test_child_timeout_is_fixed_nonblocking_json(self):
        hook = self.source("import time\nprint('private-cookie-value', flush=True)\ntime.sleep(5)\n")
        hook["timeout"] = 0.2
        self.configure([hook])
        self.apply(self.plan()[0])
        self.assertEqual(self.run_bridge()["systemMessage"], "pw-auth.py: failed")

    def test_source_update_replaces_only_owned_artifacts(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        hooks_before = (self.codex / "hooks.json").read_bytes()
        self.source("print('different-success-output')\n")
        updates, report = self.plan()
        self.assertNotIn(self.codex / "hooks.json", updates)
        self.assertEqual(report["hooks"][0]["status"], "updated")
        self.apply(updates)
        self.assertEqual((self.codex / "hooks.json").read_bytes(), hooks_before)
        self.assertEqual(self.run_bridge(), {})

    def test_preserves_existing_hooks_and_is_idempotent(self):
        existing = {"customMetadata": "keep", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "keep-start"}]}], "Stop": [{"hooks": [{"type": "command", "command": "keep-stop"}]}]}}
        self.write_json(self.codex / "hooks.json", existing)
        self.configure([self.source(), self.source(name="pw_export_guard.py")])
        outputs, _ = self.plan()
        merged = json.loads(outputs[self.codex / "hooks.json"])
        self.assertEqual(merged["customMetadata"], "keep")
        self.assertEqual(merged["hooks"]["SessionStart"], existing["hooks"]["SessionStart"])
        self.assertEqual(merged["hooks"]["Stop"][0], existing["hooks"]["Stop"][0])
        self.assertEqual(len(merged["hooks"]["Stop"]), 2)
        self.apply(outputs)
        repeated, report = self.plan()
        self.assertEqual(repeated, {})
        self.assertFalse(report["changed"])

    def test_user_changes_to_managed_bridge_or_handler_are_preserved(self):
        self.configure([self.source()])
        outputs, _ = self.plan()
        self.apply(outputs)
        bridge = self.codex / "imports" / "claude-hooks" / "stop_bridge.py"
        original = bridge.read_bytes()
        bridge.write_bytes(original + b"# user edit\n")
        self.assertEqual(self.plan()[0], {})
        self.assertIn("modified_by_user", self.plan()[1]["warnings"][0]["reason"])
        bridge.write_bytes(original)
        document = json.loads((self.codex / "hooks.json").read_text())
        document["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 999
        self.write_json(self.codex / "hooks.json", document)
        self.assertEqual(self.plan()[0], {})
        self.assertIn("managed_handler_modified", self.plan()[1]["warnings"][0]["reason"])

    def test_invalid_existing_hooks_are_not_overwritten(self):
        self.configure([self.source()])
        for contents in ('{"hooks":', '{"hooks": {"Stop": "invalid"}}', '[]'):
            (self.codex / "hooks.json").write_text(contents)
            outputs, report = self.plan()
            self.assertEqual(outputs, {})
            self.assertTrue(report["warnings"])
            self.assertEqual((self.codex / "hooks.json").read_text(), contents)

    def test_unreviewed_commands_paths_and_extra_arguments_are_rejected(self):
        approved = self.source()
        outside = self.base / "pw-auth.py"
        outside.write_text("pass\n")
        entries = [
            {**approved, "command": approved["command"] + " & echo private-cookie-value"},
            {**approved, "command": subprocess.list2cmdline([sys.executable, str(outside), "absorb"])},
            self.source(name="unreviewed.py"),
            self.source(tail=["status"]),
        ]
        self.configure(entries)
        outputs, report = self.plan()
        self.assertEqual(outputs, {})
        self.assertEqual(report["registered"], 0)
        self.assertNotIn("private-cookie-value", json.dumps(report))

    def test_non_stop_hooks_are_inventory_only_and_duplicates_are_removed(self):
        hook = self.source()
        self.configure([hook, hook], {"SessionStart": [{"hooks": [hook]}], "PostToolUse": [{"hooks": [hook]}]})
        outputs, report = self.plan()
        self.assertEqual(report["registered"], 1)
        self.assertEqual(len(json.loads(outputs[self.codex / "hooks.json"])["hooks"]["Stop"]), 1)
        self.assertEqual([row["status"] for row in report["hooks"]], ["added", "skipped", "unsupported", "unsupported"])

    def test_windows_launcher_uses_encoded_powershell_and_argv_bridge(self):
        self.configure([self.source()])
        outputs, _ = self.plan()
        handler = json.loads(outputs[self.codex / "hooks.json"])["hooks"]["Stop"][0]["hooks"][0]
        self.assertIn("-EncodedCommand", handler["commandWindows"])
        self.assertIn(b"shell=False", outputs[self.codex / "imports" / "claude-hooks" / "stop_bridge.py"])

    def test_removed_commands_retire_only_intact_managed_wrapper(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        self.configure([])
        outputs, report = self.plan()
        self.assertEqual(json.loads(outputs[self.codex / "hooks.json"])["hooks"]["Stop"], [])
        self.assertIsNone(outputs[self.codex / "imports/claude-hooks/stop_bridge.py"])
        self.assertTrue(any(row["status"] == "retired" for row in report["hooks"]))

    def test_unavailable_declared_hook_is_preserved(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        (self.claude / "scripts/pw-auth.py").unlink()
        outputs, report = self.plan()
        self.assertEqual(outputs, {})
        self.assertTrue(any(row["status"] == "unavailable" for row in report["hooks"]))

    def test_removed_hook_with_user_changed_bridge_is_preserved(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        bridge = self.codex / "imports/claude-hooks/stop_bridge.py"
        bridge.write_bytes(bridge.read_bytes() + b"# user edit\n")
        self.configure([])
        self.assertEqual(self.plan()[0], {})
        self.assertTrue(any("modified_by_user" in row["reason"] for row in self.plan()[1]["warnings"]))

    def test_removed_hook_with_edited_manifest_is_preserved(self):
        self.configure([self.source()])
        self.apply(self.plan()[0])
        manifest = self.codex / 'imports/claude-hooks/manifest.json'
        changed = json.loads(manifest.read_text())
        changed['custom'] = 'user edit'
        self.write_json(manifest, changed)
        self.configure([])
        self.assertEqual(self.plan()[0], {})


if __name__ == "__main__":
    unittest.main()
