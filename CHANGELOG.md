# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [Unreleased]

### Added

- **Full profile sync (`sync-all.cmd`, `run_profile_sync.py` and the `profile_*` modules).**
  The repository began as a memory-only bridge, and memory was only one of the things
  the two agents keep in separate places. The new entry point carries skills, command
  and agent Markdown, the global instruction block, project documents, compatible MCP
  definitions, project memory archives and a hooks inventory, in one pass, with preview
  and JSON preview modes, a per-run backup with rollback, retirement of artifacts this
  bridge owns, and a scheduler-facing runner whose success marker is published only
  after verification passes. Every test builds its trees from synthetic data in a
  temporary directory.

### Fixed

- **The inventory no longer guesses where a configuration repository lives.** When no
  manifest was named, `approved_roots` fell back to a hard-coded sibling directory. A
  guess that happens to hit authorizes link recovery from a directory nobody approved,
  and a guess that misses is a path in a public repository that describes one machine.
  The manifest is now named by `$CLAUDE_CONFIG_REPO` or `--external-skill-manifest`, or
  there is no manifest. The PII gate is what caught it, on the commit that would have
  published it.

### Changed

- **docs: unify repo structure (Skill Repo Spec v1).** The design philosophy moves to the
  top of both READMEs and is stated once for the whole repository rather than once for
  the older entry point, the Chinese language badge is localised and url-encoded, and
  the mandatory `ROADMAP.md` and `CHANGELOG.md` are added. No functional version bump:
  nothing about either entry point changed for this.

  Several requirements of that spec are deliberately not met, each because meeting it
  would claim something untrue of a pair of standalone sync scripts, and each is argued
  in `docs/2026-09-22-spec-adaptation.md` rather than left as a silent omission: no
  `.claude-plugin/plugin.json`, no `SKILL.md` and therefore no L0 or L1 documentation
  layer, no `CONTRIBUTING.md` invented for the sake of a link, a section order that
  keeps the two entry points as two halves, and five of the nine fingerprint topics
  refused.

## [1.0.0] - 2026-07-19

### Added

- Initial public release: a one-way Windows PowerShell 5.1 converter that reads a Claude
  Code project's auto-memory Markdown, applies path, encoding and credential checks, and
  stages eligible content as Codex `ad_hoc` notes. Dry run, JSON output, bounded inputs,
  all-or-nothing safety blocks, and incremental deduplication against notes already
  staged for the same project.
