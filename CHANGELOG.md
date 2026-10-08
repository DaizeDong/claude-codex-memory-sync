# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## Python package [0.3.0] - 2026-10-07

### Changed

- Pin fleet-guards 0.2.1 so the shared runtime can install alongside llmcall's
  canonical packaged companion resolver. Credential and filesystem APIs retain
  their existing contracts.
- Preserve llmcall recording failures when restoring durable results, including
  successful provider responses whose private ledger write was refused.
- Refresh the development Smith pin to its accepted storage and configuration
  admission implementation.
- Durable workflows run on the llmcall 0.3.0 call contract through skill-smith 0.2.0.
  Requirements and inherited options are stored as plain mappings (encoding version 3),
  failures are falsy Results whose `error` names the reason, and a failed agent call
  that may have started a client is still recorded as uncertain.
- Version 2 histories written with llmcall 0.2.0 types stay readable: idempotent
  retrieval compares requests by meaning, legacy Results keep retired fields as
  `legacy_fields`, a provider-reported model family becomes the answering group, and
  an exact ModelSelection continues as an exact model.
- A workspace other than the process cwd, a differing environment, a set cancellation
  token and requirements llmcall cannot enforce fail closed before dispatch.
- A continuation resumed from a directory other than its anchored workspace fails
  with `workspace_requires_process_cwd` instead of running in the anchor, which
  llmcall 0.2.0 received per call. CI installs skill-smith at fe38b760 (0.2.0 with
  MCP isolation for read_only requirements).

## Python package [0.2.1] - 2026-10-05

### Fixed

- Windows 8.3 home aliases now retain consistent hook destination keys across
  planning and applying. Generated commands use resolved paths so switching between
  the short and long spelling does not rewrite hooks.
- Pin skill-smith 0.1.6 for custom plugin paths and native working-directory checks.
  Plugin-local boundaries and destination junction rejection remain enforced.
- Exercise real Windows aliases through plan, apply and replan, including preserved
  native hooks and excluded outside-plugin agents. Legacy link fixtures use the
  resolved source spelling written by the original producer.
- Budget regression assertions tolerate floating-point roundoff while retaining
  the check that consecutive observations consume the same deadline.

Python package versions are independent of the original PowerShell converter's
v1.0.0 release below.

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
