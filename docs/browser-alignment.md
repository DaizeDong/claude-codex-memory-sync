# Browser alignment and explicit hook entrypoints

`plan_config(claude_home, codex_home, enabled_plugin_roots,
adopt_playwright=True)` explicitly adopts the native `playwright` server's launch
policy. The default remains conservative for all native server collisions.

Adoption accepts a direct `npx @playwright/mcp` invocation or the canonical
`claude_home/scripts/pw-mcp-session.py` session launcher. The latter must use an
existing absolute Python interpreter, its companion CJS hook, `pw-auth.py` and
`pw_storage_paths.py`, and arguments of the form
`[launcher, "--", npx, "@playwright/mcp", ...]`. Other wrappers are rejected.
Both forms require the source to use `--isolated`, the complete `shared.json` under `PW_AUTH_CONFIG`,
and the output directory selected by `PW_MCP_OUTPUT_DIR`. Both settings come from
`claude_home/settings.json`'s `env` object and must be nonempty strings that expand to absolute paths;
the source launch arguments must resolve to those same locations. The shared file
must already exist. Planning checks its existence without opening it. Remote
connections, persistent profiles, external configuration files,
duplicate policy flags, and incomplete source policies are rejected.

Direct launch adoption encloses only `args` in a hashed `playwright-launch`
ownership block. Session launch adoption owns `command`, `args`, and
`startup_timeout_sec` together. Its startup budget is at least 120 seconds to
allow session preparation and MCP initialization; larger existing budgets are
preserved. It can upgrade intact older args-only or command-and-args blocks.
Native environment, working directory, enabled state, tool
allow/deny settings, tool timeout, package version, other arguments, other MCP
servers, and all unrelated TOML remain intact. Existing `--init-page` arguments
are passed through to the launcher, which owns runtime hook insertion. Native inline-table layouts that
cannot be edited safely produce a conflict. A missing args assignment is also
reported rather than guessed.

The caller must publish the returned bytes through the existing full-config
plan/apply transaction, including its destination snapshot, backup and rollback.
The planner does not write config or browser state. The ordinary sync path
recognizes the ownership block thereafter, without needing another opt-in.
Changes to owned bytes cause a conflict; changes to unrelated native settings
are preserved. Missing, disabled, or degraded source definitions preserve the
adopted native server and report the condition instead of deleting it. A source
change from the session launcher back to direct `npx` is reported as a conflict;
ordinary regeneration cannot silently remove the session lifecycle integration.

## Playwright-only plan and apply

`plan_playwright_config(...)` exposes the same launch transformer without
planning project-document fallbacks, other MCP servers, hooks, skills or memory.
`profile_bridge.browser.build_playwright_plan(...)` adds destination and source
snapshots and returns an ordinary one-file plan for `profile_sync.apply_plan`.
The source snapshot includes launch helpers and source-selection inputs, so
intervening source edits or newly appearing configuration files require a fresh plan.

Run the CLI from the authoritative bridge checkout with a Python environment
that has its declared dependencies installed:

```text
python -m profile_bridge.browser --adopt-playwright
python -m profile_bridge.browser --adopt-playwright --apply
```

The first command is a zero-write preview. The second uses the existing apply
lock, destination hash checks, rollback copy and cleanup. It does not invoke
full profile sync, health checks or memory import. `--adopt-playwright` explicitly
allows taking ownership of a native launch; subsequent runs can omit it.
Existing MCP processes require a reconnect before the changed launch takes effect.
An installed wheel must be updated through its normal release process to acquire
this planner; changing the checkout does not replace installed package files.

## Source helpers

The existing source `pw_isolation_guard.py` now exposes three explicit modes:

- `--check-codex-config <config.toml>` checks the direct launch policy and
  whether shared state equals the union computed by the existing `pw-auth.py`.
  This mode is read-only, prints a fixed error on failure, and exits nonzero.
- `--prepare-shared-state` calls the existing `absorb` and, when necessary,
  `merge`. It uses every store shard, including origins, and never constructs
  an empty replacement seed. With no pending exports and an up-to-date union,
  it does not rewrite state or create another backup.
- `--repair-isolation-only` prepares state and repairs Claude plugin definitions
  without invoking cache pruning. The existing PowerShell setup entrypoint is
  now a thin caller of this mode.

All three modes return before the legacy Claude cache-pruning branch. Codex must
never invoke the guard's default Claude maintenance mode. The prepare and repair
modes change state and must be called only by an authorized operator entrypoint.
The read-only check is suitable for a launcher that honors a nonzero result
**before** it starts the Playwright MCP process. No event timing assumption is
needed. Existing processes require a later reconnect to use changed arguments.

After any browser navigation, export the **entire context** into a uniquely
named file in the configured incoming directory, then call the existing `pw-auth.py absorb`
before the context closes. Never export over `shared.json`, select only one
site as the seed, or switch to a shared persistent profile. The export reminder
now inspects the configured output directory as well as legacy cwd artifacts.
Its shared timestamps are a reminder, not proof that a particular session saved
its state. Stop jobs run `absorb` before the reminder even if source groups are
reordered. Child output remains suppressed.

## Explicit checks and earlier hook integration

The original Stop-only integration used
`plan_hooks(..., plugin_roots=enabled_plugin_roots)` to retain the owned Stop
bridge and add `report["entrypoint_checks"]`. It does not register extra
events. That review found the local app-server schema listed `SessionStart` and
`PostToolUse`, but does not define their stdin payloads or prove the additional
context response shape. Event names alone are insufficient to register the
Claude adapters as equivalent native behavior. The subsequent exact-release
implementation and its remaining invocation limits are documented in
[native hook alignment](native-hooks-alignment.md).

`plan_entrypoint_checks(claude_home, codex_home, plugin_roots=())` also exposes
these descriptors independently. They are observations, not automatic checks.
They include source hashes and distinguish missing upgraded guard modes.

`run_entrypoint_check(claude_home, codex_home, "pw_isolation_guard.py")` reuses
the reviewed source's read-only check and returns a fixed `passed`, `failed`, or
`unavailable` status. A launcher must refuse browser startup on either of the
latter statuses. Run the explicit state preparation entrypoint beforehand when
needed; the check never silently repairs state.

`run_entrypoint_check(claude_home, codex_home, "doc_budget.py",
file_path=absolute_path)` passes an explicit `tool_input.file_path` to the
existing `scripts/doc-budget/doc_budget.py`. It suppresses original output and
returns fixed document-budget feedback. The caller must invoke it after a known
file edit. The source's Claude document globs and watermark storage remain in
effect; it does **not** implement a Codex-memory budget policy or infer changed
paths from an unverified tool payload.

For an enabled superpowers plugin, the descriptor identifies its existing
`skills/using-superpowers/SKILL.md` with a hash. The integrator can load that
entrypoint through the existing instruction/skill mechanism. No new model call,
provider CLI, or duplicate implementation of the startup script is introduced.
Automatic context injection remains unverified.
