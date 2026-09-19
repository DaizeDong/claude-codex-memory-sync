# Memory authorization and durable outbox (schema 1)

The Python profile bridge archives by default. T09 embeds canonical source history
in this outbox; the PowerShell entry is now a shell over the same Python publisher.
See [canonical history and restoration](MEMORY_CANONICAL.md) for per-file ingress,
legacy aliases, and the explicit cross-home migration API. Earlier schema-1
records remain readable and retain their original publication evidence.

## Public entry points

`profile_memory.plan_memory(claude_home, codex_home, *, request_id=None,
scope=None, periodic=False, reviewed_archive=(), reviewed_scopes=())` returns a dictionary-compatible `MemoryPlan` and a
metadata-only report. Planning never writes directories, grants, history, notes,
locks, or status files. `apply_memory_plan(plan, claude_home, codex_home,
skills=None)` applies the returned object through the existing profile backup and
CAS transaction under profile resource locks. [Archive hygiene](MEMORY_ARCHIVE_HYGIENE.md)
defines reviewed decisions and the required retirement apply/rollback hooks.

`profile_sync.build_plan` and `run_profile_sync.run` accept the same keyword
arguments. Apply the original returned plan object through `apply_plan`; its
private transaction intent is not part of the JSON report or a serializable write
API. The dictionary entries for native notes in a memory preview are informational:
only the outbox publisher may create those files.

Both Python CLIs accept `--request-id ID --scope PROJECT_KEY` (repeat `--scope`
for multiple keys), or `--scope '*'` for all projects in the selected source root.
`--periodic` explicitly persists a recurring grant when applied. Previewing these
flags does not persist authorization. No flags means archive-only unless one
active persisted periodic grant matches that source root. Multiple active
periodic grants for a root require an explicit request selection.

A one-shot request binds to one increment. Replaying it cannot select a changed
snapshot. A different request selecting the same increment records a binding and
deduplicates delivery. Periodic requests may select successive increments. A
cancelled or revoked request ID cannot be reactivated by rerunning CLI flags.

## Authorization record

Private runtime path: `<codex_home>/claude-sync/memory-authorizations.json`.
It is independent of the source archive and the ingress instructions file.
The optional top-level boolean `history_initialized` defaults to false for a new
grant document and is set to true before first publication. It is an initialization
witness, not authorization. Preserve it even if all grants are revoked: it prevents
a missing outbox and index from being mistaken for a new installation.

```json
{
  "version": 1,
  "grants": [
    {
      "request_id": "approved-periodic-example",
      "source_root": "c:\\example\\claude\\projects",
      "scope": ["project-one"],
      "periodic": true,
      "state": "active"
    }
  ]
}
```

The example is synthetic. `source_root` is the absolute, platform-normalized
Claude `projects` root, checked with the shared safe-path validator. Scope is a
sorted, unique list of exact directory keys, never decoded into guessed project
paths. `['*']` cannot be mixed with named keys. Named keys must be present in the
selected source snapshot. Changing content in an unselected project does not
create a scoped increment. Grants match both root and scope; merely finding
`memories/extensions/ad_hoc/instructions.md` never creates a grant.

The controller can migrate an already approved periodic authorization by calling:

```python
from profile_bridge.memory_outbox import authorize, revoke

authorize(codex_home, claude_home / "projects",
          request_id="approved-periodic-example", scope=["project-one"],
          periodic=True, skills=shared_skills)
# Only when cancellation/revocation is explicitly authorized:
revoke(codex_home, "approved-periodic-example", skills=shared_skills,
       state="revoked")
```

`state` is `active`, `cancelled`, or `revoked`. The API takes profile/skills resource
locks, checks existing records, and uses shared atomic replacement. Migration must
use the existing approved scope, not infer or expand it. This implementation does
not register, disable, or alter scheduler tasks. Direct edits of this record are
not a concurrent writer protocol; controllers should use the API.

## Outbox format

Private runtime directory: `<codex_home>/claude-sync/memory-outbox/`.

- `history.json`: the authoritative increment chain, request bindings, delivery
  state, and receipts. Persisted with shared `atomic_replace`.
- `prepared/<increment_id>.md`: immutable expected note bytes, created through
  shared `create_no_replace`. These are durable recovery evidence, not disposable
  processing queue files. Retain them after publication and native consumption.
- `history-conflict.json`: first metadata-only migration evidence, when legacy or
  damaged history is discovered. Its presence blocks delivery until T09 explicitly
  resolves the evidence. Archive updates can continue.

