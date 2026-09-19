"""Synthetic local repositories and links; no real profile data or network."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

import profile_sync as sync


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude, self.codex = self.base / '.claude', self.base / '.codex'
        self.skills = self.base / '.agents/skills'
        self.claude.mkdir()

    def skill(self, path, name='example'):
        path.mkdir(parents=True, exist_ok=True)
        (path / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Synthetic test instructions.\n---\n')
        return path

    def repo(self, name):
        path = self.base / name
        path.mkdir()
        subprocess.run(['git', 'init', '-q', str(path)], check=True, capture_output=True)
        subprocess.run(['git', '-C', str(path), 'remote', 'add', 'origin', 'https://example.com/fixture/repo.git'], check=True)
        # HEAD can be read without creating any commits or invoking hooks.
        (path / '.git/refs/heads/main').write_text('a' * 40 + '\n')
        (path / '.git/HEAD').write_text('ref: refs/heads/main\n')
        return path

    def link(self, path, source):
        sync.make_link(path, source)
        def cleanup():
            if sync.linked(path):
                if os.name == 'nt' and not path.is_symlink():
                    os.rmdir(path)
                else:
                    path.unlink()
        self.addCleanup(cleanup)

    def broken(self, name='example'):
        source = self.claude / 'skills' / name
        source.mkdir(parents=True)
        target = self.skills / name
        self.link(target, source)
        source.rmdir()
        return target

    def test_inventory_covers_unmanaged_legacy_git_and_declared_dependencies(self):
        repo = self.repo('approved')
        source = self.skill(repo / 'skills/example')
        (source / 'requirements.txt').write_text('synthetic-library>=1\n')
        (source / 'scripts').mkdir()
        (source / 'scripts/helper.py').write_text('raise RuntimeError("must never execute")\n')
        self.link(self.skills / 'example', source)
        self.skill(self.codex / 'skills/legacy', 'legacy')
        _, report = sync.build_plan(self.claude, self.codex, self.skills)
        rows = report['inventory']['skills']
        row = next(x for x in rows if x['path'] == str(self.skills / 'example'))
        self.assertEqual(row['ownership'], 'unmanaged')
        self.assertEqual(row['repository']['remote'], 'https://example.com/fixture/repo.git')
        self.assertEqual(row['repository']['relative_path'], 'skills/example')
        self.assertEqual(row['repository']['version'], 'a' * 40)
        self.assertTrue(row['dependencies']['declarations'])
        self.assertTrue(any(x['name'] == 'python' for x in row['dependencies']['executables']))
        self.assertTrue(any(x['location'] == 'legacy_codex' for x in rows))

    def test_unique_name_suggests_but_exact_owned_source_authorizes_recovery(self):
        target = self.broken()
        repo = self.repo('approved')
        source = self.skill(repo / 'nested/different-directory')
        _, preview = sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[repo])
        row = next(x for x in preview['inventory']['skills'] if x['path'] == str(target))
        self.assertEqual(row['recovery']['status'], 'unrecoverable')
        self.assertEqual(row['recovery']['candidates'][0]['source'], str(source.resolve()))
        self.assertFalse(any(x['path'] == str(target) for x in preview['changes']))
        self.remember_source(target, source)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[repo], repair_links=True)
        result = sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertEqual(target.resolve(), source.resolve())
        self.assertEqual(sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[repo], repair_links=True)[0], [])
        sync.rollback(Path(result['backup']), self.codex, self.skills)
        self.assertTrue(sync.linked(target))
        self.assertFalse(target.exists())

    def test_ambiguous_or_name_only_sources_never_replace_unknown_link(self):
        target = self.broken()
        first, second = self.repo('first'), self.repo('second')
        self.skill(first / 'skill')
        self.skill(second / 'skill')
        self.skill(first / 'example', 'unrelated-declaration')
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[first, second], repair_links=True)
        row = next(x for x in report['inventory']['skills'] if x['path'] == str(target))
        self.assertEqual(row['recovery']['status'], 'ambiguous')
        self.assertFalse(any(x['path'] == str(target) for x in changes))

    def test_external_manifest_locates_approved_sources(self):
        target = self.broken()
        repo = self.repo('approved')
        source = self.skill(repo / 'canonical')
        self.remember_source(target, source)
        manifest = self.base / 'external-skill-repos.json'
        manifest.write_text(json.dumps({'skillRepoRoot': '.', 'repos': [{
            'dir': repo.name, 'url': 'https://example.com/fixture/repo.git',
            'skills': [{'name': 'example', 'subPath': 'canonical'}]}]}))
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, external_manifest=manifest, repair_links=True)
        self.assertTrue(any(x['path'] == str(target) for x in changes))
        self.assertEqual(report['inventory']['warnings'], [])

    def test_recovery_rejects_source_changed_after_plan(self):
        target = self.broken()
        repo = self.repo('approved')
        source = self.skill(repo / 'canonical')
        self.remember_source(target, source)
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[repo], repair_links=True)
        (source / 'SKILL.md').write_text('---\nname: unrelated\ndescription: Changed after preview.\n---\n')
        with self.assertRaises(ValueError):
            sync.apply_plan(changes, report, self.codex, self.skills)
        self.assertFalse(target.exists())
        self.assertTrue(sync.linked(target))

    def remember_source(self, target, source):
        """Generate legacy ownership separately from the candidate's name."""
        path = self.codex / 'claude-sync/managed-skills.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({str(target): str(source.resolve())}))

    def test_same_directory_name_without_matching_declaration_is_unrecoverable(self):
        target = self.broken()
        repo = self.repo('approved')
        self.skill(repo / 'example', 'different-skill')
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, approved_repos=[repo], repair_links=True)
        row = next(x for x in report['inventory']['skills'] if x['path'] == str(target))
        self.assertEqual(row['recovery']['status'], 'unrecoverable')
        self.assertFalse(any(x['path'] == str(target) for x in changes))

    def test_manifest_path_traversal_does_not_authorize_repo(self):
        target = self.broken()
        repo = self.repo('unapproved')
        self.skill(repo / 'canonical')
        manifest = self.base / 'external-skill-repos.json'
        manifest.write_text(json.dumps({'skillRepoRoot': str(self.base / 'approved'), 'repos': [{'dir': '../unapproved'}]}))
        changes, report = sync.build_plan(self.claude, self.codex, self.skills, external_manifest=manifest, repair_links=True)
        self.assertFalse(any(x['path'] == str(target) for x in changes))
        self.assertEqual(report['inventory']['warnings'][0]['reason'], 'external_skill_manifest_invalid')

    def test_managed_adapter_inventory_reports_original_repository(self):
        repo = self.repo('approved')
        commands = repo / 'commands'
        commands.mkdir()
        (commands / 'review.md').write_text('Review the synthetic input.\n')
        (self.claude / 'settings.json').write_text(json.dumps({'enabledPlugins': {'fixture@local': True}}))
        (self.claude / 'plugins').mkdir()
        (self.claude / 'plugins/installed_plugins.json').write_text(json.dumps({'plugins': {'fixture@local': [{'installPath': str(repo)}]}}))
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        sync.apply_plan(changes, report, self.codex, self.skills)
        _, report = sync.build_plan(self.claude, self.codex, self.skills)
        row = next(x for x in report['inventory']['skills'] if x['name'] == 'claude-fixture-review')
        self.assertEqual(row['repository']['relative_path'], 'commands/review.md')
        self.assertEqual(row['ownership'], 'managed')


if __name__ == '__main__':
    unittest.main()
