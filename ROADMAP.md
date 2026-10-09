# Roadmap

Current: Python profile package **0.3.0**.

Original PowerShell converter release: **v1.0.0**.

## Current capabilities

Operating behavior and rationale are in [README.md](README.md); release history
is in [CHANGELOG.md](CHANGELOG.md).

- Full profile sync through `sync-all.cmd`: skills, instruction adapters and native
  roles, the managed `AGENTS.md` block, a project-document fallback, compatible MCP
  definitions, project memory archives, and reviewed SessionStart, PostToolUse
  and Stop hook adapters with unsupported commands reported.
- Three modes on every run: preview, JSON preview, and apply behind a local backup.
- Backup and rollback per applying run, restoring only the files and links whose current
  state still matches what that run wrote.
- Retirement of artifacts this bridge owns when a plugin is disabled or a source item
  disappears, with user edits preserved.
- An inventory and link-repair path for uniquely identified legacy links from approved
  repositories, ambiguous candidates left untouched.
- `run_profile_sync.py` as a deterministic entry point for an existing scheduler, with a
  status file, a success marker published only after verification, and an exit code that
  separates deliberate exclusions from faults.
- The memory-only bridge `sync-memory.cmd` for a single project: direct `*.md` selection,
  sensitive-filename and hard-credential checks that block the whole batch, bounded
  inputs, incremental deduplication against existing notes, and atomic note publication
  under a destination-derived mutex.
- Black-box tests for both entry points, all of them building their trees from synthetic
  data in temporary directories.
- Native Windows 8.3 aliases for hook and custom plugin paths, with idempotent
  planning and unchanged plugin-local and destination-link protections.

## Planned

- **Additional tested platforms.** The memory-only bridge is tested on Windows
  PowerShell 5.1. PowerShell 7 and non-Windows hosts are not supported or measured.
- **Cross-machine portability.** Linked skills and adapted scripts currently
  depend on the source installation; portability needs explicit copying and
  dependency rules.
- **Additional reviewed hook adapters.** Commands outside the reviewed
  SessionStart, PostToolUse and Stop adapters require protocol review. Native
  discovery, trust and actual invocation remain separate acceptance checks.
- **Remote MCP probing.** Local HTTP servers are initialized without tool calls,
  stdio commands are checked for presence, and remote servers remain `not_checked`.
- **Stronger credential detection.** Current private-key and access-token checks
  are heuristic; input hygiene and dry-run review remain required.
