"""Synthetic hook payloads and source programs; never use a real profile."""
import copy
import base64
import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from profile_hooks import plan_hooks, _windows_split
from profile_bridge import ownership


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(data, encoding="utf-8")


def fixture(tmp_path, events=("SessionStart", "PostToolUse", "Stop"), plugin=True):
    home = tmp_path / "synthetic 空间 & home"
    claude, codex = home / ".claude", home / ".codex"
    codex.mkdir(parents=True)
    capture = home / "capture.jsonl"
    names = {"SessionStart": "pw_isolation_guard.py", "PostToolUse": "doc-budget/doc_budget.py", "Stop": "pw-auth.py"}
    hooks = {}
    for event in events:
        script = claude / "scripts" / names[event]
        code = ('# --check-codex-config\nimport sys, json\nfrom pathlib import Path\n'
                f'with Path({str(capture)!r}).open("a", encoding="utf-8") as stream:\n'
                f'    stream.write(json.dumps({{"event": {event!r}, "argv": sys.argv[1:], "input": sys.stdin.read()}}) + "\\n")\n')
        put(script, code)
        argv = [sys.executable, str(script)] + (["absorb"] if event == "Stop" else [])
        hooks[event] = [{"matcher": "Edit|Write" if event == "PostToolUse" else "*", "hooks": [
            {"type": "command", "command": subprocess.list2cmdline(argv), "timeout": 2}]}]
    put(claude / "settings.json", json.dumps({"hooks": hooks}))
    roots = []
    if plugin:
        root = home / "plugin"
        put(root / ".claude-plugin/plugin.json", '{"name":"superpowers"}')
        put(root / "skills/using-superpowers/SKILL.md", "Synthetic private source text; never forward me.")
        roots.append(root)
    return home, claude, codex, roots, capture


def apply(outputs):
    for path, data in outputs.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)


def run(codex, event, payload=None):
    result = subprocess.run([sys.executable, str(codex / "imports/claude-hooks/stop_bridge.py"), event],
                            input=json.dumps(payload or {"hook_event_name": event, "source": "startup"}),
                            text=True, encoding="utf-8", capture_output=True, check=True)
    assert result.stderr == ""
    return json.loads(result.stdout)


def members(codex):
    return {".codex/" + p.relative_to(codex).as_posix(): p.read_bytes()
            for p in (codex / "hooks.json", codex / "imports/claude-hooks/stop_bridge.py", codex / "imports/claude-hooks/manifest.json")}


def test_native_events_share_one_owned_group_and_recognized_timeouts(tmp_path):
    _, claude, codex, roots, capture = fixture(tmp_path)
    outputs, report = plan_hooks(claude, codex, plugin_roots=roots)
    assert not capture.exists()
    apply(outputs)
    document = json.loads((codex / "hooks.json").read_bytes())
    assert set(document["hooks"]) == {"SessionStart", "PostToolUse", "Stop"}
    for groups in document["hooks"].values():
        handler = groups[0]["hooks"][0]
        assert type(handler["timeout"]) is int
        assert "timeoutSec" not in handler
    assert document["hooks"]["PostToolUse"][0]["matcher"] == "apply_patch"
    assert report["registered"] == 4
    assert ownership.verify_members(members(codex))["status"] == "verified"
    assert plan_hooks(claude, codex, plugin_roots=roots)[0] == {}


def test_startup_readonly_source_and_context_without_raw_skill_text(tmp_path):
    _, claude, codex, roots, capture = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    output = run(codex, "SessionStart")
    context = output["hookSpecificOutput"]
    assert context["hookEventName"] == "SessionStart"
    text = context["additionalContext"]
    assert "llmcall" in text and "using-superpowers/SKILL.md" in text.replace("\\", "/")
    assert "entire context" in text and "absorb" in text and "reconnect" in text
    assert "Synthetic private source text" not in text
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [call["event"] for call in calls] == ["SessionStart"]
    assert calls[0]["argv"] == ["--check-codex-config", str(codex / "config.toml")]


