# Changelog

All notable changes to this project are documented here (Keep a Changelog style).

## [Unreleased]

### Added

- Full profile sync through `sync-all.cmd`, `run_profile_sync.py` and `profile_*`:
  skills, command and agent Markdown, global instructions, project documents,
  compatible MCP definitions, memory archives and hook inventory. Preview and
  JSON preview precede apply; each changing apply has backup and rollback,
  retirement is ownership-checked, and scheduler success is recorded only after
  verification. Tests use synthetic temporary trees.

### Changed

- An applying run no longer keeps its rollback copy under `claude-sync/backups/`.
  The copy is removed when the run succeeds and when a failed run has finished
  rolling back; only a run that stopped before cleanup (or whose rollback failed)
  leaves it, and `--rollback` removes it once restored. Successful runs used to
  leave one directory per run, which accumulated without bound. Retired memory
  copies are no longer retained after the run; their Claude source is the record.

### Fixed

- Cross-home remapping re-hashes intact ownership markers in every artifact file,
  not only `AGENTS.md`, `SKILL.md` and `*.toml`. Verification already checks the
  markers of routed alternatives (`alternatives/<name>/ENTRYPOINT.md`), so a
  home mapping that rewrote their bodies left a stale adapter hash and refused
  the whole artifact group. Markers that did not verify are still not repaired.
- Public tests use generated llmcall 0.3.1 contracts instead of installing an
  unrelated same-named PyPI package. An opt-in codec test requires a reviewed
  0.3.1 wheel and matching installation provenance; CI reports `NOT_RUN` when
  disabled.
- Inventory requires `$CLAUDE_CONFIG_REPO` or `--external-skill-manifest` for
  external repository metadata. Removing the hard-coded sibling fallback avoids
  authorizing unapproved link recovery and publishing a machine-specific path;
  the PII gate identified the original path before publication.

### Changed

- Organize bilingual documentation around the managed and memory-only entrypoints,
  their design rationale and operational limits; add `ROADMAP.md` and `CHANGELOG.md`
  under Skill Repo Spec v1, with a localized URL-encoded Chinese language badge.
  Documentation changes do not change either entrypoint or its functional version.
- [Spec adaptation notes](docs/2026-09-22-spec-adaptation.md) retain the deliberate
  exceptions for standalone sync scripts: no plugin manifest, `SKILL.md` or L0/L1
  layer, invented `CONTRIBUTING.md`, or five inapplicable fingerprint topics;
  section order keeps the two entrypoints distinct.

## [0.3.0] - 2026-10-07, Python package

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

## [0.2.1] - 2026-10-05, Python package

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

## [1.0.0] - 2026-07-19

### Added

- Initial public release: a one-way Windows PowerShell 5.1 converter that reads a Claude
  Code project's auto-memory Markdown, applies path, encoding and credential checks, and
  stages eligible content as Codex `ad_hoc` notes. Dry run, JSON output, bounded inputs,
  all-or-nothing safety blocks, and incremental deduplication against notes already
  staged for the same project.
