# Claude → Codex Profile Bridge

Bridge supported Claude Code skills, instructions, MCP settings, reviewed hook adapters, and project memory into Codex on the same machine. Preview and back up managed changes before applying them. Ownership checks protect later user edits; native memory delivery requires an explicit authorized scope.

[![Test](https://github.com/DaizeDong/claude-codex-memory-sync/actions/workflows/test.yml/badge.svg)](https://github.com/DaizeDong/claude-codex-memory-sync/actions/workflows/test.yml)
[![PowerShell 5.1](https://img.shields.io/badge/Windows%20PowerShell-5.1-5391FE?logo=powershell&logoColor=white)](sync-claude-memory-to-codex.ps1)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Languages](https://img.shields.io/badge/Languages-EN%20%2F%20CN-blue?style=flat)](#languages)

[English](README.md) | [中文版](README_CN.md)

> This is an independent community project. It is not affiliated with or endorsed by Anthropic or OpenAI.

## Managed profile sync

Use `sync-all.cmd` for supported profile components. It requires **Python 3.11 or newer** on `PATH`; Windows directory links use junctions. The original Windows PowerShell 5.1 memory-only entry remains available and is documented below.

```powershell
# Preview only; this is also the default when no mode is specified.
.\sync-all.cmd --dry-run

# Inspect conflict, skipped-source, and compatibility details without source bodies.
.\sync-all.cmd --dry-run --json

# Apply the current plan after creating a local backup.
.\sync-all.cmd --apply --json
```

The profile sync covers:

| Source | Codex result |
|---|---|
| User skills and skills from enabled, installed Claude plugins | Directory links under `~/.agents/skills`; unrelated existing skills are preserved and collisions are reported. |
| User/plugin commands and agent role Markdown | Instruction adapters exposed as skills, plus native Codex roles for supported agent definitions. Roles with unrepresentable execution or tool restrictions are reported separately. |
| Global `CLAUDE.md` and user rules | A hash-tracked managed block in `~/.codex/AGENTS.md`, preserving existing text and reporting edits to the managed block. Source rule path filters retain their scope. |
| Project `CLAUDE.md` documents | A compatible project-document fallback setting, preserving native Codex project instructions. |
| Compatible user and enabled-plugin MCP definitions | Incremental managed blocks in `config.toml`; existing native MCP names win conflicts. Unresolved environment variables and unsupported transports/settings are reported. |
| All nonempty project memory directories | Readable Markdown under `~/.codex/imports/claude-memory/`, a source-only project index, and explicitly authorized `ad_hoc` delivery through a durable outbox. |
| Claude hooks | Reviewed adapters for SessionStart, PostToolUse and Stop, including browser isolation checks, document-budget feedback and login-state export/absorb. Existing Codex hooks are preserved; unsupported source commands are reported explicitly. Native trust and a new session may be required. |

This is a **same-machine setup**: linked skills and adapted local scripts depend on their original installation. It is not a self-contained copy for a new computer. The tool keeps Codex's existing model, provider, authentication, and permission settings. Claude login/session state is not imported.

Default source is `CLAUDE_CONFIG_DIR`, or the `.claude` directory under the user home; default destination is `CODEX_HOME`, or `~/.codex`. Override paths with `--claude-home`, `--codex-home`, and `--skills-home` when needed. The active `.claude/.claude.json` is preferred over the legacy `~/.claude.json` when both exist. External repository metadata requires an explicit catalog manifest or `CLAUDE_CONFIG_REPO` pointing to the directory containing `external-skill-repos.json`.

### Memory archive behavior

The full sync reads non-archive `*.md` files recursively, skips empty project directories, and maps project scope from exact encoded paths in the active Claude configuration or explicit reviewed path evidence. Active mappings and conflicts take precedence; unknown project keys remain visible without guessing a filesystem path. It accepts UTF-8/BOM text and BOM-marked UTF-16, emits UTF-8, limits each source file to 1 MiB, rejects symlinks/junctions, and skips suspected credentials. Reports list skipped filenames and reasons without source bodies or credential values.

Control characters become visible escapes such as `\u0008` **in the archive only**, with codepoints and locations recorded in the report; source files stay unchanged. Explicit API-key documentation placeholders are accepted, while actual credential patterns remain blocked.

Every run recomputes content hashes. New installations and default invocations maintain the archive only. An `instructions.md` file establishes ingress availability, not user authorization. Explicit `--request-id` and `--scope` select an ingress request; `--periodic` persists an approved recurring scope. Existing periodic grants can be migrated through the documented authorization API without changing scheduler tasks.

Delivery uses a durable outbox and stable increment-based note names. Each increment includes its predecessor, so repeated A to B transitions remain distinct. An interrupted attempt whose delivery cannot be proven is `delivery_unknown` and is never blindly replayed. Notes are created through OS no-replace publication; existing notes and user edits are preserved. Native consolidation is asynchronous. See [memory authorization, persistence, and controller integration](docs/MEMORY_OUTBOX.md) for exact formats and recovery limits.

The current index identifies the active source snapshot. The archive planner retires unchanged owned copies when their source disappears or is excluded, retaining evidence in the existing private profile backup outside the searchable import. Unowned legacy copies require an exact-hash review decision; edited destinations are preserved and reported. Archive ownership is recorded by the existing memory outbox. See [archive hygiene and transaction hooks](docs/MEMORY_ARCHIVE_HYGIENE.md) for integration, reviewed scopes, and rollback behavior. No native `MEMORY.md`, memory summary, rollout evidence, or SQLite file is rewritten.

### Backups and rollback

Each applying run with changes stores its manifest, original file contents, and report under `~/.codex/claude-sync/backups/<run-id>` (or the selected Codex home). To restore that run:

```powershell
.\sync-all.cmd --rollback "$env:USERPROFILE\.codex\claude-sync\backups\RUN_ID" --json
```

Use the backup path reported by the applying run. Rollback restores files and links only when their current state still matches what that run wrote; later user edits are preserved. Outbox history, prepared evidence, authorization records, and appended memory notes are retained because removing a file cannot reliably retract memory already processed by Codex. Live profile data and backups must remain outside this repository.

### Scheduled runs and health

Explicitly disabled plugins and removed source items retire only intact artifacts owned by this bridge. User edits and unavailable source installations are preserved and reported. Retirement is covered by the same backup/rollback mechanism; native memory notes remain append-only.

The JSON report includes `inventory`: entry-point paths, ownership, local repository identity/version, dependency declarations and interpreter presence, plus broken-link recovery candidates. Dependency scripts are never executed during inventory. To repair only uniquely identified legacy links from approved repositories:

```powershell
.\sync-all.cmd --dry-run --repair-links --approved-repo C:\src\skill-pack --json
.\sync-all.cmd --apply --repair-links --approved-repo C:\src\skill-pack --json
```

Ambiguous candidates remain untouched. `--external-skill-manifest` can point to an existing repository manifest. Native role files and their config entries have independent ownership hashes; read-only reviewer guidance is advisory and native permissions remain inherited.

`run_profile_sync.py` is the deterministic entry point for an existing scheduler:

```powershell
python .\run_profile_sync.py --apply --write-status --probe-mcp
```

It applies the managed profile, recomputes the plan, and evaluates conflicts and missing dependencies. Exit `0` means the checked profile is healthy, `2` means unresolved findings or remaining changes, and `1` means execution failed. The JSON distinguishes deliberate compatibility exclusions from faults. Local HTTP MCP servers are initialized without invoking tools; remote servers are marked `not_checked`, and stdio commands are checked for presence only.

`--write-status` writes `claude-sync/last-run.json` under the selected Codex home. It preserves the previous `last-success.json` during a run and refreshes it only after verification passes. A retained marker describes the previous successful run; the scheduler must also check the current exit code and run identity. The runner does not send messages or create scheduled tasks; connect it to the existing scheduler, task monitor and backup registration.

The profile transform and health checks do not call a model. Automation that adds agent work should invoke the operator's `llmcall` interface with `mode="agent"`, inheriting its provider policy rather than embedding a second routing chain.

### Profile bridge tests

All tests use synthetic data in temporary directories:

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\tests\run-tests.ps1
```

The first command requires Python 3.11+. The second verifies the existing Windows PowerShell 5.1 memory-only bridge.

## Memory-only bridge: design philosophy

The remaining sections describe **`sync-memory.cmd` / `sync-claude-memory-to-codex.ps1`**, the original single-project entry. Its selection, output, and all-or-nothing credential rules differ from the full profile archive described above.

**Stage verifiable memory updates; never pretend the two agents share a brain.**

This tool is intentionally small. Its design follows three principles:

1. **Fill the seam, do not add another agent surface.** Claude Code already stores project memory, and Codex already owns its memory consolidation. This tool only converts one representation into the format accepted by the detected local ingress contract. It adds no agent terminal, daemon, MCP server, database, vector store, or model call.
2. **Stage through the detected local contract, do not impersonate Codex internals.** The script appends self-contained notes under `extensions\ad_hoc\notes\`. It never rewrites Codex memory summaries, rollout evidence, or SQLite state. A successful sync means “safely staged,” not “already consolidated or guaranteed to be recalled.”
3. **Memory quality matters more than memory volume.** Dry-run first, select conservatively, fail closed on likely credentials, bound every input, and deduplicate incrementally. Copying every log and stale decision would make the pool larger while potentially making recall worse.

The design target is the smallest auditable bridge between two existing memory systems, not a universal shared-memory platform.

## What it is (and isn't)

`claude-codex-memory-sync` is a lightweight, local, one-way converter for Windows PowerShell 5.1. It reads Claude Code project auto-memory Markdown, applies path and credential checks, and stages eligible content as Codex `ad_hoc` notes.

```text
Claude project memory (*.md)
        │ read-only selection, credential scan, incremental deduplication
        ▼
<CodexMemoriesRoot>\extensions\ad_hoc\notes\*.md
        │ asynchronous processing owned by Codex
        ▼
Codex memory consolidation
```

It does not:

- start a new agent terminal or background service;
- install an MCP server, database, or vector store;
- call a model, `codex exec`, or any network API at runtime;
- write memory back from Codex to Claude;
- turn the two native memory systems into a strongly consistent database;
- guarantee when, how, or whether Codex will recall a staged item.

## Install

Requirements:

- Windows;
- Windows PowerShell 5.1 (`powershell.exe`), the supported and tested runtime;
- a Claude memory directory containing `MEMORY.md`;
- Codex memories enabled, with an existing memories root and `extensions\ad_hoc\instructions.md` beneath it.

Clone the repository; the tool needs no additional PowerShell modules or packages. Git is used for cloning and, when available, Git-root discovery:

```powershell
git clone https://github.com/DaizeDong/claude-codex-memory-sync.git
Set-Location .\claude-codex-memory-sync
```

## Quick start

Run the first sync as a zero-write preview:

```powershell
.\sync-memory.cmd -ProjectPath "C:\path\to\your-project" -DryRun
```

Review the selected file count, byte count, planned changes, and safety results. If automatic source mapping is ambiguous, stop and pass an explicit `-ClaudeMemoryPath`. Once the preview is correct, stage the notes:

```powershell
.\sync-memory.cmd -ProjectPath "C:\path\to\your-project"
```

You can also call the PowerShell script directly:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File .\sync-claude-memory-to-codex.ps1 `
  -ProjectPath "C:\path\to\your-project" `
  -DryRun
```

The wrapper's `-ExecutionPolicy Bypass` applies only to that PowerShell process. It does not change the persistent system or user execution policy. `sync-memory.cmd` forwards both arguments and the script's exit code.

## How it works

The default Codex memories root is:

1. `$env:CODEX_HOME\memories`, when `CODEX_HOME` is set;
2. `%USERPROFILE%\.codex\memories`, otherwise.

The script writes only to `extensions\ad_hoc\notes\`. It does not directly edit `MEMORY.md`, `memory_summary.md`, `raw_memories.md`, rollout evidence, or Codex SQLite state.

Sync is one-way and append/update oriented. Deleting or renaming a Claude source file does not delete, rename, or retract memory already staged for Codex. To correct or forget an old memory, ask Codex explicitly; deleting only the Claude source is not sufficient.

“Staged” also does not mean “consolidated.” Codex processes ingress notes asynchronously on its own schedule.

## Path discovery and explicit overrides

Defaults:

- `ProjectPath`: the current working directory.
- `ClaudeProjectsRoot`: `%USERPROFILE%\.claude\projects`.
- `CodexMemoriesRoot`: `$env:CODEX_HOME\memories`, or `%USERPROFILE%\.codex\memories` when `CODEX_HOME` is unset.

When Git is available and `git rev-parse` succeeds for `ProjectPath`, the Git root defines project identity and automatic mapping. Otherwise, discovery checks `ProjectPath` and its parents and requires exactly one matching Claude memory directory. Path encoding can be ambiguous, and a moved repository may leave an old directory behind. Moving the project also changes its identity. The summary deliberately avoids printing the complete auto-discovered path. If the mapping is uncertain, do not write; use an explicit override.

Specify the Claude project key:

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" `
  -ClaudeProjectsRoot "D:\claude\projects" `
  -ClaudeProjectKey D--src-app `
  -DryRun
```

Or specify both the Claude memory directory and Codex memories root directly:

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" `
  -ClaudeMemoryPath "D:\claude\projects\D--src-app\memory" `
  -CodexMemoriesRoot "D:\codex\memories" `
  -DryRun
```

`ClaudeProjectKey` and `ClaudeMemoryPath` are mutually exclusive source modes. `ClaudeMemoryPath` overrides only the source directory; project identity still comes from the Git root or `ProjectPath`. Invalid parameters or paths exit with code `1`.

## Parameters

| Parameter | Default | Meaning |
|---|---:|---|
| `-ProjectPath <path>` | Current directory | Repository/project path used for source mapping and project identity. |
| `-ClaudeProjectsRoot <path>` | `%USERPROFILE%\.claude\projects` | Claude Code projects root. |
| `-ClaudeProjectKey <name>` | Auto-discovered | Explicit Claude project directory name. |
| `-ClaudeMemoryPath <path>` | Auto-discovered | Direct Claude memory directory; use for nonstandard layouts or ambiguous mapping. |
| `-CodexMemoriesRoot <path>` | `$env:CODEX_HOME\memories`, otherwise `%USERPROFILE%\.codex\memories` | Codex memories root; override for portable, test, or nondefault installations. |
| `-DryRun` | Off | Perform discovery, filtering, safety checks, and planning without writing a staging note. |
| `-IncludeReadme` | Off | Include a root-level basename matching `README.md` exactly, case-insensitively; excluded by default to reduce explanatory noise. |
| `-IncludeArchive` | Off | Also include direct `memory\archive\*.md` children; archived material is excluded by default. |
| `-IncludeSensitiveNames` | Off | Let candidates with sensitive filenames continue to content scanning. It **never** bypasses credential scanning. |
| `-MaxFileBytes <int>` | `65536` | Per-source limit; valid range is 1 KiB to 1 MiB. |
| `-MaxTotalBytes <int>` | `4194304` | Total candidate-content limit; must be at least `MaxFileBytes` and at most 64 MiB. |
| `-LockTimeoutSeconds <int>` | `10` | Wait for the global destination lock; valid range is 0 to 300 seconds. |
| `-OutputFormat Text\|Json` | `Text` | Human-readable output or machine-readable JSON. |

A snapshot may contain at most 500 selected source files. History reads are separately bounded to 4,096 notes, 64 MiB total, and 4 MiB per note for the current project.

JSON preview example:

```powershell
.\sync-memory.cmd -ProjectPath "D:\src\app" -DryRun -OutputFormat Json
```

| `status` | Meaning |
|---|---|
| `preview` | A dry run found content to add or update. |
| `staged` | At least one note was staged. |
| `no_changes` | Nothing changed; this is success. |
| `blocked` | A preflight safety rule blocked the batch. |
| `error` | A handled script failure occurred; this can include a final generated-note safety rejection. |

Normal results and preflight safety blocks use the full JSON schema: `tool`, `version`, `status`, `dry_run`, `project_id`, `selected_files`, `selected_bytes`, `added`, `updated`, `unchanged`, `blocked`, `notes_written`, `partial_write`, `blocked_items`, `consolidation`, and `deletes_propagated`. Runtime failures caught after successful parameter binding, including a safety rejection found only after the final note envelope is built, use the smaller `status: "error"` schema: `tool`, `version`, `status`, `message`, `notes_written`, `partial_write`, and `consolidation`. PowerShell startup, parsing, and parameter-binding failures occur before the script's formatter and may produce native stderr instead of JSON. Treat the process exit code as authoritative.

## Selection and safety model

By default, the tool selects direct `*.md` children of the Claude memory root without recursion and excludes a case-insensitive exact filename match for `README.md`. `-IncludeArchive` additionally selects direct `memory\archive\*.md` children. Reparse points, UNC/device/alternate-data-stream paths, paths that cannot be resolved safely, and nonregular files are not trusted as sources.

The reader accepts strict UTF-8, UTF-8 with BOM, and BOM-marked UTF-16 LE/BE. It normalizes supported line separators to LF and text to NFC, rejects NUL and dangerous control characters, and checks that a source stays stable while it is read. An unstable or invalid source fails with exit code `1` before new notes are written.

Safety checks have two layers:

1. **Sensitive filename checks.** A match blocks the entire batch by default. `-IncludeSensitiveNames` only lets that file proceed to content scanning.
2. **Hard credential/secret content checks.** Likely private keys, access tokens, and other hard credentials block the entire batch. No include option bypasses this layer.

A safety block is all-or-nothing: exit code `2`, zero writes, and no partial staging notes. These checks reduce accidental exposure; they are heuristic and are not a complete secret scanner. Always review the dry-run result, and never put passwords, tokens, private keys, personal data, or regulated data into agent memory.

Imported Claude text is wrapped as quoted, untrusted data and is explicitly marked as non-executable. This reduces instruction confusion; it is not a reason to sync hostile or unnecessary content.

Notes retain the absolute project path to preserve scope. Usernames, customer names, and directory structure inside that path therefore become memory content. If the path itself is sensitive, use a neutral project location and, when needed, point `-ClaudeMemoryPath` to the source explicitly.

## Incremental semantics and trust boundary

Repeated runs compare source identity and content state, so unchanged content is not staged again. A changed source appends a new update note; it never edits the old note in place. `no_changes` is a successful result.

Each note is published atomically, but a multi-note batch is not a filesystem transaction. If an I/O error interrupts a batch, already-published notes remain. JSON accurately reports `partial_write=true` and the actual `notes_written`; after the underlying problem is fixed, rerunning skips those notes and continues with the remaining changes.

For incremental baselines, the tool reads valid CCMS history notes for the current project from the destination. Other projects and non-CCMS notes are not part of that baseline. A destination-derived `Global\` mutex serializes cooperating instances of this tool that target the same destination. A local account or process that can write that directory is inside the same trust boundary. Structural checks, current/previous snapshot hashes, metadata, filenames, timestamps, and version-chain validation can detect corruption, but they do not authenticate writes by an equally privileged local process. Note contents are data, not executable instructions.

The tool does not propagate:

- Codex changes back to Claude;
- Claude-side deletions;
- source renames as Codex-side renames or retractions: a rename becomes a new source while the old source remains;
- automatic resolution of semantic conflicts between existing memories.

This is not an equivalent conversion between Claude and Codex native memory. Codex may summarize, rewrite, omit, or conflict with staged content during consolidation. Update notes carry previous/current snapshots and a superseded marker, which preserves lineage but may still add a small amount of noise.

Keep stable, mandatory project rules in `AGENTS.md` or repository documentation. Use memory as an auxiliary recall layer, not as the only source of truth.

## Exit codes

| Code | Meaning |
|---:|---|
| `0` | Success, including `staged`, `preview`, or `no_changes`. |
| `1` | Fatal parameter, path, encoding, size, history, lock, I/O, or internal error. For JSON output, inspect `partial_write` and `notes_written`. |
| `2` | Safety rejection caused by a hard secret or a sensitive filename not explicitly allowed; the batch writes nothing. Preflight rejection normally uses `status: "blocked"`; final generated-note scanning may use `status: "error"`. |

Windows batch example that preserves the exact exit code:

```bat
call sync-memory.cmd -ProjectPath "D:\src\app" -DryRun -OutputFormat Json
set "SYNC_CODE=%ERRORLEVEL%"
if "%SYNC_CODE%"=="2" echo Safety block: nothing was written.
if not "%SYNC_CODE%"=="0" if not "%SYNC_CODE%"=="2" echo Fatal error.
exit /b %SYNC_CODE%
```

For nontrivial automation, prefer parsing the JSON object.

Dry run performs no writes, but it still validates the source, ingress contract, safety rules, and existing history. It can therefore return `1` or `2`.

## Verification and tests

Run the complete black-box suite from the repository root:

```powershell
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass `
  -File .\tests\run-tests.ps1
```

Then preview the real local configuration without writing:

```powershell
.\sync-memory.cmd `
  -ProjectPath "C:\path\to\your-project" `
  -DryRun `
  -OutputFormat Json
```

`-DryRun` neither creates the notes directory nor writes staging notes, but it still validates the real target's `extensions\ad_hoc\instructions.md`. Do not point `CodexMemoriesRoot` at an empty temporary directory without that ingress contract; use the bundled black-box suite for isolated tests.

## Limitations

- This release supports Windows PowerShell 5.1 on Windows. PowerShell 7 and other operating systems are not tested targets.
- `extensions\ad_hoc\instructions.md` is a contract detected in the local Codex installation, not a publicly guaranteed stable API. If it is absent or a future Codex change invalidates it, the tool fails closed with exit code `1` and may need an update.
- Sync remains one-way and Codex consolidation remains asynchronous; immediate or guaranteed recall is out of scope.
- Credential detection is heuristic. Dry-run review and careful source hygiene remain required.

## Languages

English (`README.md`, authoritative) · 中文 ([`README_CN.md`](README_CN.md))

## License

[MIT](LICENSE)
