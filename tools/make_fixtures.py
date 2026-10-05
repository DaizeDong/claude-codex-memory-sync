"""Generate synthetic fixtures through real producer APIs, never copied user data."""
from pathlib import Path
from unittest.mock import patch

import profile_memory as memory
from profile_bridge import memory_outbox as outbox


def make_workflow_client_contracts():
    """Generate a synthetic typed client; no private runtime package is imported."""
    from contextlib import contextmanager
    from contextvars import ContextVar
    from dataclasses import dataclass, field
    from types import SimpleNamespace

    @dataclass
    class Attempt:
        provider: str = "fake"
        ok: bool = True
        error: str | None = None

    @dataclass
    class Result:
        text: str = ""
        provider: str = "fake"
        error: str | None = None
        outcome: str = "success"
        effects: str = "none"
        execution_started: bool | None = False
        effective_model: str | None = None
        model_source: str | None = None
        model_family: str | None = None
        data: object = None
        attempts: list = field(default_factory=list)

        def __bool__(self):
            return bool(self.text) and self.error is None

    @dataclass(frozen=True)
    class ModelSelection:
        mode: str
        model: str | None = None

    @dataclass(frozen=True)
    class ExecutionRequirements:
        access: str = "read_only"
        workspace: str | None = None
        tool_network: str = "default"
        replay: str = "never_after_start"
        required_tools: tuple = ()
        required_mcp: tuple = ()
        tool_allowlist: tuple | None = None

    @dataclass(frozen=True)
    class CallContext:
        cwd: str
        env: dict

    context = ContextVar("synthetic_workflow_context", default=None)

    @contextmanager
    def use_context(value):
        token = context.set(value)
        try:
            yield value
        finally:
            context.reset(token)

    def resolve_context(cwd=None, env=None):
        current = context.get() or CallContext(str(Path.cwd()), {})
        target = Path(cwd) if cwd is not None else Path(current.cwd)
        if not target.is_absolute():
            target = Path(current.cwd) / target
        return CallContext(str(target.resolve()), {**current.env, **(env or {})})

    process = SimpleNamespace(CallContext=CallContext, use_context=use_context,
                              resolve_context=resolve_context)
    return SimpleNamespace(Result=Result, Attempt=Attempt, ModelSelection=ModelSelection,
                           ExecutionRequirements=ExecutionRequirements, process=process)


def make_adapter_descriptions():
    """Generate metadata edge cases without importing a real source transcript."""
    summary = "Review synthetic error handling when explicitly requested."
    example = "Examples:\\n\\n<example>\\nSynthetic example dialogue.\\n</example>"
    return {
        "escaped_examples": summary + " " + example * 30,
        "block_marker": "|\n  " + summary,
        "folded_marker": ">-\n  " + summary,
        "long_summary": "Review synthetic input for correctness. " * 40,
        "comparisons": "Review synthetic values < 5 and > 1 when requested.",
        "comparison_summary": "Review synthetic values less than 5 and greater than 1 when requested.",
        "unspaced_comparisons": "Use when x<y and z>5 to verify the mathematical condition.",
        "unspaced_summary": "Use when x less than y and z greater than 5 to verify the mathematical condition.",
        "code_type": "Use when a List<T> value requires inspection.",
        "code_type_summary": "Use when a List less than T greater than value requires inspection.",
        "markup": 'Use <strong>careful</strong> review of <span class="condition">synthetic</span> conditions.',
        "markup_summary": 'Use careful review of synthetic conditions.',
        "leading_operator": '>5 failures require review.',
        "leading_operator_summary": 'greater than 5 failures require review.',
        "absolute_value": '|x| > 5 requires review.',
        "absolute_value_summary": '|x| greater than 5 requires review.',
        "chained_comparison": 'Use when a<b>c to review the local maximum.',
        "chained_comparison_summary": 'Use when a less than b greater than c to review the local maximum.',
        "inline_example": 'Use when a record has an example: field; only inspect that field.',
        "inline_example_summary": 'Use when a record has an example: field; only inspect that field.',
        "literal_newline": r'Use when matching the regex \\n in synthetic text.',
        "literal_newline_summary": r'Use when matching the regex \\n in synthetic text.',
        "summary": summary,
    }


def make_adapter_description_source(base, case):
    """Generate real YAML block scalars and quoted literal description values."""
    import json
    value = make_adapter_descriptions()[case]
    serialized = value if case in {'block_marker', 'folded_marker'} else json.dumps(value)
    source = Path(base) / 'commands/review.md'
    source.parent.mkdir(parents=True)
    source.write_text('---\nname: review\ndescription: ' + serialized + '\n---\nReview synthetic inputs.\n', encoding='utf-8')
    return source


