# 2026-09-22: applying the house repository spec to a pair of sync scripts

This repository was brought in line with Skill Repo Spec v1, which was written for Claude
Code skill repositories. This one is not a skill. It is two standalone entry points, a
Windows PowerShell 5.1 memory bridge and a Python full profile sync, that a person or a
scheduler runs directly. Most of the spec applies as written. A few requirements are
adapted, and a few are refused outright, because meeting them would mean claiming
something untrue.

This file is the record of those decisions, so the next person auditing this repository
against the spec finds a reasoned position rather than silence, and can argue with it. It
is dated evidence, not a rule: the rules live in `README.md`, the version history in
`CHANGELOG.md`.

## Refused, because meeting them would be a false claim

**No `.claude-plugin/plugin.json`.** Chapter 1 makes it mandatory so that a repository
stays installable with `/plugin install`. There is nothing here for that command to
install: no `SKILL.md`, no skill entrypoint, no agent behaviour at all. A manifest would
be advertising rather than metadata. The two entry points are `sync-all.cmd` and
`sync-memory.cmd`, and a user runs them from a clone.

**No `Claude Code Skill` badge.** Chapter 3 fixes the first badge as the type marker. The
slot is kept, because a reader should learn what kind of thing this is from the first
line of badges, but its content is the existing `Windows PowerShell 5.1` badge, which is
both true and the single fact that decides whether this repository is usable at all.

**No `claude-plugin`, `claude-skill` or `skill` topic.** Chapter 6 calls the base nine an
identity fingerprint every repository carries. Three of the nine are false here: nothing
in this repository is a plugin or an Agent Skill, and `skill` on GitHub reads as "Agent
Skill". Putting them on would pollute the search results for the repositories where they
are true. `llm` is refused for a different reason: the transform and the health checks
make no model call, and the README says so, so the topic would promise a model where
there is none. The remaining five are honest: `claude-code` is already set, and `claude`,
`ai`, `ai-agent` and `agent` are true of a bridge between two coding agents. Changing the
topic list is a repository setting rather than a commit, so it is recorded here rather
than performed by this change.

**No `SKILL.md`, and no L0 or L1 layer.** Chapter 11's first two layers are the
frontmatter description and the per-invocation preamble, both of which exist because a
skill pays for them on every turn. A command-line tool is executed, not invoked into a
context window, so there is nothing to pay and nothing to budget. L2 through L5 apply
unchanged: the two READMEs are the tour, and `ROADMAP.md` plus `CHANGELOG.md` are the
only places a version number appears in prose.

**No load-budget workflow.** `style/ci/load-budget` measures what a `SKILL.md` costs to
load. With no `SKILL.md` it would report that there is nothing to measure, on every
commit, forever. A check that cannot fail is worse than no check, because it teaches
people that green means something. The repository already dropped its vendored copy for
this reason in `0d8d965`.

**No `CONTRIBUTING.md` invented for the sake of a link.** Appendix A ends the README with
a line that points at one. The closing section points at `ROADMAP.md`, `CHANGELOG.md` and
`LICENSE`, all of which exist, and names `.github/workflows/` as the gates a change has
to pass, which is the true answer to the question `CONTRIBUTING.md` would have answered.
A file is written when there is guidance that is not already in the README.

## Adapted, because the intent survives and the letter does not

**Section order.** Chapter 4 fixes eleven sections, philosophy first. The philosophy now
comes first, stated once for the whole repository instead of once for the older entry
point, and `Languages` and the closing section are in their prescribed places at the end.
Between them the README keeps its own shape, because it documents two entry points with
different selection rules, different outputs and different credential behaviour, and the
spec's single `Install` and `Quick start` pair assumes one. Flattening them into one
sequence would force a reader to hold both tools in mind at once in order to follow
either. `Skills at a glance`, `How to invoke` and `Example output` are skipped: there are
no skills, no trigger phrases, and the output is a JSON object whose full schema the
README already lists field by field.

**Version consistency.** Chapter 7 asks for four copies of the version kept in step.
There is no `plugin.json`, so there are three. The one literal is
`$script:ToolVersion = '1.0.0'` in `sync-claude-memory-to-codex.ps1`, and it is what the
Roadmap badge, the `ROADMAP.md` heading and the newest released `CHANGELOG.md` entry now
carry. The `"version": 1` that the profile sync reports is a report-schema version and is
deliberately not the same number; it is not a second copy of this one. Nothing mechanical
holds the three prose copies to the literal, which is a real gap, smaller than the gap of
having no version statement at all.

**The unreleased work is listed as unreleased.** The full profile sync is new and is not
in any released version, so `CHANGELOG.md` puts it under `[Unreleased]` rather than
bumping `1.0.0` to a number that the code does not declare. The literal moves when a
release does.

## What was already in place, and was left alone

Chapters 8, 9 and 10 needed nothing. The PII gate, the data boundary and the dash gate
all arrived through the `guards` and `style` submodules, all three have a workflow in
`.github/workflows/`, and `.pii-allow` and `.dataclass.json` both carry arguments rather
than defaults: the allowlist has one entry with a written reason, and the data class
declares four sealed paths plus an argued reason for an empty `fixture` list.

## What was measured, and where

| Claim | How it was checked |
| --- | --- |
| The guards are armed, not merely present | `python guards/tools/pii_guard.py --tree` printed `clean (tree) [15 file(s) scanned, 2 skipped]`, and `python guards/tools/data_boundary.py` printed `clean (4 DATA + 0 sealed paths not tracked, 0 FIXTUREs generator-reproducible, 17 tracked files carry no real-run shape)`, both exit 0 |
| The style gate runs and sees the prose | `python style/tools/dash_guard.py --tree` printed `clean (2 file(s) examined)`, which is the two READMEs, the only tracked Markdown before this change |
| The dash-guard workflow is wired to the submodule, not to a copy | `.github/workflows/dash-guard.yml` checks out with `submodules: true` and calls `./style/ci/dash-guard` |
| The declared version | `grep` over the tree finds exactly one tool-version literal, `$script:ToolVersion = '1.0.0'`; the other `version` fields are report schemas fixed at `1` |
| The initial release date | `git log --reverse` dates `030074e Initial public release` at 2026-07-19 |
