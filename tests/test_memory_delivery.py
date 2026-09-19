"""Synthetic crash/replay tests; no live profiles, models, tasks or services."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import profile_memory as memory
import profile_sync as sync
from profile_bridge import memory_outbox as outbox


class MemoryDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='t03-delivery-')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.claude, self.codex = self.base / 'claude', self.base / 'codex'
        self.skills = self.base / 'skills'
        self.source = self.claude / 'projects/project-one/memory/MEMORY.md'
        self.write(self.source, b'# Synthetic A\n')
        self.contract = self.codex / 'memories/extensions/ad_hoc/instructions.md'
        self.write(self.contract, b'# Synthetic append contract\n')
        self.protected = {}
        for name in ('MEMORY.md', 'memory_summary.md', 'raw_memories.md', 'registry.json', 'evidence.json', 'state.sqlite'):
            path = self.codex / 'memories' / name
            self.write(path, b'native synthetic bytes\x00\r\n')
            self.protected[path] = path.read_bytes()

    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)

    def grant(self, request_id='periodic-one', **kw):
        return outbox.authorize(self.codex, self.claude / 'projects', request_id=request_id,
                                scope=kw.pop('scope', ['*']), periodic=kw.pop('periodic', True), **kw)

    def plan(self, **kw):
        return memory.plan_memory(self.claude, self.codex, **kw)

    def apply(self, **kw):
        plans, report = self.plan(**kw)
        result = memory.apply_memory_plan(plans, self.claude, self.codex, skills=self.skills)
        return result, report

    def notes(self):
        return sorted((self.contract.parent / 'notes').glob('*.md'))

    def tree(self):
        return {str(p.relative_to(self.base)): p.read_bytes() for p in self.base.rglob('*') if p.is_file()}

    def assert_protected(self):
        for path, value in self.protected.items():
            self.assertEqual(path.read_bytes(), value)

    def crash(self, boundary, consume=False):
        code = '''
import os, sys
from pathlib import Path
from profile_bridge import memory_outbox as outbox
import profile_sync as sync
base=Path(sys.argv[1]); boundary=sys.argv[2]
def stop(point):
    if point == boundary:
        if sys.argv[3] == 'consume':
            for note in (base/'codex/memories/extensions/ad_hoc/notes').glob('*.md'):
                note.unlink()
        os._exit(73)
outbox.checkpoint=stop
changes,report=sync.build_plan(base/'claude',base/'codex',base/'skills')
sync.apply_plan(changes,report,base/'codex',base/'skills')
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.base), boundary,
                                 'consume' if consume else 'retain'], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 73, result.stderr.decode(errors='replace'))

    def test_default_contract_does_not_authorize_and_preview_is_read_only(self):
        before = self.tree()
        plans, report = self.plan()
        self.assertEqual(before, self.tree())
        self.assertEqual(report['mode'], 'archive')
        self.assertFalse(report['note_planned'])
        self.apply()
        self.assertEqual(self.notes(), [])
        self.assertNotIn(b'note-source-sha256', (self.codex / 'imports/claude-memory/index.md').read_bytes())
        self.assert_protected()

    def test_explicit_preview_does_not_persist_grant(self):
        before = self.tree()
        _, report = self.plan(request_id='one-shot', scope=['*'])
        self.assertTrue(report['note_planned'])
        self.assertEqual(before, self.tree())
        self.assertFalse(outbox.authorization_path(self.codex).exists())
        result, _ = self.apply(request_id='one-shot', scope=['*'])
        self.assertEqual(result['delivery_state'], 'published')
        self.source.write_bytes(b'# Changed after one shot\n')
        self.apply()
        self.assertEqual(len(self.notes()), 1)

    def test_same_and_different_requests_deduplicate_increment(self):
        first, _ = self.apply(request_id='request-one', scope=['*'])
        second, _ = self.apply(request_id='request-one', scope=['*'])
        third, _ = self.apply(request_id='request-two', scope=['*'])
        self.assertEqual(first['increment_id'], second['increment_id'])
        self.assertEqual(first['increment_id'], third['increment_id'])
        self.assertEqual(len(self.notes()), 1)
        self.notes()[0].unlink()  # Published receipt survives native consumption.
        self.apply(request_id='request-two', scope=['*'])
        self.assertEqual(self.notes(), [])

    def test_repeated_aba_edges_have_distinct_predecessor_ids(self):
        self.grant()
        ids = []
        for content in (b'A', b'B', b'A', b'B'):
            self.source.write_bytes(content)
            result, _ = self.apply()
            ids.append(result['increment_id'])
        self.assertEqual(len(set(ids)), 4)
        self.assertEqual(len(self.notes()), 4)
        history = json.loads((outbox.state_root(self.codex) / 'history.json').read_bytes())
        self.assertEqual([r['predecessor'] for r in history['records']], [None] + ids[:-1])

    def test_hard_exits_at_all_publication_boundaries(self):
        self.grant()
        for boundary in ('outbox_prepared', 'index', 'publication_started', 'note_created', 'receipt'):
            with self.subTest(boundary=boundary):
                self.source.write_bytes(boundary.encode())
                self.crash(boundary)
                plans, report = self.plan()
                before = self.tree()
                self.plan()
                self.assertEqual(before, self.tree())
                result = memory.apply_memory_plan(plans, self.claude, self.codex)
                expected = 'delivery_unknown' if boundary == 'publication_started' else 'published'
                self.assertEqual(result['delivery_state'], expected)
                again, _ = self.apply()
                self.assertEqual(again['increment_id'], result['increment_id'])
                self.assertEqual(again['delivery_state'], expected)
                self.assert_protected()

    def test_unrecorded_note_identity_is_unknown_even_with_surviving_bytes(self):
        self.grant()
        self.crash('note_published_unrecorded')
        original = self.notes()[0].read_bytes()
        result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'delivery_unknown')
        self.assertEqual(len(self.notes()), 1)
        self.assertEqual(self.notes()[0].read_bytes(), original)

    def test_identical_replacement_with_different_file_identity_conflicts(self):
        self.grant()
        self.crash('note_created')
        note = self.notes()[0]
        data = note.read_bytes()
        replacement = note.with_suffix('.replacement')
        replacement.write_bytes(data)
        os.replace(replacement, note)
        result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'conflict')
        self.assertEqual(note.read_bytes(), data)

    def test_scope_excludes_unrelated_project_changes(self):
        other = self.claude / 'projects/project-two/memory/MEMORY.md'
        self.write(other, b'other synthetic project')
        self.grant(scope=['project-one'])
        first, _ = self.apply()
        other.write_bytes(b'unrelated change')
        second, _ = self.apply()
        self.assertEqual(first['increment_id'], second['increment_id'])
        self.assertEqual(len(self.notes()), 1)

    def test_replacement_before_identity_persistence_is_never_owned(self):
        observed = {}
        def replace(point):
            if point == 'note_published_unrecorded':
                note = self.notes()[0]
                observed['original'] = outbox.identity(note)
                replacement = note.with_suffix('.replacement')
                replacement.write_bytes(note.read_bytes())
                os.replace(replacement, note)
                observed['competitor'] = outbox.identity(note)
        with patch.object(outbox, 'checkpoint', replace):
            result, _ = self.apply(request_id='replace-before-receipt', scope=['project-one'])
        self.assertNotEqual(observed['original'], observed['competitor'])
        self.assertIn(result['delivery_state'], {'conflict', 'delivery_unknown'})
        row = json.loads((outbox.state_root(self.codex) / 'history.json').read_bytes())['records'][0]
        self.assertNotEqual(row.get('note_identity'), observed['competitor'])
        self.assertNotIn('receipt', row)
        self.assertEqual(outbox.identity(self.notes()[0]), observed['competitor'])
        again, _ = self.apply(request_id='replace-before-receipt', scope=['project-one'])
        self.assertEqual(again['delivery_state'], result['delivery_state'])

    def _unrelated_queue(self, second_claude, second_scope):
        self.write(second_claude / 'projects' / second_scope / 'memory/MEMORY.md', b'Synthetic second stream')
        self.contract.unlink()
        first, _ = self.apply(request_id='queued-a', scope=['project-one'])
        self.assertEqual(first['delivery_state'], 'prepared')
        self.write(self.contract, b'# Restored synthetic contract\n')
        plan, _ = memory.plan_memory(second_claude, self.codex, request_id='queued-b', scope=[second_scope])
        second = memory.apply_memory_plan(plan, second_claude, self.codex, skills=self.skills)
        self.assertEqual(second['delivery_state'], 'published')
        rows = json.loads((outbox.state_root(self.codex) / 'history.json').read_bytes())['records']
        self.assertEqual(rows[0]['delivery_state'], 'prepared')
        self.assertEqual(len(self.notes()), 1)
        again, _ = self.apply(request_id='queued-a', scope=['project-one'])
        self.assertEqual(again['increment_id'], first['increment_id'])
        self.assertEqual(again['delivery_state'], 'published')
        self.assertEqual(len(self.notes()), 2)

    def test_unrelated_explicit_scope_survives_contract_outage(self):
        self._unrelated_queue(self.claude, 'project-two')

    def test_same_scope_in_different_source_root_survives_contract_outage(self):
        self._unrelated_queue(self.base / 'second-claude', 'project-one')

    def test_native_edits_cannot_change_prepared_recovery_bytes(self):
        result, _ = self.apply(request_id='independent-evidence', scope=['project-one'])
        prepared = outbox.state_root(self.codex) / result['prepared_note_path']
        expected = prepared.read_bytes()
        note = self.notes()[0]
        self.assertNotEqual(outbox.identity(prepared), outbox.identity(note))
        note.write_bytes(b'Synthetic native edit')
        self.assertEqual(prepared.read_bytes(), expected)
        self.assertEqual(list(note.parent.iterdir()), [note])
        self.assertFalse(list(self.codex.rglob('.fleet-guards-*')))

    def test_actual_same_stream_successor_supersedes_prepared_ancestor(self):
        self.contract.unlink()
        first, _ = self.apply(request_id='ancestor', scope=['project-one'])
        self.source.write_bytes(b'Synthetic successor')
        self.write(self.contract, b'# Restored synthetic contract\n')
        second, _ = self.apply(request_id='successor', scope=['project-one'])
        self.assertEqual(second['predecessor'], first['increment_id'])
        rows = json.loads((outbox.state_root(self.codex) / 'history.json').read_bytes())['records']
        self.assertEqual([r['delivery_state'] for r in rows], ['superseded', 'published'])
        self.assertEqual(len(self.notes()), 1)

    def test_selecting_ancestor_cannot_supersede_prepared_successor(self):
        self.contract.unlink()
        first, _ = self.apply(request_id='ancestor', scope=['project-one'])
        self.source.write_bytes(b'Synthetic successor')
        plan, _ = self.plan(request_id='successor', scope=['project-one'])
        outbox.prepare(plan.delivery, skills=self.skills)
        # Resume an earlier bound request without selecting the newer observation.
        self.source.write_bytes(b'# Synthetic A\n')
        again, _ = self.apply(request_id='ancestor', scope=['project-one'])
        self.assertEqual(again['increment_id'], first['increment_id'])
        rows = json.loads((outbox.state_root(self.codex) / 'history.json').read_bytes())['records']
        self.assertEqual([r['delivery_state'] for r in rows], ['prepared', 'prepared'])

    def test_one_shot_request_cannot_select_a_different_increment_on_replay(self):
        self.apply(request_id='one-shot', scope=['*'])
        self.source.write_bytes(b'new synthetic increment')
        _, report = self.apply(request_id='one-shot', scope=['*'])
        self.assertEqual(report['delivery']['delivery_state'], 'authorization_invalid')
        self.assertEqual(len(self.notes()), 1)
        result, _ = self.apply(request_id='new-request', scope=['*'])
        self.assertEqual(result['delivery_state'], 'published')
        self.assertEqual(len(self.notes()), 2)

    def test_consumed_before_receipt_becomes_unknown_and_never_replayed(self):
        self.grant()
        self.crash('note_created', consume=True)
        result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'delivery_unknown')
        self.apply(request_id='different-request', scope=['*'])
        self.assertEqual(self.notes(), [])
        self.assert_protected()

    def test_preexisting_collision_and_edited_note_are_preserved(self):
        self.grant()
        plans, report = self.plan()
        note = Path(report['note_path'])
        self.write(note, b'user-created competitor')
        result = memory.apply_memory_plan(plans, self.claude, self.codex)
        self.assertEqual(result['delivery_state'], 'conflict')
        self.assertEqual(note.read_bytes(), b'user-created competitor')
        self.source.write_bytes(b'second increment')
        self.crash('note_created')
        second = next(n for n in self.notes() if n != note)
        second.write_bytes(b'user edit after publication')
        result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'conflict')
        self.assertEqual(second.read_bytes(), b'user edit after publication')
        self.assert_protected()

    def test_os_no_replace_race_preserves_competitor(self):
        self.grant()
        real = outbox.create_no_replace_with_identity
        def competing(path, payload):
            if Path(path).parent == self.contract.parent / 'notes':
                self.write(Path(path), b'racing user')
            return real(path, payload)
        with patch.object(outbox, 'create_no_replace_with_identity', competing):
            result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'conflict')
        self.assertEqual(self.notes()[0].read_bytes(), b'racing user')

    def test_cancellation_after_prepare_prevents_queued_delivery(self):
        self.grant()
        self.crash('outbox_prepared')
        outbox.revoke(self.codex, 'periodic-one', state='cancelled')
        result, _ = self.apply(request_id='periodic-one', scope=['*'], periodic=True)
        self.assertEqual(result['delivery_state'], 'cancelled')
        self.assertEqual(self.notes(), [])

    def test_cancellation_at_publication_boundary_blocks_create(self):
        self.grant()
        def cancel(point):
            if point == 'publication_started':
                outbox.revoke(self.codex, 'periodic-one')
        with patch.object(outbox, 'checkpoint', cancel):
            result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'cancelled')
        self.assertEqual(self.notes(), [])

    def test_missing_contract_archives_and_later_delivers_same_increment(self):
        self.grant()
        self.contract.unlink()
        result, report = self.apply()
        self.assertEqual(result['delivery_state'], 'prepared')
        self.assertEqual(report['status'], 'unsupported')
        self.assertTrue((self.codex / 'imports/claude-memory/index.md').exists())
        self.write(self.contract, b'# Restored synthetic contract\n')
        later, _ = self.apply()
        self.assertEqual(later['increment_id'], result['increment_id'])
        self.assertEqual(later['delivery_state'], 'published')

    def test_missing_or_corrupt_history_and_legacy_index_never_reimport(self):
        self.grant()
        self.apply()
        history = outbox.state_root(self.codex) / 'history.json'
        for bad in (None, b'{broken', b'{"version":1,"records":[{}]}'):
            with self.subTest(bad=bad):
                if bad is None:
                    history.unlink()
                else:
                    history.write_bytes(bad)
                self.source.write_bytes(b'archive can still update')
                plans, report = self.plan()
                self.assertEqual(report['delivery']['delivery_state'], 'history_conflict')
                self.assertFalse(report['note_planned'])
                memory.apply_memory_plan(plans, self.claude, self.codex)
                self.assertEqual(len(self.notes()), 1)
        self.assert_protected()

    def test_legacy_index_without_surviving_note_is_migration_conflict(self):
        self.grant()
        index = self.codex / 'imports/claude-memory/index.md'
        self.write(index, ('<!-- claude-memory-source-sha256: ' + 'a'*64 + ' -->\n'
                           '<!-- claude-memory-note-source-sha256: ' + 'a'*64 + ' -->\n').encode())
        _, report = self.apply()
        self.assertEqual(report['delivery']['migration'], 'T09')
        self.assertEqual(self.notes(), [])
        # A legacy marker is inert evidence, not ownership or delivery authority.
        # Preserve this unowned index until an exact-hash adoption is reviewed.
        self.assertIn(b'note-source-sha256', index.read_bytes())
        self.assertEqual(self.plan()[1]['delivery']['delivery_state'], 'history_conflict')

    def test_rollback_preserves_outbox_history_notes_and_replay_identity(self):
        self.grant()
        changes, report = sync.build_plan(self.claude, self.codex, self.skills)
        report = sync.apply_plan(changes, report, self.codex, self.skills)
        before = {p: p.read_bytes() for p in outbox.state_root(self.codex).rglob('*') if p.is_file()}
        notes = {p: p.read_bytes() for p in self.notes()}
        sync.rollback(Path(report['backup']), self.codex, self.skills)
        for path, data in {**before, **notes}.items():
            self.assertEqual(path.read_bytes(), data)
        result, _ = self.apply()
        self.assertEqual(result['delivery_state'], 'published')
        self.assertEqual(len(self.notes()), 1)
        self.assert_protected()

    def test_invalid_authorization_does_not_destroy_archive_lineage(self):
        _, report = self.apply(request_id='bad-request', scope=['absent-project'])
        self.assertEqual(report['delivery']['delivery_state'], 'authorization_invalid')
        self.assertEqual(self.notes(), [])
        result, _ = self.apply(request_id='valid-request', scope=['*'])
        self.assertEqual(result['delivery_state'], 'published')

    def test_missing_entire_outbox_and_index_still_cannot_reinitialize(self):
        self.grant()
        self.apply()
        self.notes()[0].unlink()
        # Remove only individual synthetic evidence files, preserving authorization.
        for path in outbox.state_root(self.codex).rglob('*'):
            if path.is_file():
                path.unlink()
        (outbox.state_root(self.codex) / 'prepared').rmdir()
        outbox.state_root(self.codex).rmdir()
        (self.codex / 'imports/claude-memory/index.md').unlink()
        _, report = self.apply()
        self.assertEqual(report['delivery']['delivery_state'], 'history_conflict')
        self.assertEqual(self.notes(), [])

    def test_orphaned_prepared_file_is_not_a_fresh_install(self):
        self.grant()
        self.crash('prepared_note')
        _, report = self.apply()
        self.assertEqual(report['delivery']['delivery_state'], 'history_conflict')
        self.assertEqual(self.notes(), [])

    def test_corrupt_prepared_or_receipt_or_empty_history_blocks_delivery(self):
        self.grant()
        self.apply()
        path = outbox.state_root(self.codex) / 'history.json'
        original = path.read_bytes()
        data = json.loads(original)
        data['records'][0]['receipt']['content_hash'] = '0' * 64
        path.write_bytes(outbox.encoded(data))
        self.assertEqual(self.plan()[1]['delivery']['delivery_state'], 'history_conflict')
        path.write_bytes(original)
        prepared = outbox.state_root(self.codex) / data['records'][0]['prepared_note_path']
        payload = prepared.read_bytes()
        prepared.write_bytes(b'changed prepared note')
        self.assertEqual(self.plan()[1]['delivery']['delivery_state'], 'history_conflict')
        prepared.write_bytes(payload)
        data = json.loads(original)
        data['records'] = []
        path.write_bytes(outbox.encoded(data))
        self.assertEqual(self.plan()[1]['delivery']['delivery_state'], 'history_conflict')
        self.assertEqual(len(self.notes()), 1)

    def test_unsafe_contract_blocks_delivery_but_archive_updates(self):
        self.grant()
        real = outbox.read_bounded
        def unreadable(path, limit):
            if Path(path) == self.contract:
                raise ValueError('unsafe_contract')
            return real(path, limit)
        with patch.object(outbox, 'read_bounded', unreadable):
            result, report = self.apply()
        self.assertEqual(result['delivery_state'], 'prepared')
        self.assertEqual(report['status'], 'unsupported')
        self.assertEqual(self.notes(), [])
        self.assertTrue((self.codex / 'imports/claude-memory/index.md').exists())

    def test_public_authorization_api_respects_shared_profile_locks(self):
        code = """
