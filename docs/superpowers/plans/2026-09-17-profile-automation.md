# Profile automation implementation plan

> Execute the approved integration in this session. Use llmcall for all model and external agent calls. The operator has approved implementation, local installation and verification.

**Goal:** Keep the local Claude and Codex profiles aligned, visible in the existing task console, and recoverable through the existing configuration backup.

**Architecture:** Deterministic Python owns profile planning, managed removal, inventory and health. Windows Task Scheduler owns triggers; the existing task gate, hidden launcher, health monitor and console own operations. The existing private configuration backup captures, restores and reconciles Codex. Any agent invocation goes through `llmcall.call(..., mode="agent")`, inheriting its routing policy.

## 1. Managed lifecycle and inventory

- Extend managed ownership to removal of intact generated skills, MCP blocks and hooks. Preserve user modifications and distinguish source unavailability from explicit disablement.
- Inventory skill entry points, links, source repositories and dependency declarations. Recover legacy broken links only when an authoritative local source is unique. Unknown sources stay visible and untouched.
- Exercise removal, rollback, edits, unavailable sources and ambiguous recovery with synthetic temporary profiles.

## 2. Native agent roles

- Translate supported local agent instructions into Codex native agent definitions using the installed runtime schema.
- Preserve model/provider defaults and manual definitions. Track hashes and source identities for incremental updates and safe retirement.
- Verify generated TOML parsing, role selection and protected edits; document unsupported tool constraints.

## 3. Scheduled sync and health

- Add a runner that applies a profile plan, verifies a fresh preview and emits sanitized structured health.
- Classify known compatibility exclusions separately from new failures. A fresh log or process exit alone cannot claim health.
- Publish success only after verification; invalidate success on failed runs. Support deterministic negative tests without network or live profiles.
- Register one hourly task through the existing task gate and hidden-launcher helper, with finite timeouts, retry, battery and missed-run settings.
- Register the same task in the backup allowlist, monitor and console category map; verify scheduler readback and registry checker.

## 4. Backup and restore

- Extend the private backup's capture, restore and drift checks together. Capture durable configuration, instructions, agents, memory and skills by explicit policy; exclude credentials, sessions and caches.
- Preserve links as source manifests, remap home paths during restore and report missing prerequisites.
- Route backup curation through llmcall while preserving deterministic backup and memory fallback behavior.
- Test round trips, drift detection, secret exclusions, path traversal and overwrite protection with temporary synthetic homes.

## 5. Integration and validation

- Run the relevant Python suites and original PowerShell tests. Obtain a review through llmcall and resolve material findings.
- Apply the profile with its existing rollback backup; run again to verify convergence.
- Capture Codex locally into the private backup and verify the new drift check. Do not run the orchestration that sends messages or pushes commits during testing.
- Verify scheduled task metadata and console visibility. Record runtime evidence outside the public tool repository.

## Execution rulings

- Continue in named branches in the existing checkouts: the profile bridge is already an uncommitted implementation installed from this checkout, so copying it into a second worktree would split the live source.
- Keep prior uncommitted work. Do not commit, push or send messages as part of validation.
- Use llmcall for delegated implementation and review as well as any shipped model call; do not create a parallel provider ladder.
