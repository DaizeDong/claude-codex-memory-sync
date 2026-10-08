"""Durable local contexts with a fake llmcall; no provider or native session."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from skill_smith import overlays, source_workflows
from profile_bridge.workflows import DurableWorkflow, _hash
from catalog_fixture_factory import write as text_file
from tools.make_fixtures import make_workflow_client_contracts


contracts = make_workflow_client_contracts()
Result, Attempt = contracts.Result, contracts.Attempt
process = contracts.process
LADDER = ('codexg', 'codex', 'cc', 'claude')


class Fake:
    """The llmcall 0.3.0 surface: call options only, no per-call cwd/env/cancel."""
    Result, Attempt = Result, Attempt
    process = process
    rung_group = staticmethod(contracts.rung_group)
    model_group = staticmethod(contracts.model_group)

    def __init__(self, ladder=LADDER):
        self.calls = []
        self.fail = False
        self.group = 'review-group'
        self.ladder = tuple(ladder)

    def active_chain(self):
        return self.ladder

    def call(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail:
            raise RuntimeError('synthetic transport interruption')
        return Result(text='full response ' + str(len(self.calls)), provider='fake', group=self.group,
                      attempts=[Attempt('fake', True)])


@pytest.fixture(autouse=True)
def _cwd(tmp_path, monkeypatch):
    # llmcall 0.3.0 runs its clients in the process cwd, so workflows anchor there.
    monkeypatch.chdir(tmp_path)


def descriptor(tmp_path, cli=False):
    path = tmp_path / 'source/SKILL.md'
    text_file(path, '---\nname: synthetic\ndescription: Synthetic context.\n---\nInspect current inputs.\n')
    ep = dict(source_id='synthetic', kind='skill', name='synthetic', relative_path='SKILL.md',
              resolved_path=str(path), path=str(path), source_hash=overlays.digest(path.read_bytes()), client='claude', scope='user')
    rec = dict(source_id='synthetic', source_hash=ep['source_hash'], resolved_path=str(path.parent),
               status={'resolved': 'yes'}, entrypoints=[ep])
    built = overlays.build(rec, 'codex', {'capabilities': {'llmcall.contexts': {'status': 'supported', 'evidence': ['fake']}}})
    built['recipe'] = {'kind': 'cli_compat' if cli else 'research-review'}
    built['artifact_hash'] = overlays.fingerprint({k: v for k, v in built.items() if k != 'artifact_hash'})
    return built


def author():
    return Result(text='Complete original producer result', provider='fake', group='author-group')


def workflow(tmp_path, client, built, **kwargs):
    return DurableWorkflow(tmp_path / '.codex', tmp_path / '.agents/skills', 'synthetic-workflow', built,
                           client=client, **kwargs)


def test_missing_attempt_contract_is_refused_before_state_write(tmp_path):
    fake = Fake()
    fake.Attempt = None
    with pytest.raises(ValueError, match='workflow_contract_unavailable:Attempt'):
        workflow(tmp_path, fake, descriptor(tmp_path))
    assert not (tmp_path / '.codex').exists()
    assert not fake.calls


@pytest.mark.parametrize('missing', ['rung_group', 'model_group', 'active_chain'])
def test_client_without_the_0_3_contract_is_refused_before_state_write(tmp_path, missing):
    fake = Fake()
    setattr(fake, missing, None)
    with pytest.raises(ValueError, match='llmcall_contract_unavailable:' + missing):
        workflow(tmp_path, fake, descriptor(tmp_path))
    assert not (tmp_path / '.codex').exists()
    assert not fake.calls


def test_restart_replays_result_and_continuation_retains_every_round(tmp_path):
    fake, built = Fake(), descriptor(tmp_path)
    first = workflow(tmp_path, fake, built)
    options = dict(context='review', operation='start', prompt='Round one', inputs={'full_file': 'old contents'},
                   producer=author(), exact_model='user-exact', effort='medium')
    result = first.run('round-1', **options)
    assert result and len(fake.calls) == 1
    resumed = workflow(tmp_path, fake, built)
    assert resumed.run('round-1', **options).text == result.text
    assert len(fake.calls) == 1
    second = resumed.run('round-2', context='review', operation='reply', prompt='Full rebuttal',
                         inputs={'full_file': 'revised contents'})
    assert second and len(fake.calls) == 2
    text, arguments = fake.calls[-1]
    for value in ('Round one', 'old contents', 'full response 1', 'Full rebuttal', 'revised contents', 'Complete original producer result'):
        assert value in text
    assert arguments['model'] == 'user-exact' and arguments['gateway_best'] is False
    assert arguments['effort'] == 'medium' and arguments['avoid'] == 'author-group'
    assert not {'selection', 'requirements', 'cwd', 'env', 'cancel'} & set(arguments)
    assert resumed.run('poll', context='review', operation='poll', prompt='').text == second.text
    assert len(fake.calls) == 2
    stored = json.loads(sorted(first.root.glob('*.request.json'))[0].read_text())
    assert stored['request']['encoding_version'] == 3


def test_recording_failure_round_trip_preserves_completed_provider_result(tmp_path):
    import llmcall
    from tools.make_fixtures import make_workflow_recording_result

    fake, built = Fake(), descriptor(tmp_path)
    fake.Result, fake.Attempt = llmcall.Result, llmcall.Attempt
    fake.RecordingFailure = llmcall.RecordingFailure
    recorded = make_workflow_recording_result(llmcall)

    def answer(prompt, **kwargs):
        fake.calls.append((prompt, kwargs))
        return recorded

    fake.call = answer
    options = dict(context='review', operation='start', prompt='Synthetic review', producer=author())
    first = workflow(tmp_path, fake, built).run('one', **options)
    assert first and first.error is None and len(fake.calls) == 1
    restored = workflow(tmp_path, fake, built).run('one', **options)
    assert restored and restored.error is None and restored.text == recorded.text
    assert restored.provider == recorded.provider
    assert restored.recording_errors == recorded.recording_errors
    assert isinstance(restored.recording_errors[0], llmcall.RecordingFailure)
    assert len(fake.calls) == 1


def test_interrupted_call_and_orphan_intent_never_rerun_after_restart(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    fake.fail = True
    first = workflow(tmp_path, fake, built)
    with pytest.raises(RuntimeError):
        first.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit')
    fake.fail = False
    resumed = workflow(tmp_path, fake, built)
    result = resumed.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit')
    assert not result and result.error == 'workflow_outcome_uncertain'
    assert resumed.run_cli('followup', ['exec', 'resume', '--last'], 'Continue').error == 'workflow_outcome_uncertain'
    assert len(fake.calls) == 1
    next(first.root.glob('*.result.json')).unlink()
    assert resumed.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit').error == 'workflow_outcome_uncertain'
    assert len(fake.calls) == 1


def test_failed_agent_call_that_started_a_client_is_uncertain(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    fake.call = lambda prompt, **kwargs: (fake.calls.append((prompt, kwargs)) or
                                          Result(error='timeout', attempts=[Attempt('codex', False, reason='timeout')]))
    session = workflow(tmp_path, fake, built)
    assert not session.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Edit')
    receipt = json.loads(next(session.root.glob('*.result.json')).read_text())
    assert receipt['state'] == 'uncertain'
    assert session.run_cli('next', ['exec'], 'Other').error == 'workflow_outcome_uncertain'


def test_cli_followup_keeps_effects_history_and_exact_user_precedence(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    first = workflow(tmp_path, fake, built, inherited={'model': 'inherited', 'effort': 'low'})
    first.run_cli('first', ['exec', '-m', 'source-flag', '--sandbox', 'workspace-write'], 'Edit once', exact_model='explicit-user')
    resumed = workflow(tmp_path, fake, built, inherited={'model': 'inherited', 'effort': 'low'})
    assert resumed.run_cli('next', ['exec', 'resume', '--last'], 'Inspect the edit')
    text, options = fake.calls[-1]
    assert 'Edit once' in text and 'full response 1' in text and 'Inspect the edit' in text
    assert options['model'] == 'explicit-user' and options['gateway_best'] is False
    # workspace_write is enforced by running only on sandboxed (codex) rungs in agent mode.
    assert options['mode'] == 'agent' and options['chain'] == ['codexg', 'codex']
    assert len(fake.calls) == 2
    assert resumed.run_cli('bad', ['exec', 'resume', 'native-id'], 'No').error == 'unsupported_cli_flag_or_native_session'
    assert len(fake.calls) == 2


def test_identity_and_permissions_failures_are_explicit(tmp_path):
    fake, built = Fake(), descriptor(tmp_path)
    session = workflow(tmp_path, fake, built)
    unknown = Result(provider='fake', text='Unknown actual identity')
    result = session.run('unknown', context='review', operation='start', prompt='Review', producer=unknown)
    assert result.error == 'independent_reviewer_unavailable' and not fake.calls
    fake.group = 'author-group'
    result = session.run('same', context='review', operation='start', prompt='Review', producer=author())
    assert result.error == 'independent_reviewer_unavailable'
    fresh = DurableWorkflow(tmp_path / '.codex', tmp_path / '.agents/skills', 'adversary', built, client=fake)
    result = fresh.run('unsafe', context='fresh', operation='start', prompt='Read repo', producer=author(), mode='agent')
    assert result.error == 'independent_repository_review_requires_read_only'
    fake.group = 'review-group'
    calls = len(fake.calls)
    result = fresh.run('restricted', context='fresh', operation='start', prompt='Read repo', producer=author(), mode='agent',
                       requirements={'access': 'read_only'})
    assert result
    options = fake.calls[-1][1]
    assert len(fake.calls) == calls + 1
    assert options['mode'] == 'judge' and options['chain'] == ['codexg', 'codex'] and options['avoid'] == 'author-group'


def test_read_only_review_without_a_sandboxed_rung_fails_closed(tmp_path):
    fake, built = Fake(ladder=('cc', 'claude')), descriptor(tmp_path)
    fresh = DurableWorkflow(tmp_path / '.codex', tmp_path / '.agents/skills', 'adversary', built, client=fake)
    result = fresh.run('restricted', context='fresh', operation='start', prompt='Read repo', producer=author(), mode='agent',
                       requirements={'access': 'read_only'})
    assert result.error == 'execution_requirements_unenforceable:no_sandboxed_rung' and not fake.calls


def test_request_id_reuse_cannot_change_task(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    session = workflow(tmp_path, fake, built)
    session.run_cli('one', ['exec'], 'One')
    assert session.run_cli('one', ['exec'], 'Different').error == 'request_id_reused_with_different_input'
    assert len(fake.calls) == 1


def test_completed_agent_request_replays_in_a_new_python_process(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    session = workflow(tmp_path, fake, built)
    session.run_cli('one', ['exec'], 'One')
    path = tmp_path / 'descriptor.json'
    path.write_text(json.dumps(built))
    # This subprocess may only use the fake client. Any fresh transport call
    # throws, so a successful reply proves on-disk replay after process restart.
    script = '''import json, pathlib, sys
sys.path[:0] = json.loads(sys.argv[1])
from test_t10_workflow_context import Fake, workflow
root = pathlib.Path(sys.argv[2])
fake = Fake(); fake.fail = True
saved = json.loads((root / 'descriptor.json').read_text())
result = workflow(root, fake, saved).run_cli('one', ['exec'], 'One')
assert result and not fake.calls
print(result.text)
'''
    roots = [str(Path(__file__).parent), *sys.path]
    result = subprocess.run([sys.executable, '-B', '-c', script, json.dumps(roots), str(tmp_path)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'full response 1'


def _write_legacy(session, built, tmp_path):
    request, receipt = contracts.legacy_v2_records(
        artifact_hash=built['artifact_hash'], workflow_id='synthetic-workflow', cwd=str(Path.cwd().resolve()),
        request_id='legacy-1', prompt='Round one', reply='Saved legacy reply',
        producer_text='Complete original producer result', producer_family='author-group',
        reviewer_family='review-group', exact_model='user-exact')
    saved = {'sequence': 0, 'previous': None, 'request': request}
    receipt = {'request_hash': _hash(saved), **receipt}
    receipt['receipt_hash'] = _hash(receipt)
    session.root.mkdir(parents=True)
    (session.root / '00000000.request.json').write_text(json.dumps(saved))
    (session.root / '00000000.result.json').write_text(json.dumps(receipt))


def test_version_2_history_stays_readable_and_resumable(tmp_path):
    fake, built = Fake(), descriptor(tmp_path)
    session = workflow(tmp_path, fake, built)
    _write_legacy(session, built, tmp_path)
    saved = session.run('legacy-1', context='review', operation='start', prompt='Round one',
                        producer=author(), exact_model='user-exact')
    assert saved.text == 'Saved legacy reply' and saved.group == 'review-group'
    assert saved.legacy_fields['outcome'] == 'success' and not fake.calls
    # Negative control: the same ID with a different producer is still refused.
    other = Result(text='Different producer', provider='fake', group='author-group')
    assert session.run('legacy-1', context='review', operation='start', prompt='Round one',
                       producer=other, exact_model='user-exact').error == 'request_id_reused_with_different_input'
    reply = session.run('legacy-2', context='review', operation='reply', prompt='Next round')
    assert reply and len(fake.calls) == 1
    text, options = fake.calls[0]
    assert 'Saved legacy reply' in text and 'Complete original producer result' in text
    assert options['model'] == 'user-exact' and options['gateway_best'] is False
    assert options['avoid'] == 'author-group' and options['mode'] == 'judge'