def make_raw_metadata_home(base, case):
    """Generate strict YAML positives and malformed raw installation inputs."""
    base = Path(base)
    claude, codex, skills = base / 'claude', base / 'codex', base / 'installed'
    cases = {
        'colon': 'name: synthetic\ndescription: use: broken YAML\n',
        'list': 'name: [synthetic]\ndescription: [not, a, string]\n',
        'quote': 'name: synthetic\ndescription: "unterminated\n',
        'boolean': 'name: synthetic\ndescription: true\n',
        'quoted': 'name: synthetic\ndescription: "Use: synthetic input with a colon."\n',
        'literal': 'name: synthetic\ndescription: |\n  Use synthetic input\n  with multiple lines.\n',
        'folded': 'name: synthetic\ndescription: >-\n  Use synthetic input\n  with folded lines.\n',
        'duplicate_name': 'name: first\nname: second\ndescription: Use synthetic input.\n',
        'duplicate_description': 'name: synthetic\ndescription: Review one input.\ndescription: Review all inputs.\n',
        'duplicate_nested': 'name: synthetic\ndescription: Use synthetic input.\nmetadata:\n  mode: first\n  mode: second\n',
        'unique_nested': 'name: synthetic\ndescription: Use synthetic input.\nmetadata:\n  first: one\n  second: two\n',
    }
    source = claude / 'skills/synthetic/SKILL.md'
    source.parent.mkdir(parents=True)
    source.write_text('---\n' + cases[case] + '---\nSynthetic instructions.\n', encoding='utf-8')
    return {'claude': claude, 'codex': codex, 'skills': skills, 'source': source}


def make_wrapper_resource_home(base, *, role=False):
    """Generate a plugin with command and skill resources for real adapter plans."""
    import json
    import subprocess
    base = Path(base)
    claude, codex, skills = base / 'claude', base / 'codex', base / 'installed'
    root = base / 'synthetic-source'
    command = root / 'commands/review.md'
    entrypoint = root / ('agents/code-reviewer.md' if role else 'skills/example/SKILL.md')
    reference = root / 'references/rules.md'
    plugin = 'pr-review-toolkit' if role else 'synthetic'
    registry_key = plugin + ('@claude-plugins-official' if role else '@local')
    entry_name = 'code-reviewer' if role else 'example'
    relative_reference = '../references/rules.md' if role else '../../references/rules.md'
    files = {
        command: '---\nname: review\ndescription: Review synthetic input.\n---\nRead [rules](../references/rules.md).\n',
        entrypoint: f'---\nname: {entry_name}\ndescription: Read synthetic rules.\n---\nRead [rules]({relative_reference}).\n',
        reference: 'Synthetic pinned rules.\n',
        root / '.claude-plugin/plugin.json': json.dumps({'name': plugin}),
        claude / 'settings.json': json.dumps({'enabledPlugins': {registry_key: True}}),
        claude / 'plugins/installed_plugins.json': json.dumps({'plugins': {
            registry_key: [{'installPath': str(root), 'scope': 'user', 'version': '1'}]}}),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
    subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
    return {'claude': claude, 'codex': codex, 'skills': skills, 'root': root,
            'command': command, 'entrypoint': entrypoint, 'reference': reference}


def make_adapter_overlay(base):
    """Produce an overlay with synthetic examples through the real builder."""
    from skill_smith import overlays
    source = Path(base) / 'source/SKILL.md'
    source.parent.mkdir(parents=True)
    source.write_text('---\nname: synthetic\ndescription: Synthetic workflow.\n---\n'
                      'Inspect synthetic inputs.\n<example>Keep the full example here.</example>\n',
                      encoding='utf-8')
    entry = dict(source_id='synthetic', kind='skill', name='synthetic', relative_path='SKILL.md',
                 resolved_path=str(source), path=str(source), source_hash=overlays.digest(source.read_bytes()),
                 client='claude', scope='user')
    record = dict(source_id='synthetic', source_hash=entry['source_hash'],
                  resolved_path=str(source.parent), status={'resolved': 'yes'}, entrypoints=[entry])
    capabilities = {'capabilities': {'llmcall.contexts': {'status': 'supported', 'evidence': ['synthetic observation']}}}
    return overlays.build(record, 'codex', capabilities)


def make_resource_tree(base):
    """Generate a source tree whose shared files differ from installed siblings."""
    base = Path(base)
    source = base / "source-package"
    entrypoint = source / "skills/example/SKILL.md"
    reference = source / "shared-references/rules.md"
    outside = base / "outside/rules.md"
    installed = base / "installed/example"
    files = {
        entrypoint: "---\nname: example\ndescription: Read synthetic rules.\n---\n"
                    "Read [rules](../../shared-references/rules.md#checks).\n",
        reference: "# Synthetic source rules\n",
        outside: "# Synthetic unrelated rules\n",
        base / "shared-references/rules.md": "# Synthetic alias-relative decoy\n",
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    installed.parent.mkdir(parents=True)
    return {"root": source, "entrypoint": entrypoint, "reference": reference,
            "outside": outside, "installed": installed, "logical_source": Path('synthetic-policy-label')}


def make_instruction_home(base):
    """Generate a profile with separately owned source and native instructions."""
    base = Path(base)
    claude, codex = base / "claude", base / "codex"
    claude.mkdir(parents=True)
    codex.mkdir(parents=True)
    (claude / "CLAUDE.md").write_text("Use concise synthetic replies.\n", encoding="utf-8")
    (codex / "AGENTS.md").write_text("Keep the native synthetic rule.\n", encoding="utf-8")
    return claude, codex


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
