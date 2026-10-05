"""Reviewed archive scopes supplement exact active mappings without decoding keys."""
from pathlib import Path
import re

from fleet_guards import secrets
from fleet_guards.filesystem import validate_path
from .. import memory_outbox as outbox


def merge_reviewed(active, rows, source_root, skipped, evidence_report):
    result, supplied = dict(active), {}
    root = outbox._root(source_root)
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'project', 'scope_path', 'source_root', 'evidence_sha256'} or
                not isinstance(row['scope_path'], str) or not Path(row['scope_path']).is_absolute() or
                '..' in Path(row['scope_path']).parts or
                not isinstance(row['evidence_sha256'], str) or
                not re.fullmatch('[0-9a-f]{64}', row['evidence_sha256']) or
                outbox._scope([row['project']]) == ['*'] or outbox._root(row['source_root']) != root):
            raise ValueError('invalid_reviewed_scope')
        if secrets.scan(outbox.encoded(row).decode(), policy='credential-shapes-v1')['state'] != 'clean':
            raise ValueError('unsafe_reviewed_scope_metadata')
        validate_path(row['scope_path'])
        supplied.setdefault(row['project'].casefold(), []).append(row['scope_path'])
    for key, paths in supplied.items():
        if any(item['reason'] == 'scope_mapping_unavailable' for item in skipped):
            result[key] = active.get(key)
            status = 'active_mapping_unavailable'
        elif len(paths) != 1 or key in active and active[key] != paths[0]:
            # Never override an exact live mapping, including a live collision.
            if key not in active:
                result[key] = None
            skipped.append({'path': key, 'reason': 'scope_mapping_conflict'})
            status = 'conflict'
        else:
            result[key] = paths[0]
            status = 'matched_active' if key in active else 'reviewed'
        evidence_report.extend(dict(row, status=status) for row in rows if row['project'].casefold() == key)
    return result
