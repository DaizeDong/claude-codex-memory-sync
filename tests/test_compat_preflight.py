"""Rejected compatibility batches cannot grant or publish any intent."""
import copy
import pytest
from profile_bridge.memory.compat import plan_files, apply_files


@pytest.mark.parametrize('damage', ['first_stale', 'later_stale', 'other_target', 'other_history'])
def test_all_compat_preconditions_precede_authorization(tmp_path, damage):
    codex = tmp_path / 'codex'
    intents, _ = plan_files(codex, tmp_path / 'projects', 'synthetic', [
        dict(project='synthetic', path=name, text='Synthetic fact')
        for name in ('MEMORY.md', 'other.md')], request_id='synthetic')
    assert len(intents) == 2
    if damage == 'first_stale':
        intents[0].expected = b'stale'
    elif damage == 'later_stale':
        intents[1].expected = b'stale'
    elif damage == 'other_target':
        intents[1].codex = tmp_path / 'other'
    else:
        intents[1].history = copy.deepcopy(intents[1].history)
        intents[1].history['requests']['unexpected'] = 'foreign'
    with pytest.raises(ValueError):
        apply_files(intents, skills=tmp_path / 'skills')
    assert not list(tmp_path.rglob('*.json'))
    assert all(intent.new_grant for intent in intents)
