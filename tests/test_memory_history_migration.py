"""Synthetic evidence validation and real-process restart tests for T09."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from profile_bridge import memory_outbox as outbox
from profile_bridge.memory.core import canonical_text
from profile_bridge.memory.history import observe, validate
from profile_bridge.memory.legacy import import_notes, parse_note
from profile_bridge.memory.restore import migrate_restore_state
from tools.make_fixtures import make_ccms_note, make_t03_history, t03_state_bytes


def test_archived_receipt_is_only_an_alias(tmp_path):
    from profile_bridge.memory.legacy import migrate_archive_state
    raw = b'Synthetic archive fact\n'
    records = [dict(project='key', path='MEMORY.md', sha256=outbox.sha(raw), raw_sha256=outbox.sha(raw), bytes=len(raw))]
    scopes = {'key': None}
    digest = outbox.sha(json.dumps(dict(schema=1, sources=records, scope=scopes), sort_keys=True,
                                  ensure_ascii=True, separators=(',', ':')).encode())
    index = f'<!-- claude-memory-source-sha256: {digest} -->\n<!-- claude-memory-note-source-sha256: {digest} -->\n'.encode()
    files = migrate_archive_state(tmp_path / 'codex', tmp_path / 'claude/projects', index, records, scopes, {('key', 'MEMORY.md'): raw})
    state = json.loads(files['claude-sync/memory-outbox/history.json'])
    assert state['records'][0]['delivery_state'] == 'aliased'
    assert json.loads(files['claude-sync/memory-authorizations.json'])['grants'] == []
    assert len(files) == 2
    with pytest.raises(ValueError):
        migrate_archive_state(tmp_path / 'codex', tmp_path / 'claude/projects', index, records, scopes, {('key', 'MEMORY.md'): b'changed'})


@pytest.mark.parametrize('raw', [b'\xef\xbb\xbfCafe\xcc\x81\r\n\r\n', 'Café\n'.encode('utf-16'), 'Café\r'.encode(), 'Café\u2028'.encode()])
def test_old_ps_normalization_vectors(raw):
    assert canonical_text(raw, strict=True) == 'Café\n'


def test_canonical_aba_delete_reappear_and_move():
    value = None
    for text in ('A', 'A\r\n', 'B', 'A'):
        value, _ = observe(value, 'registered-root', [dict(project='key', path='MEMORY.md', text=text)], complete_projects=['key'])
    assert len(value['records']) == 3
    assert len({e['increment_id'] for e in value['records']}) == 3
    value, changes = observe(value, 'registered-root', [], complete_projects=['key'])
    assert changes[0]['operation'] == 'delete'
    value, changes = observe(value, 'registered-root', [dict(project='key', path='MEMORY.md', text='A')])
    assert changes[0]['operation'] == 'reappear'
    value, changes = observe(value, 'registered-root', [dict(project='key', path='moved.md', text='A')], complete_projects=['key'])
    assert {e['operation'] for e in changes} == {'add', 'delete'}
    validate(value)


def test_path_collision_rejected_and_project_namespace_distinct():
    with pytest.raises(ValueError, match='collision'):
        observe(None, 'root', [dict(project='one', path=p, text='A') for p in ['A.md', 'a.md']])
    value, _ = observe(None, 'root', [dict(project=p, path='a.md', text='A') for p in ['a-b', 'a_b']])
    assert len(value['sources']) == 2


def test_legacy_chain_alias_twice_and_tamper(tmp_path):
    project = str(tmp_path / 'synthetic-project')
    first, raw, previous = make_ccms_note(project, 'MEMORY.md', 'A')
    second, updated, _ = make_ccms_note(project, 'MEMORY.md', 'B', previous, 2)
    notes = {second: updated, first: raw}
    value = import_notes(None, 'registered-root', 'key', project, notes)
    assert len(value['records']) == 2
    assert all(a['delivery'] == 'unproven' for a in value['aliases'])
    assert import_notes(value, 'registered-root', 'key', project, notes) == value
    with pytest.raises(ValueError):
        parse_note(first, raw.replace(b'> A', b'> Z'))
    with pytest.raises(ValueError, match='disconnected'):
        import_notes(None, 'registered-root', 'key', project, {second: updated})
    with pytest.raises(ValueError, match='scope'):
        import_notes(None, 'registered-root', 'key', project + '-different', notes)


def migrate_fixture(tmp_path):
    fixture = make_t03_history(tmp_path / 'old')
    codex = tmp_path / 'old/.codex'
    target = tmp_path / 'new/.codex'
    files = t03_state_bytes(codex)
    auth = json.loads(files['claude-sync/memory-authorizations.json'])
    history = json.loads(files['claude-sync/memory-outbox/history.json'])
    scopes = {(r['source_root'], tuple(r['scope'])) for r in history['records']}
    scopes |= {(g['source_root'], tuple(g['scope'])) for g in auth['grants']}
    evidence = [dict(source_root=root, target_root=outbox._root(tmp_path / 'new/.claude/projects'), scope=list(scope)) for root, scope in scopes]
    return codex, target, files, evidence


def test_cross_home_twice_no_witness_remap_or_new_notes(tmp_path):
    codex, target, files, evidence = migrate_fixture(tmp_path)
    result = migrate_restore_state(codex, target, files, scope_evidence=evidence)
    outbox.validate_restore_state(target, result['files'])
    state = json.loads(result['files']['claude-sync/memory-outbox/history.json'])
    assert all('receipt' not in r and 'note_identity' not in r for r in state['records'])
    assert 'published' not in {r['delivery_state'] for r in state['records']}
    assert 'prepared' not in {r['delivery_state'] for r in state['records']}
    assert result['report']['native_notes_written'] == 0
    again = migrate_restore_state(codex, target, files, scope_evidence=evidence, destination_files=result['files'])
    assert again['files'] == {} and not again['report']['changed']
    for key, payload in result['files'].items():
        path = target / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    before = {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()}
    code = "from pathlib import Path; import sys; from profile_bridge.memory_outbox import _read_history; h,_=_read_history(Path(sys.argv[1])); assert all('note_identity' not in r for r in h['records'])"
    proc = subprocess.run([sys.executable, '-c', code, str(target)], capture_output=True, timeout=30)
    assert proc.returncode == 0, proc.stderr.decode()
    assert before == {p.relative_to(target).as_posix(): p.read_bytes() for p in target.rglob('*') if p.is_file()}


def test_restore_scope_and_destination_conflicts(tmp_path):
    codex, target, files, evidence = migrate_fixture(tmp_path)
    with pytest.raises(outbox.HistoryConflict):
        migrate_restore_state(codex, target, files, scope_evidence=evidence[:-1])
    with pytest.raises(outbox.HistoryConflict):
        migrate_restore_state(codex, target, files, scope_evidence=evidence, destination_files={'newer': b'preserve'})
    damaged = dict(files)
    key = next(k for k in damaged if '/prepared/' in k)
    damaged[key] += b'corrupt'
    with pytest.raises(outbox.HistoryConflict):
        migrate_restore_state(codex, target, damaged, scope_evidence=evidence)
