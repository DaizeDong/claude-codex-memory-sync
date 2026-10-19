# Claude → Codex Profile Bridge

Bridge supported Claude Code skills, instructions, MCP settings, reviewed hook adapters, and project memory into Codex on the same machine. Preview and back up managed changes before applying them. Ownership checks protect later user edits; native memory delivery requires an explicit authorized scope.

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)

[English](README.md) | [中文版](README_CN.md)

> This is an independent community project. It is not affiliated with or endorsed by Anthropic or OpenAI.

## Managed profile sync

Use `sync-all.cmd` for supported profile components. It requires **Python 3.11 or newer** on `PATH`; Windows directory links use junctions.

Install the source checkout and its dependencies from the pinned public revisions:

```powershell
python -m pip install -e "$(git config --get fleet.guards)"
python -m pip install -r requirements-dev.txt
```

Windows 8.3 directory aliases are supported when the filesystem provides them.
Hook plans preserve the caller's destination spelling; generated commands use the
resolved path. Destination junctions and symlinks remain rejected before writes.

```powershell
# Preview only; this is also the default when no mode is specified.
.\sync-all.cmd --dry-run

# Inspect conflict, skipped-source, and compatibility details without source bodies.
.\sync-all.cmd --dry-run --json

# Apply the current plan behind a rollback copy that is removed when the run ends.
.\sync-all.cmd --apply --json
```

The profile sync covers:

| Source | Codex result |
|---|---|
| User skills and skills from enabled, installed Claude plugins | Directory links under `~/.agents/skills`; unrelated existing skills are preserved and collisions are reported. |
| User/plugin commands and agent role Markdown | Instruction adapters exposed as skills, plus native Codex roles for supported agent definitions. Roles with unrepresentable execution or tool restrictions are reported separately. |
| Global `CLAUDE.md` and user rules | A hash-tracked managed block in `~/.codex/AGENTS.md`, preserving existing text and reporting edits to the managed block. Source rule path filters retain their scope. |
| Project `CLAUDE.md` documents | A compatible project-document fallback setting, preserving native Codex project instructions. |
| Compatible user and enabled-plugin MCP definitions | Incremental managed blocks in `config.toml`; existing native MCP names win conflicts. Unresolved environment variables and unsupported transports/settings are reported. Whole tables another writer placed inside a managed block (the Codex desktop app appends new tables after the last table it knows) are moved to just after the block when the rest of the block still matches its hash and the parsed configuration stays identical; they are listed under `config.foreign_tables` and, after an apply, under `foreign_tables_relocated` in the run result. Any other edit inside a block stays a conflict. |
| All nonempty project memory directories | Readable Markdown under `~/.codex/imports/claude-memory/`, a source-only project index, and explicitly authorized `ad_hoc` delivery through a durable outbox. |
| Claude hooks | Reviewed adapters for SessionStart, PostToolUse and Stop, including browser isolation checks and document-budget feedback. Existing Codex hooks are preserved; unsupported source commands are reported explicitly. The launcher runs the bridge on the base interpreter of the running environment, never a virtual environment's own `python.exe`, and names paths under the user home through `$env:USERPROFILE` (`$HOME` in `command`). Native trust and a new session may be required. |

This is a **same-machine setup**: linked skills and adapted local scripts depend on their original installation. It is not a self-contained copy for a new computer. The tool keeps Codex's existing model, provider, authentication, and permission settings. Claude login/session state is not imported.

Default source is `CLAUDE_CONFIG_DIR`, or the `.claude` directory under the user home; default destination is `CODEX_HOME`, or `~/.codex`. Override paths with `--claude-home`, `--codex-home`, and `--skills-home` when needed. The active `.claude/.claude.json` is preferred over the legacy `~/.claude.json` when both exist. External repository metadata requires an explicit catalog manifest or `CLAUDE_CONFIG_REPO` pointing to the config repository root, whose `restore/external-skill-repos.json` is read.

### Memory archive behavior

The full sync reads non-archive `*.md` files recursively, skips empty project directories, and maps project scope from exact encoded paths in the active Claude configuration or explicit reviewed path evidence. Active mappings and conflicts take precedence; unknown project keys remain visible without guessing a filesystem path. It accepts UTF-8/BOM text and BOM-marked UTF-16, emits UTF-8, limits each source file to 1 MiB, rejects symlinks/junctions, and skips suspected credentials. Reports list skipped filenames and reasons without source bodies or credential values.

