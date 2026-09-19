"""Archive ownership and retirement hooks for the existing profile transaction.

Ownership is hash-only metadata in the memory outbox, prepared by its existing
writer. Retired bytes live in the profile backup, never in the searchable import.
This module does not own a journal, lock, backup store, or history writer.
"""
from copy import deepcopy
from pathlib import Path, PurePosixPath
import re

from fleet_guards import filesystem as fs, secrets
from .. import memory_outbox as outbox, ownership

FIELD = 'archive_ownership'
RETIREMENT = 'memory_retirement'
LIMIT = 16 * 1024 * 1024
HASH = re.compile(r'[0-9a-f]{64}\Z')


def _relative(value):
    if (not isinstance(value, str) or '\\' in value or ':' in value or
            not value or any(p in {'', '.', '..'} for p in value.split('/')) or
            PurePosixPath(value).is_absolute() or any(ord(c) < 32 for c in value)):
        raise ValueError('invalid_archive_relative_path')
    return value


def validate_ownership(value):
    """Validate captured metadata without reading paths or memory bodies."""
    if (not isinstance(value, dict) or set(value) != {'version', 'roots'} or
            type(value['version']) is not int or value['version'] != 1 or not isinstance(value['roots'], dict)):
        raise ValueError('invalid_archive_ownership')
    for root, files in value['roots'].items():
        outbox._canonical_root(root)
        if not isinstance(files, dict):
            raise ValueError('invalid_archive_ownership')
        seen = set()
        for path, hashes in files.items():
            key = _relative(path).casefold()
            if (key in seen or not isinstance(hashes, list) or not hashes or
                    any(not isinstance(h, str) or not HASH.fullmatch(h) for h in hashes) or
                    hashes != sorted(set(hashes))):
                raise ValueError('invalid_archive_ownership')
            seen.add(key)
    return value


def _reviews(rows, source_root):
    result = {}
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'path', 'sha256', 'action', 'source_root', 'review_id'} or
                row['action'] not in {'adopt', 'quarantine'} or
                not isinstance(row['sha256'], str) or not HASH.fullmatch(row['sha256']) or
                not isinstance(row['review_id'], str) or
                not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', row['review_id']) or
                outbox._root(row['source_root']) != source_root):
            raise ValueError('invalid_archive_review')
        key = _relative(row['path']).casefold()
        if key in result:
            raise ValueError('ambiguous_archive_review')
        if secrets.scan(outbox.encoded(row).decode(), policy='credential-shapes-v1')['state'] != 'clean':
            raise ValueError('unsafe_archive_review_metadata')
        result[key] = row
    return result


def _files(root):
    """Enumerate only regular, bounded archive files; never follow a link."""
    fs.validate_path(root)
    if not root.exists():
        return
    for path in sorted(root.iterdir()):
        fs.validate_path(path, root=root)
        if path.is_dir():
            yield from _files(path)
        else:
            yield path


