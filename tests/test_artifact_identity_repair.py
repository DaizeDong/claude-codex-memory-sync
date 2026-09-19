"""Pure synthetic ownership regressions; no profile or filesystem fixtures."""
import importlib.util
import json
import os

import pytest

from profile_bridge import ownership as owner

if os.environ.get("T08_OWNER_BASELINE"):
    spec = importlib.util.spec_from_file_location("baseline_owner", os.environ["T08_OWNER_BASELINE"])
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)

HOME = "C:/Synthetic"
MANIFEST = ".codex/claude-sync/managed-artifacts.json"


def group(mixed=False, retired=False):
    members = {".codex/z.txt": b"old z\n"}
    links = {}
    items = {}
    for name in ("z", "a"):
        rel = ".codex/" + name + ".txt"
        state = {"kind": "file", "sha256": owner.digest(b"old " + name.encode() + b"\n")}
        if name == "a" and mixed:
            state = {"kind": "link", "target": HOME + "/source/a", "link_type": "junction"}
            links[rel] = dict(state)
        else:
            members[rel] = b"old " + name.encode() + b"\n"
        items[HOME + "/" + rel] = {"owner": owner.OWNER, "source": HOME + "/source/" + name,
                                   "source_id": "synthetic:" + name, "original": state}
    if retired:
        items[HOME + "/.codex/retired.txt"] = {"owner": owner.OWNER, "retired": True,
            "original": {"kind": "file", "sha256": "1" * 64}}
    # Preserve deliberately non-sorted original insertion order.
    members[MANIFEST] = json.dumps({"version": 1, "items": items}).encode()
    assert owner.verify_members(members, home=HOME, links=links)["status"] == "verified"
    changed = {k: (owner.encoded(json.loads(v)) if k == MANIFEST else v.replace(b"old", b"new"))
               for k, v in members.items()}
    return members, changed, links


@pytest.mark.parametrize("mixed", [False, True])
def test_sorted_rows_keep_exact_member_hash(mixed):
    original, changed, links = group(mixed)
    result, conflicts = owner.remap_members(original, changed, home=HOME, links=links)
    assert not conflicts
    assert owner.verify_members(result, home=HOME, links=links)["status"] == "verified"
    rows = json.loads(result[MANIFEST])["items"]
    for key, row in rows.items():
        if row["original"]["kind"] == "file":
            assert row["original"]["sha256"] == owner.digest(result[key[len(HOME) + 1:]])


def test_explicit_home_and_source_mapping():
    original, changed, links = group(True, True)
    mapping = {HOME: "D:/Target", HOME + "/source/z": "E:/Elsewhere/a"}
    changed = {k: owner.map_paths(v, mapping) for k, v in changed.items()}
    result, conflicts = owner.remap_members(original, changed, home=HOME, links=links, path_mapping=mapping)
    assert not conflicts
    mapped_links = json.loads(owner.map_paths(owner.encoded(links), mapping))
    assert owner.verify_members(result, home="D:/Target", links=mapped_links)["status"] == "verified"


@pytest.mark.parametrize("tamper", [None, "root", "type"])
def test_catalog_origin_root_follows_only_explicit_mapping(tamper):
    original, _, links = group()
    manifest = json.loads(original[MANIFEST])
    key = HOME + "/.codex/z.txt"
    manifest["items"][key]["catalog_identity"] = {
        "origin": {"type": "directory", "root": HOME + "/.claude/commands"},
        "scope_path": HOME, "source_hash": "synthetic-identity"}
    original[MANIFEST] = owner.encoded(manifest)
    mapping = {HOME: "D:/Target"}
    changed = {k: owner.map_paths(v, mapping) for k, v in original.items()}
    if tamper:
        damaged = json.loads(changed[MANIFEST])
        damaged["items"]["D:/Target/.codex/z.txt"]["catalog_identity"]["origin"][tamper] = "unapproved"
        changed[MANIFEST] = owner.encoded(damaged)
        with pytest.raises(owner.OwnershipConflict, match="artifact_authority_changed"):
            owner.remap_members(original, changed, home=HOME, path_mapping=mapping)
    else:
        result, conflicts = owner.remap_members(original, changed, home=HOME, path_mapping=mapping)
        assert not conflicts
        assert owner.verify_members(result, home="D:/Target", links=links)["status"] == "verified"


