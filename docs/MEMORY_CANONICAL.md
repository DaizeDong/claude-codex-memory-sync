# Canonical memory history and restoration

T09 embeds `canonical` in T03's `memory-outbox/history.json`. There is one
durable history writer: `memory_outbox.prepare` and its existing publisher.
The root memory facade still produces the searchable archive. The PS/CMD
compatibility entry invokes `python -m profile_bridge.memory.compat` and uses
the same outbox for previous/current per-file notes. `CCMS_PYTHON` can select
the installed interpreter; absence or failure is an error, with no old writer
fallback. No models, schedulers, or services participate.

Source identity hashes a registered root namespace, exact project directory key,
and NFC, slash-separated, case-insensitive relative path. Encoded Claude keys
are never decoded into project paths. Spelling collisions require review.
The increment hashes source ID, predecessor increment, operation, and content
digest with explicit SHA-256 and `ccms-nfc-lf-v1` normalization. UTF-8/BOM and
BOM-marked UTF-16 decode strictly; CRLF, CR, NEL and Unicode line separators
become LF, text becomes NFC, and one final LF is retained. Compatibility input
rejects unsafe controls; archive presentation retains its visible escape policy.

Repeated content does not add an event. A/B/A is three events. Complete project
observations can record deletion and later reappearance; deletion is history,
not automatic retraction of consolidated facts. A path move is a deletion and
new source, not an inferred identity alias. Incomplete observations cannot prove
deletion. Existing native notes, summaries, registry and evidence are retained.

Archive snapshot publication and per-file ingress have distinct envelopes. Both
reference canonical events. `aliased` means another verified history record or
legacy envelope already represents the event; it has no native receipt or file
identity and never schedules publication. It must not be reported as delivered.
All T03 prepared/started/published/unknown and no-replace rules remain in force.

`memory.legacy.parse_note` verifies old filename, timestamp, import digest,
source/project provenance, quoted current/previous digests and marker order.
`import_notes` requires a complete single-parent chain and explicit project
binding. Missing parents, forks, changed evidence and ambiguous overlaps fail.
Aliases retain the envelope digest and `delivery=unproven`. `verify_archive`
checks original raw sources and the old index hash; an index delivery marker
alone never proves native publication. Existing T03 histories seed canonical
heads only when the current original snapshot digest matches the prior head;
otherwise migration requires explicit evidence.

## CONFIG integration contract

The pure API is:

```python
from profile_bridge.memory.restore import migrate_restore_state

result = migrate_restore_state(
    original_codex, target_codex, captured_files,
    scope_evidence=[{
        "source_root": original_projects_root,
        "target_root": target_projects_root,
        "scope": ["synthetic-project"],
    }],
    destination_files=existing_target_group,
)
```

Paths in the byte mapping are Codex-relative POSIX paths. Scope evidence must
exactly cover the original grants and history streams, and cover canonical
sources; named scopes cannot become `*`. Original periodic flags and revoked
states are preserved. No grant is inferred from ingress instructions. The API
validates every original envelope, prepared file, request and publication
identity before migration. It does no filesystem access or writes.

The result contains a complete `files` dependency group and metadata-only
`report`. Source namespaces persist while root bindings move. Snapshot delivery
IDs and root-bound envelopes are regenerated. Original device/inode values and
receipts are not installed as target ownership. Prepared, started, published,
unknown and conflicting attempts become `delivery_unknown`; cancelled and
superseded records remain terminal. Migration metadata binds original file
digests, exact scope evidence and target home to a new migration ID. The original
captured generation must remain available for audit.

CONFIG must call this before its existing cross-home rejection only when the
controller supplies explicit scope evidence. Hold backup then sorted profile
locks, validate the entire source group, compare the destination group, and
stage the returned dependency group in the existing restore transaction. Do not
feed regenerated prepared filenames through the old manifest unchanged: derive
new manifest members and hashes for this group. Publish the group atomically or
retain an explicit incomplete-restore journal that blocks sync. An identical
second call returns zero files. A different or newer target fails closed; the API
does not merge or overwrite it. Raw home-string replacement is forbidden.

`profile_sync` already passes the private DeliveryPlan to T03 prepare/deliver;
canonical history is inside that plan, so it needs no second state writer or new
serialization path. CONFIG integration remains a controller-owned change.

## Verification and compatibility limits

The T09 suites use generated synthetic CCMS fixtures, corruption checks,
fresh-process `os._exit` publication boundaries, cross-home validation/restart,
mixed modes and a second migration with no notes or history drift. The old
PowerShell suite also checks the JSON/exit contracts, legacy mutex contention,
concurrent invocation, previous/current quoting and immutable prepared evidence.
It now checks canonical envelopes and preserves native edits after publication;
corrupting prepared evidence still blocks writes. These checks use synthetic
temporary homes and do not establish that live migration has occurred.
