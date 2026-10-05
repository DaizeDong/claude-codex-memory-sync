# llmcall overlay integration

`profile_sync.build_plan` consumes one catalog snapshot and accepts explicit
`runtime_policy`, `capabilities`, `selection_requests`, `resource_roots`, and
`role_equivalence` inputs. The CLI exposes the four file inputs as
`--runtime-policy`, `--capability-snapshot`, `--overlay-resource-roots`, and
`--role-equivalence`. It reads them; it does not initialize or guess private
bindings. Missing policy/capabilities, ambiguous selection, and unsupported
sources remain visible in `runtime_selection`. Source presence is not capability
evidence. The existing private selection policy remains the only policy registry.

The planner calls `skill_smith.conflicts.select` for caller requests and explicit
entry overrides. Each available primary, fallback and format specialist receives
a distinct adapter destination; explicit user selection precedes task/format and
tier preferences. Native loader ordering is not modified or claimed. Existing
user installations remain intact. Runtime discovery and the final choice of
exposed primary names require separate deployment acceptance.

SMITH owns structural conversion. SYNC renders `SKILL.md`, `workflow.json`, and
`payload/<resource-relative-path>` in the existing skill ownership group. It uses
the bridge verifier for all original member bytes, the existing destination
baseline, locks, apply transaction, backup and rollback. Every generated member
has source identity and a content hash. Source file, referenced file sets,
resource hashes, descriptor hash and transform version are checked under the
apply lock, including when a caller passes an ordinary list of plan rows.
No second parser, catalog, writer, registry or installer is introduced.

The installed `profile_bridge.workflows.load_descriptor` checks the source pin
before use. Roles call `skill_smith.role_entrypoints.invoke`. Stateful workflows
use `profile_bridge.workflows.DurableWorkflow`. Both receive inherited session
options, exact user choices and llmcall ExecutionRequirements. All nested model
and agent work follows llmcall; normal local skill tools remain local operations.
The new modules are included by the existing `profile_bridge*` package discovery.
Generated content never imports a checkout or RUN directory.

Seven official unrestricted templates are eligible for migration. Native
registrations retire only when the entire replacement bundle can be planned and
the caller supplies an observation keyed by `source_id + ':' + relative_path`
with `status='verified'`, the exact `artifact_hash`, and nonempty `evidence`.
Existing bridge ownership and config alias checks still govern deletion. User
roles are preserved. Missing equivalence preserves existing native registrations
and prevents adding a new native registration for these known official roles.
The three restricted roles keep their restrictions; unverified hard enforcement
remains blocked. An unresolved source resource is independently unsupported.

The exact reviewed research-review and auto-review-loop revisions use explicit
llmcall semantics for all former routes. Their prompts, full round context,
reviewer memory, debate, independent adversarial review, stop criteria, human
checkpoints and output requirements are retained. Fixed source model/provider
constants are provenance. The tracing resource receives its own exact-revision
conversion. Ordinary source resource bytes remain unchanged upstream. Unknown
revisions require review instead of broad text replacement.

`DurableWorkflow(codex, skills, workflow_id, descriptor, inherited=...)` requires
explicit caller roots and a workflow ID. `run(request_id, context=..., operation=
'start'|'reply'|'poll', prompt=..., inputs=..., producer=...)` is a single turn;
the authorized caller controls rounds, output publication and optional actions.
The actual producer Result and provider-reported reviewer family establish
independence through llmcall avoid checks. A fresh repository reviewer uses agent
mode and must request enforced read-only execution. Prompt instructions alone do
not satisfy that requirement. Cancellation and the remaining timeout are passed
to llmcall. Project output actions are not implicit effects of a judge call.

Private history lives in `<codex>/claude-sync/workflows/<workflow-id-hash>/`.
Existing profile locks and fleet no-replace primitives protect immutable request
and result records. Each result records the complete transcript, requirements,
model intent, producer/reviewer evidence, effects and preceding receipt hash.
A repeated request ID returns its saved result; different input with the same ID
fails. An interrupted request or possibly side-effectful failed result remains
uncertain and blocks new work in that workflow. There is no automatic retry,
global orchestration service, SQLite ledger or native memory write. If private
durable storage is disallowed, this compatibility mode is unsupported.

The recovered skill-codex 1.1.0 uses `run_cli(request_id, argv, prompt, ...)`.
Only parsed intent is translated; no provider CLI executes. Model and reasoning
effort options, working directory and read-only/workspace-write sandbox intent
map to llmcall contracts. `--skip-git-repo-check` records caller preflight intent.
`exec resume --last` means continuation of the explicit local workflow ID with
full prior text and inherited options. Native session IDs, approval modes,
`--full-auto`, and `danger-full-access` are explicitly unsupported. Resume cannot
widen restrictions already attached to a context. It may start a separately
authorized context instead. Environment and cancellation objects are supplied
anew by the caller and are not persisted as credentials.

This implementation does not certify installed package versions, provider hard
permissions, native discovery, runtime primary-name uniqueness, or real model
execution. Those require the root integration's separate runtime evidence.

Explicit runtime files are decoded once as UTF-8 with an optional BOM. Both
CLIs, parsed `run(...)` inputs, and public planning validate document shapes
before discovery or destination locking. Omit optional arguments to retain the
uninitialized behavior; explicit JSON null or parsed `None` is malformed.
Policy schema 1 requires an entries array with unique rule IDs and typed exact
selectors. Capability observations retain unknown/unverified statuses; missing
evidence cannot grant support. Resource roots map source IDs to path strings.
Role receipts contain status, artifact_hash, and an evidence string array;
an empty receipt map is valid. Duplicate JSON object keys are rejected.
Input validation failures return sanitized type-only errors and preserve status files.

A legacy plugin command/role adapter sidecar can acquire catalog identity only
after complete original ownership verification and one exact importable catalog
match. The seven legacy fields, provider registry key/root, entrypoint kind and
relative path, source bytes, original target hash, and complete legacy adapter
template must agree. Partial identities, changed/retired artifacts, unavailable
providers, marketplace transfers and ambiguous entries cannot authorize this
conversion. The ordinary managed-artifacts transaction publishes the identity;
the adapter's original bytes/hash remain unchanged unless entering a verified
ready overlay. Apply rechecks the original ownership group, source bytes and
provider selection files. This conversion repairs no native memory history.
