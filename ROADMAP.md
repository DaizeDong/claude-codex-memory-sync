# Roadmap

Current: **v1.0.0**

## v1.0.0 (current)

Feature names only. Why each one behaves the way it does lives in `README.md`, and what
changed lives in `CHANGELOG.md`.

- Full profile sync through `sync-all.cmd`: skills, instruction adapters and native
  roles, the managed `AGENTS.md` block, a project-document fallback, compatible MCP
  definitions, project memory archives, and a hooks inventory.
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

## Planned

- **A tested target beyond Windows PowerShell 5.1.** The memory-only bridge is written
  for it and is tested only there. PowerShell 7 and non-Windows hosts are neither
  supported nor measured, which is a claim about testing rather than about the language.
- **A second machine.** Skills are linked and adapted scripts point at their original
  installation, so the result is a same-machine bridge. Making it portable means deciding
  what a copy of a link should even mean.
- **Hooks beyond the two reviewed adapters.** Everything else in the Claude hooks
  inventory is reported as needing protocol review, which is honest and is also the
  largest untranslated surface.
- **A remote MCP server that is actually probed.** Local HTTP servers are initialized
  without invoking tools, stdio commands are checked for presence only, and a remote
  server is marked `not_checked` rather than pretended about.
- **Credential detection stronger than a heuristic.** The current checks block a batch on
  likely private keys and access tokens. They are not a complete secret scanner, and the
  dry-run review still carries part of the load.
