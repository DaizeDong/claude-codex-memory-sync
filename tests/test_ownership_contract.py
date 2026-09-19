"""Generated synthetic ownership groups; no copied profile or source fixtures."""
import json
from pathlib import Path

import pytest

from profile_bridge import ownership as owner
from profile_agents import plan_agents


def make_agents(tmp_path):
    """Use the real writer's byte renderer as this fixture generator."""
    home = tmp_path / "home"
    claude, codex = home / ".claude", home / ".codex"
    source = claude / "agents" / "writer.md"
    source.parent.mkdir(parents=True)
    source.write_text("---\nname: writer\ndescription: Synthetic writer\n---\nRead the synthetic source.\n", encoding="utf-8")
    files, config, report = plan_agents(claude, codex, [], b'model = "synthetic"\n')
    assert report["registered"] == 1
    members = {p.relative_to(home).as_posix(): data for p, data in files.items()}
    members[".codex/config.toml"] = config
    return home, members


def make_hooks():
    """Generate a V1 dependency group using the hook writer's encoders."""
    from profile_hooks import _json_bytes, _fingerprint
    bridge = b'print("C:/Example/source.py")\n'
    group = {"hooks": [{"type": "command", "command": "python C:/Example/source.py"}]}
    manifest = {"owner": owner.HOOKS, "version": 1, "bridge_sha256": owner.digest(bridge),
                "handler_sha256": _fingerprint(group)}
    manifest["manifest_sha256"] = _fingerprint(manifest)
    return {".codex/hooks.json": _json_bytes({"hooks": {"Stop": [group]}}),
            ".codex/imports/claude-hooks/stop_bridge.py": bridge,
            ".codex/imports/claude-hooks/manifest.json": _json_bytes(manifest)}


def envelope(members, home=None):
    return owner.encoded({"home": str(home) if home else "", "members": {k: v.hex() for k, v in members.items()}})


def test_real_role_group_remaps_together(tmp_path):
    home, members = make_agents(tmp_path)
    assert owner.verify(envelope(members, home), "profile")["status"] == "verified"
    new_home = tmp_path / "new-home"
    relocated = owner.remap(envelope(members, home), "profile", {str(home): str(new_home)})
    assert owner.verify(relocated, "profile")["status"] == "verified"
    assert str(home).encode().hex() not in json.loads(relocated)["members"].values()


@pytest.mark.parametrize("damage", ["role_edit", "missing_role", "config_edit", "schema", "unknown_group"])
def test_dependency_damage_cannot_be_reauthorized(tmp_path, damage):
    home, members = make_agents(tmp_path)
    clean = dict(members)
    manifest_path = ".codex/claude-sync/managed-agents.json"
    role_path = next(p for p in members if p.endswith(".toml") and p != ".codex/config.toml")
    if damage == "role_edit":
        members[role_path] += b"# user comment\n"
    elif damage == "missing_role":
        del members[role_path]
        del clean[role_path]
    elif damage == "config_edit":
        members[".codex/config.toml"] = members[".codex/config.toml"].replace(b"# END", b"# user comment\n# END")
    elif damage == "schema":
        manifest = json.loads(members[manifest_path]); manifest["version"] = 19
        members[manifest_path] = owner.encoded(manifest)
    else:
        members[".codex/claude-sync/managed-unknown.json"] = b'{"version":1,"items":{}}'
        clean[".codex/claude-sync/managed-unknown.json"] = members[".codex/claude-sync/managed-unknown.json"]
    assert owner.verify(envelope(members, home), "profile")["status"] == "conflict"
    # Simulate normalization deleting the user's edit. It must not earn any
    # newly valid marker or manifest even when the original body reappears.
    result, conflicts = owner.remap_members(members, clean, home=home)
    assert conflicts
    assert owner.verify(envelope(result, home), "profile")["status"] == "conflict"
    with pytest.raises(owner.OwnershipConflict):
        owner.remap(envelope(members, home), "profile", {str(home): str(tmp_path / "other")})


@pytest.mark.parametrize("damage", ["bridge", "handler", "missing", "duplicate", "unknown", "schema"])
def test_hook_groups_fail_closed(damage):
    members = make_hooks()
    clean = dict(members)
    manifest_path = ".codex/imports/claude-hooks/manifest.json"
    if damage == "bridge":
        members[".codex/imports/claude-hooks/stop_bridge.py"] += b"# edit\n"
    elif damage in {"handler", "duplicate"}:
        hooks = json.loads(members[".codex/hooks.json"])
        if damage == "duplicate":
            hooks["hooks"]["Stop"] *= 2
        else:
            hooks["hooks"]["Stop"][0]["matcher"] = "user-edit"
        members[".codex/hooks.json"] = owner.encoded(hooks)
    elif damage == "missing":
        del members[".codex/hooks.json"]
        del clean[".codex/hooks.json"]
    else:
        manifest = json.loads(members[manifest_path])
        manifest["version" if damage == "schema" else "unrecognized_dependencies"] = 7
        manifest["manifest_sha256"] = owner.object_hash({k: v for k, v in manifest.items() if k != "manifest_sha256"})
        members[manifest_path] = owner.encoded(manifest)
    result, conflicts = owner.remap_members(members, clean)
    assert conflicts
    assert owner.verify(envelope(result), "profile")["status"] == "conflict"