@pytest.mark.parametrize("event,name", [("SessionStart", "pw_isolation_guard.py"), ("PostToolUse", "doc-budget/doc_budget.py")])
def test_source_drift_never_executes_or_discloses_source(tmp_path, event, name):
    home, claude, codex, roots, capture = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    put(claude / "scripts" / name, "raise Exception('synthetic-secret')")
    payload = patch_payload(home, "*** Begin Patch\n*** Add File: x.md\n+x\n*** End Patch", "A x.md")
    output = run(codex, event, payload if event == "PostToolUse" else None)
    assert "failed" in json.dumps(output) and "synthetic-secret" not in json.dumps(output)
    assert not capture.exists()


def patch_payload(cwd, command, files):
    return {"hook_event_name": "PostToolUse", "cwd": str(cwd), "tool_name": "apply_patch",
            "tool_input": {"command": command},
            "tool_response": "Success. Updated the following files:\n" + files + "\n"}


def test_post_tool_maps_multiple_files_moves_and_data_without_shell(tmp_path):
    home, claude, codex, roots, capture = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    for path in ("first file.md", "moved.md", "$(echo injected).md"):
        put(home / path, "synthetic")
    patch = "*** Begin Patch\n*** Add File: first file.md\n+synthetic-secret\n*** Update File: old.md\n*** Move to: moved.md\n@@\n-a\n+b\n*** Delete File: gone.md\n*** Add File: $(echo injected).md\n+content\n*** End Patch"
    output = run(codex, "PostToolUse", patch_payload(home, patch, "A first file.md\nM moved.md\nD gone.md\nA $(echo injected).md"))
    assert output == {}
    calls = [json.loads(line) for line in capture.read_text(encoding="utf-8").splitlines()]
    assert [json.loads(call["input"])["tool_input"]["file_path"] for call in calls] == [
        str(home / "first file.md"), str(home / "moved.md"), str(home / "$(echo injected).md")]
    assert all(call["argv"] == [] for call in calls)
    assert "synthetic-secret" not in capture.read_text()


def test_patch_targets_deduplicate_and_ignore_opaque_response_paths(tmp_path):
    home, claude, codex, _, capture = fixture(tmp_path, events=("PostToolUse",), plugin=False)
    apply(plan_hooks(claude, codex)[0])
    patch = "*** Begin Patch\n*** Update File: doc.md\n@@\n-a\n+b\n*** Update File: ./doc.md\n@@\n-b\n+c\n*** Add File: second.md\n+*** Update File: synthetic-secret.md\n*** End Patch"
    payload = patch_payload(home, patch, "M untrusted-output-path.md")
    payload["tool_response"] = "synthetic-secret-response with unrelated file paths"
    assert run(codex, "PostToolUse", payload) == {}
    calls = [json.loads(line) for line in capture.read_text().splitlines()]
    assert [json.loads(call["input"])["tool_input"]["file_path"] for call in calls] == [str(home / "doc.md"), str(home / "second.md")]
    assert "synthetic-secret" not in capture.read_text()


@pytest.mark.parametrize("response", [{"output": "Success", "file_path": "invented.md"}, None, [], 0])
def test_guessed_apply_patch_response_shapes_never_execute_sources(tmp_path, response):
    home, claude, codex, _, capture = fixture(tmp_path, events=("PostToolUse",), plugin=False)
    apply(plan_hooks(claude, codex)[0])
    payload = patch_payload(home, "*** Begin Patch\n*** Add File: x.md\n+x\n*** End Patch", "A x.md")
    payload["tool_response"] = response
    assert "failed" in json.dumps(run(codex, "PostToolUse", payload))
    assert not capture.exists()