Control characters become visible escapes such as `\u0008` **in the archive only**, with codepoints and locations recorded in the report; source files stay unchanged. Explicit API-key documentation placeholders are accepted, while actual credential patterns remain blocked.

Every run recomputes content hashes. New installations and default invocations maintain the archive only. An `instructions.md` file establishes ingress availability, not user authorization. Explicit `--request-id` and `--scope` select an ingress request; `--periodic` persists an approved recurring scope. Existing periodic grants can be migrated through the documented authorization API without changing scheduler tasks.

Delivery uses a durable outbox and stable increment-based note names. Each increment includes its predecessor, so repeated A to B transitions remain distinct. An interrupted attempt whose delivery cannot be proven is `delivery_unknown` and is never blindly replayed. Notes are created through OS no-replace publication; existing notes and user edits are preserved. Native consolidation is asynchronous. The outbox keeps only what delivery needs (the latest event of each memory file and the few delivery rows still in use), so it stays small however many runs pass; a history it cannot read stops delivery until the outbox is reset. See [memory authorization, persistence, and controller integration](docs/MEMORY_OUTBOX.md) for exact formats, recovery limits and the reset.

The current index identifies the active source snapshot. The archive planner retires unchanged owned copies when their source disappears or is excluded: the copy leaves the searchable import, is held in the run's rollback copy while that run can still fail, and is gone when the run ends (the Claude memory it was copied from is the source of truth). Unowned legacy copies require an exact-hash review decision; edited destinations are preserved and reported. Archive ownership is recorded by the existing memory outbox. See [archive hygiene and transaction hooks](docs/MEMORY_ARCHIVE_HYGIENE.md) for integration, reviewed scopes, and rollback behavior. No native `MEMORY.md`, memory summary, rollout evidence, or SQLite file is rewritten.

### Rollback copy

Before an applying run changes anything, it copies the manifest and the original bytes of every destination it will change to `~/.codex/claude-sync/backups/<run-id>` (or the selected Codex home). The run removes this temporary copy after success or a completed rollback. If cleanup cannot finish, `cleanup_pending` reports the directory of remaining files; it may contain only fragments and is not a promise of a usable rollback copy. If automatic rollback fails, cleanup does not run and `recovery_required` identifies the retained recovery material. A process interrupted before cleanup can also leave a copy. Restore from a complete copy with:

```powershell
.\sync-all.cmd --rollback "$env:USERPROFILE\.codex\claude-sync\backups\RUN_ID" --json
```

A completed rollback removes the copy as well. Rollback restores files and links only when their current state still matches what that run wrote; later user edits are preserved. Outbox history, prepared evidence, authorization records, and appended memory notes are retained because removing a file cannot reliably retract memory already processed by Codex. Live profile data and rollback copies must remain outside this repository.

Explicitly disabled plugins and removed source items retire only intact artifacts owned by this bridge. User edits and unavailable source installations are preserved and reported. Retirement is covered by the same rollback copy; native memory notes remain append-only.

### Inventory and linked resources

The JSON report includes `inventory`: entry-point paths, ownership, local repository identity/version, dependency declarations and interpreter presence, plus broken-link recovery candidates. Dependency scripts are never executed during inventory. To repair only uniquely identified legacy links from approved repositories:

```powershell
.\sync-all.cmd --dry-run --repair-links --approved-repo C:\src\skill-pack --json
.\sync-all.cmd --apply --repair-links --approved-repo C:\src\skill-pack --json
```

Ambiguous candidates remain untouched. `--external-skill-manifest` can point to an existing repository manifest. Native role files and their config entries have independent ownership hashes; read-only reviewer guidance is advisory and native permissions remain inherited.

For linked skills, resolve relative resources from the canonical source entrypoint. Use both `resource_context.canonical_entrypoint` and `resource_context.source_root` from the inventory. Forwarding wrappers point to their verified upstream file; overlay bundles point to their owned local payload. Changed wrapper, descriptor, or payload entrypoint content makes the context unavailable. A resource in a sibling directory may require an explicitly approved package root. Run this command in the Python environment where `profile-sync` is installed:

```powershell
python -m profile_bridge.resources --entrypoint C:\skills\example\SKILL.md --source-root C:\src\example-package --reference ../../shared-references/rules.md
```

The command prints the existing canonical file path as JSON and exits `0`. Missing files, absolute or remote references, and paths escaping the selected root through `..` or symlinks return a reason and exit `2`. It does not read workflow content, execute resources, or change the profile. Keep the root tied to the known package; do not broaden it to make a failed reference pass.

