"""Per-source ledger embedded in the durable T03 outbox transaction."""
from copy import deepcopy
import re

from .core import NORMALIZATION, canonical_text, content_digest, encoded, increment_id, sha, source_id


def empty():
    return {'version': 1, 'normalization': NORMALIZATION, 'roots': {},
            'sources': {}, 'records': [], 'aliases': []}


def validate(value):
    if value['version'] != 1 or value['normalization'] != NORMALIZATION:
        raise ValueError('unsupported_canonical_history')
    if not isinstance(value['roots'], dict) or not isinstance(value['aliases'], list):
        raise ValueError('invalid_canonical_registry')
    for key, source in value['sources'].items():
        if source_id(source['namespace'], source['project'], source['path']) != key:
            raise ValueError('invalid_canonical_source')
        if source['namespace'] not in value['roots'].values():
            raise ValueError('unbound_canonical_namespace')
    heads, ids, previous_rows = {}, set(), {}
    for row in value['records']:
        sid = row['source_id']
        if sid not in value['sources'] or row['predecessor'] != heads.get(sid):
            raise ValueError('invalid_canonical_predecessor')
        digest = content_digest(row['current']) if row['operation'] != 'delete' else sha(b'')
        if row['content_digest'] != digest or (row['operation'] == 'delete' and row['current'] is not None):
            raise ValueError('invalid_canonical_content')
        expected = increment_id(sid, heads.get(sid), row['operation'], digest)
        if row['increment_id'] != expected or expected in ids:
            raise ValueError('invalid_canonical_increment')
        prior = previous_rows.get(sid)
        operation = ('add' if prior is None else 'reappear' if prior['operation'] == 'delete' else 'update')
        if row['operation'] != 'delete' and row['operation'] != operation:
            raise ValueError('invalid_canonical_operation')
        if row['operation'] == 'delete' and (prior is None or prior['operation'] == 'delete'):
            raise ValueError('invalid_canonical_delete')
        ids.add(expected)
        heads[sid] = expected
        previous_rows[sid] = row
    for alias in value['aliases']:
        if alias['canonical_increment'] not in ids or alias['kind'] not in {'ccms', 'archive'}:
            raise ValueError('invalid_canonical_alias')
        if alias['delivery'] != 'unproven' or any(not re.fullmatch('[0-9a-f]{64}', alias[k]) for k in ('legacy_id', 'evidence_sha256')):
            raise ValueError('invalid_canonical_alias_evidence')
    return value


def observe(value, root, sources, *, complete_projects=()):
    """Observe verified source text; incomplete projects never imply deletions."""
    value = deepcopy(value) if value is not None else empty()
    validate(value)
    namespace = value['roots'].setdefault(root, sha(encoded({'source_root': root})))
    heads = {r['source_id']: r for r in value['records']}
    observed, changes = set(), []
    for source in sources:
        sid = source_id(namespace, source['project'], source['path'])
        if sid in observed:
            raise ValueError('memory_project_or_path_collision')
        observed.add(sid)
        registered = {'namespace': namespace, 'project': source['project'], 'path': source['path']}
        if sid in value['sources'] and value['sources'][sid] != registered:
            raise ValueError('memory_path_spelling_requires_alias')
        value['sources'][sid] = registered
        prior = heads.get(sid)
        digest = content_digest(source['text'])
        if prior and prior['operation'] != 'delete' and prior['content_digest'] == digest:
            continue
        operation = 'add' if prior is None else 'reappear' if prior['operation'] == 'delete' else 'update'
        changes.append(_event(sid, prior, operation, digest, canonical_text(source['text'])))
    for sid, prior in heads.items():
        source = value['sources'][sid]
        if (source['namespace'] == namespace and source['project'] in complete_projects
                and sid not in observed and prior['operation'] != 'delete'):
            changes.append(_event(sid, prior, 'delete', sha(b''), None))
    value['records'].extend(changes)
    validate(value)
    return value, changes


def _event(sid, prior, operation, digest, text):
    predecessor = prior['increment_id'] if prior else None
    return dict(source_id=sid, predecessor=predecessor, operation=operation,
                content_digest=digest, current=text,
                increment_id=increment_id(sid, predecessor, operation, digest))


def snapshot_digest(value, root, scope):
    namespace = value['roots'][root]
    heads = {r['source_id']: r for r in value['records']}
    selected = {sid: r['increment_id'] for sid, r in heads.items()
                if value['sources'][sid]['namespace'] == namespace
                and (scope == ['*'] or value['sources'][sid]['project'] in scope)}
    return sha(encoded(selected))