@pytest.mark.parametrize("damage", ["bridge", "handler", "duplicate", "missing", "manifest"])
def test_v2_damage_cannot_be_reauthorized_by_redaction_or_sync(tmp_path, damage):
    _, claude, codex, roots, _ = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    original = members(codex)
    changed = copy.deepcopy(original)
    key = ".codex/hooks.json"
    doc = json.loads(changed[key])
    if damage == "bridge":
        changed[".codex/imports/claude-hooks/stop_bridge.py"] += b"# edited\n"
    elif damage == "manifest":
        key = ".codex/imports/claude-hooks/manifest.json"
        doc = json.loads(changed[key]); doc["source"] = "edited"
        changed[key] = json.dumps(doc).encode()
    else:
        if damage == "handler":
            doc["hooks"]["SessionStart"][0]["hooks"][0]["timeoutSec"] = 999
        elif damage == "duplicate":
            doc["hooks"]["PostToolUse"] *= 2
        else:
            doc["hooks"]["SessionStart"] = []
        changed[key] = json.dumps(doc).encode()
    mapped, conflicts = ownership.remap_members(changed, original)
    assert conflicts
    assert ownership.verify_members(mapped)["status"] == "conflict"
    for path, data in changed.items():
        (codex.parent / path).write_bytes(data)
    assert plan_hooks(claude, codex, plugin_roots=roots)[0] == {}


def test_v2_restore_redaction_rehashes_each_event(tmp_path):
    home, claude, codex, roots, _ = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    envelope = ownership.encoded({"home": str(home), "members": {k: v.hex() for k, v in members(codex).items()}})
    moved = ownership.remap(envelope, "profile", {str(home): str(tmp_path / "restored")})
    assert ownership.verify(moved, "profile")["status"] == "verified"
    mapped = json.loads(bytes.fromhex(json.loads(moved)["members"][".codex/hooks.json"]))
    for groups in mapped["hooks"].values():
        command = groups[0]["hooks"][0]["commandWindows"]
        decoded = base64.b64decode(command.rsplit(" ", 1)[1]).decode("utf-16-le")
        assert str(home) not in decoded
        assert str(tmp_path / "restored") in decoded


def test_disabled_hooks_retire_owned_events_and_missing_sources_preserve(tmp_path):
    _, claude, codex, roots, _ = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    source = claude / "scripts/pw_isolation_guard.py"
    source.unlink()
    assert plan_hooks(claude, codex, plugin_roots=roots)[0] == {}
    settings = json.loads((claude / "settings.json").read_text())
    settings["disableAllHooks"] = True
    put(claude / "settings.json", json.dumps(settings))
    outputs, _ = plan_hooks(claude, codex, plugin_roots=roots)
    apply(outputs)
    assert all(not groups for groups in json.loads((codex / "hooks.json").read_bytes())["hooks"].values())
    assert not (codex / "imports/claude-hooks/manifest.json").exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command launcher")
def test_windows_encoded_launcher_preserves_json_stdin(tmp_path):
    home, claude, codex, roots, capture = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    handler = json.loads((codex / "hooks.json").read_bytes())["hooks"]["PostToolUse"][0]["hooks"][0]
    payload = patch_payload(home, "*** Begin Patch\n*** Add File: file & name.md\n+data\n*** End Patch", "A file & name.md")
    result = subprocess.run(_windows_split(handler["commandWindows"]), input=json.dumps(payload),
                            capture_output=True, text=True, encoding="utf-8", timeout=15)
    assert result.returncode == 0 and not result.stderr
    assert json.loads(result.stdout) == {}
    assert json.loads(json.loads(capture.read_text())["input"])["tool_input"]["file_path"] == str(home / "file & name.md")


@pytest.mark.parametrize("payload", [
    {"hook_event_name": "Stop"},
    {"hook_event_name": "PostToolUse", "tool_name": "exec_command", "tool_input": {"command": "echo synthetic-secret"}},
    {"hook_event_name": "PostToolUse", "tool_name": "apply_patch", "tool_input": {"file_path": "synthetic-secret"}},
    {"hook_event_name": "PostToolUse", "tool_name": "apply_patch", "tool_input": []},
])
def test_unknown_payloads_are_fixed_failures_without_source_execution(tmp_path, payload):
    _, claude, codex, roots, capture = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    output = run(codex, "PostToolUse", payload)
    assert "failed" in json.dumps(output) and "synthetic-secret" not in json.dumps(output)
    assert not capture.exists()