def plan(plans, desired, codex, source_root, *, complete_projects, snapshot_complete,
         excluded, reviewed=()):
    """Add reversible writes and reviewed retirements to a MemoryPlan.

    Equal clean source/destination bytes may acquire ownership without mutation.
    Every generated hash remains recognized across prepare/crash/rollback. Unknown
    bytes are never inferred to be owned from a filename or an index marker.
    """
    root = Path(codex) / 'imports/claude-memory'
    source_root = outbox._root(source_root)
    reviews = _reviews(reviewed, source_root)
    intent = plans.delivery
    prior = intent.history.get(FIELD) if intent else None
    if prior is not None:
        validate_ownership(prior)
    owned = deepcopy(prior or dict(version=1, roots={}))
    owned_files = owned['roots'].setdefault(source_root, {})
    known = {key.casefold(): values for key, values in owned_files.items()}
    generated = {}
    for files in owned['roots'].values():
        for key, hashes in files.items():
            generated.setdefault(key.casefold(), set()).update(hashes)
    result = dict(retirements=[], preserved=[], adoptions=[], blocked=False)
    actual = {}
    for path in _files(root):
        relative = path.relative_to(root).as_posix()
        # Bound even unsupported files. An unreadable item aborts reconciliation;
        # an empty enumeration must never be mistaken for an empty archive.
        raw = fs.read_bounded(path, LIMIT)
        if relative.casefold() in actual:
            raise ValueError('ambiguous_archive_destination')
        actual[relative.casefold()] = (path, raw, ownership.digest(raw), fs.identity(path))
    wanted = {p.relative_to(root).as_posix().casefold() for p in desired}
    if len(wanted) != len(desired):
        raise ValueError('ambiguous_archive_destination')
    # A review is an exact decision, not a wildcard approval of future bytes.
    for key, review in reviews.items():
        if key not in actual or actual[key][2] != review['sha256']:
            raise ValueError('stale_archive_review')
        if (key in wanted) != (review['action'] == 'adopt'):
            raise ValueError('archive_review_action_mismatch')
    for path, payload in desired.items():
        relative = path.relative_to(root).as_posix()
        key, digest = relative.casefold(), ownership.digest(payload)
        current = actual.get(key)
        before = {'kind': 'missing'} if current is None else {'kind': 'file', 'sha256': current[2]}
        plans.archive_before[path] = before
        approved = key in reviews
        if current and current[2] != digest and current[2] not in generated.get(key, set()) and not approved:
            result['preserved'].append(dict(path=str(path), sha256=current[2], reason='destination_modified'))
            result['blocked'] = True
            continue
        hashes = set(known.get(key, [])) | {digest}
        if approved:
            hashes.add(current[2])
            result['adoptions'].append(dict(path=str(path), sha256=current[2], review_id=reviews[key]['review_id']))
        # Preserve registered spelling; case-fold aliases cannot create duplicate authorities.
        stored_key = next((p for p in owned_files if p.casefold() == key), relative)
        owned_files[stored_key] = sorted(hashes)
        if current is None or current[1] != payload:
            plans[path] = payload
    for key, (path, raw, digest, file_identity) in actual.items():
        if key in wanted:
            continue
        relative = path.relative_to(root).as_posix()
        scan = secrets.scan(raw.decode('utf-8', errors='replace'), policy='credential-shapes-v1')
        risk = scan['state'] == 'findings'
        reason = excluded.get(key)
        if reason is None and (snapshot_complete or relative.split('/')[0] in complete_projects):
            reason = 'source_absent'
        review = reviews.get(key)
        why = ('unowned_archive_copy' if key not in known else 'destination_modified'
               if digest not in known[key] else 'source_snapshot_incomplete' if reason is None else None)
        if scan['state'] not in {'clean', 'findings'}:
            why = 'credential_scan_failed'
        if review and scan['state'] in {'clean', 'findings'}:
            why, reason = None, reason or 'reviewed_quarantine'
        if why:
            result['preserved'].append(dict(path=str(path), sha256=digest, reason=why,
                                             credential_findings=risk))
            continue
        metadata = dict(version=1, sha256=digest, identity=file_identity, reason=reason,
                        credential_findings=risk, review_id=review['review_id'] if review else None)
        plans.archive_before[path] = {'kind': 'file', 'sha256': digest}
        plans.retirements[path] = metadata
        plans[path] = None
        result['retirements'].append(dict(path=str(path), **metadata))
    if result['blocked']:
        plans.clear()
        plans.retirements.clear()
        plans.delivery = None
    elif intent and owned != prior:
        intent.history[FIELD] = owned
        intent.changed = True
    return result


def profile_changes(plans, codex):
    """Translate the private plan to the existing profile transaction row API."""
    rows = []
    for path, payload in plans.items():
        if path.is_relative_to(Path(codex) / 'memories'):
            continue  # Only outbox.deliver publishes ingress notes.
        fs.validate_path(path, root=Path(codex) / 'imports/claude-memory')
        row = dict(path=str(path), before=plans.archive_before[path],
                   after={'kind': 'missing'} if payload is None else
                   {'kind': 'file', 'sha256': ownership.digest(payload)}, data=payload, append_only=False)
        if path in plans.retirements:
            row[RETIREMENT] = plans.retirements[path]
        rows.append(row)
    return rows


def apply_retirement(row, codex, backup):
    """Profile apply hook, after its durable backup manifest, before delivery.

    Return False for ordinary rows. Reuse Windows handle-bound CAS to move the
    original object into the same backup. Unsupported platforms fail closed.
    """
    if RETIREMENT not in row:
        return False
    path = fs.validate_path(row['path'], root=Path(codex) / 'imports/claude-memory')
    backup = fs.validate_path(backup, root=Path(codex) / 'claude-sync/backups')
    metadata = row[RETIREMENT]
    if (row['after'] != {'kind': 'missing'} or row['before'] != {'kind': 'file', 'sha256': metadata['sha256']} or
            path.name == 'index.md' and path.parent == Path(codex) / 'imports/claude-memory'):
        raise ValueError('invalid_archive_retirement')
    import json
    manifest = json.loads(fs.read_bounded(backup / 'manifest.json', LIMIT))
    matches = [r for r in manifest['changes'] if r['path'] == str(path) and
               r.get(RETIREMENT) == metadata and r['before'] == row['before']]
    if len(matches) != 1:
        raise ValueError('archive_retirement_backup_missing')
    stored = fs.validate_path(backup / matches[0]['backup_file'], root=backup)
    payload = fs.read_bounded(stored, LIMIT)
    if ownership.digest(payload) != metadata['sha256']:
        raise ValueError('archive_retirement_backup_changed')
    retained = backup / ('quarantine-' + ownership.digest(str(path).encode()) + '.bin')
    if not path.exists():
        if fs.read_bounded(retained, LIMIT) == payload and fs.identity(retained) == metadata['identity']:
            return True  # Exact retry of a completed detach; no new authority.
        raise ValueError('archive_retirement_evidence_missing')
    if not fs.detach_if_matches(path, payload, metadata['identity'], retained):
        raise ValueError('archive_retirement_cas_conflict')
    fs.sync_directory(path.parent)
    fs.sync_directory(backup)
    outbox.checkpoint('archive_retired')
    return True


def preserve_on_rollback(row):
    """Rollback must retain evidence and must not republish retired memories."""
    return RETIREMENT in row