@pytest.mark.parametrize("damage", ["omit", "extra", "rename", "kind", "source", "source_id", "owner", "retired", "schema", "collision", "link_target"])
def test_changed_authority_is_refused(damage):
    original, changed, links = group(True, True)
    obj = json.loads(changed[MANIFEST])
    rows = obj["items"]
    key = HOME + "/.codex/z.txt"
    if damage == "omit":
        del rows[key]
    elif damage in {"extra", "rename", "collision"}:
        dest = ".codex/z.txt" if damage == "collision" else HOME + "/.codex/extra.txt"
        rows[dest] = dict(rows[key])
        if damage == "rename":
            del rows[key]
    elif damage == "kind":
        rows[key]["original"] = dict(links[".codex/a.txt"])
    elif damage == "retired":
        rows[key]["retired"] = True
    elif damage == "schema":
        obj["version"] = 2
    elif damage == "link_target":
        rows[HOME + "/.codex/a.txt"]["original"]["target"] = HOME + "/source/unapproved"
    else:
        rows[key][damage] = "unapproved"
    changed[MANIFEST] = owner.encoded(obj)
    with pytest.raises(owner.OwnershipConflict):
        owner.remap_members(original, changed, home=HOME, links=links)


def test_relative_absolute_slash_and_case_identity():
    original, changed, links = group()
    obj = json.loads(changed[MANIFEST])
    obj["items"] = {k[len(HOME) + 1:].replace("/", "\\").upper(): v for k, v in obj["items"].items()}
    changed[MANIFEST] = owner.encoded(obj)
    result, conflicts = owner.remap_members(original, changed, home=HOME)
    assert not conflicts
    assert owner.verify_members(result, home=HOME)["status"] == "verified"


def test_original_alias_collision_is_invalid():
    original, changed, links = group()
    obj = json.loads(original[MANIFEST])
    obj["items"][".codex/z.txt"] = obj["items"][HOME + "/.codex/z.txt"]
    original[MANIFEST] = owner.encoded(obj)
    assert owner.verify_members(original, home=HOME)["status"] == "conflict"


def test_missing_dependency_and_opaque_bytes_stay_refused_or_untouched():
    original, changed, links = group()
    del changed[".codex/z.txt"]
    opaque = ".codex/memories/MEMORY.md"
    original[opaque], changed[opaque] = b"\xffopaque\r\n", b"changed"
    result, conflicts = owner.remap_members(original, changed, home=HOME)
    assert conflicts
    assert result[opaque] == original[opaque]
    assert owner.verify_members(result, home=HOME)["status"] == "conflict"


def test_retired_row_survives_without_member():
    original, changed, links = group(retired=True)
    result, conflicts = owner.remap_members(original, changed, home=HOME)
    assert not conflicts
    assert owner.verify_members(result, home=HOME)["status"] == "verified"


def test_restore_threads_target_home():
    from profile_backup.restore import restored_payload
    original, _, links = group(True)
    manifest = {"source_home": HOME, "files": [{"path": k, "remap_home": True} for k in original],
                "links": [{"path": k, "ownership": v} for k, v in links.items()]}
    result = restored_payload(manifest, original, "D:/Target")
    mapped_links = json.loads(owner.map_paths(owner.encoded(links), {HOME: "D:/Target"}))
    assert owner.verify_members(result, home="D:/Target", links=mapped_links)["status"] == "verified"


def test_home_remap_reverses_mixed_absolute_relative_sort_order():
    original, changed, links = group()
    obj = json.loads(original[MANIFEST])
    absolute = HOME + "/.codex/z.txt"
    obj["items"][".codex/z.txt"] = obj["items"].pop(absolute)
    original[MANIFEST] = json.dumps(obj).encode()
    mapping = {HOME: "D:/Target"}
    changed[MANIFEST] = owner.map_paths(owner.encoded(obj), mapping)
    result, conflicts = owner.remap_members(original, changed, home=HOME, path_mapping=mapping)
    assert not conflicts
    assert owner.verify_members(result, home="D:/Target")["status"] == "verified"


@pytest.mark.parametrize("target", [".codex/../z.txt", ".codex//z.txt", ".codex/./z.txt", "C:/Outside/z.txt"])
def test_ambiguous_or_outside_path_refused(target):
    original, changed, _ = group()
    obj = json.loads(changed[MANIFEST])
    obj["items"][target] = obj["items"].pop(HOME + "/.codex/z.txt")
    changed[MANIFEST] = owner.encoded(obj)
    with pytest.raises(owner.OwnershipConflict):
        owner.remap_members(original, changed, home=HOME)


def test_mapping_collision_refused():
    original, changed, _ = group()
    mapping = {HOME + "/.codex/z.txt": HOME + "/.codex/a.txt"}
    changed[MANIFEST] = owner.map_paths(changed[MANIFEST], mapping)
    with pytest.raises(owner.OwnershipConflict):
        owner.remap_members(original, changed, home=HOME, path_mapping=mapping)


