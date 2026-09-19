"""Pure SYNC projection of SMITH selection/overlay descriptors.

The existing profile planner owns publication, member ownership and rollback.
This module neither discovers sources nor writes profile files.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from skill_smith import conflicts, overlays, role_entrypoints


OMITTED = object()


def validate_inputs(values):
    """Validate explicit documents before profile locks, discovery or writes."""
    for name, value in values.items():
        if name == 'runtime_policy':
            conflicts.validate_policy(value)
        elif name == 'capabilities':
            conflicts.validate_capabilities(value)
        elif name == 'resource_roots':
            if not isinstance(value, dict) or any(
                not isinstance(k, str) or not k or not isinstance(v, str) or not v
                for k, v in value.items()):
                raise ValueError('invalid_resource_roots')
        elif name == 'role_equivalence':
            if not isinstance(value, dict):
                raise ValueError('invalid_role_equivalence')
            for identity, receipt in value.items():
                if (not isinstance(identity, str) or not identity or not isinstance(receipt, dict)
                    or not isinstance(receipt.get('status'), str) or not receipt['status']
                    or not isinstance(receipt.get('artifact_hash'), str)
                    or not isinstance(receipt.get('evidence'), list)
                    or any(not isinstance(v, str) or not v for v in receipt['evidence'])):
                    raise ValueError('invalid_role_equivalence')
    return values


def require_valid_selection(report):
    """A malformed selection cannot proceed to apply or a healthy marker."""
    if report.get('runtime_selection', {}).get('status') in {'unsupported', 'invalid', 'error'}:
        raise ValueError('invalid_runtime_selection')


def key(entry):
    return (entry.get('source_id'), entry.get('kind'), entry.get('relative_path'),
            entry.get('client'), entry.get('scope'))


def selector(entry):
    return {k: entry[k] for k in ('source_id', 'kind', 'relative_path', 'client', 'scope') if k in entry}


def known_role(record, entry):
    plugin = (record.get('registry_key') or '').removesuffix('@claude-plugins-official')
    return ((plugin, entry.get('name')) in role_entrypoints.NATIVE_ROLES + role_entrypoints.RESTRICTED_ROLES
            and record.get('registry_key', '').endswith('@claude-plugins-official'))


def prepare(snapshot, policy, capabilities, *, requests=(), resource_roots=None, equivalence=None):
    """Consume the caller's single catalog snapshot and explicit observations."""
    validate_inputs({k: v for k, v in (('runtime_policy', policy), ('capabilities', capabilities),
        ('resource_roots', resource_roots), ('role_equivalence', equivalence)) if v is not None})
    resource_roots, equivalence = resource_roots or {}, equivalence or {}
    state = {'entries': {}, 'roles': {}, 'selections': [], 'status': 'ready',
             'policy_hash': conflicts.fingerprint(policy),
             'capability_hash': conflicts.fingerprint(capabilities), 'discovery': 'unchecked'}
    initial = conflicts.select({}, capabilities, policy, snapshot)
    if initial.get('reason') in {'runtime_policy_missing', 'schema_version', 'invalid_selection_input', 'invalid_policy_entry', 'capability_snapshot_missing'}:
        state.update(status=initial['status'], reason=initial['reason'])
    for request in requests:
        state['selections'].append({'request': deepcopy(request),
                                    'decision': conflicts.select(request, capabilities, policy, snapshot)})
    candidates = []
    if state['status'] == 'ready':
        for rule in (policy or {}).get('entries', []):
            decision = conflicts.select({'override': rule['id']}, capabilities, policy, snapshot)
            state['selections'].append({'request': {'override': rule['id']}, 'decision': decision})
            for candidate in decision.get('candidates', []):
                candidates.append((candidate['entrypoint'], rule['id'], decision, candidate))
    for record in snapshot.get('records', []):
        for ep in record.get('entrypoints', []):
            role = ep.get('kind') == 'agent_template' and known_role(record, ep)
            choices = [(policy_id, decision, candidate) for entry, policy_id, decision, candidate in candidates if key(entry) == key(ep)]
            picked = [policy_id for policy_id, _, _ in choices]
            eligible = [choice for choice in choices if not choice[2]['blocked']
                        and choice[1]['status'] == 'selected'
                        and key(choice[1]['selection']['entrypoint']) == key(ep)]
            if not role and not picked:
                continue
            if state['status'] != 'ready':
                descriptor = {'status': state['status'], 'reasons': [state.get('reason')]}
            elif capabilities is None:
                descriptor = {'status': 'blocked', 'reasons': ['capability_snapshot_missing']}
            elif choices and not eligible:
                blockers = sorted({reason for _, _, candidate in choices for reason in candidate['blocked']})
                descriptor = {'status': 'blocked', 'reasons': blockers or ['candidate_not_selected']}
            else:
                descriptor = overlays.build(record, 'codex', capabilities, selector=selector(ep),
                    resource_root=resource_roots.get(record['source_id']),
                    workflow_kind='review' if ep.get('name') in {'research-review', 'auto-review-loop'} else None)
            item = {'record': record, 'entrypoint': ep, 'overlay': descriptor, 'policy_ids': picked}
            state['entries'][key(ep)] = item
            if role:
                receipt = equivalence.get(record['source_id'] + ':' + ep['relative_path'], {})
                item['equivalent'] = (descriptor.get('status') == 'ready'
                    and receipt.get('artifact_hash') == descriptor.get('artifact_hash')
                    and receipt.get('status') == 'verified' and bool(receipt.get('evidence')))
    state['selection_policy'] = deepcopy(policy)
    state['selection_capabilities'] = deepcopy(capabilities)
    state['selection_snapshot'] = {'schema_version': snapshot['schema_version'], 'records': [
        {'source_id': record['source_id'], 'status': deepcopy(record.get('status', {})),
         'entrypoints': [{k: deepcopy(v) for k, v in ep.items() if k != 'evidence'}
                         for ep in record.get('entrypoints', []) if key(ep) in state['entries']]}
        for record in snapshot.get('records', [])
        if any(key(ep) in state['entries'] for ep in record.get('entrypoints', []))]}
    return state