`history.json` has this structure (angle-bracket values describe types):

```text
{
  version: 1,
  algorithm: "sha256",
  normalization: "archive-utf8-controls-v1",
  requests: { <one-shot request_id>: <increment_id>, ... },
  records: [
    {
      increment_id: <64 lowercase hex>,
      predecessor: <prior increment_id in this root/scope stream, or null>,
      source_root: <normalized absolute projects root>,
      scope: [<exact project keys>],
      source_hash: <64 lowercase hex>,
      request_id: <first delivery request ID, or null for archive-only>,
      periodic: <boolean>,
      delivery_state: <state below>,
      prepared_note_path: "prepared/<increment_id>.md",
      content_hash: <SHA-256 of expected note bytes>,
      note_identity: [<device>, <inode>],
      receipt: {
        increment_id: <same increment_id>,
        content_hash: <same content_hash>,
        note_identity: [<same device>, <same inode>]
      }
    }
  ]
}
```

`note_identity` appears only after native creation was observed. `receipt` appears
only for `published`. `archive_only` records have no prepared file, identity, or
receipt. Native note paths are derived, never accepted from a report:
`<codex_home>/memories/extensions/ad_hoc/notes/claude-memory-<increment_id>.md`.

Canonical increment input is the object `{version: 1, algorithm: "sha256",
normalization: "archive-utf8-controls-v1", source_root, scope, source_hash,
predecessor}`, serialized by `json.dumps(sort_keys=True, ensure_ascii=True,
indent=2) + '\n'`, encoded as UTF-8, then SHA-256 hashed. It includes the predecessor
increment ID, not only the preceding source hash. A → B → A → B produces four
identities. Request IDs do not affect increment identity. The existing archive
snapshot hash format remains supported; a named scope hashes only its selected
source records and scope mapping.

## Publication and recovery

1. Persist any explicit authorization, immutable expected note, and `prepared`
   history before writing the source archive/index.
2. Apply reversible profile/archive changes. The archive index contains source
   metadata only; it never claims native publication.
3. Recheck authorization and ingress availability, persist
   `publication_started`, recheck again, then call shared OS
   `create_no_replace_with_identity`. The existing boolean `create_no_replace`
   delegates to that same implementation.
4. Record the identity obtained from the opened staging object before publication,
   while still `publication_started`. Destination lookups can verify that identity;
   they must never establish ownership or replace the recorded identity.
5. Verify the expected note envelope, full bytes, and recorded file identity;
   persist a `published` receipt. This means publication, not consolidation.

| State | Meaning and retry behavior |
| --- | --- |
| `archive_only` | Observed source increment; no native delivery selected. An explicit grant can prepare this same increment later. |
| `prepared` | Durable expected note; native create has not started. May publish only while authorization and contract remain valid. |
| `publication_started` | The irreversible boundary may have been crossed. Recovery only inspects the expected path. It never repeats create. |
| `published` | Verified publication receipt. A consumed or later edited native note is never recreated or overwritten. |
| `delivery_unknown` | Publication cannot be proven, including an absent note or a crash before file identity was persisted. No automatic replay, even under a different request ID. |
| `conflict` | A competitor occupied the path, bytes differ, or observed file identity changed. Preserve native bytes and evidence. |
| `cancelled` | Authorization was revoked/cancelled before creation. Queued delivery is blocked. |
| `superseded` | A prepared ancestor was overtaken by a selected successor in the same source-root/scope stream. Unrelated queued scopes and roots remain prepared for later selection. |

If an exact surviving note has a persisted matching identity, recovery can record
the receipt. The smaller create-to-identity-record window is deliberately unknown,
even with matching bytes: identical replacement files cannot prove ownership.
Already-published receipts remain valid publication evidence after consumption.

An identical-content competitor replacing the pathname between creation and
identity persistence is a conflict when its identity differs from the opened
publication object. Publication occurred; the conflict does not prove it never
happened. A crash before identity persistence remains unknown even if exact bytes
survive. Expected prepared bytes and the native note are independent files, so a
native edit cannot modify recovery evidence. No extra hard-link anchor survives
normal completion. Windows uses the shared pinned-parent and handle-rename
helpers. POSIX pathname linking assumes a trusted cooperative staging directory;
device/inode evidence cannot exclude hostile staging swaps or eventual inode
reuse, and it does not establish a portable identity after restore.

