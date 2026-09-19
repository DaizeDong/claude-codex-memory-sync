"""Strict legacy evidence readers. Aliases never prove native publication."""
from datetime import datetime, timezone
import re

from .core import canonical_text, content_digest, encoded, sha
from .history import observe, validate


def project_id(path):
    return sha(('ccms.project.v1\0' + invariant_upper(str(path).replace('\\', '/').rstrip('/'))).encode())


def invariant_upper(value):
    # .NET invariant casing does not expand a character into several letters.
    return ''.join(c.upper() if len(c.upper()) == 1 else c for c in value)


def legacy_source_id(project, path):
    return sha(('ccms.source.v1\0' + project + '\0' + invariant_upper(path.replace('\\', '/'))).encode())


def _policy_rules(text):
    from fleet_guards import secrets
    result = secrets.scan(text, policy='credential-shapes-v1')
    if result['state'] not in {'clean', 'findings'}:
        raise ValueError('legacy_credential_scan_failed')
    return {finding['rule_id'] for finding in result['findings']}


def archive_identity(row):
    """Opaque exact spelling identity; reports never need a blocked filename."""
    return sha(encoded({'project': row['project'], 'path': row['path']}))


def archive_policy_exclusions(index, records, scopes, raw_files, *, source_root):
    """Verify ALL original bytes first, then return scanner decisions, not text."""
    from pathlib import Path
    from profile_memory import _decode, _normalize_controls
    verify_archive(index, records, scopes, raw_files)
    exclusions = []
    for row in records:
        raw = raw_files[row['project'], row['path']]
        text = _decode(raw)
        path = Path(source_root) / row['project'] / 'memory' / row['path']
        rules = set()
        for value in (str(path), text, _normalize_controls(text)[0], canonical_text(raw)):
            rules.update(_policy_rules(value))
        if rules:
            exclusions.append(dict(identity_sha256=archive_identity(row), raw_sha256=sha(raw),
                                   reason='possible_credential', rules=sorted(rules)))
    return exclusions


def migrate_archive_state(codex, source_root, index, records, scopes, raw_files, *, reviewed_exclusions=()):
    """Build an explicit legacy migration group from fully verified source bytes.

    The controller retains the original index/conflict evidence and publishes
    this whole dependency group through its restore transaction. No grant or
    native receipt is inferred from the archived index.
    """
    from .. import memory_outbox as outbox
    from .history import snapshot_digest
    alias = verify_archive(index, records, scopes, raw_files)
    exclusions = archive_policy_exclusions(index, records, scopes, raw_files, source_root=source_root)
    if list(reviewed_exclusions) != exclusions:
        raise ValueError('legacy_policy_exclusions_require_exact_review')
    excluded = {row['identity_sha256'] for row in exclusions}
    root = outbox._root(source_root)
    sources = [dict(project=r['project'], path=r['path'], text=raw_files[r['project'], r['path']])
               for r in records if archive_identity(r) not in excluded]
    canonical, events = observe(None, root, sources)
    for event in events:
        canonical['aliases'].append(dict(alias, canonical_increment=event['increment_id']))
    digest = snapshot_digest(canonical, root, ['*'])
    inc = outbox.increment_id(root, ['*'], digest, None)
    row = dict(increment_id=inc, predecessor=None, source_root=root, scope=['*'], source_hash=digest,
               request_id=None, periodic=False, delivery_state='aliased', prepared_note_path=f'prepared/{inc}.md',
               canonical_increments=[e['increment_id'] for e in events])
    row['content_hash'] = outbox.sha(outbox.render_note(row, codex))
    history = dict(version=1, algorithm=outbox.ALGORITHM, normalization=outbox.NORMALIZATION,
                   canonical=canonical, records=[row], requests={})
    history['legacy_archive'] = dict(alias, verified_records=len(records), imported_records=len(sources),
                                    scope_sha256=sha(encoded(scopes)), policy='credential-shapes-v1',
                                    exclusions=exclusions)
    files = {'claude-sync/memory-authorizations.json': outbox.encoded(dict(version=1, grants=[], history_initialized=True)),
             'claude-sync/memory-outbox/history.json': outbox.encoded(history)}
    outbox.validate_restore_state(codex, files)
    # validate_restore_state is structural; it does not run a credential scan.
    for raw in files.values():
        if outbox.scan_state_credentials(raw):
            raise ValueError('legacy_output_credential_check_failed')
    return files


