"""Real bounded deadlines distinguish slow observations from source churn."""
import time
import pytest
from profile_bridge import sources


def request(tmp_path):
    member = tmp_path/'source'
    member.write_bytes(b'synthetic bytes')
    return {'sources':[{'source_id':'synthetic', 'relative_path':'source', 'path':member}],
            'stage':tmp_path/'stage', 'policy':'synthetic-v1', 'seconds':0.2}


@pytest.mark.parametrize('value', [True, False, float('nan'), float('inf'), '30', 301, 0, -1])
def test_budget_is_finite_and_keeps_default_cap(tmp_path, value):
    with pytest.raises(ValueError, match='invalid_source_budget'):
        sources.freeze({**request(tmp_path), 'seconds':value})


@pytest.mark.parametrize('phase', ['source_discovery', 'source_read', 'staging', 'generation'])
def test_real_phase_deadline_never_reports_accepted_generation(tmp_path, monkeypatch, phase):
    value = request(tmp_path)
    def delayed(original):
        def wrapped(*args, **kwargs):
            result = original(*args, **kwargs)
            time.sleep(0.25)
            return result
        return wrapped
    if phase == 'source_discovery':
        rows = value['sources']
        value['sources'] = delayed(lambda: rows)
    elif phase == 'source_read':
        monkeypatch.setattr(sources, 'read_bounded', delayed(sources.read_bounded))
    elif phase == 'staging':
        monkeypatch.setattr(sources, 'atomic_replace', delayed(sources.atomic_replace))
    else:
        monkeypatch.setattr(sources, '_encoded', delayed(sources._encoded))
    result = sources.freeze(value)
    assert result['status'] == 'busy_sources'
    assert result['reason'] == 'source_deadline_exhausted'
    # The final checkpoint after a completed member runs before the next
    # supplier operation; its phase must still identify the just-completed read.
    assert result['phase'] == phase
    assert result['attempts'] == 1
    assert 'source_generation' not in result


def test_observed_churn_is_distinct_and_reports_completed_members(tmp_path, monkeypatch):
    value = request(tmp_path)
    value.pop('seconds')
    def churn(point, attempt):
        if point == 'between_rounds':
            value['sources'][0]['path'].write_bytes(str(attempt).encode())
    monkeypatch.setattr(sources, 'checkpoint', churn)
    result = sources.freeze(value)
    assert result == {'status':'busy_sources', 'reason':'source_observations_changed',
        'attempts':3, 'phase':'observation_comparison', 'completed_members':1}


def test_access_denied_is_sanitized_and_not_retried(tmp_path):
    value = request(tmp_path)
    def denied():
        raise PermissionError('do not expose this input')
    value['sources'] = denied
    result = sources.freeze(value)
    assert result == {'status':'busy_sources', 'reason':'source_access_denied',
        'attempts':1, 'phase':'source_discovery', 'completed_members':0}


@pytest.mark.parametrize('seconds', [None, 120, 300])
def test_explicit_caller_budget_is_shared_and_default_stays_thirty(tmp_path, seconds):
    value = request(tmp_path)
    rows = value.pop('sources')
    value.pop('seconds')
    observed = []
    def supplier():
        observed.append(sources.remaining_seconds(300))
        # Windows monotonic ticks can be identical for two tiny observations.
        # Advance real time so the strict assertion proves a shared deadline.
        time.sleep(0.04)
        return rows
    value['sources'] = supplier
    if seconds is not None:
        value['seconds'] = seconds
    result = sources.freeze(value)
    expected = 30 if seconds is None else seconds
    assert result['status'] == 'frozen'
    assert len(observed) == 2 and expected-5 < observed[1] < observed[0] <= expected
