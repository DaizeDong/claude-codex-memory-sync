"""Pure cross-home migration. CONFIG owns capture, locking and publication."""
import json
from pathlib import Path

from .. import memory_outbox as outbox


def migrate_restore_state(original_codex, target_codex, files, *, scope_evidence, destination_files=None):
    """Validate and return a complete dependency group without filesystem writes.

    Scope evidence must exactly match every original grant and history stream.
    The target must be empty or byte-identical to this migration. Original file
    identities remain evidence in the immutable capture, never target ownership.
    """
    outbox.validate_restore_state(original_codex, files)
    original_codex, target_codex = Path(original_codex), Path(target_codex)
    if not target_codex.is_absolute() or original_codex == target_codex:
        raise outbox.HistoryConflict('cross_home_migration_requires_distinct_absolute_homes')
    prefix = 'claude-sync/memory-outbox/'
    auth_key = 'claude-sync/memory-authorizations.json'
    auth = json.loads(files[auth_key])
    history = json.loads(files[prefix + 'history.json']) if prefix + 'history.json' in files else None
    required = {(g['source_root'], tuple(g['scope'])) for g in auth['grants']}
    if history:
        required |= {(r['source_root'], tuple(r['scope'])) for r in history['records']}
    mappings, supplied = {}, set()
    for row in scope_evidence:
        if set(row) != {'source_root', 'target_root', 'scope'} or row['scope'] != outbox._scope(row['scope']):
            raise outbox.HistoryConflict('invalid_migration_scope_evidence')
        old, new = outbox._canonical_root(row['source_root']), outbox._canonical_root(row['target_root'])
        key = old, tuple(row['scope'])
        if key in supplied or (old in mappings and mappings[old] != new):
            raise outbox.HistoryConflict('ambiguous_migration_scope_evidence')
        supplied.add(key)
        mappings[old] = new
    if required != supplied or len(set(mappings.values())) != len(mappings):
        raise outbox.HistoryConflict('migration_scope_not_exact')
    evidence = {'version': 1, 'original_codex': str(original_codex), 'target_codex': str(target_codex),
                'files': {k: outbox.sha(v) for k, v in sorted(files.items())},
                'scope_evidence': sorted(scope_evidence, key=lambda r: (r['source_root'], r['scope']))}
    migration_id = outbox.sha(outbox.encoded(evidence))
    result = {}
    for grant in auth['grants']:
        grant['source_root'] = mappings[grant['source_root']]
    result[auth_key] = outbox.encoded(auth)
    if history:
        aliases, ids, heads = [], {}, {}
        if 'archive_ownership' in history:
            archive = history['archive_ownership']
            if set(archive['roots']) - set(mappings):
                raise outbox.HistoryConflict('unscoped_archive_ownership')
            archive['roots'] = {mappings[root]: files for root, files in archive['roots'].items()}
        if 'canonical' in history:
            canonical = history['canonical']
            if set(canonical['roots']) - set(mappings):
                raise outbox.HistoryConflict('unscoped_canonical_root')
            for source in canonical['sources'].values():
                roots = [r for r, ns in canonical['roots'].items() if ns == source['namespace']]
                if len(roots) != 1 or not any(r == roots[0] and ('*' in scope or source['project'] in scope)
                                             for r, scope in supplied):
                    raise outbox.HistoryConflict('unscoped_canonical_source')
            canonical['roots'] = {mappings[root]: namespace for root, namespace in canonical['roots'].items()}
        for row in history['records']:
            old_id, old_state = row['increment_id'], row['delivery_state']
            row['source_root'] = mappings[row['source_root']]
            if 'memories_root' in row:
                row['memories_root'] = str(target_codex / Path(row['memories_root']).name)
            stream = row['source_root'], tuple(row['scope']), row.get('source_id')
            row['predecessor'] = heads.get(stream)
            if row.get('format') != 'canonical-file-v1':
                row['increment_id'] = outbox.increment_id(row['source_root'], row['scope'], row['source_hash'], row['predecessor'])
            ids[old_id] = row['increment_id']
            heads[stream] = row['increment_id']
            row.pop('note_identity', None)
            row.pop('receipt', None)
            if old_state not in {'archive_only', 'aliased', 'cancelled', 'superseded'}:
                row['delivery_state'] = 'delivery_unknown'
            row['prepared_note_path'] = f"prepared/{row['increment_id']}.md"
            row['content_hash'] = outbox.sha(outbox.render_note(row, target_codex))
            if row['delivery_state'] not in {'archive_only', 'aliased'}:
                result[prefix + row['prepared_note_path']] = outbox.render_note(row, target_codex)
            aliases.append({'original_increment': old_id, 'target_increment': row['increment_id'],
                            'original_delivery_state': old_state, 'delivery': 'unproven_on_target'})
        history['requests'] = {request: ids[inc] for request, inc in history['requests'].items()}
        history['migration'] = dict(evidence, migration_id=migration_id, aliases=aliases)
        result[prefix + 'history.json'] = outbox.encoded(history)
    outbox.validate_restore_state(target_codex, result)
    # Rebinding can introduce credential shapes through paths and evidence.
    # Validate every complete transformed member before accepting the group,
    # including an otherwise byte-identical destination.
    for raw in result.values():
        try:
            findings = outbox.scan_state_credentials(raw)
        except ValueError as exc:
            raise outbox.HistoryConflict('migration_output_credential_scan_failed') from exc
        if findings:
            raise outbox.HistoryConflict('migration_output_credential_check_failed')
    if destination_files and dict(destination_files) != result:
        raise outbox.HistoryConflict('destination_memory_state_requires_review')
    changed = dict(destination_files or {}) != result
    return {'files': result if changed else {}, 'report': {'migration_id': migration_id,
            'changed': changed, 'native_notes_written': 0, 'records': len(history['records']) if history else 0,
            'delivery': 'aliases_only', 'authorization': 'original_scope_and_periodic_preserved'}}