def parse_note(name, raw):
    text = canonical_text(raw, strict=True)
    from fleet_guards import secrets
    scan = secrets.scan(text, policy='credential-shapes-v1')
    if scan['state'] == 'scan_failed' or scan['findings']:
        raise ValueError('legacy_evidence_credential_check_failed')
    if not text.startswith('<!-- ccms-metadata-v1\n'):
        raise ValueError('invalid_legacy_header')
    header, body = text.split('-->\n', 1)
    if len(header) > 4096:
        raise ValueError('invalid_legacy_header')
    fields = [line.split('=', 1) for line in header.splitlines()[1:]]
    metadata = dict(fields)
    keys = {'schema', 'import_id', 'operation', 'project_id', 'source_id', 'content_sha256',
            'previous_content_sha256', 'previous_import_id', 'synced_at_utc'}
    if len(fields) != len(metadata) or set(metadata) != keys or metadata['schema'] != 'ccms.note/v1':
        raise ValueError('invalid_legacy_metadata')
    for key in ('import_id', 'project_id', 'source_id', 'content_sha256'):
        if not re.fullmatch('[0-9a-f]{64}', metadata[key]):
            raise ValueError('invalid_legacy_digest')
    operation = metadata['operation']
    if operation not in {'add', 'update'}:
        raise ValueError('invalid_legacy_operation')
    for key in ('previous_content_sha256', 'previous_import_id'):
        if (metadata[key] != 'none' if operation == 'add' else not re.fullmatch('[0-9a-f]{64}', metadata[key])):
            raise ValueError('invalid_legacy_predecessor')
    stamp = metadata['synced_at_utc']
    if not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{7}(?:Z|[+-]\d\d:\d\d)', stamp):
        raise ValueError('invalid_legacy_timestamp')
    time = datetime.fromisoformat(stamp.replace('Z', '+00:00')).astimezone(timezone.utc)
    expected_id = sha(('ccms.note.v1\0' + '\0'.join(metadata[k] for k in
                      ('project_id', 'source_id', 'content_sha256', 'previous_import_id', 'synced_at_utc'))).encode())
    expected_name = (time.strftime('%Y%m%dT%H%M%S') + f'{time.microsecond // 1000:03d}Z-ccms-v1-'
                     + metadata['project_id'][:12] + '-' + metadata['source_id'][:12] + '-'
                     + operation + '-' + metadata['content_sha256'][:24] + '-' + expected_id[:12] + '.md')
    if expected_id != metadata['import_id'] or name != expected_name:
        raise ValueError('invalid_legacy_filename_binding')
    payloads, positions = {}, []
    for kind in ('previous', 'current'):
        begin, end = f'<!-- ccms-{kind}-begin -->', f'<!-- ccms-{kind}-end -->'
        if text.splitlines().count(begin) != 1 or text.splitlines().count(end) != 1:
            raise ValueError('invalid_legacy_markers')
        left, right = text.index('\n' + begin + '\n'), text.index('\n' + end + '\n')
        positions += [left, right]
        block = text[left + len(begin) + 2:right]
        lines = block.split('\n')
        if any(line != '>' and not line.startswith('> ') for line in lines):
            raise ValueError('invalid_legacy_quote')
        payloads[kind] = '\n'.join('' if line == '>' else line[2:] for line in lines)
    if positions != sorted(set(positions)) or not text.endswith('<!-- ccms-current-end -->\n'):
        raise ValueError('invalid_legacy_marker_order')
    if sha(payloads['current'].encode()) != metadata['content_sha256']:
        raise ValueError('invalid_legacy_content')
    if operation == 'add':
        if payloads['previous'] != '(none)':
            raise ValueError('invalid_legacy_previous')
    elif sha(payloads['previous'].encode()) != metadata['previous_content_sha256']:
        raise ValueError('invalid_legacy_previous')
    roots = re.findall(r'^> applies_to: cwd=(.+)$', body, re.M)
    paths = re.findall(r'^> source_relative_path: (.+)$', body, re.M)
    if len(roots) != 1 or len(paths) != 1:
        raise ValueError('missing_legacy_provenance')
    if project_id(roots[0]) != metadata['project_id'] or legacy_source_id(metadata['project_id'], paths[0]) != metadata['source_id']:
        raise ValueError('invalid_legacy_source_binding')
    return dict(metadata, current=payloads['current'], previous=payloads['previous'],
                project_path=roots[0], path=paths[0].replace('\\', '/'), evidence_sha256=sha(raw))