def test_doc_feedback_reaches_context_without_child_output(tmp_path):
    home, claude, codex, roots, capture = fixture(tmp_path)
    put(claude / "scripts/doc-budget/doc_budget.py", "import sys\nprint('synthetic-secret')\nprint('synthetic-secret', file=sys.stderr)\nsys.exit(2)\n")
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    payload = patch_payload(home, "*** Begin Patch\n*** Update File: doc.md\n@@\n-a\n+b\n*** End Patch", "M doc.md")
    output = run(codex, "PostToolUse", payload)
    assert "document budget" in output["hookSpecificOutput"]["additionalContext"]
    assert "synthetic-secret" not in json.dumps(output)


def test_upgrade_v1_preserves_native_groups(tmp_path):
    from test_ownership_contract import make_hooks
    _, claude, codex, roots, _ = fixture(tmp_path)
    old = make_hooks()
    old_doc = json.loads(old[".codex/hooks.json"])
    native = {"hooks": [{"type": "command", "command": "native-start", "timeoutSec": 7}]}
    old_doc["hooks"]["SessionStart"] = [native]
    old[".codex/hooks.json"] = json.dumps(old_doc).encode()
    apply({codex.parent / path: data for path, data in old.items()})
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    assert json.loads((codex / "hooks.json").read_bytes())["hooks"]["SessionStart"][0] == native
    assert json.loads((codex / "imports/claude-hooks/manifest.json").read_bytes())["version"] == 2
    assert ownership.verify_members(members(codex))["status"] == "verified"


def test_missing_superpowers_preserves_wrapper_but_disabled_plugin_retires_it(tmp_path):
    _, claude, codex, roots, _ = fixture(tmp_path, events=())
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    (roots[0] / "skills/using-superpowers/SKILL.md").unlink()
    assert plan_hooks(claude, codex, plugin_roots=roots)[0] == {}
    put(claude / "settings.json", '{"enabledPlugins":{"superpowers@example":false},"hooks":{}}')
    outputs, _ = plan_hooks(claude, codex, plugin_roots=roots)
    apply(outputs)
    assert json.loads((codex / "hooks.json").read_bytes())["hooks"]["SessionStart"] == []


def test_enabled_superpowers_missing_from_catalog_preserves_owned_hook(tmp_path):
    _, claude, codex, roots, _ = fixture(tmp_path, events=())
    put(claude / "settings.json", '{"enabledPlugins":{"superpowers@example":true},"hooks":{}}')
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    outputs, report = plan_hooks(claude, codex, plugin_roots=[])
    assert outputs == {}
    assert any(row["status"] == "unavailable" for row in report["entrypoint_checks"])


def test_redaction_cannot_change_v2_manifest_authority(tmp_path):
    _, claude, codex, roots, _ = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    original = members(codex)
    changed = dict(original)
    key = ".codex/imports/claude-hooks/manifest.json"
    manifest = json.loads(changed[key])
    manifest["version"] = 1
    changed[key] = json.dumps(manifest).encode()
    mapped, conflicts = ownership.remap_members(original, changed)
    assert conflicts
    assert ownership.verify_members(mapped)["status"] == "conflict"


@pytest.mark.parametrize("event", ["SessionStart", "PostToolUse"])
def test_malformed_source_groups_preserve_existing_owned_hooks(tmp_path, event):
    _, claude, codex, roots, _ = fixture(tmp_path)
    apply(plan_hooks(claude, codex, plugin_roots=roots)[0])
    put(claude / "settings.json", json.dumps({"hooks": {event: "malformed"}}))
    assert plan_hooks(claude, codex, plugin_roots=roots)[0] == {}


