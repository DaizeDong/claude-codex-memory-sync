"""Per-source head ledger embedded in the durable outbox transaction.

Only the latest event of each memory file is kept: its increment ID, the
predecessor it chains to, the operation and the content digest. The file text
itself lives in the searchable archive, never in this ledger.
"""
from copy import deepcopy

from .core import NORMALIZATION, content_digest, encoded, increment_id, sha, source_id

VERSION = 2
OPERATIONS = {'add', 'update', 'delete', 'reappear'}


def empty():
    return {'version': VERSION, 'normalization': NORMALIZATION, 'roots': {}, 'sources': {}}


def validate(value):
    if set(value) != {'version', 'normalization', 'roots', 'sources'}:
        raise ValueError('unsupported_canonical_history')
    if value['version'] != VERSION or value['normalization'] != NORMALIZATION:
        raise ValueError('unsupported_canonical_history')
    if not isinstance(value['roots'], dict) or not isinstance(value['sources'], dict):
        raise ValueError('invalid_canonical_registry')
    namespaces = set(value['roots'].values())
    for key, head in value['sources'].items():
        if source_id(head['namespace'], head['project'], head['path']) != key:
            raise ValueError('invalid_canonical_source')
        if head['namespace'] not in namespaces:
            raise ValueError('unbound_canonical_namespace')
        operation = head['operation']
        if operation not in OPERATIONS or (head['predecessor'] is None) != (operation == 'add'):
            raise ValueError('invalid_canonical_predecessor')
        if operation == 'delete' and head['content_digest'] != sha(b''):
            raise ValueError('invalid_canonical_content')
        if head['increment_id'] != increment_id(key, head['predecessor'], operation, head['content_digest']):
            raise ValueError('invalid_canonical_increment')
    return value


def observe(value, root, sources, *, complete_projects=()):
    """Observe verified source text; incomplete projects never imply deletions."""
    value = deepcopy(value) if value is not None else empty()
    validate(value)
    namespace = value['roots'].setdefault(root, sha(encoded({'source_root': root})))
    heads = value['sources']
    observed, changes = set(), []
    for source in sources:
        sid = source_id(namespace, source['project'], source['path'])
        if sid in observed:
            raise ValueError('memory_project_or_path_collision')
        observed.add(sid)
        prior = heads.get(sid)
        if prior and (prior['project'], prior['path']) != (source['project'], source['path']):
            raise ValueError('memory_path_spelling_requires_alias')
        digest = content_digest(source['text'])
        if prior and prior['operation'] != 'delete' and prior['content_digest'] == digest:
            continue
        operation = 'add' if prior is None else 'reappear' if prior['operation'] == 'delete' else 'update'
        changes.append(_head(sid, namespace, source['project'], source['path'], prior, operation, digest))
    for sid, prior in heads.items():
        if (prior['namespace'] == namespace and prior['project'] in complete_projects
                and sid not in observed and prior['operation'] != 'delete'):
            changes.append(_head(sid, namespace, prior['project'], prior['path'], prior, 'delete', sha(b'')))
    for sid, head in changes:
        heads[sid] = head
    validate(value)
    return value, [head for _, head in changes]


def _head(sid, namespace, project, path, prior, operation, digest):
    predecessor = prior['increment_id'] if prior else None
    return sid, dict(namespace=namespace, project=project, path=path, predecessor=predecessor,
                     operation=operation, content_digest=digest,
                     increment_id=increment_id(sid, predecessor, operation, digest))


def selected(value, root, scope):
    """Head increment IDs of one root and scope, keyed by source ID."""
    namespace = value['roots'][root]
    return {sid: head['increment_id'] for sid, head in value['sources'].items()
            if head['namespace'] == namespace and (scope == ['*'] or head['project'] in scope)}


def snapshot_digest(value, root, scope):
    return sha(encoded(selected(value, root, scope)))
