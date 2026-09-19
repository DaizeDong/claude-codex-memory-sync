"""Legacy argument/output adapter using the one durable memory publisher."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from contextlib import contextmanager

from fleet_guards.filesystem import read_bounded, validate_path
from fleet_guards import secrets
from .. import memory_outbox as outbox
from .core import canonical_text, sha
from .history import observe
from .legacy import import_notes, project_id


@contextmanager
def compatibility_lock(notes_root, timeout):
    """Retain the legacy Windows mutex while using T03's resource locks too."""
    if os.name != 'nt':
        yield
        return
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = 'Global\\ClaudeCodexMemorySync-' + sha(str(notes_root).upper().encode())[:16]
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    held = False
    try:
        held = kernel.WaitForSingleObject(handle, timeout * 1000) in (0, 0x80)
        if not held:
            raise ValueError('memory_writer_busy')
        yield
    finally:
        if held:
            kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def plan_files(codex, root, project, sources, *, request_id, legacy_notes=None, project_path=None, memories_root=None):
    """Plan per-file output with the same canonical ledger and T03 writer."""
    history, expected = outbox._read_history(codex)
    before = outbox.encoded(history)
    grant, new_grant = outbox.select_authorization(codex, root, request_id=request_id, scope=[project])
    if grant['state'] != 'active':
        raise ValueError('inactive_memory_authorization')
    canonical = history.get('canonical')
    binding = outbox._root(root) + '\0' + project
    if canonical and project_path:
        prior_path = canonical.get('project_bindings', {}).get(binding)
        if prior_path is not None and prior_path != str(project_path):
            raise ValueError('encoded_project_name_collision')
    if legacy_notes:
        canonical = import_notes(canonical, outbox._root(root), project, project_path, legacy_notes)
    canonical, changes = observe(canonical, outbox._root(root), sources)
    if project_path:
        canonical.setdefault('project_bindings', {})[binding] = str(project_path)
    history['canonical'] = canonical
    events = {e['increment_id']: e for e in canonical['records']}
    heads = {e['source_id']: e for e in canonical['records']}
    namespace = canonical['roots'][outbox._root(root)]
    selected_paths = {s['path'] for s in sources}
    selected = [e for sid, e in heads.items() if canonical['sources'][sid]['namespace'] == namespace
                and canonical['sources'][sid]['project'] == project and canonical['sources'][sid]['path'] in selected_paths]
    covered = {i for r in history['records'] if r['delivery_state'] not in {'archive_only', 'cancelled', 'superseded'}
               for i in r.get('canonical_increments', [])}
    covered |= {a['canonical_increment'] for a in canonical['aliases']}
    added = updated = unchanged = 0
    intents = []
    for event in selected:
        inc = event['increment_id']
        row = next((r for r in history['records'] if r['increment_id'] == inc), None)
        file_request = 'ccms-file:' + inc
        row_grant, row_new = outbox.select_authorization(codex, root, request_id=file_request, scope=[project])
        if row_grant['state'] != 'active':
            raise ValueError('inactive_memory_authorization')
        if row is None:
            prior_rows = [r for r in history['records'] if r.get('source_id') == event['source_id']]
            parent = events.get(event['predecessor'])
            row = dict(format='canonical-file-v1', increment_id=inc,
                       predecessor=prior_rows[-1]['increment_id'] if prior_rows else None,
                       canonical_predecessor=event['predecessor'], source_id=event['source_id'],
                       operation=event['operation'], previous=parent['current'] if parent else None, current=event['current'],
                       source_root=outbox._root(root), scope=[project], source_hash=event['content_digest'],
                       request_id=file_request, periodic=False,
                       delivery_state='aliased' if inc in covered else 'prepared',
                       prepared_note_path=f'prepared/{inc}.md', canonical_increments=[inc])
            if memories_root:
                row['memories_root'] = str(memories_root)
            row['content_hash'] = outbox.sha(outbox.render_note(row, codex))
            history['records'].append(row)
        if row['delivery_state'] == 'prepared':
            if event['operation'] == 'add':
                added += 1
            else:
                updated += 1
        else:
            unchanged += 1
        # A compatibility invocation authorizes its complete selected snapshot;
        # each file uses an explicit, derived one-shot request binding.
        if row['request_id'] == file_request:
            history['requests'][file_request] = inc
        intents.append(outbox.DeliveryPlan(Path(codex), history, expected, row, row_grant, row_new, False))
    changed = before != outbox.encoded(history)
    for intent in intents:
        intent.changed = changed
    return intents, dict(added=added, updated=updated, unchanged=unchanged)


