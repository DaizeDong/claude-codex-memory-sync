"""Producer-generated capture bytes, validation, and same-home restart behavior."""
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

import pytest

from profile_bridge import memory_outbox as outbox
from tools.make_fixtures import make_t03_history, t03_state_bytes


@pytest.fixture
def produced(tmp_path):
    fixture = make_t03_history(tmp_path / 'home')
    return fixture, t03_state_bytes(fixture['codex'])


def test_real_producer_state_is_complete_and_validation_is_pure(produced):
    fixture, values = produced
    with patch.object(outbox, 'read_bounded', side_effect=AssertionError('disk read')),\
            patch.object(outbox, 'validate_path', side_effect=AssertionError('filesystem probe')):
        result = outbox.validate_restore_state(fixture['codex'], values)
    assert result == {'history_initialized': True, 'records': 4, 'grants': 4,
                      'states': ['cancelled', 'delivery_unknown', 'prepared', 'published'],
                      'revoked_requests': ['fixture-revoked']}
    assert len([k for k in values if '/prepared/' in k]) == 4
    assert t03_state_bytes(fixture['codex']) == values


@pytest.mark.parametrize('damage', ['missing_prepared', 'missing_auth', 'missing_history',
                                   'witness_false', 'empty_history', 'receipt', 'conflict', 'cross_home'])
def test_partial_or_changed_restore_state_requires_review(produced, damage):
    fixture, values = produced
    codex = fixture['codex']
    auth = 'claude-sync/memory-authorizations.json'
    history = 'claude-sync/memory-outbox/history.json'
    if damage == 'missing_prepared':
        values.pop(next(k for k in values if '/prepared/' in k))
    elif damage == 'missing_auth':
        values.pop(auth)
    elif damage == 'missing_history':
        values.pop(history)
    elif damage == 'witness_false':
        document = json.loads(values[auth])
        document['history_initialized'] = False
        values[auth] = outbox.encoded(document)
    elif damage in {'empty_history', 'receipt'}:
        document = json.loads(values[history])
        if damage == 'empty_history':
            document['records'] = []
        else:
            next(r for r in document['records'] if r['delivery_state'] == 'published').pop('receipt')
        values[history] = outbox.encoded(document)
    elif damage == 'conflict':
        values['claude-sync/memory-outbox/history-conflict.json'] = b'{"reason":"synthetic"}'
    else:
        codex = fixture['home'] / 'other-codex'
    with pytest.raises(outbox.HistoryConflict):
        outbox.validate_restore_state(codex, values)


def test_byte_capture_restore_then_fresh_process_preserves_outcomes(produced):
    fixture, values = produced
    codex = fixture['codex']
    snapshot = fixture['home'] / 'synthetic-capture'
    # Capture's .codex/ keys match CONFIG's public build_snapshot payload shape.
    for relative, content in values.items():
        saved = snapshot / '.codex' / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        saved.write_bytes(content)
        (codex / relative).unlink()
    for relative in values:
        (codex / relative).write_bytes((snapshot / '.codex' / relative).read_bytes())
    assert t03_state_bytes(codex) == values
    # Consumption of both native files cannot justify retrying unknown/published.
    notes = codex / 'memories/extensions/ad_hoc/notes'
    for note in notes.glob('*.md'):
        note.unlink()
    script = '''
import json, sys
from pathlib import Path
import profile_memory as memory
home = Path(sys.argv[1])
results = {}
for state in ('published', 'unknown', 'revoked', 'prepared'):
    plan, _ = memory.plan_memory(home/'.claude', home/'.codex',
        request_id='fixture-' + state, scope=['project-' + state], periodic=state == 'revoked')
    results[state] = memory.apply_memory_plan(plan, home/'.claude', home/'.codex',
        skills=home/'.agents/skills')['delivery_state']
print(json.dumps(results))
'''
    process = subprocess.run([sys.executable, '-c', script, str(fixture['home'])],
                             capture_output=True, text=True, timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout) == {'published': 'published', 'unknown': 'delivery_unknown',
                                         'revoked': 'cancelled', 'prepared': 'published'}
    assert len(list(notes.glob('*.md'))) == 1
    auth = json.loads(outbox.authorization_path(codex).read_bytes())
    assert auth['history_initialized'] is True
    revoked = next(g for g in auth['grants'] if g['request_id'] == 'fixture-revoked')
    assert revoked['state'] == 'revoked' and revoked['scope'] == ['project-revoked']


@pytest.mark.parametrize('identity', [[1], 'synthetic', {'inode':2}, [1,0], [True,2],
                                     [1,False], ['1',2], [1,2,3], None, [1,-1]])
@pytest.mark.parametrize('state', ['published', 'publication_started', 'delivery_unknown'])
def test_malformed_publication_identity_is_preserved_for_review(produced, identity, state):
    fixture, values = produced
    key = 'claude-sync/memory-outbox/history.json'
    history = json.loads(values[key])
    row = next(r for r in history['records'] if r['delivery_state'] == 'published')
    row['delivery_state'] = state
    row['note_identity'] = identity
    if state == 'published':
        row['receipt']['note_identity'] = identity
    else:
        row.pop('receipt')
    values[key] = outbox.encoded(history)
    before = dict(values)
    with pytest.raises(outbox.HistoryConflict):
        outbox.validate_restore_state(fixture['codex'], values)
    assert values == before