import sys, time
from pathlib import Path
from profile_lock import profile_locks
with profile_locks(Path(sys.argv[1]), Path(sys.argv[2])):
    print('locked', flush=True)
    time.sleep(30)
"""
        proc = subprocess.Popen([sys.executable, '-c', code, str(self.base / 'other-profile'), str(self.skills)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        try:
            self.assertEqual(proc.stdout.readline().strip(), 'locked')
            before = self.tree()
            with self.assertRaises(RuntimeError):
                self.grant(skills=self.skills)
            self.assertEqual(self.tree(), before)
        finally:
            proc.kill()
            proc.communicate(timeout=10)

    def test_scope_root_validation_and_metadata_only_reports(self):
        for scope in (['../outside'], [], ['*', 'project-one'], ['absent-project']):
            plans, report = self.plan(request_id='bad-scope', scope=scope)
            self.assertFalse(report['note_planned'])
            self.assertEqual(report['status'], 'partial')
        self.grant()
        data = json.loads(outbox.authorization_path(self.codex).read_bytes())
        data['grants'][0]['source_root'] = str(self.base / 'another-root')
        outbox.authorization_path(self.codex).write_bytes(outbox.encoded(data))
        self.assertFalse(self.plan()[1]['note_planned'])
        _, report = self.plan(request_id='explicit-valid', scope=['project-one'])
        self.assertNotIn('Synthetic A', json.dumps(report))
        self.assert_protected()


if __name__ == '__main__':
    unittest.main()