def apply_files(intents, *, skills=None):
    """Apply through T03 while holding the complete resource lock set."""
    if not intents:
        return 0
    with outbox._locked(intents[0].codex, skills):
        return _apply_files(intents, skills=skills)


def _apply_files(intents, *, skills=None):
    outbox.preflight_plans(intents)
    for intent in intents:
        if intent.new_grant:
            outbox.authorize(intent.codex, intent.grant['source_root'], request_id=intent.grant['request_id'],
                             scope=intent.grant['scope'], periodic=False, skills=skills)
            intent.new_grant = False
    outbox.prepare(intents[0], skills=skills)
    written = 0
    for intent in intents:
        was = intent.record['delivery_state']
        try:
            result = outbox.deliver(intent, skills=skills)
        except (OSError, ValueError) as exc:
            exc.notes_written = written
            raise
        if was == 'prepared' and result['delivery_state'] == 'published':
            written += 1
    return written


def _snapshot(root, args):
    paths = [p for p in root.iterdir() if p.is_file() and p.suffix.casefold() == '.md'
             and (args.IncludeReadme or p.name.casefold() != 'readme.md')]
    if args.IncludeArchive and (root / 'archive').exists():
        paths += [p for p in (root / 'archive').iterdir() if p.is_file() and p.suffix.casefold() == '.md']
    if not any(p.name.casefold() == 'memory.md' and p.parent == root for p in paths) or len(paths) > 500:
        raise ValueError('missing_index_or_excessive_snapshot')
    values = []
    total = 0
    for path in sorted(paths):
        raw = read_bounded(validate_path(path), args.MaxFileBytes)
        total += len(raw)
        if total > args.MaxTotalBytes:
            raise ValueError('memory_total_limit')
        text = canonical_text(raw, strict=True)
        values.append(dict(path=path.relative_to(root).as_posix(), text=text, raw=sha(raw), size=len(raw)))
    return values, total