The managed instruction block also explains host capability checks and writing-skill scope. Claude-specific tools and Cowork actions require actual host support; installation alone establishes no runtime capability. Named-persona writing requires explicit persona intent. Raw skills must have valid YAML frontmatter with unique mapping keys and nonempty string names and descriptions before installation is planned. Adapter descriptions use parsed source scalars, preserving comparison conditions and inline trigger text while example sections remain in the source workflow. Inventory preserves catalog source hashes and plugin versions, and reports unavailable git provenance without inventing an upstream revision.

Runtime selection verifies every declared generated member of the selected alternative, including its installed instructions, descriptor and payload, against hashes stored in the selection snapshot. Missing or changed members block that alternative and preserve the files. An older selection snapshot without member hashes must be regenerated through the normal profile plan before it can select an alternative.

### Scheduled runs and health

`run_profile_sync.py` is the deterministic entry point for an existing scheduler:

```powershell
python .\run_profile_sync.py --apply --write-status --probe-mcp
```

It applies the managed profile, recomputes the plan, and evaluates conflicts and missing dependencies. Exit `0` means the checked profile is healthy, `2` means unresolved findings or remaining changes, and `1` means execution failed. The JSON distinguishes deliberate compatibility exclusions from faults. Local HTTP MCP servers are initialized without invoking tools; remote servers are marked `not_checked`, and stdio commands are checked for presence only.

`--accepted-findings <file>` names a reviewed baseline of standing findings, such as skills blocked on an unverified capability or hooks awaiting review. Keep it in private configuration, never in a public repository. Each entry is `{"identity": {...}, "count": n}`, where the identity is the finding's area, status, reason and subject (name, hook event, warning source, capability reasons, or the skipped memory files), never message text. When every finding matches an entry the status is `accepted` and the exit code is `0`. Any finding outside the baseline, including a second occurrence beyond the accepted count, keeps exit `2` and is listed under `new_findings`. Pending changes cannot be accepted, and a malformed baseline is an execution error (`1`). Entries that no longer match are reported as `stale_accepted`, so a resolved item can be removed from the baseline.

`--write-status` writes `claude-sync/last-run.json` under the selected Codex home. It preserves the previous `last-success.json` during a run and refreshes it only after verification passes with status `healthy` or `accepted`. A retained marker describes the previous successful run; the scheduler must also check the current exit code and run identity. The runner does not send messages or create scheduled tasks; connect it to the existing scheduler, task monitor and backup registration.

Hook warning identities include the event. Preserved archive copies normally retain only their distinct preservation reasons, so ordinary growth of the same kind stays accepted. If a preserved copy has credential findings, its reason, path and SHA256 also enter `detail.archive_credentials`. New credential findings, changed contents or a different affected file require review. Older baselines without these subject fields do not accept those findings.

The profile transform and health checks do not call a model. Automation that adds agent work should invoke the operator's `llmcall` interface with `mode="agent"`, inheriting its provider policy rather than embedding a second routing chain.

Executing a workflow requires an installed `llmcall` client with the 0.3.0 call
contract; an older client fails before workflow state is written. Its clients run
in the caller's process cwd and environment, so a workflow continues only from
its anchored workspace: a continuation started from another directory fails with
`workspace_requires_process_cwd` (under llmcall 0.2.0 the anchor was passed per
call instead), so change into the anchored workspace first. An idempotent
retrieval of a completed request still answers from any directory. Histories
written for llmcall 0.2.0 stay readable and resumable.

## Limitations

- `extensions\ad_hoc\instructions.md` is a contract detected in the local Codex installation, not a publicly guaranteed stable API. If it is absent or a future Codex change invalidates it, delivery stops (the memory report says `unsupported`) and the bridge may need an update.
- Sync remains one-way and Codex consolidation remains asynchronous; immediate or guaranteed recall is out of scope.
- Credential detection is heuristic. Dry-run review and careful source hygiene remain required.

## Languages

English (`README.md`, authoritative) · 中文 ([`README_CN.md`](README_CN.md))

## Roadmap, contributing and license

See [ROADMAP.md](ROADMAP.md), [CHANGELOG.md](CHANGELOG.md), and [LICENSE](LICENSE) (MIT).
The repository checks are defined in `.github/workflows/`; [the spec adaptation notes](docs/2026-09-22-spec-adaptation.md) explain repository-specific exceptions.