def test_hook_mapping_and_unknown_kind():
    members = make_hooks()
    mapped = owner.remap(envelope(members), "profile", {"C:/Example": "D:/Synthetic"})
    assert owner.verify(mapped, "profile")["status"] == "verified"
    assert owner.verify(b"content", "future-schema")["status"] == "conflict"
    with pytest.raises(owner.OwnershipConflict):
        owner.remap(b"content", "future-schema", {})


def test_instruction_edit_removed_by_sanitizer_never_regains_marker():
    body = b"Read C:/Example/source.md\n"
    intact = b"<!-- claude-profile-sync:begin sha256=" + owner.digest(body).encode() + b" -->\n" + body + b"<!-- claude-profile-sync:end -->\n"
    edited = intact.replace(body, body + b"user edit\n")
    changed, conflicts = owner.remap_members({".codex/AGENTS.md": edited}, {".codex/AGENTS.md": intact})
    assert conflicts
    assert owner.verify(changed[".codex/AGENTS.md"], "instruction")["status"] == "conflict"
    assert owner.verify(owner.remap(intact, "instruction", {"C:/Example": "D:/Synthetic"}), "instruction")["status"] == "verified"


def test_native_memory_and_durable_state_are_opaque():
    data = b'\xffC:/Example\r\n<!-- claude-profile-sync:bad -->'
    members = {".codex/memories/MEMORY.md": data, ".codex/claude-sync/memory-outbox/history.json": data}
    mapped = json.loads(owner.remap(envelope(members), "profile", {"C:/Example": "D:/Synthetic"}))
    assert all(bytes.fromhex(v) == data for v in mapped["members"].values())


def test_link_group_requires_explicit_original_link_evidence():
    home = "C:/Example"
    target, source = home + "/.agents/skills/example", home + "/sources/example"
    state = {"kind": "link", "target": source, "link_type": "junction"}
    members = {".codex/claude-sync/managed-skills.json": owner.encoded({target: source}),
               ".codex/claude-sync/managed-artifacts.json": owner.encoded({"version": 1, "items": {
                   target: {"owner": owner.OWNER, "source": source + "/SKILL.md", "original": state}}})}
    links = {".agents/skills/example": state}
    request = json.loads(envelope(members, home)); request["links"] = links
    mapped = owner.remap(owner.encoded(request), "profile", {home: "D:/Synthetic"})
    assert owner.verify(mapped, "profile")["status"] == "verified"
    assert owner.verify(envelope(members, home), "profile")["status"] == "conflict"
    changed_links = {".agents/skills/example": {**state, "target": home + "/user-source"}}
    result, conflicts = owner.remap_members(members, members, home=home, links=changed_links)
    assert conflicts
    # Legacy readers must not see target-to-source strings after conflict.
    assert not any(isinstance(v, str) for v in json.loads(result[".codex/claude-sync/managed-skills.json"]).values())


def test_duplicate_markers_and_nonpath_mappings_conflict():
    body = b"synthetic\n"
    block = b"<!-- claude-profile-sync:begin sha256=" + owner.digest(body).encode() + b" -->\n" + body + b"<!-- claude-profile-sync:end -->\n"
    assert owner.verify(block + block, "instruction")["status"] == "conflict"
    with pytest.raises(owner.OwnershipConflict):
        owner.remap(block, "instruction", {"synthetic": "replacement"})


def test_public_verification_supplies_byte_spans_and_rejects_malformed_envelope():
    body = "Synthetic Unicode café\n".encode()
    block = b"<!-- claude-profile-sync:begin sha256=" + owner.digest(body).encode() + b" -->\n" + body + b"<!-- claude-profile-sync:end -->"
    result = owner.verify(block, "instruction")
    span = result["markers"][0]
    assert block[span["body_start"]:span["body_end"]] == body
    assert owner.verify(b'{"members": []}', "profile")["status"] == "conflict"


def test_conflict_sentinel_is_never_a_valid_marker(monkeypatch):
    block = b"<!-- claude-profile-sync:begin sha256=" + b"0" * 64 + b" -->\nsynthetic\n<!-- claude-profile-sync:end -->"
    monkeypatch.setattr(owner, "digest", lambda data: owner.ZERO)
    assert owner.verify(block, "instruction")["status"] == "conflict"
