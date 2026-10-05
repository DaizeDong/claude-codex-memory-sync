"""Resolve the client-loaded task entrypoint through the pinned runtime policy."""
import hashlib
import json
from pathlib import Path

from skill_smith import conflicts
from .workflows import load_descriptor


def select_entrypoint(path, request):
    """Return one exact runnable alternative; explicit overrides keep precedence."""
    from profile_sync import assert_plain_path
    path = Path(path)
    assert_plain_path(path)
    envelope = json.loads(path.read_text(encoding='utf-8'))
    expected = envelope.pop('selection_hash', None)
    if expected != conflicts.fingerprint(envelope) or envelope.get('schema_version') != 1:
        raise ValueError('selection_snapshot_invalid')
    decision = conflicts.select(request, envelope['capabilities'], envelope['policy'], envelope['snapshot'])
    if decision['status'] != 'selected':
        return decision
    ep = decision['selection']['entrypoint']
    identity = [ep.get(k) for k in ('source_id', 'kind', 'relative_path', 'client', 'scope')]
    matches = [item for item in envelope['alternatives'] if item['identity'] == identity]
    if len(matches) != 1:
        return dict(decision, status='blocked', reason='selected_adapter_unavailable', selection=None)
    alternative = matches[0]
    descriptor_path = path.parent / alternative['descriptor']
    entrypoint_path = path.parent / alternative['entrypoint']
    for target in (descriptor_path, entrypoint_path):
        if '..' in target.parts or not target.is_relative_to(path.parent):
            raise ValueError('selection_path_outside_bundle')
        assert_plain_path(target)
    members = alternative.get('members')
    if (not isinstance(members, dict) or not members
            or not {alternative['descriptor'], alternative['entrypoint']}.issubset(members)):
        return dict(decision, status='blocked', reason='selected_adapter_unverified', selection=None)
    for relative, expected_hash in members.items():
        member = Path(relative)
        if (member.is_absolute() or member.drive or '..' in member.parts
                or not (path.parent / member).is_relative_to(entrypoint_path.parent)):
            raise ValueError('selection_path_outside_bundle')
        target = path.parent / member
        assert_plain_path(target)
        try:
            content = target.read_bytes()
        except OSError:
            return dict(decision, status='blocked', reason='selected_adapter_missing', selection=None)
        if hashlib.sha256(content).hexdigest() != expected_hash:
            return dict(decision, status='blocked', reason='selected_adapter_changed', selection=None)
    descriptor = load_descriptor(descriptor_path)
    if descriptor['artifact_hash'] != alternative['artifact_hash']:
        raise ValueError('selected_adapter_changed')
    return dict(decision, descriptor=descriptor, descriptor_path=str(descriptor_path),
                entrypoint_path=str(entrypoint_path))
