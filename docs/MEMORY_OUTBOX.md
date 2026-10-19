# Memory authorization and durable outbox (history version 2)

The Python profile bridge archives by default and delivers to Codex only under an explicit grant. The outbox keeps what delivery needs and nothing more: the latest event of each memory file (see [the canonical ledger](MEMORY_CANONICAL.md)), the delivery rows still needed to chain, deduplicate and recover, and the archive ownership hashes. A history in any earlier format stops delivery; the fix is the reset below, not a migration.

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

- `history.json`: the canonical ledger, the retained delivery rows, request bindings, delivery state, receipts and archive ownership. Persisted with shared `atomic_replace`.
- `prepared/<increment_id>.md`: immutable expected note bytes, created through shared `create_no_replace` before publication. They are needed only while their row is `prepared` or `publication_started`; the next `prepare` removes the rest, since `render_note` reproduces any of them from the row.
- `history-conflict.json`: metadata-only evidence written the first time history cannot be read (damaged, missing next to an existing archive, or in an older format). Its presence blocks delivery until the outbox is reset.

`history.json` has this structure (angle-bracket values describe types):

```text
{
  version: 2,
  algorithm: "sha256",
  normalization: "archive-utf8-controls-v1",
  canonical: <the per-file ledger, MEMORY_CANONICAL.md>,
  archive_ownership: <archive hashes, MEMORY_ARCHIVE_HYGIENE.md>,
  requests: { <one-shot request_id>: <increment_id>, ... },
  records: [
    {
      increment_id: <64 lowercase hex>,
      predecessor: <prior increment_id in this root/scope stream, or null>,
      source_root: <normalized absolute projects root>,
      scope: [<exact project keys>],
      source_hash: <canonical snapshot digest of this root and scope>,
      canonical_increments: [<sorted head increment IDs of that snapshot>],
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

`note_identity` appears only after native creation was observed. `receipt` appears only for `published`. `archive_only` records have no prepared file, identity, or receipt. Native note paths are derived, never accepted from a report: `<codex_home>/memories/extensions/ad_hoc/notes/claude-memory-<increment_id>.md`.

### Which rows are kept

Every plan prunes `records` to the rows delivery still reads:

1. The newest row of each root/scope stream: it is the next row's predecessor and the "has the snapshot changed" comparison.
2. The newest row of each stream in a state that represents its heads to Codex (anything except `archive_only`, `cancelled` and `superseded`). A head that is still current was in that row if it was in any earlier one, so its `canonical_increments` decide whether a new snapshot is already `aliased`.
3. Rows that are still pending (`prepared`, `publication_started`) or unresolved (`delivery_unknown`, `conflict`, `cancelled`), which the report keeps listing.
4. Rows bound to a one-shot request.

Everything else, including old `published` rows and their receipts, is dropped. Rows of one stream stay in append order, so validation checks the chain link by link: each increment ID is the hash of its own predecessor and snapshot, IDs are unique, no two kept rows of a stream share a predecessor, and a predecessor that is still kept comes earlier in the same stream. A predecessor that was dropped is a hash link only. The history stays a few hundred kilobytes whatever the run count.

Canonical increment input is the object `{version: 1, algorithm: "sha256",
normalization: "archive-utf8-controls-v1", source_root, scope, source_hash,
predecessor}`, serialized by `json.dumps(sort_keys=True, ensure_ascii=True,
indent=2) + '\n'`, encoded as UTF-8, then SHA-256 hashed. It includes the predecessor
increment ID, not only the preceding source hash. A → B → A → B produces four
identities. Request IDs do not affect increment identity. `source_hash` is the canonical snapshot digest, so a named scope hashes only the heads of its own projects.

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
A published receipt stays valid publication evidence after consumption for as long as its row is kept; once a newer row of the stream represents the heads, the old row and its receipt are dropped.

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

Missing, empty, malformed, inconsistent, or truncated history is a conflict, not a fresh install, and so is a history in an older format. Prepared-file corruption for a pending row and missing/invalid receipts also block delivery. The index hashes of the archive are retained in `history-conflict.json` before the source-only index is rewritten.

### Resetting the outbox

The one way out of a history conflict is a fresh history under the same grant:

1. Stop the sync (let a running `SyncClaudeToCodex` finish).
2. Delete `<codex_home>/claude-sync/memory-outbox/` and the archive `<codex_home>/imports/claude-memory/`. The archive is generated from Claude memory and its ownership hashes lived in the deleted history, so a kept archive would read as user-edited and block. Check first that no archive file was edited by hand: every file's SHA-256 should appear under its path in the old `archive_ownership`.
3. In `memory-authorizations.json` set `history_initialized` to `false`. Leave `grants` exactly as they are.
4. Run the sync once. It rebuilds the archive, writes a new history and publishes one new note for the current snapshot. Native notes already in Codex stay where they are.

All public writes use the T01 profile resource locks. These locks serialize
cooperating sync/backup/controller writers; they do not lock out Codex consumers
or editors. Shared filesystem durability applies: file contents are fsynced,
publication is atomic, POSIX directory fsync failures propagate, and Windows
portable directory-entry power-loss durability is not guaranteed. No exactly-once
claim is made for native consumption.

## Backup and restore

The private configuration backup captures only `claude-sync/memory-authorizations.json`, rewritten with `history_initialized: false`. The outbox and the archive are not captured: after a restore the first sync starts a fresh history from the restored Claude memory under the same grant, the same state the reset above produces. Native notes belong to Codex and this bridge never rewrites them.

Rollback of a profile apply intentionally preserves the full outbox and all native notes.

### Pure state validation

`profile_bridge.memory_outbox.validate_restore_state(codex_home, files)` validates a captured group without filesystem reads or writes. `files` maps Codex-relative POSIX names (strip the `.codex/` payload prefix) to original bytes. An authorization-only group is valid while `history_initialized` is false, which is the form the backup writes. A group with `history.json` must also say `history_initialized: true` and carry the prepared bytes of every pending row, and it passes the same history and grant checks as a runtime read. The validator returns metadata counts, states and revoked request IDs or raises `HistoryConflict`. It proves internal consistency only, not freshness or permission to overwrite.
