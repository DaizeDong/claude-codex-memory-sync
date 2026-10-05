"""Read-only reference to CONFIG's authoritative restore journal.

Presence always blocks ordinary writers, including a corrupt or orphaned marker.
Only CONFIG recovery may validate the reference while holding all owner locks.
"""
import json
import hashlib
import os
from pathlib import Path

from fleet_guards.filesystem import read_bounded, validate_path


class RestoreRecoveryRequired(ValueError):
    reason = 'recovery_required'


def marker_path(codex):
    return Path(codex) / 'claude-sync/restore-incomplete.json'


def recovery_status(codex):
    try:
        path = validate_path(marker_path(codex))
        present = os.path.lexists(path)
    except (OSError, ValueError):
        present = True
    return {'status': 'recovery_required', 'reason': 'incomplete_restore'} if present else None


def require_ready(codex):
    if recovery_status(codex):
        raise RestoreRecoveryRequired('recovery_required')


def validate_owner(codex, backup, journal_path, plan_sha256):
    """Validate the exact journal reference; never repair or import CONFIG."""
    backup = Path(backup).absolute()
    expected_journal = backup.parent / ('.codex-profile-transaction-' + backup.name) / 'journal.json'
    if Path(journal_path).absolute() != expected_journal:
        raise RestoreRecoveryRequired('restore_owner_mismatch')
    expected = {'version': 1, 'owner': 'profile-backup-restore',
                'home': str(Path(codex).absolute().parent), 'backup': str(backup),
                'journal': str(expected_journal), 'plan_sha256': plan_sha256}
    path = validate_path(marker_path(codex))
    if os.path.lexists(path):
        if json.loads(read_bounded(path, 16384)) != expected:
            raise RestoreRecoveryRequired('restore_owner_mismatch')
    journal = json.loads(read_bounded(expected_journal, 16 * 1024 * 1024))
    plan_hash = hashlib.sha256((json.dumps(journal.get('plan'), ensure_ascii=False,
                                         sort_keys=True, indent=2) + '\n').encode()).hexdigest()
    if (journal.get('owner') != expected['owner'] or journal.get('version') != 4 or
            journal.get('home') != expected['home'] or journal.get('backup') != str(backup) or
            journal.get('plan_sha256') != plan_sha256 or plan_hash != plan_sha256):
        raise RestoreRecoveryRequired('restore_owner_mismatch')
    return expected