def bundle(name, description, target, descriptor):
    """Render owned files with resource hierarchy, using installed entrypoints."""
    if descriptor.get('status') != 'ready' or not overlays.validate(descriptor, target_runtime='codex'):
        raise ValueError('overlay_not_ready_or_stale')
    target = Path(target)
    descriptor_path = target.parent / 'workflow.json'
    source_relative = Path(descriptor['source_file']).relative_to(descriptor['resource_root'])
    source_payload = target.parent / 'payload' / source_relative
    template_path = source_payload
    if target.name != 'SKILL.md' and source_payload.name == 'SKILL.md':
        template_path = source_payload.with_name('SOURCE.md')
    body = (f'---\nname: {name}\ndescription: {json.dumps(description)}\n---\n\n'
        + descriptor['execution_instructions'] + '\n\n'
        + f'Load the pinned descriptor `{descriptor_path.as_posix()}` with the installed '
          '`profile_bridge.workflows.load_descriptor`. Use '
          '`skill_smith.role_entrypoints.invoke` for a role template or '
          '`profile_bridge.workflows.DurableWorkflow` for workflow turns. Supply the current '
          'session options, exact user model choices, actual producer Result, current inputs, '
          'cancellation and remaining timeout. Never run the unadapted upstream workflow.\n\n'
          'For an ordinary local skill with no recipe or steps, carry out the adapted '
          'instructions with available runtime tools directly; llmcall is required for '
          'nested model/agent work, not deterministic local tool operations.\n\n'
        + f'Read the adapted instructions at [{name}]({template_path.as_posix()}). '
          'Its relative resources retain their hierarchy under `payload/`. '
          'Resource examples do not override the llmcall execution instructions above.\n')
    digest = hashlib.sha256(body.encode()).hexdigest()
    files = {target: (body + '\n<!-- claude-profile-sync:adapter sha256=' + digest + ' -->\n').encode(),
             descriptor_path: (json.dumps(descriptor, sort_keys=True, indent=2) + '\n').encode(),
             template_path: (descriptor['execution_instructions'] + '\n\n' + descriptor['template']).encode()}
    for resource in descriptor['resources']:
        for member in resource['files']:
            path = target.parent / 'payload' / member['relative_path']
            data = Path(member['resolved_path']).read_bytes()
            if overlays.digest(data) != member['hash']:
                raise ValueError('overlay_resource_drift')
            if member['relative_path'] in descriptor.get('resource_templates', {}):
                data = descriptor['resource_templates'][member['relative_path']].encode()
            if path in {template_path, source_payload}:
                continue
            if path in files and files[path] != data:
                raise ValueError('overlay_resource_collision')
            files[path] = data
    return files


def report(state):
    return {k: deepcopy(v) for k, v in state.items() if k not in {'entries', 'roles', 'source_checks', 'replacement_groups',
        'selection_policy', 'selection_capabilities', 'selection_snapshot'}} | {
        'entries': [{'source_id': item['record']['source_id'], 'entrypoint': selector(item['entrypoint']),
                     'status': item['overlay']['status'], 'reasons': item['overlay'].get('reasons', []),
                     'artifact_hash': item['overlay'].get('artifact_hash'),
                     'equivalence': bool(item.get('equivalent')), 'policy_ids': item['policy_ids']}
                    for item in state['entries'].values()]}
