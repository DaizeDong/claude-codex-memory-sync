"""Durable local contexts with a fake llmcall; no provider or native session."""
import json
from pathlib import Path
import subprocess
import sys

import pytest
from llmcall.contracts import Result, Attempt, ModelSelection, ExecutionRequirements
from skill_smith import overlays, source_workflows
from profile_bridge.workflows import DurableWorkflow
from catalog_fixture_factory import write as text_file


class Fake:
    Result, Attempt = Result, Attempt
    ModelSelection, ExecutionRequirements = ModelSelection, ExecutionRequirements

    def __init__(self):
        self.calls = []
        self.fail = False
        self.family = 'review-family'
        self.refuse = False

    def call(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        if self.fail:
            raise RuntimeError('synthetic transport interruption')
        if self.refuse:
            return Result(error='capability_unavailable', outcome='capability_unavailable', execution_started=False)
        return Result(text='full response ' + str(len(self.calls)), provider='fake',
            effective_model='synthetic-model', model_source='provider_reported',
            model_family=self.family, effects='observed' if kwargs['mode'] == 'agent' else 'none',
            execution_started=True, outcome='success')


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
    return Result(text='Complete original producer result', provider='fake',
                  model_source='provider_reported', model_family='author-family')


def workflow(tmp_path, client, built, **kwargs):
    return DurableWorkflow(tmp_path / '.codex', tmp_path / '.agents/skills', 'synthetic-workflow', built,
                           client=client, **kwargs)


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
    assert arguments['selection'] == ModelSelection('exact', 'user-exact')
    assert arguments['effort'] == 'medium' and arguments['avoid'] == 'author-family'
    assert resumed.run('poll', context='review', operation='poll', prompt='').text == second.text
    assert len(fake.calls) == 2


def test_interrupted_call_and_orphan_intent_never_rerun_after_restart(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    fake.fail = True
    first = workflow(tmp_path, fake, built)
    with pytest.raises(RuntimeError):
        first.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit')
    fake.fail = False
    resumed = workflow(tmp_path, fake, built)
    result = resumed.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit')
    assert result.outcome == 'workflow_outcome_uncertain' and result.effects == 'possible'
    assert resumed.run_cli('followup', ['exec', 'resume', '--last'], 'Continue').outcome == 'workflow_outcome_uncertain'
    assert len(fake.calls) == 1
    next(first.root.glob('*.result.json')).unlink()
    assert resumed.run_cli('edit', ['exec', '--sandbox', 'workspace-write'], 'Perform approved edit').outcome == 'workflow_outcome_uncertain'
    assert len(fake.calls) == 1


def test_cli_followup_keeps_effects_history_and_exact_user_precedence(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    first = workflow(tmp_path, fake, built, inherited={'model': 'inherited', 'effort': 'low'})
    first.run_cli('first', ['exec', '-m', 'source-flag', '--sandbox', 'workspace-write'], 'Edit once', exact_model='explicit-user')
    resumed = workflow(tmp_path, fake, built, inherited={'model': 'inherited', 'effort': 'low'})
    assert resumed.run_cli('next', ['exec', 'resume', '--last'], 'Inspect the edit')
    text, options = fake.calls[-1]
    assert 'Edit once' in text and 'full response 1' in text and 'Inspect the edit' in text
    assert options['selection'] == ModelSelection('exact', 'explicit-user')
    assert options['requirements'].access == 'workspace_write'
    assert len(fake.calls) == 2
    assert resumed.run_cli('bad', ['exec', 'resume', 'native-id'], 'No').outcome == 'unsupported_cli_flag_or_native_session'
    assert len(fake.calls) == 2


def test_identity_and_permissions_failures_are_explicit(tmp_path):
    fake, built = Fake(), descriptor(tmp_path)
    session = workflow(tmp_path, fake, built)
    unknown = Result(provider='fake', text='Unknown actual identity')
    result = session.run('unknown', context='review', operation='start', prompt='Review', producer=unknown)
    assert result.outcome == 'independent_reviewer_unavailable' and not fake.calls
    fake.family = 'author-family'
    result = session.run('same', context='review', operation='start', prompt='Review', producer=author())
    assert result.outcome == 'independent_reviewer_unavailable'
    fresh = DurableWorkflow(tmp_path / '.codex', tmp_path / '.agents/skills', 'adversary', built, client=fake)
    result = fresh.run('unsafe', context='fresh', operation='start', prompt='Read repo', producer=author(), mode='agent')
    assert result.outcome == 'independent_repository_review_requires_read_only'
    fake.refuse = True
    result = fresh.run('restricted', context='fresh', operation='start', prompt='Read repo', producer=author(), mode='agent',
                       requirements=ExecutionRequirements(access='read_only'))
    assert result.outcome == 'capability_unavailable'


def test_request_id_reuse_cannot_change_task(tmp_path):
    fake, built = Fake(), descriptor(tmp_path, cli=True)
    session = workflow(tmp_path, fake, built)
    session.run_cli('one', ['exec'], 'One')
    assert session.run_cli('one', ['exec'], 'Different').outcome == 'request_id_reused_with_different_input'
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