Missing, empty, malformed, inconsistent, or truncated history with orphaned
prepared files is a conflict, not a fresh install. Prepared-file corruption and
missing/invalid receipts also block delivery. Legacy index hashes are retained in
`history-conflict.json` before the source-only index is rewritten. T09 must verify
old envelopes/aliases; this task does not silently reimport them.

All public writes use the T01 profile resource locks. These locks serialize
cooperating sync/backup/controller writers; they do not lock out Codex consumers
or editors. Shared filesystem durability applies: file contents are fsynced,
publication is atomic, POSIX directory fsync failures propagate, and Windows
portable directory-entry power-loss durability is not guaranteed. No exactly-once
claim is made for native consumption.

## Controller CONFIG requirements

The controller owns changes to CONFIG. Before deployment it must include
`claude-sync/memory-authorizations.json` and the complete `claude-sync/memory-outbox/`
in the private profile capture/restore policy, drift checking, and required payload
validation. Include `history.json`, all prepared notes (including published and
unknown records), and any `history-conflict.json`. These files are durable state;
do not classify prepared notes as an excluded temporary queue. Ignore only the
shared atomic writer's unpublished `.fleet-guards-*` temporary files.

Capture under the established backup-then-sorted-profile/skills lock order. Restore
must preserve newer destination history, grants, revoked states, unknown outcomes,
and native notes. Do not overwrite a newer ledger with an older snapshot or reset
history to an empty file. A partial restored set must report conflict. Restoring an
unpublished attempt onto another machine cannot establish whether the original
machine published or consumed its note. OS file identities are not portable.

Records and expected note bytes bind absolute source/archive roots. Cross-home
restore needs the controller's T08/T09 validated remap/alias procedure; copying
these bytes to another root must remain a conflict, not synthesize successful
receipts or reauthorize delivery. Do not rewrite native registry, summary,
evidence, or database files. This T03 implementation does not change CONFIG,
active tasks, deployed profiles, or the legacy PowerShell history engine.

Rollback of a profile apply intentionally preserves the full outbox and all
native notes. Code rollback must target a version that understands this outbox;
reverting to the index-as-receipt implementation would lose the safety contract.

### Pure state validation and producer fixture

`profile_bridge.memory_outbox.validate_restore_state(codex_home, files)` validates
a captured dependency group without filesystem reads or writes. `files` maps
Codex-relative POSIX names to original bytes, for example
`claude-sync/memory-authorizations.json` and
`claude-sync/memory-outbox/prepared/<increment_id>.md`. Strip CONFIG's `.codex/`
payload prefix before calling. The validator shares the runtime history/grant
schema checks and verifies prepared membership, envelopes, predecessor chains,
request bindings, receipts, initialization witness, and grant scope/root bindings.
It returns metadata counts/states/revoked request IDs or raises `HistoryConflict`.
An authorization-only group is valid before history initialization. An existing
`history-conflict.json`, a partial group, or changed root-bound envelopes requires
review; validation never repairs or removes that evidence.

This proves internal consistency of captured bytes only. CONFIG must still hold
the agreed locks, enforce the whole dependency group, compare destination bytes
and freshness, preserve newer revocations, and block cross-home restore pending
T08/T09 aliases. Validation is not permission to overwrite, reactivate grants,
reinterpret `delivery_unknown`, or claim current ownership of a native file.

For controller integration tests, `tools.make_fixtures.make_t03_history(home)`
generates a fresh synthetic home with `.claude`, `.codex`, and `.agents/skills`
using real T03 prepare/publish/recover/revoke APIs. It produces prepared, published,
unknown and cancelled records, revoked authorization, and `history_initialized`.
`t03_state_bytes(codex_home)` returns its complete byte mapping. The SYNC test
`tests/test_memory_restore_state.py` checks completeness, damaged/missing members,
pure validation, byte capture/restore at the same home, and restart in a fresh
Python process. CONFIG's actual capture/restore end-to-end wiring remains the
controller's integration test; this helper does not implement CONFIG or aliases.

## Validation

`python -m pytest tests/test_memory_delivery.py tests/test_memory_restore_state.py tests/test_profile_memory.py
 tests/test_profile_sync.py -q` exercises synthetic temporary homes. The delivery
suite uses real child-process `os._exit` at persistence/publication boundaries,
native consumption, replay, scoped changes, repeated ABA, identity/content
conflicts, cancellation, missing contracts, legacy/corrupt history, rollback, and
protected native-byte equality. JSON output contains identifiers, hashes, paths,
states and counts, never source bodies or prepared note content.
