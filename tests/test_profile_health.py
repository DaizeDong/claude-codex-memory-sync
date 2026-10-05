"""Health assertions use synthetic profiles and mocked MCP transport only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import profile_health as health
import run_profile_sync as runner


def report():
    return {"change_count": 0, "skills": [], "plugins_skipped": [],
            "config": {"mcp": [], "settings": []}, "hooks": {"hooks": []},
            "instructions": {"status": "unchanged"}, "memory": {"status": "unchanged"}}


class HealthTests(unittest.TestCase):
    def test_rc_zero_does_not_hide_conflict(self):
        r = report()
        r["skills"] = [{"name": "fixture", "status": "conflict", "reason": "managed_was_edited"}]
        self.assertEqual(health.assess(r)["status"], "degraded")

    def test_deliberate_native_preservation_is_exclusion(self):
        r = report()
        r["skills"] = [{"name": "fixture", "status": "conflict", "reason": "existing_legacy_codex_skill_preserved"}]
        r["config"]["mcp"] = [{"name": "fixture", "status": "unsupported", "reason": "recursive_codex_mcp_server_not_imported"}]
        self.assertEqual(health.assess(r)["status"], "healthy")
        self.assertEqual(len(health.assess(r)["exclusions"]), 2)

    def test_missing_credentials_are_not_compatibility_success(self):
        r = report()
        r["config"]["mcp"] = [{"name": "fixture", "status": "unsupported", "reason": "unresolved_environment_references"}]
        self.assertEqual(health.assess(r)["status"], "degraded")

    def test_pending_post_apply_changes_fail_verification(self):
        r = report()
        r["change_count"] = 1
        self.assertEqual(health.assess(r)["status"], "degraded")

    def test_ignored_invalid_input_is_a_health_failure(self):
        r = report()
        r["config"]["warnings"] = [{"reason": "invalid_or_unreadable_json"}]
        self.assertEqual(health.assess(r)["status"], "degraded")

    def test_hook_feature_merge_failure_is_not_healthy(self):
        r = report()
        r['hooks'].update(requires_hooks_feature=True, feature='feature_table_requires_manual_merge')
        self.assertEqual(health.assess(r)['status'], 'degraded')

    def test_inventory_missing_interpreter_is_a_health_failure(self):
        r = report()
        r["inventory"] = {"skills": [{"name": "fixture", "status": "available",
            "dependencies": {"executables": [{"name": "fixture-executable", "available": False}]}}]}
        self.assertEqual(health.assess(r)["status"], "degraded")

    def test_remote_mcp_is_not_contacted_or_claimed_verified(self):
        with patch.object(health, "probe_local", side_effect=AssertionError("network")):
            result = health.check_mcp({"remote": {"url": "https://example.com/mcp?token=secret"}}, probe=True)
        self.assertEqual(result[0]["status"], "not_checked")
        self.assertNotIn("secret", json.dumps(result))

    def test_failed_probe_reports_type_not_response_or_url(self):
        with patch.object(health.urllib.request, "build_opener", side_effect=OSError("secret-body")):
            result = health.probe_local("fixture", {"url": "http://127.0.0.1:9000/mcp?token=secret"})
        self.assertEqual(result["status"], "unavailable")
        self.assertNotIn("secret", json.dumps(result))

    def test_mcp_canonical_redirect_preserves_post_and_stays_same_origin(self):
        request = health.urllib.request.Request('http://127.0.0.1:9000/mcp', data=b'{}', method='POST')
        redirected = health._LocalRedirect().redirect_request(request, None, 307, '', {}, 'http://127.0.0.1:9000/mcp/')
        self.assertEqual(redirected.get_method(), 'POST')
        self.assertEqual(redirected.data, b'{}')
        with self.assertRaises(OSError):
            health._LocalRedirect().redirect_request(request, None, 307, '', {}, 'https://example.com/mcp')

    def test_source_failures_preserve_previous_success_and_report_current_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            c, x, s = root / "claude", root / "codex", root / "skills"
            success = x / "claude-sync/last-success.json"
            success.parent.mkdir(parents=True)
            success.write_text('{"status":"healthy"}')
            with patch.object(runner, "build_plan", side_effect=ValueError("private-input")):
                outcome = runner.run(c, x, s, apply=True, write_status=True)
            self.assertEqual(outcome["exit_code"], 1)
            self.assertEqual(success.read_text(), '{"status":"healthy"}')
            stored = json.loads((success.parent / "last-run.json").read_text())
            self.assertEqual(stored["status"], "error")
            self.assertNotIn("private-input", json.dumps(stored))

    def test_lock_contention_preserves_active_runner_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            success = root / "codex/claude-sync/last-success.json"
            success.parent.mkdir(parents=True)
            success.write_bytes(b"existing marker")
            with patch.object(runner, "destination_lock", side_effect=RuntimeError("busy")):
                outcome = runner.run(root/"claude", root/"codex", root/"skills", apply=True, write_status=True)
            self.assertEqual(outcome["exit_code"], 1)
            self.assertEqual(success.read_bytes(), b"existing marker")

    def test_interruption_before_publish_retains_last_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            success = root / 'codex/claude-sync/last-success.json'
            success.parent.mkdir(parents=True)
            success.write_bytes(b'{"status":"healthy","finished_at":"prior-run"}')
            with patch.object(runner, 'build_plan', side_effect=SystemExit(77)):
                with self.assertRaises(SystemExit):
                    runner.run(root/'claude', root/'codex', root/'skills', apply=True, write_status=True)
            self.assertEqual(success.read_bytes(), b'{"status":"healthy","finished_at":"prior-run"}')
            self.assertFalse((success.parent/'last-run.json').exists())

    def test_success_requires_second_preview_and_publishes_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            r = report()
            with patch.object(runner, "build_plan", return_value=([], r)) as build:
                result = runner.run(root/"claude", root/"codex", root/"skills", apply=True, write_status=True)
            self.assertEqual(build.call_count, 2)
            self.assertEqual(result["exit_code"], 0)
            self.assertTrue((root/"codex/claude-sync/last-success.json").is_file())


if __name__ == "__main__":
    unittest.main()
