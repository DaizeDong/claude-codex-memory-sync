# Canonical memory ledger

`memory-outbox/history.json` carries `canonical`, the per-file ledger that tells the delivery planner which Claude memory files changed. Only `memory_outbox.prepare` writes it, under the profile locks, together with the rest of the outbox history.

## What it holds

Version 2 keeps one entry per memory file ever observed under a source root, keyed by source ID:

```text
canonical: {
  version: 2,
  normalization: "ccms-nfc-lf-v1",
  roots: { <normalized projects root>: <namespace hash> },
  sources: {
    <source_id>: {
      namespace, project, path,
      increment_id: <64 lowercase hex>,
      predecessor: <increment_id this event replaced, or null>,
      operation: "add" | "update" | "delete" | "reappear",
      content_digest: <SHA-256 of the normalized text; of empty bytes for delete>
    }
  }
}
```

The text of a memory file is not stored here. The searchable archive under `imports/claude-memory/` holds the current copy and the Claude source holds the original. Earlier versions are not kept anywhere in the outbox: nothing in delivery reads them, and keeping the text of every version is what pushed the history past its 16 MiB read limit on 2026-10-13, after which every run stopped at `history_conflict`.

## Identities

Source identity hashes the root namespace, the exact project directory key and the NFC, slash-separated, case-insensitive relative path. Encoded Claude keys are never decoded into project paths. Two spellings of one path stop the run with `memory_path_spelling_requires_alias`.

The increment ID hashes the source ID, the predecessor increment ID, the operation and the content digest, with SHA-256 and `ccms-nfc-lf-v1` normalization: UTF-8 (with or without BOM) and BOM-marked UTF-16 decode, CRLF, CR, NEL and Unicode line separators become LF, the text becomes NFC and keeps one final LF. Because the predecessor is part of the hash, A then B then A gives three different IDs, and an ID never comes back once its file has moved on.

## Observation

Each run observes every selected source file:

1. Same digest as the head: nothing changes.
2. A new file is `add` with no predecessor, changed text is `update`, and a file that was deleted and comes back is `reappear`.
3. A file missing from a project whose traversal completed is `delete`. An incomplete traversal never implies deletion. A path move is a deletion plus a new source.

The new event replaces the head. Validation checks every head on its own: the source ID matches the registered namespace, project and path, `add` has no predecessor and every other operation has one, a delete carries the empty digest, and the increment ID is the hash of the head's own fields. A predecessor is a hash link only; the event it names is no longer stored.

Deletion is recorded, not retracted: facts Codex already consolidated stay until Codex itself drops them.

## Snapshot digest

The delivery planner hashes `{source_id: increment_id}` over the heads of the selected root and scope, deleted heads included. That digest is the delivery row's `source_hash`, and the sorted head IDs are its `canonical_increments`. See [the outbox](MEMORY_OUTBOX.md) for how rows use them.
