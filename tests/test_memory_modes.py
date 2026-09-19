"""One history and publisher across the archive and compatibility entry points."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
import profile_memory
from profile_bridge import memory_outbox as outbox
from profile_bridge.memory.compat import plan_files, apply_files


def setup_home(base):
    claude, codex = base / 'claude', base / 'codex'
    source = claude / 'projects/key/memory/MEMORY.md'
    source.parent.mkdir(parents=True)
    source.write_bytes(b'A\n')
    contract = codex / 'memories/extensions/ad_hoc/instructions.md'
    contract.parent.mkdir(parents=True)
    contract.write_bytes(b'Synthetic ingress')
    return claude, codex, source


def files(claude, codex, text='A'):
    return plan_files(codex, claude / 'projects', 'key', [dict(project='key', path='MEMORY.md', text=text)], request_id='synthetic-request')


def test_archive_then_ingress_and_normalization(tmp_path):
    claude, codex, source = setup_home(tmp_path)
    plans, report = profile_memory.plan_memory(claude, codex)
    profile_memory.apply_memory_plan(plans, claude, codex)
    assert report['mode'] == 'archive'
    intents, counts = files(claude, codex)
    assert apply_files(intents) == 1
    note = outbox.native_note_path(intents[0].record, codex)
    assert b'> A' in note.read_bytes()
    history = (outbox.state_root(codex) / 'history.json').read_bytes()
    intents, counts = files(claude, codex, 'A\r\n')
    assert counts['unchanged'] == 1 and apply_files(intents) == 0
    assert (outbox.state_root(codex) / 'history.json').read_bytes() == history
    source.write_bytes(b'A\r\n')
    plans, _ = profile_memory.plan_memory(claude, codex)
    profile_memory.apply_memory_plan(plans, claude, codex)
    assert len(json.loads((outbox.state_root(codex) / 'history.json').read_bytes())['canonical']['records']) == 1


def test_snapshot_delivery_then_perfile_is_alias(tmp_path):
    claude, codex, _ = setup_home(tmp_path)
    plans, _ = profile_memory.plan_memory(claude, codex, request_id='one', scope=['key'])
    profile_memory.apply_memory_plan(plans, claude, codex)
    intents, _ = files(claude, codex)
    assert intents[0].record['delivery_state'] == 'aliased'
    assert apply_files(intents) == 0
    assert len(list((codex / 'memories/extensions/ad_hoc/notes').glob('*.md'))) == 1


@pytest.mark.parametrize('boundary,state', [('outbox_prepared', 'published'), ('note_published_unrecorded', 'delivery_unknown'), ('note_created', 'published')])
def test_fresh_process_restart_file_delivery(tmp_path, boundary, state):
    claude, codex, _ = setup_home(tmp_path)
    code = '''import os,sys
from pathlib import Path
from profile_bridge import memory_outbox as o
from profile_bridge.memory.compat import plan_files, apply_files
c=Path(sys.argv[1]); d=Path(sys.argv[2])
def stop(point):
    if point == sys.argv[3]: os._exit(73)
o.checkpoint=stop
plans,_=plan_files(d,c/'projects','key',[dict(project='key',path='MEMORY.md',text='A')],request_id='synthetic-request')
apply_files(plans)
'''
    proc = subprocess.run([sys.executable, '-c', code, str(claude), str(codex), boundary], capture_output=True, timeout=30)
    assert proc.returncode == 73, proc.stderr.decode()
    intents, _ = files(claude, codex)
    apply_files(intents)
    history, _ = outbox._read_history(codex)
    assert history['records'][-1]['delivery_state'] == state
    before = (outbox.state_root(codex) / 'history.json').read_bytes()
    intents, _ = files(claude, codex)
    assert apply_files(intents) == 0
    assert before == (outbox.state_root(codex) / 'history.json').read_bytes()
