# Native hook alignment for Codex 0.154.0

The profile planner now returns one owned bridge and a v2 manifest covering
SessionStart, PostToolUse and Stop. The existing profile transaction applies
those bytes with its normal snapshot, compare-before-write, backup and rollback
behavior. There is no second registry or hook writer. Native handlers remain
in their original event groups. Edited owned handlers, bridges or manifests
cause a conflict; they are not overwritten.

V1 Stop manifests remain valid input, including older manifests without a
manifest checksum. An intact v1 group upgrades to v2 on the next plan. V2 hashes
each owned event handler and the bridge, and requires its manifest checksum.
Restore and redaction validate the original dependency group before rehashing.
Windows encoded launch commands are relocated along with their plain paths.

## Source entrypoints

SessionStart runs the declared source `pw_isolation_guard.py` only with
`--check-codex-config` and the absolute Codex config path. This is the source's
read-only mode. It neither absorbs browser state nor prunes the Claude plugin
cache. The source script and its adjacent `pw-auth.py` dependency are pinned by
hash. Installing a changed source requires a fresh profile sync.

Its `hookSpecificOutput.additionalContext` reminds the agent to keep Playwright
isolated, use the full shared union, export the entire browser context to a
unique incoming file, and run the existing absorb entrypoint before closing.
It also points to the enabled superpowers plugin's existing
`skills/using-superpowers/SKILL.md`. The source text is not copied into the hook
output. Platform-specific agent instructions must be adapted to the installed
`llmcall` interface and current user instructions; no provider CLI or native
spawn example becomes executable authority.

PostToolUse matches the canonical `apply_patch` name. The exact release's core
handler puts the raw patch under `tool_input.command`; `Edit` and `Write` are
matcher aliases, not the tool name in stdin. The adapter resolves local Add and
Update paths against the supplied absolute cwd, uses move destinations,
deduplicates paths, and omits deleted documents. Each path goes to the existing
`doc_budget.py` as `tool_input.file_path` using JSON stdin and shell-free argv.
The source's existing document globs, watermark and limits remain authoritative.
No Codex memory limits are added.

The exact-release `ApplyPatchToolOutput::post_tool_use_response` returns a JSON
string, and the core registry dispatches PostToolUse only after tool success.
The adapter checks that string shape and derives file destinations from the
successful patch input. It does not scrape human-readable output for paths or
accept guessed object response schemas. Shell-intercepted patches, remote
environment selection, wrappers and other tools remain outside this adapter.

Stop retains the reviewed absorb-before-export-reminder order. All subprocess
stdout, stderr and exception details are suppressed. Fixed failures and document
feedback become nonblocking Codex messages; startup and edit feedback also reach
the model through the event's additionalContext field.

## Native configuration and launch boundary

In **hooks.json**, the timeout key is **`timeout`**. The app-server output DTO
names that field `timeoutSec`. A no-model probe of the installed 0.154.0 binary
confirmed that `timeout: 11` was reported as `timeoutSec: 11`, while input keys
`timeoutSec` and `timeout_sec` were ignored and reported the 600-second default.
The official HookHandlerConfig also explicitly renames timeout_sec to timeout.
Do not derive the input file format from the app-server output schema.

The synthetic app-server probe recognizes the final generated SessionStart
handler and its configured timeout. Following the official TUI procedure, the
probe saved that synthetic handler's exact key/currentHash through
`config/batchWrite` at `hooks.state` and verified a trusted status. `thread/start`
completed, but the source handler still did not execute. Discovery and trust do
not prove invocation. The probe made no model turn, configured no MCP server,
and did not bypass native trust. Production invocation must be checked by the
root integrator after applying the new build. No production profile was changed.

SessionStart timing does not guarantee a check before MCP bootstrap. Exact
Playwright launch arguments enforce isolation; existing MCP sessions must
reconnect to receive changed arguments. Shared-state preparation is a separate
explicit call to the source `--prepare-shared-state` mode.

## Root integration

Pass enabled plugin roots to `plan_hooks`, apply all returned bytes through the
existing profile transaction, and retain the explicit `features.hooks` handling.
The new `profile_bridge.hook_runtime` helper is included by the existing
`profile_bridge*` package discovery; no package-list expansion is needed.
Rebuild before applying so the bridge embeds this runtime rather than an older
installed copy. Update CLI inventory and limitation prose that still says only
Stop is adapted. Do not report hooks as active when the feature is disabled or
native execution has not been observed.

Deploy the reviewed source guard and sibling auth helper first, then prepare
the full shared union through the source mode. After profile apply, verify
native discovery, configured timeout, trust and actual startup invocation.
Reconnect existing MCP sessions. Production browser checks remain root work.