def test_actual_document_source_retains_watermark_and_globs(tmp_path):
    configured = os.environ.get("PROFILE_BRIDGE_TEST_SCRIPTS")
    if not configured:
        pytest.skip("source-helper integration requires PROFILE_BRIDGE_TEST_SCRIPTS")
    source = Path(configured) / "doc-budget/doc_budget.py"
    if not source.is_file():
        pytest.skip("adjacent source worktree unavailable")
    home, claude, codex, roots, _ = fixture(tmp_path, events=("PostToolUse",), plugin=False)
    doc_script = claude / "scripts/doc-budget/doc_budget.py"
    doc_script.write_bytes(source.read_bytes())
    apply(plan_hooks(claude, codex)[0])
    managed = claude / "projects/example/memory/reference_example.md"
    native = codex / "memories/example.md"
    put(managed, "synthetic original\n")
    put(native, "synthetic native\n")
    patch = "*** Begin Patch\n*** Update File: .claude/projects/example/memory/reference_example.md\n@@\n-old\n+synthetic original\n*** Update File: .codex/memories/example.md\n@@\n-old\n+synthetic native\n*** End Patch"
    payload = patch_payload(home, patch, "M .claude/projects/example/memory/reference_example.md\nM .codex/memories/example.md")
    assert run(codex, "PostToolUse", payload) == {}
    lock = doc_script.with_name("sizes.lock.json")
    watermark = json.loads(lock.read_text(encoding="utf-8"))
    assert list(watermark) == [str(managed).replace("\\", "/")]
    put(managed, "synthetic original\nsynthetic addition\n")
    output = run(codex, "PostToolUse", payload)
    assert "document budget" in output["hookSpecificOutput"]["additionalContext"]
    assert "synthetic addition" not in json.dumps(output)
    assert "synthetic original" not in lock.read_text()


def test_actual_start_guard_is_readonly_and_dependency_drift_is_rejected(tmp_path, monkeypatch):
    configured = os.environ.get("PROFILE_BRIDGE_TEST_SCRIPTS")
    if not configured:
        pytest.skip("source-helper integration requires PROFILE_BRIDGE_TEST_SCRIPTS")
    scripts = Path(configured)
    if not (scripts / "pw_isolation_guard.py").is_file():
        pytest.skip("adjacent source worktree unavailable")
    home, claude, codex, _, _ = fixture(tmp_path, events=("SessionStart",), plugin=False)
    for name in ("pw_isolation_guard.py", "pw-auth.py"):
        (claude / "scripts" / name).write_bytes((scripts / name).read_bytes())
    for variable in ("HOME", "USERPROFILE"):
        monkeypatch.setenv(variable, str(home))
    state = '{"cookies":[],"origins":[]}'
    put(home / ".pw-auth/store/synthetic.json", state)
    put(home / ".pw-auth/shared.json", state)
    put(home / ".pw-auth/incoming/pending.json", state)
    put(claude / "plugins/cache/example/plugin/stale/keep.txt", "keep")
    put(codex / "config.toml", '[mcp_servers.playwright]\ncommand="npx"\nargs=' + json.dumps([
        "@playwright/mcp@latest", "--isolated", "--storage-state", str(home / ".pw-auth/shared.json"),
        "--output-dir", str(home / ".playwright-mcp-output")]))
    apply(plan_hooks(claude, codex)[0])
    before = {str(p): p.read_bytes() for base in (home / ".pw-auth", claude / "plugins") for p in base.rglob("*") if p.is_file()}
    output = run(codex, "SessionStart")
    assert "systemMessage" not in output
    assert before == {str(p): p.read_bytes() for base in (home / ".pw-auth", claude / "plugins") for p in base.rglob("*") if p.is_file()}
    dependency = claude / "scripts/pw-auth.py"
    dependency.write_bytes(dependency.read_bytes() + b"\nraise Exception('synthetic-secret')\n")
    output = run(codex, "SessionStart")
    assert "failed" in json.dumps(output) and "synthetic-secret" not in json.dumps(output)