def test_preexisting_invalid_marker_cannot_regain_ownership():
    body = b"synthetic\n"
    intact = (b"<!-- claude-profile-sync:begin sha256=" + owner.digest(body).encode() +
              b" -->\n" + body + b"<!-- claude-profile-sync:end -->\n")
    original = {".codex/AGENTS.md": intact.replace(body, b"edited\n")}
    result, conflicts = owner.remap_members(original, {".codex/AGENTS.md": intact})
    assert conflicts
    assert owner.verify(result[".codex/AGENTS.md"], "instruction")["status"] == "conflict"


def test_sorted_normalizer_integration():
    from profile_backup.redaction import normalize
    original, changed, links = group(True)
    changed[MANIFEST], requirements = normalize(MANIFEST, original[MANIFEST])
    assert not requirements
    result, conflicts = owner.remap_members(original, changed, home=HOME, links=links)
    assert not conflicts
    assert owner.verify_members(result, home=HOME, links=links)["status"] == "verified"


def test_profile_envelope_maps_home_links_and_provenance():
    original, _, links = group(True)
    request = owner.encoded({"home": HOME, "members": {k: v.hex() for k, v in original.items()}, "links": links})
    result = owner.remap(request, "profile", {HOME: "D:/Target", HOME + "/source/z": "E:/Alternate/a"})
    assert owner.verify(result, "profile")["status"] == "verified"


@pytest.mark.parametrize("relative", [False, True])
def test_profile_envelope_non_order_preserving_member_mapping(relative):
    original, _, links = group(True)
    if relative:
        obj = json.loads(original[MANIFEST])
        obj["items"] = {k[len(HOME) + 1:]: v for k, v in obj["items"].items()}
        original[MANIFEST] = owner.encoded(obj)
    request = owner.encoded({"home": HOME, "members": {k: v.hex() for k, v in original.items()}, "links": links})
    mapping = {HOME: "D:/Target", HOME + "/.codex/z.txt": "D:/Target/.codex/b.txt",
               HOME + "/.codex/a.txt": "D:/Target/.codex/y.txt"}
    result = owner.remap(request, "profile", mapping)
    assert owner.verify(result, "profile")["status"] == "verified"
    output = json.loads(result)
    assert ".codex/b.txt" in output["members"]
    assert ".codex/y.txt" in output["links"]


def test_row_swap_refused():
    original, changed, _ = group()
    obj = json.loads(changed[MANIFEST])
    keys = list(obj["items"])
    obj["items"][keys[0]], obj["items"][keys[1]] = obj["items"][keys[1]], obj["items"][keys[0]]
    changed[MANIFEST] = owner.encoded(obj)
    with pytest.raises(owner.OwnershipConflict):
        owner.remap_members(original, changed, home=HOME)


@pytest.mark.parametrize("field", ["provider", "plugin", "source_sha256", "catalog_identity"])
def test_provenance_change_refused(field):
    original, changed, _ = group()
    obj = json.loads(changed[MANIFEST])
    obj["items"][HOME + "/.codex/z.txt"][field] = "unapproved"
    changed[MANIFEST] = owner.encoded(obj)
    with pytest.raises(owner.OwnershipConflict):
        owner.remap_members(original, changed, home=HOME)


def test_original_bad_hash_cannot_be_repaired_by_redaction():
    original, changed, _ = group()
    original[".codex/z.txt"] += b"unapproved edit\n"
    changed[".codex/z.txt"] = b"old z\n"
    result, conflicts = owner.remap_members(original, changed, home=HOME)
    assert conflicts
    assert owner.verify_members(result, home=HOME)["status"] == "conflict"


def test_opaque_artifact_cannot_gain_ownership():
    rel = ".codex/memories/MEMORY.md"
    raw = b"\xffsynthetic opaque\r\n"
    original = {rel: raw, MANIFEST: owner.encoded({"version": 1, "items": {
        HOME + "/" + rel: {"owner": owner.OWNER, "original": {"kind": "file", "sha256": owner.digest(raw)}}}})}
    result, conflicts = owner.remap_members(original, {**original, rel: b"changed"}, home=HOME)
    assert conflicts
    assert result[rel] == raw


def test_retired_member_recreation_and_file_link_alias_refused():
    original, _, links = group(retired=True)
    original[".codex/retired.txt"] = b"recreated"
    assert owner.verify_members(original, home=HOME)["status"] == "conflict"
    original, _, _ = group()
    assert owner.verify_members(original, home=HOME, links={".CODEX/Z.TXT": {"kind": "link"}})["status"] == "conflict"
