"""Synthetic regressions for ownership authority and repeatable source input."""
import json
from pathlib import Path

import pytest

from profile_bridge import ownership as owner, sources


MANIFEST = ".codex/claude-sync/managed-agents.json"
CONFIG = ".codex/config.toml"
OPAQUE_PATHS = [".codex/memories/MEMORY.md",
                ".codex/claude-sync/memory-outbox/prepared/note.md",
                ".codex/claude-sync/memory-authorizations.json"]


def block(name, body, authority=owner.AGENTS):
    return (f"# BEGIN {authority} {name} sha256={owner.digest(body)}\n".encode()
            + body + f"# END {authority} {name}\n".encode())


def agents():
    members, roles, config = {}, {}, b""
    for name in ("writer", "reviewer"):
        instructions = b'developer_instructions = "C:/Example/source.md"\n'
        source_hash = owner.digest(name.encode())
        role = (f"# Managed by {owner.AGENTS}; source_sha256={source_hash}\n"
                f"# instructions_sha256={owner.digest(instructions)}\n".encode() + instructions)
        section = block(name, f'[agents.{name}]\nconfig_file = "agents/{name}.toml"\n'.encode())
        members[f".codex/agents/{name}.toml"] = role
        config += section
        roles[name] = {"category": "synthetic", "plugin": "", "relative": name + ".md",
                       "source": "C:/Example/" + name + ".md", "source_sha256": source_hash,
                       "role_sha256": owner.digest(role), "config_sha256": owner.digest(section)}
    members[CONFIG] = config
    members[MANIFEST] = owner.encoded({"owner": owner.AGENTS, "version": 1, "roles": roles,
                                      "roles_sha256": owner.digest(owner.encoded(roles))})
    return members


def envelope(members):
    return owner.encoded({"home": "C:/Example", "members": {k: v.hex() for k, v in members.items()}})


@pytest.mark.parametrize("path", OPAQUE_PATHS)
@pytest.mark.parametrize("authority", ["unknown-owner", owner.OWNER])
def test_sidecar_cannot_claim_or_poison_opaque_bytes(path, authority):
    raw = b"\xffC:/Example opaque sha256=" + b"a" * 64 + b"\r\n"
    sidecar = ".codex/claude-sync/managed-artifacts.json"
    members = {path: raw, sidecar: owner.encoded({"version": 1, "items": {
        "C:/Example/" + path: {"owner": authority,
                               "original": {"kind": "file", "sha256": owner.digest(raw)}}}})}
    changed, conflicts = owner.remap_members(members, members, home="C:/Example")
    assert changed[path] == raw
    assert conflicts
    assert json.loads(changed[sidecar])["ownership_conflict"] is True
    assert owner.verify_members(changed, home="C:/Example")["status"] == "conflict"


@pytest.mark.parametrize("damage", ["empty", "partial", "unrelated"])
def test_agents_require_exact_manifest_marker_and_role_coverage(damage):
    members = agents()
    assert owner.verify(envelope(members), "profile")["status"] == "verified"
    if damage == "unrelated":
        del members[MANIFEST]
        members[".codex/claude-sync/managed-artifacts.json"] = owner.encoded({
            "version": 1, "items": {"C:/Example/" + path: {
                "owner": owner.OWNER, "original": {"kind": "file", "sha256": owner.digest(raw)}}
                for path, raw in members.items()}})
    else:
        manifest = json.loads(members[MANIFEST])
        if damage == "empty":
            manifest["roles"] = {}
            del members[".codex/agents/writer.toml"]
            del members[".codex/agents/reviewer.toml"]
        else:
            del manifest["roles"]["writer"]
            del members[".codex/agents/writer.toml"]
        manifest["roles_sha256"] = owner.digest(owner.encoded(manifest["roles"]))
        members[MANIFEST] = owner.encoded(manifest)
    assert owner.verify(envelope(members), "profile")["status"] == "conflict"
    changed, conflicts = owner.remap_members(members, members, home="C:/Example")
    assert conflicts
    assert owner.verify(envelope(changed), "profile")["status"] == "conflict"
    with pytest.raises(owner.OwnershipConflict):
        owner.remap(envelope(members), "profile", {"C:/Example": "D:/Synthetic"})


def test_bom_marker_offsets_and_real_agents_detail():
    members = agents()
    members[CONFIG] = b"\xef\xbb\xbf" + members[CONFIG]
    report = owner.verify_members(members, home="C:/Example")
    assert report["status"] == "verified"
    assert report["markers"][CONFIG][0]["start"] == 3
    assert report["groups"][0][3] == {"writer": ".codex/agents/writer.toml",
                                     "reviewer": ".codex/agents/reviewer.toml"}
    mapped = owner.remap(envelope(members), "profile", {"C:/Example": "D:/Synthetic"})
    assert owner.verify(mapped, "profile")["status"] == "verified"
    assert bytes.fromhex(json.loads(mapped)["members"][CONFIG]).startswith(b"\xef\xbb\xbf# BEGIN")


@pytest.mark.parametrize("prefix,suffix", [(b'description = """\n', b'"""\n'),
                                           (b"description = '''\n", b"'''\n"),
                                           (b'description = """\xef\xbb\xbf', b'"""\n')])
def test_bom_does_not_authorize_markers_inside_toml_strings(prefix, suffix):
    section = block("config", b'model = "synthetic"\n', owner.OWNER)
    assert owner.verify(b"\xef\xbb\xbf" + prefix + section + suffix, "config")["status"] == "conflict"


def test_bom_duplicate_marker_still_conflicts():
    section = block("config", b'model = "synthetic"\n', owner.OWNER)
    assert owner.verify(b"\xef\xbb\xbf" + section + section, "config")["status"] == "conflict"


def test_one_shot_source_is_rejected_before_consumption(tmp_path):
    file = tmp_path / "source.md"
    file.write_bytes(b"synthetic bytes")
    record = {"source_id": "synthetic", "relative_path": "source.md", "path": file}
    records = iter([record])
    stage = tmp_path / "accepted"
    with pytest.raises(ValueError, match="reusable|iterator"):
        sources.freeze({"sources": records, "stage": stage, "policy": "synthetic-v1"})
    assert next(records) == record
    assert not stage.exists()


def test_reusable_sources_keep_members_after_retry(tmp_path, monkeypatch):
    file = tmp_path / "source.md"
    file.write_bytes(b"first")
    def churn(point, attempt):
        if point == "between_rounds" and attempt == 1:
            file.write_bytes(b"accepted")
    monkeypatch.setattr(sources, "checkpoint", churn)
    result = sources.freeze({"sources": [{"source_id": "synthetic", "relative_path": "source.md", "path": file}],
                             "stage": tmp_path / "accepted", "policy": "synthetic-v1"})
    assert result["status"] == "frozen" and result["attempts"] == 2
    assert len(result["members"]) == 1
    assert Path(result["members"][0]["staged_path"]).read_bytes() == b"accepted"
