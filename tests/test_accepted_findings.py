"""Accepted-findings baseline: synthetic reports and temporary homes only."""
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


class AcceptedFindingsTests(unittest.TestCase):
    """A reviewed private baseline turns standing findings green; anything else stays rc 2."""

    def standing(self):
        r = report()
        r["skills"] = [{"name": "fixture-skill", "status": "blocked", "reasons": ["capability:b", "capability:a"]}]
        r["hooks"]["hooks"] = [{"event": "SessionStart", "status": "unsupported", "reason": "fixture_not_reviewed"}] * 2
        r["agents"] = {"agents": [], "warnings": [{"reason": "fixture_warning", "source": "C:/fixture/root"}]}
        return r

    def baseline(self, root, entries):
        path = root / "accepted.json"
        path.write_text(json.dumps({"version": 1, "accepted": entries}), encoding="utf-8")
        return path

    def accepted_entries(self):
        return [
            {"identity": {"area": "skills", "name": "fixture-skill", "status": "blocked",
                          "reasons": ["capability:a", "capability:b"]}},
            {"identity": {"area": "hooks", "event": "SessionStart", "status": "unsupported",
                          "reason": "fixture_not_reviewed"}, "count": 2},
            {"identity": {"area": "agents", "reason": "fixture_warning", "source": "C:/fixture/root"}},
        ]

    def run_main(self, root, r, entries):
        argv = ["--claude-home", str(root / "claude"), "--codex-home", str(root / "codex"),
                "--skills-home", str(root / "skills"), "--apply", "--write-status",
                "--accepted-findings", str(self.baseline(root, entries))]
        with patch.object(runner, "build_plan", return_value=([], r)), patch("builtins.print"):
            try:
                rc = runner.main(argv)
            except SystemExit as exc:  # an unknown option exits through argparse
                rc = exc.code
        state = root / "codex/claude-sync"
        last = json.loads((state / "last-run.json").read_text()) if (state / "last-run.json").exists() else {}
        return rc, last, state / "last-success.json"

    def test_only_accepted_findings_exit_zero_and_publish_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, last, success = self.run_main(Path(tmp), self.standing(), self.accepted_entries())
            self.assertEqual((rc, last.get("status")), (0, "accepted"))
            self.assertEqual(last["new_findings"], [])
            self.assertEqual(len(last["accepted_findings"]), 4)
            self.assertEqual(json.loads(success.read_text())["accepted_count"], 4)

    def test_finding_outside_the_baseline_keeps_rc_two_and_is_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.standing()
            r["skills"].append({"name": "fixture-new", "status": "unsupported", "reason": "invalid_skill_frontmatter_yaml"})
            rc, last, success = self.run_main(Path(tmp), r, self.accepted_entries())
            self.assertEqual((rc, last.get("status")), (2, "degraded"))
            self.assertEqual([f["name"] for f in last["new_findings"]], ["fixture-new"])
            self.assertFalse(success.exists())

    def test_same_kind_on_a_different_subject_is_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.standing()
            r["agents"]["warnings"].append({"reason": "fixture_warning", "source": "C:/fixture/other"})
            r["skills"][0]["reasons"].append("capability:c")
            rc, last, _ = self.run_main(Path(tmp), r, self.accepted_entries())
            self.assertEqual(rc, 2)
            self.assertEqual(sorted(f["area"] for f in last["new_findings"]), ["agents", "skills"])

    def test_accepted_count_is_a_multiset(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.standing()
            r["hooks"]["hooks"] = r["hooks"]["hooks"] + [dict(r["hooks"]["hooks"][0])]
            rc, last, _ = self.run_main(Path(tmp), r, self.accepted_entries())
            self.assertEqual(rc, 2)
            self.assertEqual([f.get("event") for f in last["new_findings"]], ["SessionStart"])

    def test_pending_changes_are_never_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self.standing()
            r["change_count"] = 1
            rc, last, success = self.run_main(Path(tmp), r, self.accepted_entries())
            self.assertEqual(rc, 2)
            self.assertEqual([f["reason"] for f in last["new_findings"]], ["pending_changes"])
            self.assertFalse(success.exists())
            entries = self.accepted_entries() + [{"identity": {"area": "sync", "reason": "pending_changes"}}]
            rc, _, _ = self.run_main(Path(tmp), r, entries)
            self.assertEqual(rc, 1)

    def test_new_skipped_memory_file_is_new_under_the_same_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = report()
            r["memory"] = {"status": "partial", "skipped": [{"path": "C:/fixture/a.md", "reason": "possible_credential"}]}
            entries = [{"identity": {"area": "memory", "status": "partial", "detail": {
                "skipped": [["possible_credential", "C:/fixture/a.md"]], "delivery_state": None,
                "delivery_unresolved": False, "archive_preserved": ["fixture_reason"]}}}]
            r["memory"]["archive_hygiene"] = {"preserved": [{"path": "C:/fixture/x.md", "reason": "fixture_reason"}]}
            rc, last, _ = self.run_main(Path(tmp), r, entries)
            self.assertEqual((rc, last.get("status")), (0, "accepted"))
            r["memory"]["archive_hygiene"]["preserved"].append({"path": "C:/fixture/y.md", "reason": "fixture_reason"})
            rc, last, _ = self.run_main(Path(tmp), r, entries)
            self.assertEqual(rc, 0, "a growing archive of the same kind stays accepted")
            r["memory"]["skipped"].append({"path": "C:/fixture/b.md", "reason": "possible_credential"})
            rc, last, _ = self.run_main(Path(tmp), r, entries)
            self.assertEqual(rc, 2)
            self.assertEqual([f["area"] for f in last["new_findings"]], ["memory"])

    def test_without_a_baseline_behaviour_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(runner, "build_plan", return_value=([], self.standing())):
                result = runner.run(root/"claude", root/"codex", root/"skills", apply=True, write_status=True)
            self.assertEqual((result["exit_code"], result["status"]), (2, "degraded"))
            self.assertNotIn("new_findings", result)


if __name__ == "__main__":
    unittest.main()
