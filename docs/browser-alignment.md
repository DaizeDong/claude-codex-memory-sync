# Browser alignment and explicit hook entrypoints

`plan_config(claude_home, codex_home, enabled_plugin_roots,
adopt_playwright=True)` explicitly adopts the native `playwright` server's launch
policy. The default remains conservative for all native server collisions.

Adoption accepts a direct `npx @playwright/mcp` invocation. It requires the
source to use `--isolated`, the complete `claude_home.parent/.pw-auth/shared.json`
seed, and `claude_home.parent/.playwright-mcp-output`. The shared file must
already exist. Planning checks its existence without opening it. Wrappers,
remote connections, persistent profiles, external configuration files,
duplicate policy flags, and incomplete source policies are rejected.

Only the native `args` assignment is enclosed in a hashed `playwright-launch`
ownership block. Native environment, working directory, enabled state, tool
allow/deny settings, timeouts, package version, other arguments, other MCP
servers, and all unrelated TOML remain intact. Native inline-table layouts that
cannot be edited safely produce a conflict. A missing args assignment is also
reported rather than guessed.

The caller must publish the returned bytes through the existing full-config
plan/apply transaction, including its destination snapshot, backup and rollback.
The planner does not write config or browser state. The ordinary sync path
recognizes the ownership block thereafter, without needing another opt-in.
Changes to owned bytes cause a conflict; changes to unrelated native settings
are preserved. Missing, disabled, or degraded source definitions preserve the
adopted native server and report the condition instead of deleting it.

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

## Hook protocol and integration

`plan_hooks(..., plugin_roots=enabled_plugin_roots)` retains the existing owned
Stop bridge and adds `report["entrypoint_checks"]`. It does not register extra
events. The available local app-server schema lists `SessionStart` and
`PostToolUse`, but does not define their stdin payloads or prove the additional
context response shape. Event names alone are insufficient to register the
Claude adapters as equivalent native behavior.

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