def run(args):
    if not 1024 <= args.MaxFileBytes <= 1048576 or not args.MaxFileBytes <= args.MaxTotalBytes <= 67108864:
        raise ValueError('invalid_memory_limits')
    if not 0 <= args.LockTimeoutSeconds <= 300:
        raise ValueError('invalid_lock_timeout')
    project_path = validate_path(Path(args.ProjectPath).absolute())
    if not project_path.is_dir():
        raise ValueError('missing_project_path')
    git = subprocess.run(['git', '-C', str(project_path), 'rev-parse', '--show-toplevel'], capture_output=True, text=True)
    if git.returncode == 0:
        project_path = validate_path(git.stdout.strip())
    projects = validate_path(Path(args.ClaudeProjectsRoot or Path.home() / '.claude/projects').absolute())
    if args.ClaudeMemoryPath:
        memory = validate_path(Path(args.ClaudeMemoryPath).absolute())
        if memory.name == 'memory':
            project, projects = memory.parent.name, memory.parent.parent
        else:
            project, projects = 'direct-' + project_id(project_path), memory.parent
    else:
        project = args.ClaudeProjectKey or re.sub('[^A-Za-z0-9_-]', '-', str(project_path))
        outbox._scope([project])
        memory = validate_path(projects / project / 'memory')
    memories = validate_path(Path(args.CodexMemoriesRoot or Path(os.environ.get('CODEX_HOME', Path.home() / '.codex')) / 'memories').absolute())
    codex = memories.parent
    from ..restore_interlock import recovery_status
    recovery = recovery_status(codex)
    if recovery:
        return dict(recovery, notes_written=0, partial_write=False), 2
    if not outbox.contract_available(codex, memories):
        raise ValueError('missing_or_unsafe_ad_hoc_contract')
    one, total = _snapshot(memory, args)
    two, _ = _snapshot(memory, args)
    if one != two:
        raise ValueError('memory_source_unstable')
    pid = project_id(project_path)
    summary = dict(tool='claude-codex-memory-sync', version='1.0.0', status='preview', dry_run=args.DryRun,
                   project_id=pid[:12], selected_files=len(two), selected_bytes=total, added=0, updated=0, unchanged=0,
                   blocked=0, notes_written=0, partial_write=False, blocked_items=[], consolidation='not_requested', deletes_propagated=False)
    for source in two:
        findings = secrets.scan(source['text'], policy='credential-shapes-v1')
        if findings['state'] == 'scan_failed':
            raise ValueError('credential_scan_failed')
        legacy_rules = {'github_pat_fine': 'github_token', 'openai_key': 'openai_anthropic_key',
                        'anthropic_key': 'openai_anthropic_key'}
        rules = sorted({legacy_rules.get(f['rule_id'], f['rule_id']) for f in findings['findings']})
        if not args.IncludeSensitiveNames and re.search(r'(secret|credential|password|token|private)', source['path'], re.I):
            rules.append('sensitive_filename')
        if rules:
            summary['blocked_items'].append(dict(source_id=sha(source['path'].encode())[:12], rules=rules))
    if summary['blocked_items']:
        summary.update(status='blocked', blocked=len(summary['blocked_items']))
        return summary, 2
    notes_root = memories / 'extensions/ad_hoc/notes'
    legacy = {p.name: read_bounded(validate_path(p), 4 * 1024 * 1024)
              for p in notes_root.glob(f'*-ccms-v1-{pid[:12]}-*.md')}
    if len(legacy) > 4096 or sum(map(len, legacy.values())) > 64 * 1024 * 1024:
        raise ValueError('legacy_history_limit')
    request = 'ccms-' + sha(outbox.encoded(two))[:40]
    sources = [dict(project=project, path=s['path'], text=s['text']) for s in two]
    def execute():
        intents, counts = plan_files(codex, projects, project, sources, request_id=request, legacy_notes=legacy,
                                    project_path=project_path, memories_root=memories)
        summary.update(counts)
        if args.DryRun:
            summary['status'] = 'preview' if counts['added'] + counts['updated'] else 'no_changes'
        else:
            written = apply_files(intents)
            summary.update(notes_written=written, status='staged' if written else 'no_changes',
                           consolidation='pending_codex_consolidation' if written else 'not_requested')
        return summary, 0
    if args.DryRun:
        return execute()
    with compatibility_lock(notes_root, args.LockTimeoutSeconds):
        with outbox._locked(codex, None):
            return execute()


def main(argv=None):
    parser = argparse.ArgumentParser()
    for name in ('ProjectPath', 'ClaudeProjectsRoot', 'ClaudeProjectKey', 'ClaudeMemoryPath', 'CodexMemoriesRoot'):
        parser.add_argument('-' + name, default=str(Path.cwd()) if name == 'ProjectPath' else None)
    for name in ('DryRun', 'IncludeReadme', 'IncludeArchive', 'IncludeSensitiveNames'):
        parser.add_argument('-' + name, action='store_true')
    for name, default in [('MaxFileBytes', 65536), ('MaxTotalBytes', 4194304), ('LockTimeoutSeconds', 10)]:
        parser.add_argument('-' + name, type=int, default=default)
    parser.add_argument('-OutputFormat', choices=['Text', 'Json'], default='Text')
    args = parser.parse_args(argv)
    try:
        result, code = run(args)
    except (OSError, ValueError, KeyError, TypeError) as error:
        from ..restore_interlock import RestoreRecoveryRequired
        if isinstance(error, RestoreRecoveryRequired):
            print(json.dumps(dict(status='recovery_required', notes_written=0, partial_write=False)))
            return 2
        written = getattr(error, 'notes_written', 0)
        result = dict(tool='claude-codex-memory-sync', version='1.0.0', status='error',
                      message='Memory history, scope, source or runtime requires review.',
                      notes_written=written, partial_write=written > 0,
                      consolidation='pending_codex_consolidation' if written else 'not_requested')
        code = 1
    print(json.dumps(result) if args.OutputFormat == 'Json' else 'CCMS status: ' + result['status'])
    return code


if __name__ == '__main__':
    sys.exit(main())
