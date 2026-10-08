"""Caller-owned llmcall contexts in private SYNC state, without native resume.

There is no scheduler or global ledger. Existing profile locks serialize callers;
fleet filesystem primitives publish immutable request/result records. A request
without its result is uncertain after restart and is never automatically replayed.
"""
from copy import deepcopy
from dataclasses import fields, is_dataclass
import hashlib
import inspect
import json
from pathlib import Path

from fleet_guards.filesystem import create_no_replace
from profile_lock import profile_locks
from skill_smith import overlays, role_entrypoints, source_workflows

# Version 3 records hold plain requirement and session-option mappings for the
# llmcall 0.3.0 contract. Version 2 records (typed 0.2.0 ModelSelection and
# ExecutionRequirements, richer Result fields) stay readable and resumable.
ENCODING_VERSION = 3
READABLE_ENCODINGS = frozenset({2, 3})


def _json(value):
    return (json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')) + '\n').encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def load_descriptor(path):
    from profile_sync import assert_plain_path
    path = Path(path)
    assert_plain_path(path)
    descriptor = json.loads(path.read_text(encoding='utf-8'))
    if descriptor.get('status') != 'ready' or not overlays.validate(descriptor, target_runtime='codex'):
        raise ValueError('overlay_unavailable_or_stale')
    return descriptor


def _encode(value):
    if is_dataclass(value):
        return {'encoding': 'contract', 'type': type(value).__name__, 'fields': {f.name: _encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, dict):
        return {'encoding': 'dict', 'items': {str(k): _encode(v) for k, v in value.items()}}
    if isinstance(value, (list, tuple)):
        return [_encode(v) for v in value]
    if isinstance(value, Path):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise ValueError('workflow_context_not_serializable')


def _construct(client, kind, data):
    """Build a client contract from stored fields, keeping retired fields readable.

    llmcall 0.2.0 Results carried fields 0.3.0 dropped. They are attached as
    ``legacy_fields`` instead of failing the read; a provider-reported 0.2.0
    model family is the 0.3.0 answering group, so it becomes ``group``.
    """
    cls = getattr(client, kind, None)
    if not callable(cls):
        raise ValueError('workflow_contract_unavailable:' + kind)
    try:
        params = inspect.signature(cls).parameters
    except (TypeError, ValueError):
        params = {}
    open_kwargs = any(p.kind is p.VAR_KEYWORD for p in params.values())
    accepted = {k: v for k, v in data.items() if open_kwargs or k in params}
    legacy = {k: v for k, v in data.items() if k not in accepted}
    if (kind == 'Result' and 'group' in params and accepted.get('group') is None
            and legacy.get('model_source') == 'provider_reported' and legacy.get('model_family')):
        accepted['group'] = legacy['model_family']
    value = cls(**accepted)
    if legacy:
        value.legacy_fields = legacy
    return value


def _decode(value, client):
    if isinstance(value, list):
        return [_decode(v, client) for v in value]
    if not isinstance(value, dict):
        return value
    if set(value) == {'encoding', 'items'} and value['encoding'] == 'dict':
        return {k: _decode(v, client) for k, v in value['items'].items()}
    if set(value) == {'encoding', 'type', 'fields'} and value['encoding'] == 'contract':
        # Stub types can have different names; persistence uses structural kinds
        # assigned by _session below for llmcall options and Results.
        kind = value['type']
        if kind not in {'Result', 'Attempt', 'RecordingFailure', 'ModelSelection', 'ExecutionRequirements'}:
            raise ValueError('unsupported_workflow_contract_type')
        data = {k: _decode(v, client) for k, v in value['fields'].items()}
        if kind == 'ExecutionRequirements':
            # Retired 0.2.0 type: read as the plain mapping llmcall 0.3.0 callers use.
            return role_entrypoints.requirements_mapping(data)
        if kind == 'ModelSelection':
            return data
        return _construct(client, kind, data)
    raise ValueError('invalid_workflow_encoding')


def _contract(value, kind):
    encoded = _encode(value)
    if value is not None:
        encoded['type'] = kind
    return encoded


def _session(session):
    contexts = {}
    for name, state in session.contexts.items():
        contexts[name] = {'history': _encode(state['history']),
            'producer': _contract(state['producer'], 'Result'),
            'result': _contract(state['result'], 'Result'),
            'requirements': _encode(state['requirements']),
            'cwd': state.get('cwd'),
            'effects': state.get('effects', 'possible'),
            'inherited': _encode(role_entrypoints.normalize_inherited(state['inherited']))}
    return {'encoding_version': ENCODING_VERSION, 'contexts': contexts,
            'completed': {str(k): _contract(v, 'Result') for k, v in session.completed.items()}}


def _restore(session, state):
    if state.get('encoding_version') not in READABLE_ENCODINGS:
        raise ValueError('legacy_workflow_requires_explicit_migration')
    contexts = {}
    for name, context in state['contexts'].items():
        restored = {key: value if key in ('cwd', 'effects') else _decode(value, session.client)
                    for key, value in context.items()}
        restored['requirements'] = role_entrypoints.requirements_mapping(restored.get('requirements'))
        restored['inherited'] = role_entrypoints.normalize_inherited(restored.get('inherited'))
        if 'effects' not in restored:
            # Version 2 kept effects on the Result; unknown means possibly side-effectful.
            legacy = getattr(restored.get('result'), 'legacy_fields', None) or {}
            restored['effects'] = legacy.get('effects', 'possible')
        contexts[name] = restored
    session.contexts = contexts
    session.completed = {int(k): _decode(v, session.client) for k, v in state['completed'].items()}


def _comparable(request, client):
    """A request's meaning, independent of which encoding version stored it."""
    data = {k: v for k, v in request.items() if k != 'encoding_version'}
    data['requirements'] = role_entrypoints.requirements_mapping(_decode(request.get('requirements'), client))
    data['inherited'] = role_entrypoints.normalize_inherited(_decode(request.get('inherited'), client))
    producer = _decode(request.get('producer'), client)
    data['producer'] = None if producer is None else {
        'text': producer.text, 'provider': producer.provider, 'error': getattr(producer, 'error', None),
        'group': role_entrypoints.result_group(producer), 'data': _encode(getattr(producer, 'data', None))}
    return data


class DurableWorkflow:
    """Explicit local compatibility context, scoped to one caller profile.

    Root/profile paths and workflow IDs are mandatory. Callers supply current
    environment and cancellation objects per invocation; neither is persisted.
    Completed request IDs return immutable prior results, even after restart.
    Distinct followup IDs are new calls carrying full history, not re-execution.
    """
    def __init__(self, codex, skills, workflow_id, descriptor, *, inherited=None, client=None):
        if not isinstance(workflow_id, str) or not workflow_id:
            raise ValueError('explicit_workflow_id_required')
        self.codex, self.skills = Path(codex).absolute(), Path(skills).absolute()
        self.workflow_id, self.descriptor = workflow_id, deepcopy(descriptor)
        self.client = role_entrypoints._client(client)
        if not callable(getattr(self.client, 'Attempt', None)):
            raise ValueError('workflow_contract_unavailable:Attempt')
        self.inherited = role_entrypoints.normalize_inherited(inherited)
        self.root = self.codex / 'claude-sync/workflows' / _hash(workflow_id)

    def _failure(self, reason):
        """A falsy Result. 'workflow_outcome_uncertain' marks possible prior effects."""
        return self.client.Result(error=reason)

    def read_legacy_result(self, request_id):
        """Explicit V1 compatibility: retrieve verified results without replay.

        V1 histories have ambiguous nested tags and unanchored workspaces. They
        cannot authorize a continuation. Only schema-defined Result/Attempt
        positions are interpreted; data and transcript dictionaries stay opaque.
        """
        from profile_sync import assert_plain_path

        def result_contract(value, kind):
            if not isinstance(value, dict) or set(value) != {'type', 'fields'} or value['type'] != kind:
                raise ValueError('invalid_legacy_workflow_contract')
            data = deepcopy(value['fields'])
            if kind == 'Result':
                data['attempts'] = [result_contract(item, 'Attempt') for item in data.get('attempts', [])]
            return _construct(self.client, kind, data)

        with profile_locks(self.codex, self.skills):
            assert_plain_path(self.root)
            previous = None
            for sequence, path in enumerate(sorted(self.root.glob('*.request.json'))):
                assert_plain_path(path)
                saved = json.loads(path.read_text(encoding='utf-8'))
                if (saved.get('sequence') != sequence or saved.get('previous') != previous or
                    saved.get('request', {}).get('artifact_hash') != self.descriptor['artifact_hash']):
                    raise ValueError('workflow_history_integrity_failed')
                result_path = path.with_name(path.name.replace('.request.json', '.result.json'))
                assert_plain_path(result_path)
                if not result_path.exists():
                    return self._failure('workflow_outcome_uncertain')
                receipt = json.loads(result_path.read_text(encoding='utf-8'))
                previous = receipt.get('receipt_hash')
                if (receipt.get('request_hash') != _hash(saved) or previous !=
                    _hash({k: v for k, v in receipt.items() if k != 'receipt_hash'})):
                    raise ValueError('workflow_result_integrity_failed')
                if receipt.get('state') != 'completed':
                    return self._failure('workflow_outcome_uncertain')
                if saved['request']['request_id'] == request_id:
                    if saved['request'].get('encoding_version') is not None:
                        return self._failure('legacy_record_required')
                    return result_contract(receipt['result'], 'Result')
            return self._failure('legacy_request_unavailable')

    def run_cli(self, request_id, argv, prompt, **options):
        if (self.descriptor.get('recipe') or {}).get('kind') != 'cli_compat':
            return self._failure('cli_compat_recipe_required')
        try:
            translated = source_workflows.cli_request(argv)
        except ValueError as error:
            return self._failure(str(error))
        request = translated.pop('requirements')
        for field in ('exact_model', 'effort', 'cwd'):
            if options.get(field) is None and translated[field] is not None:
                options[field] = translated[field]
        try:
            options['requirements'] = role_entrypoints._requirements(
                request, options.get('requirements') or request, options.get('cwd'))
        except (ValueError, TypeError) as error:
            return self._failure(str(error))
        return self.run(request_id, context='cli', operation='reply' if translated['resume'] else 'start',
                        prompt=prompt, mode='agent', compatibility=translated, **options)

    def run(self, request_id, *, context, operation, prompt, inputs=None, producer=None,
            mode='judge', exact_model=None, effort=None, requirements=None, cwd=None,
            env=None, cancel=None, timeout=None, compatibility=None, allow_workspace_change=False):
        from profile_sync import assert_plain_path, ensure_external
        if not isinstance(request_id, str) or not request_id:
            return self._failure('explicit_request_id_required')
        if self.descriptor.get('status') != 'ready' or not overlays.validate(self.descriptor):
            return self._failure('overlay_unavailable_or_stale')
        try:
            requirements = role_entrypoints.requirements_mapping(requirements)
        except (ValueError, TypeError) as error:
            return self._failure(str(error))
        recipe = self.descriptor.get('recipe') or {}
        independent = recipe.get('kind') != 'cli_compat'
        if independent and mode == 'agent':
            if requirements is None or requirements['access'] != 'read_only':
                return self._failure('independent_repository_review_requires_read_only')
        request = {'encoding_version': ENCODING_VERSION, 'request_id': request_id, 'workflow_id': self.workflow_id,
            'artifact_hash': self.descriptor['artifact_hash'], 'context': context,
            'operation': operation, 'prompt': prompt, 'inputs': _encode(inputs),
            'producer': _contract(producer, 'Result'), 'mode': mode,
            'exact_model': exact_model, 'effort': effort,
            'requirements': _encode(requirements), 'cwd': str(cwd) if cwd else None,
            'inherited': _encode(self.inherited), 'compatibility': compatibility,
            'environment_digest': _hash(env) if env is not None else None,
            'workspace_request': {'cwd': str(cwd) if cwd is not None else None,
                'workspace': requirements['workspace'] if requirements is not None else None,
                'allow_change': allow_workspace_change}}
        ensure_external(self.root)
        assert_plain_path(self.root)
        with profile_locks(self.codex, self.skills):
            assert_plain_path(self.root)
            self.root.mkdir(parents=True, exist_ok=True)
            session = role_entrypoints.WorkflowSession(self.descriptor, client=self.client, inherited=self.inherited)
            prior_hash = None
            pending = False
            replay = None
            paths = sorted(self.root.glob('*.request.json'))
            for index, path in enumerate(paths):
                assert_plain_path(path)
                saved = json.loads(path.read_text(encoding='utf-8'))
                if (saved.get('sequence') != index or saved.get('previous') != prior_hash
                    or saved.get('request', {}).get('artifact_hash') != self.descriptor['artifact_hash']):
                    raise ValueError('workflow_history_integrity_failed')
                if saved['request'].get('encoding_version') not in READABLE_ENCODINGS:
                    # V1 overloaded ordinary JSON with contract tags and did not
                    # pin a caller cwd. Keep those immutable records readable as
                    # evidence, but require explicit migration before execution.
                    return self._failure('legacy_workflow_requires_explicit_migration')
                result_path = path.with_name(path.name.replace('.request.json', '.result.json'))
                assert_plain_path(result_path)
                if result_path.exists():
                    receipt = json.loads(result_path.read_text(encoding='utf-8'))
                    if receipt.get('request_hash') != _hash(saved) or receipt.get('receipt_hash') != _hash({k: v for k, v in receipt.items() if k != 'receipt_hash'}):
                        raise ValueError('workflow_result_integrity_failed')
                    if receipt['state'] == 'completed':
                        _restore(session, receipt['session'])
                    else:
                        pending = True
                    prior_hash = receipt['receipt_hash']
                else:
                    receipt = None
                    pending = True
                if saved['request']['request_id'] == request_id:
                    if receipt is None or receipt['state'] != 'completed':
                        return self._failure('workflow_outcome_uncertain')
                    replay = (saved, receipt)
                    break
                if pending:
                    return self._failure('workflow_outcome_uncertain')
            previous = session.contexts.get(context)
            if replay is not None:
                # Idempotent retrieval uses the original absolute anchor, even
                # if the retrieving process has a different caller cwd.
                previous = {'cwd': replay[0]['request']['cwd']}
                if request['workspace_request'] != replay[0]['request']['workspace_request']:
                    return self._failure('request_id_reused_with_different_input')
                cwd = previous['cwd']
                requirements = role_entrypoints.requirements_mapping(
                    _decode(replay[0]['request']['requirements'], self.client))
            try:
                requirements, cwd = role_entrypoints.anchor_workspace(
                    self.client, requirements, cwd, env, previous=previous,
                    allow_change=allow_workspace_change)
            except (ValueError, TypeError) as error:
                return self._failure(str(error))
            request['cwd'] = cwd
            request['requirements'] = _encode(requirements)
            if replay is not None:
                if _comparable(replay[0]['request'], self.client) != _comparable(request, self.client):
                    return self._failure('request_id_reused_with_different_input')
                return _decode(replay[1]['result'], self.client)
            # Validate ordering before recording intent; invalid continuation is
            # a definite non-execution and must not poison an otherwise fresh run.
            if operation in {'reply', 'poll'} and context not in session.contexts:
                return self._failure('context_unavailable')
            if operation == 'start' and context in session.contexts:
                return self._failure('context_already_started')
            sequence = len(paths)
            saved = {'sequence': sequence, 'previous': prior_hash, 'request': request}
            path = self.root / f'{sequence:08d}.request.json'
            result_path = self.root / f'{sequence:08d}.result.json'
            if not create_no_replace(path, _json(saved)):
                raise ValueError('workflow_request_conflict')
            try:
                result = session.run_turn(context=context, operation=operation, prompt=prompt,
                    mode=mode, independent_review=independent, inputs=inputs, producer=producer,
                    exact_model=exact_model, effort=effort, requirements=requirements,
                    cwd=cwd, env=env, cancel=cancel, timeout=timeout,
                    allow_workspace_change=allow_workspace_change)
                # A failed call that may have started an agent leaves its effects unknown.
                uncertain = not result and session.last_effects == "possible"
                receipt = {'request_hash': _hash(saved), 'state': 'uncertain' if uncertain else 'completed',
                           'result': _contract(result, 'Result'), 'session': _session(session)}
            except BaseException:
                # Includes cancellation/interruptions after provider dispatch.
                # The immutable request remains replay protection if receipt
                # publication itself is interrupted.
                receipt = {'request_hash': _hash(saved), 'state': 'uncertain',
                           'result': None, 'session': None}
                receipt['receipt_hash'] = _hash(receipt)
                create_no_replace(result_path, _json(receipt))
                raise
            receipt['receipt_hash'] = _hash(receipt)
            if not create_no_replace(result_path, _json(receipt)):
                raise ValueError('workflow_result_conflict')
            return result
