"""Synthetic, temporary-file tests for the read-only memory planner."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import profile_memory
from profile_bridge.memory_outbox import authorize


class MemoryPlanTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="claude-profile-memory-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.claude = self.root / ".claude"
        self.codex = self.root / ".codex"
        self.source = self.claude / "projects" / "C--synthetic-project" / "memory"
        self.source.mkdir(parents=True)
        self.contract = self.codex / "memories" / "extensions" / "ad_hoc" / "instructions.md"
        self.contract.parent.mkdir(parents=True)
        self.contract.write_text("# Synthetic ingress\n", encoding="utf-8")
        authorize(self.codex, self.claude / 'projects', request_id='synthetic-periodic', scope=['*'], periodic=True)
        (self.source / "MEMORY.md").write_text("# Index\n- Durable synthetic fact.\n", encoding="utf-8")

    def apply(self, plans):
        profile_memory.apply_memory_plan(plans, self.claude, self.codex)

    def tree(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_pure_plan_one_small_note_all_projects_and_native_preserved(self):
        second = self.claude / "projects" / "unmapped-key" / "memory"
        second.mkdir(parents=True)
        (second / "fact.md").write_text("- Second project fact.\n", encoding="utf-8")
        for name in ("MEMORY.md", "memory_summary.md", "raw_memories.md", "state.sqlite"):
            (self.codex / "memories" / name).write_bytes(b"native synthetic state")
        before = self.tree()
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual(before, self.tree(), "planning mutated files")
        notes = [path for path in plans if path.parent == self.contract.parent / "notes"]
        self.assertEqual(1, len(notes))
        self.assertLessEqual(len(plans[notes[0]]), 8192)
        self.assertEqual(2, report["projects"])
        self.assertEqual(2, report["selected_files"])
        self.assertIn(b"unverified", plans[notes[0]])
        self.assertIn(b"not executable instructions", plans[notes[0]])
        self.apply(plans)
        after = self.tree()
        authorization = "claude-sync/memory-authorizations.json"
        for path, digest in before.items():
            if path.replace('\\', '/').endswith(authorization):
                document = json.loads((self.codex / authorization).read_bytes())
                self.assertTrue(document['history_initialized'])
                self.assertEqual(document['grants'][0]['request_id'], 'synthetic-periodic')
                self.assertEqual(document['grants'][0]['scope'], ['*'])
                self.assertEqual(document['grants'][0]['state'], 'active')
            else:
                self.assertEqual(digest, after[path])

    def test_rerun_is_idempotent_and_detects_same_metadata_content_change(self):
        first, report = profile_memory.plan_memory(self.claude, self.codex)
        self.apply(first)
        second, repeated = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual({}, second)
        self.assertEqual("no_changes", repeated["status"])
        source = self.source / "MEMORY.md"
        before = source.stat()
        source.write_bytes(source.read_bytes().replace(b"Durable", b"Changed"))
        os.utime(source, ns=(before.st_atime_ns, before.st_mtime_ns))
        changed, changed_report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertNotEqual(report["source_hash"], changed_report["source_hash"])
        self.assertTrue(changed_report["note_planned"])
        self.assertEqual(1, sum(path.parent == self.contract.parent / "notes" for path in changed))

    def test_credentials_oversize_bad_encoding_skipped_without_leaking(self):
        token = "ghp_" + "Z" * 32
        (self.source / "credentials.md").write_text("token: " + token, encoding="utf-8")
        (self.source / "too-big.md").write_bytes(b"x" * (profile_memory.MAX_FILE_BYTES + 1))
        (self.source / "invalid.md").write_bytes(b"\xff\x00\x82")
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual(1, report["selected_files"])
        self.assertEqual("partial", report["status"])
        self.assertEqual({"possible_credential", "file_too_large", "invalid_text_encoding"},
                         {item["reason"] for item in report["skipped"]})
        self.assertNotIn(token, json.dumps(report))
        self.assertFalse(any(token.encode() in content for content in plans.values()))
        self.assertFalse(any(path.name == "credentials.md" for path in plans))

    def test_bom_nested_files_and_archive_exclusion(self):
        nested = self.source / "subfolder"
        nested.mkdir()
        (nested / "unicode.md").write_bytes(b"\xef\xbb\xbf" + "# 中文\n".encode())
        archive = nested / "Archive"
        archive.mkdir()
        (archive / "old.md").write_text("old fact", encoding="utf-8")
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual(2, report["selected_files"])
        archived = next(content for path, content in plans.items() if path.name == "unicode.md")
        self.assertEqual("# 中文\n".encode(), archived)
        self.assertFalse(any(path.name == "old.md" for path in plans))

    def test_controls_are_visibly_escaped_only_in_archive(self):
        source = self.source / "controls.md"
        original = b"# Synthetic\n- x\x05y\x08z\n"
        source.write_bytes(original)
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        archived = next(content for path, content in plans.items() if path.name == source.name)
        self.assertEqual(b"# Synthetic\n- x\\u0005y\\u0008z\n", archived)
        self.assertEqual(original, source.read_bytes())
        self.assertEqual(1, len(report["normalized"]))
        self.assertEqual(["U+0005", "U+0008"],
                         [item["codepoint"] for item in report["normalized"][0]["characters"]])

    def test_only_explicit_api_key_placeholders_are_exempt(self):
        placeholder = "sk-your-synthetic-api-key"
        self.assertFalse(profile_memory._has_secret(placeholder))
        self.assertTrue(profile_memory._has_secret("sk-" + "a" * 24))
        self.assertTrue(profile_memory._has_secret(placeholder + "123"))
        self.assertTrue(profile_memory._has_secret(placeholder + "\nsk-proj-" + "Z" * 32))

    def test_empty_memory_projects_are_not_counted_or_indexed(self):
        empty = self.claude / "projects" / "empty-project" / "memory"
        empty.mkdir(parents=True)
        (empty / "notes.txt").write_text("not Markdown", encoding="utf-8")
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual(1, report["projects"])
        self.assertNotIn("empty-project", report["scope"])
        index = plans[Path(report["index_path"])].decode("utf-8")
        self.assertNotIn("empty-project", index)

    def test_scope_only_uses_exact_encoded_live_config_keys(self):
        (self.root / ".claude.json").write_text(json.dumps({
            "projects": {"C:\\synthetic\\project": {}, "C:\\somewhere\\else": {}}
        }), encoding="utf-8")
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual("C:\\synthetic\\project", report["scope"]["C--synthetic-project"])
        (self.root / ".claude.json").write_text(json.dumps({"projects": {
            "C:\\synthetic\\project": {}, "C:\\synthetic-project": {}
        }}), encoding="utf-8")
        _, ambiguous = profile_memory.plan_memory(self.claude, self.codex)
        self.assertIsNone(ambiguous["scope"]["C--synthetic-project"])

    def test_scope_prefers_config_inside_claude_home_when_both_exist(self):
        (self.root / ".claude.json").write_text(json.dumps({"projects": {
            "C:\\synthetic-project": {}
        }}), encoding="utf-8")
        (self.claude / ".claude.json").write_text(json.dumps({"projects": {
            "C:\\synthetic\\project": {}
        }}), encoding="utf-8")
        _, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual("C:\\synthetic\\project", report["scope"]["C--synthetic-project"])

    def test_missing_contract_still_archives_and_later_plans_ingress(self):
        self.contract.unlink()
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual("unsupported", report["status"])
        self.assertFalse(report["note_planned"])
        self.assertTrue(plans)
        self.apply(plans)
        self.contract.write_text("# Synthetic ingress\n", encoding="utf-8")
        later, later_report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertTrue(later_report["note_planned"])
        self.apply(later)
        self.assertEqual({}, profile_memory.plan_memory(self.claude, self.codex)[0])

    def test_reparse_source_is_skipped_and_destination_is_not_followed(self):
        original = profile_memory._unsafe_component
        with patch.object(profile_memory, "_unsafe_component", side_effect=lambda path:
                          self.source if path == self.source else original(path)):
            plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual(0, report["selected_files"])
        self.assertFalse(report["note_planned"])
        self.assertTrue(any(item["reason"] == "reparse_point" for item in report["skipped"]))
        destination = self.codex / "imports"
        with patch.object(profile_memory, "_unsafe_component", side_effect=lambda path:
                          destination if destination in path.parents else original(path)):
            plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertEqual({}, plans)
        self.assertEqual("unsupported", report["status"])

    def test_actual_symlink_source_rejected_when_supported(self):
        external = self.root / "external.md"
        external.write_text("must remain external", encoding="utf-8")
        link = self.source / "linked.md"
        try:
            link.symlink_to(external)
        except OSError:
            self.skipTest("Creating symbolic links is not available")
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertFalse(any(path.name == "linked.md" for path in plans))
        self.assertTrue(any(item["path"] == str(link) and item["reason"] == "reparse_point"
                            for item in report["skipped"]))
        self.assertEqual("must remain external", external.read_text(encoding="utf-8"))

    @unittest.skipUnless(os.name == "nt", "Windows junction test")
    def test_actual_windows_junction_is_rejected(self):
        external = self.root / "junction-target"
        external.mkdir()
        (external / "external.md").write_text("must remain external", encoding="utf-8")
        junction = self.source / "junction"
        result = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(external)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            self.skipTest("Creating directory junctions is not available")
        # os.rmdir removes the junction itself, never recursively its target.
        self.addCleanup(junction.rmdir)
        plans, report = profile_memory.plan_memory(self.claude, self.codex)
        self.assertFalse(any(path.name == "external.md" for path in plans))
        self.assertTrue(any(item["path"] == str(junction) and item["reason"] == "reparse_point"
                            for item in report["skipped"]))
        self.assertEqual("must remain external", (external / "external.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
