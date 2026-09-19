"""Generate synthetic fixtures through real producer APIs, never copied user data."""
from pathlib import Path
from unittest.mock import patch

import profile_memory as memory
from profile_bridge import memory_outbox as outbox


def make_ccms_note(project_path, relative, text, previous=None, sequence=1):
    """Generate the historical PS envelope from synthetic inputs only."""
    from profile_bridge.memory.core import canonical_text, sha
    from profile_bridge.memory.legacy import project_id, legacy_source_id
    from profile_bridge.memory.ingress import quote
    text = canonical_text(text, strict=True)
    pid = project_id(project_path)
    sid = legacy_source_id(pid, relative)
    digest = sha(text.encode())
    old_id = previous['import_id'] if previous else 'none'
    old_hash = previous['content_sha256'] if previous else 'none'
    stamp = f'2026-01-01T00:00:{sequence:02d}.0000000Z'
    inc = sha(('ccms.note.v1\0' + '\0'.join([pid, sid, digest, old_id, stamp])).encode())
    operation = 'update' if previous else 'add'
    metadata = dict(schema='ccms.note/v1', import_id=inc, operation=operation, project_id=pid, source_id=sid,
                    content_sha256=digest, previous_content_sha256=old_hash, previous_import_id=old_id, synced_at_utc=stamp)
    body = '<!-- ccms-metadata-v1\n' + ''.join(k + '=' + v + '\n' for k, v in metadata.items()) + '-->\n'
    body += f'# Claude Code memory sync\n> applies_to: cwd={project_path}\n> source_relative_path: {relative}\n'
    body += '<!-- ccms-previous-begin -->\n' + quote(previous['current'] if previous else '(none)') + '\n<!-- ccms-previous-end -->\n'
    body += '<!-- ccms-current-begin -->\n' + quote(text) + '\n<!-- ccms-current-end -->\n'
    name = f'20260101T0000{sequence:02d}000Z-ccms-v1-{pid[:12]}-{sid[:12]}-{operation}-{digest[:24]}-{inc[:12]}.md'
    return name, body.encode(), dict(metadata, current=text)


def make_t03_history(home):
    """Build capture-compatible .claude/.codex state in a fresh synthetic home.

    Returns paths and request IDs for controller capture/restore/restart tests.
    All ledger records and grants are written by T03. The injected interruption
    models the unrecorded-publication window; the normal producer performs recovery.
    """
    home = Path(home).absolute()
    if home.exists() and any(home.iterdir()):
        raise ValueError('fixture_home_must_be_empty')
    claude, codex, skills = home / '.claude', home / '.codex', home / '.agents/skills'
    requests = {state: 'fixture-' + state for state in ('prepared', 'published', 'unknown', 'revoked')}
    for state in requests:
        source = claude / 'projects' / ('project-' + state) / 'memory/MEMORY.md'
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(('Synthetic fixture for ' + state + '\n').encode())
    contract = codex / 'memories/extensions/ad_hoc/instructions.md'

    def apply(state):
        plan, _ = memory.plan_memory(claude, codex, request_id=requests[state],
                                     scope=['project-' + state], periodic=state == 'revoked')
        return memory.apply_memory_plan(plan, claude, codex, skills=skills)

    apply('prepared')  # Missing contract queues this explicit scope.
    contract.parent.mkdir(parents=True, exist_ok=True)
    contract.write_bytes(b'# Synthetic ingress contract\n')
    apply('published')

    class Interrupted(BaseException):
        pass

    def interrupt(point):
        if point == 'note_published_unrecorded':
            raise Interrupted()

    try:
        with patch.object(outbox, 'checkpoint', interrupt):
            apply('unknown')
    except Interrupted:
        pass
    apply('unknown')
    contract.unlink()
    apply('revoked')
    outbox.revoke(codex, requests['revoked'], skills=skills)
    apply('revoked')
    contract.write_bytes(b'# Synthetic ingress contract\n')
    return dict(home=home, claude=claude, codex=codex, skills=skills, requests=requests)


def t03_state_bytes(codex):
    """Collect the complete dependency group with Codex-relative POSIX keys."""
    codex = Path(codex)
    paths = [outbox.authorization_path(codex)]
    paths.extend(p for p in outbox.state_root(codex).rglob('*') if p.is_file())
    return {p.relative_to(codex).as_posix(): p.read_bytes() for p in paths}


def make_archive_hygiene(home):
    """Generate a small archive and changes without using any operator data."""
    home = Path(home).absolute()
    claude, codex = home / 'claude', home / 'codex'
    source = claude / 'projects/synthetic-project/memory/fact.md'
    source.parent.mkdir(parents=True)
    payloads = {
        'original': b'# Synthetic archive fact\n',
        'updated': b'# Updated synthetic archive fact\n',
        'edited': b'# Synthetic local edit to preserve\n',
        'risk': ('# Synthetic scanner probe\n' + 'ghp_' + 'Z' * 32 + '\n').encode(),
    }
    source.write_bytes(payloads['original'])
    plan, _ = memory.plan_memory(claude, codex)
    memory.apply_memory_plan(plan, claude, codex)
    return dict(claude=claude, codex=codex, source=source, payloads=payloads,
                destination=codex / 'imports/claude-memory/synthetic-project/fact.md')


def make_retirement_backup(codex, row):
    """Produce the existing profile backup envelope for synthetic hook tests."""
    backup = Path(codex) / 'claude-sync/backups/synthetic-retirement'
    backup.mkdir(parents=True)
    stored = {k: v for k, v in row.items() if k != 'data'}
    stored['backup_file'] = '00000.bin'
    (backup / stored['backup_file']).write_bytes(Path(row['path']).read_bytes())
    (backup / 'manifest.json').write_bytes(outbox.encoded({
        'codex_home': str(codex), 'skills_home': str(Path(codex).parent / '.agents/skills'),
        'changes': [stored],
    }))
    return backup