def import_notes(canonical, root, project, project_path, notes):
    """Import a complete CCMS chain using an explicitly registered project scope."""
    parsed = [parse_note(name, raw) for name, raw in notes.items()]
    expected_project = project_id(project_path)
    if any(row['project_id'] != expected_project for row in parsed):
        raise ValueError('legacy_project_scope_mismatch')
    by_id = {row['import_id']: row for row in parsed}
    if len(by_id) != len(parsed):
        raise ValueError('duplicate_legacy_import')
    pending = dict(by_id)
    imported, children = {}, set()
    for row in parsed:
        parent_id = row['previous_import_id']
        if parent_id != 'none':
            parent = by_id.get(parent_id)
            if (parent is None or parent_id in children or parent['source_id'] != row['source_id']
                    or parent['content_sha256'] != row['previous_content_sha256']
                    or parent['current'] != row['previous']):
                raise ValueError('disconnected_or_forked_legacy_history')
            children.add(parent_id)
    existing = {a['legacy_id']: a for a in (canonical or {}).get('aliases', []) if a['kind'] == 'ccms'}
    roots = set()
    while pending:
        ready = [row for row in pending.values() if row['previous_import_id'] == 'none' or row['previous_import_id'] in imported]
        if not ready:
            raise ValueError('legacy_history_cycle')
        for row in sorted(ready, key=lambda item: item['import_id']):
            if row['previous_import_id'] == 'none':
                if row['source_id'] in roots:
                    raise ValueError('multiple_legacy_roots')
                roots.add(row['source_id'])
            old_alias = existing.get(row['import_id'])
            if old_alias:
                if old_alias['evidence_sha256'] != row['evidence_sha256']:
                    raise ValueError('changed_legacy_evidence')
                imported[row['import_id']] = old_alias['canonical_increment']
            else:
                canonical, changes = observe(canonical, root, [dict(project=project, path=row['path'], text=row['current'])])
                if len(changes) != 1:
                    raise ValueError('ambiguous_legacy_canonical_overlap')
                event = changes[0]
                parent = imported.get(row['previous_import_id'])
                if event['predecessor'] != parent:
                    raise ValueError('legacy_history_overlap_requires_review')
                alias = dict(kind='ccms', legacy_id=row['import_id'], canonical_increment=event['increment_id'],
                             evidence_sha256=row['evidence_sha256'], delivery='unproven')
                canonical['aliases'].append(alias)
                imported[row['import_id']] = event['increment_id']
            del pending[row['import_id']]
    return validate(canonical)


def verify_archive(index, records, scopes, raw_files):
    """Verify the old Python archive hash against original source bytes."""
    from profile_memory import _decode, _normalize_controls
    from .core import relative_path
    if not isinstance(records, list) or not records or not isinstance(scopes, dict):
        raise ValueError('invalid_legacy_archive_metadata')
    identities = set()
    for row in records:
        if (set(row) != {'project', 'path', 'sha256', 'raw_sha256', 'bytes'} or
                type(row['bytes']) is not int or row['bytes'] < 0 or
                not all(re.fullmatch('[0-9a-f]{64}', row[k]) for k in ('sha256', 'raw_sha256'))):
            raise ValueError('invalid_legacy_archive_metadata')
        project = relative_path(row['project'])
        key = (project, relative_path(row['path']))
        if '/' in project or key in identities:
            raise ValueError('duplicate_legacy_archive_source')
        identities.add(key)
    if set(scopes) != {row['project'] for row in records} or any(
            value is not None and not isinstance(value, str) for value in scopes.values()):
        raise ValueError('invalid_legacy_archive_scope')
    if set(raw_files) != {(r['project'], r['path']) for r in records}:
        raise ValueError('incomplete_legacy_archive')
    for row in records:
        raw = raw_files[row['project'], row['path']]
        content = _normalize_controls(_decode(raw))[0].encode()
        if row['raw_sha256'] != sha(raw) or row['sha256'] != sha(content) or row['bytes'] != len(content):
            raise ValueError('invalid_legacy_archive_content')
    import json
    digest = sha(json.dumps({'schema': 1, 'sources': records, 'scope': scopes}, sort_keys=True,
                           ensure_ascii=True, separators=(',', ':')).encode())
    if index.decode('utf-8').count(f'<!-- claude-memory-source-sha256: {digest} -->') != 1:
        raise ValueError('invalid_legacy_archive_envelope')
    return {'kind': 'archive', 'legacy_id': digest, 'evidence_sha256': sha(index), 'delivery': 'unproven'}
