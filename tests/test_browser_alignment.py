"""Generated synthetic homes only; never load browser credentials or launch a browser."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib

import pytest

from profile_bridge import ownership
from profile_config import plan_config
from profile_hooks import plan_hooks, plan_entrypoint_checks, run_entrypoint_check


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = Path(os.environ["PROFILE_BRIDGE_TEST_SCRIPTS"]) if os.environ.get("PROFILE_BRIDGE_TEST_SCRIPTS") else None


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))


def put_json(path, data):
    put(path, json.dumps(data))


def browser_home(tmp_path):
    home = tmp_path / "synthetic home"
    claude, codex, plugin = home / ".claude", home / ".codex", home / "plugin"
    put_json(claude / "settings.json", {})
    put_json(home / ".pw-auth/shared.json", {"cookies": [], "origins": []})
    spec = {"command": "npx", "args": ["@playwright/mcp@latest", "--isolated", "--storage-state",
            str(home / ".pw-auth/shared.json"), "--output-dir", str(home / ".playwright-mcp-output")]}
    put_json(plugin / ".mcp.json", {"playwright": spec})
    original = (b'project_doc_fallback_filenames = ["CLAUDE.md"]\n'
                b'model = "synthetic"\n'
                b'[mcp_servers.playwright]\ncommand = "npx"\n'
                b'args = ["-y", "@playwright/mcp@latest", "--browser", "chromium"]\n'
                b'enabled = false\nstartup_timeout_sec = 45\ntool_timeout_sec = 90\n'
                b'enabled_tools = ["browser_snapshot"]\n'
                b'[mcp_servers.playwright.env]\nSYNTHETIC = "keep"\n'
                b'[mcp_servers.other]\ncommand = "preserve-other"\n'
                b'[profiles.example]\nmodel = "preserve-profile"\n')
    put(codex / "config.toml", original)
    return home, claude, codex, plugin, spec, original


def parsed(data):
    return tomllib.loads(data.decode("utf-8-sig"))


def test_opt_in_preserves_all_unrelated_fields_and_bytes_then_converges(tmp_path):
    _, claude, codex, plugin, spec, original = browser_home(tmp_path)
    assert plan_config(claude, codex, [plugin])[0] == original
    merged, report = plan_config(claude, codex, [plugin], adopt_playwright=True)
    expected = parsed(original)
    expected["mcp_servers"]["playwright"]["args"] += spec["args"][1:]
    assert parsed(merged) == expected
    assert original.split(b'enabled = false')[1] in merged
    assert report["mcp"][0]["ownership"] == "playwright_args_only"
    assert ownership.verify_members({".codex/config.toml": merged})["status"] == "verified"
    put(codex / "config.toml", merged)
    repeated, report = plan_config(claude, codex, [plugin])
    assert repeated == merged
    assert not report["changed"]


def test_native_settings_can_change_after_adoption_but_owned_args_cannot(tmp_path):
    _, claude, codex, plugin, _, _ = browser_home(tmp_path)
    merged, _ = plan_config(claude, codex, [plugin], adopt_playwright=True)
    edited = merged.replace(b'tool_timeout_sec = 90', b'tool_timeout_sec = 120')
    put(codex / "config.toml", edited)
    assert plan_config(claude, codex, [plugin])[0] == edited
    damaged = edited.replace(b'"--isolated", ', b'')
    put(codex / "config.toml", damaged)
    result, report = plan_config(claude, codex, [plugin], adopt_playwright=True)
    assert result == damaged
    assert report["mcp"][0]["reason"] == "managed_block_modified_by_user"


@pytest.mark.parametrize("mutation", ["per_site", "no_isolation", "persistent", "duplicate", "remote", "missing_union"])
def test_invalid_source_policy_fails_closed_without_adopting(tmp_path, mutation):
    home, claude, codex, plugin, spec, original = browser_home(tmp_path)
    if mutation == "per_site":
        spec["args"][3] = str(home / ".pw-auth/store/site.json")
    elif mutation == "no_isolation":
        spec["args"].remove("--isolated")
    elif mutation == "persistent":
        spec["args"] += ["--user-data-dir", str(home / "profile")]
    elif mutation == "duplicate":
        spec["args"] += ["--isolated"]
    elif mutation == "remote":
        spec = {"url": "https://example.com/mcp"}
    else:
        (home / ".pw-auth/shared.json").unlink()
    put_json(plugin / ".mcp.json", {"playwright": spec})
    result, report = plan_config(claude, codex, [plugin], adopt_playwright=True)
    assert result == original
    assert report["mcp"][0]["status"] == "conflict"


@pytest.mark.parametrize("args", [b'command = "custom-wrapper"', b'command = "npx"\nurl = "https://example.com/mcp"'])
def test_arbitrary_native_transport_is_never_forced(tmp_path, args):
    _, claude, codex, plugin, _, original = browser_home(tmp_path)
    original = original.replace(b'command = "npx"', args)
    put(codex / "config.toml", original)
    assert plan_config(claude, codex, [plugin], adopt_playwright=True)[0] == original


def test_source_degradation_or_disappearance_cannot_remove_adopted_policy(tmp_path):
    _, claude, codex, plugin, spec, _ = browser_home(tmp_path)
    adopted, _ = plan_config(claude, codex, [plugin], adopt_playwright=True)
    put(codex / "config.toml", adopted)
    spec["args"].remove("--isolated")
    put_json(plugin / ".mcp.json", {"playwright": spec})
    assert plan_config(claude, codex, [plugin])[0] == adopted
    put_json(plugin / ".mcp.json", {})
    result, report = plan_config(claude, codex, [plugin])
    assert result == adopted
    assert report["mcp"][0]["status"] == "unavailable"
    spec["disabled"] = True
    put_json(plugin / ".mcp.json", {"playwright": spec})
    assert plan_config(claude, codex, [plugin])[0] == adopted


def test_bom_crlf_multiline_and_fake_args_in_string(tmp_path):
    _, claude, codex, plugin, _, original = browser_home(tmp_path)
    original = original.replace(b'model = "synthetic"', b'model = "synthetic"\nnote = """\nargs = ["fake"]\n"""')
    original = original.replace(b'args = ["-y", "@playwright/mcp@latest", "--browser", "chromium"]',
                                b'"args" = [\n "@playwright/mcp@latest",\n]')
    original = b'\xef\xbb\xbf' + original.replace(b'\n', b'\r\n')
    put(codex / "config.toml", original)
    merged, _ = plan_config(claude, codex, [plugin], adopt_playwright=True)
    assert merged != original
    assert parsed(merged)["note"] == parsed(original)["note"]
    assert merged.startswith(b'\xef\xbb\xbf')
    assert b'\n' not in merged.replace(b'\r\n', b'')
    put(codex / "config.toml", merged)
    assert plan_config(claude, codex, [plugin])[0] == merged


def test_existing_apply_backups_and_rollback_cover_adoption(tmp_path):
    import profile_sync as sync
    _, claude, codex, plugin, _, original = browser_home(tmp_path)
    skills = codex.parent / ".agents/skills"
    merged, _ = plan_config(claude, codex, [plugin], adopt_playwright=True)
    path = codex / "config.toml"
    change = {"path": str(path), "before": sync.snapshot(path), "after": {"kind": "file", "sha256": sync.digest(merged)},
              "data": merged, "append_only": False}
    result = sync.apply_plan([change], {}, codex, skills)
    backup = Path(result["backup"])
    assert (backup / "00000.bin").read_bytes() == original
    assert path.read_bytes() == merged
    sync.rollback(backup, codex, skills)
    assert path.read_bytes() == original
    put(path, original + b'# intervening user edit\n')
    with pytest.raises(ValueError, match="changed"):
        sync.apply_plan([change], {}, codex, skills)
    assert path.read_bytes().endswith(b'# intervening user edit\n')


def load_script(name):
    if SCRIPTS is None or not (SCRIPTS / (name + ".py")).is_file():
        pytest.skip("source-helper integration requires PROFILE_BRIDGE_TEST_SCRIPTS")
    spec = importlib.util.spec_from_file_location("synthetic_" + name, SCRIPTS / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def state(domain, expiry=100, value="synthetic-cookie"):
    return {"cookies": [{"name": "session", "value": value, "domain": domain, "path": "/", "expires": expiry}],
            "origins": [{"origin": "https://" + domain, "localStorage": [{"name": "synthetic", "value": "fixture"}]}]}


def test_guard_uses_every_shard_and_absorbs_full_context_without_pruning(tmp_path, monkeypatch, capsys):
    home = tmp_path / "synthetic home"
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    guard = load_script("pw_isolation_guard")
    authdir = home / ".pw-auth"
    put_json(authdir / "store/alpha.json", state("alpha.example.com"))
    put_json(authdir / "store/beta.json", state("beta.example.com"))
    put_json(authdir / "shared.json", state("old.example.com"))
    put_json(authdir / "incoming/context.json", state("new.example.com"))
    monkeypatch.setattr(guard, "prune_stale", lambda *a: pytest.fail("must not prune"))
    monkeypatch.setattr(guard, "patch_isolation", lambda *a: pytest.fail("must not patch plugins"))
    monkeypatch.setattr(sys, "argv", ["guard", "--prepare-shared-state"])
    assert guard.main() == 0
    shared = json.loads((authdir / "shared.json").read_bytes())
    assert {c["domain"] for c in shared["cookies"]} == {"alpha.example.com", "beta.example.com", "new.example.com"}
    assert len(shared["origins"]) == 3
    assert not (authdir / "incoming/context.json").exists()
    assert list((authdir / "consumed").glob("*.json"))
    assert capsys.readouterr() == ("", "")
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in authdir.rglob("*") if p.is_file()}
    assert guard.main() == 0
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in authdir.rglob("*") if p.is_file()}
    config = home / ".codex/config.toml"
    put(config, '[mcp_servers.playwright]\ncommand="npx"\nargs=' + json.dumps(guard.WANT_ARGS) + '\n')
    monkeypatch.setattr(sys, "argv", ["guard", "--check-codex-config", str(config)])
    before = {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    assert guard.main() == 0
    assert before == {p: p.read_bytes() for p in home.rglob("*") if p.is_file()}
    put_json(authdir / "store/extra.json", state("extra.example.com"))
    assert guard.main() == 1
    assert "synthetic-cookie" not in capsys.readouterr().err


@pytest.mark.parametrize("mutation", ["launcher", "duplicate", "per_site", "persistent", "corrupt_state"])
def test_readonly_guard_rejects_bad_policy_and_redacts_errors(tmp_path, monkeypatch, capsys, mutation):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    guard = load_script("pw_isolation_guard")
    auth = guard.auth_module()
    saved = state("alpha.example.com")
    put_json(tmp_path / ".pw-auth/store/alpha.json", saved)
    put_json(tmp_path / ".pw-auth/shared.json", auth.merge_states([saved]))
    args = list(guard.WANT_ARGS)
    command = "npx"
    if mutation == "launcher":
        command = "unreviewed-wrapper"
    elif mutation == "duplicate":
        args.append("--isolated")
    elif mutation == "per_site":
        args[3] = str(tmp_path / ".pw-auth/store/alpha.json")
    elif mutation == "persistent":
        args.extend(["--user-data-dir", str(tmp_path / "persistent")])
    else:
        put(tmp_path / ".pw-auth/shared.json", '{"synthetic-private-value":')
    config = tmp_path / ".codex/config.toml"
    put(config, '[mcp_servers.playwright]\ncommand=' + json.dumps(command) + '\nargs=' + json.dumps(args) + '\n')
    monkeypatch.setattr(sys, "argv", ["guard", "--check-codex-config", str(config)])
    assert guard.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "synthetic-private-value" not in output.err
    assert "check failed" in output.err


def test_uninitialized_union_is_never_replaced_with_empty_seed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    guard = load_script("pw_isolation_guard")
    monkeypatch.setattr(sys, "argv", ["guard", "--prepare-shared-state"])
    assert guard.main() == 1
    assert not (tmp_path / ".pw-auth/shared.json").exists()
    assert "check failed" in capsys.readouterr().err


def test_export_reminder_checks_configured_output_directory(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    guard = load_script("pw_export_guard")
    put(tmp_path / ".playwright-mcp-output/page-example.txt", "synthetic capture")
    assert guard.main() == 0
    message = capsys.readouterr().err
    assert "Export the full context" in message
    assert "indexedDB:true" in message
    assert "unique incoming filename" in message
    assert "pw-auth.py absorb" in message
    assert "probably dead" not in message
    put(tmp_path / ".pw-auth/incoming/unique.json", "{}")
    assert guard.main() == 0
    assert capsys.readouterr().err == ""


def test_explicit_entrypoints_reuse_scripts_alongside_proven_native_events(tmp_path):
    home, claude, codex, plugin, _, _ = browser_home(tmp_path)
    sentinel = home / "explicit-check"
    guard = claude / "scripts/pw_isolation_guard.py"
    doc = claude / "scripts/doc-budget/doc_budget.py"
    put(guard, f'# --check-codex-config\nfrom pathlib import Path\nPath({str(sentinel)!r}).write_text("checked")\n')
    put(doc, 'import json, sys\npayload=json.load(sys.stdin)\nassert payload["tool_input"]["file_path"]\nprint("synthetic-private-output", file=sys.stderr)\nsys.exit(2)\n')
    command = lambda path: {"type": "command", "command": subprocess.list2cmdline([sys.executable, str(path)])}
    put_json(claude / "settings.json", {"hooks": {"SessionStart": [{"hooks": [command(guard)]}],
             "PostToolUse": [{"matcher": "Edit|Write", "hooks": [command(doc)]}]}})
    put_json(plugin / ".claude-plugin/plugin.json", {"name": "superpowers"})
    put(plugin / "skills/using-superpowers/SKILL.md", "Synthetic startup instruction.")
    outputs, report = plan_hooks(claude, codex, plugin_roots=[plugin])
    assert codex / "hooks.json" in outputs
    assert report["registered"] == 3
    assert len(report["entrypoint_checks"]) == 3
    assert not sentinel.exists()
    assert run_entrypoint_check(claude, codex, "pw_isolation_guard.py")["status"] == "passed"
    assert sentinel.exists()
    result = run_entrypoint_check(claude, codex, "doc_budget.py", file_path=home / "document.md")
    assert result == {"status": "warning", "reason": "source_document_budget_feedback"}
    assert run_entrypoint_check(claude, codex, "doc_budget.py")["status"] == "unavailable"
    assert all(not row["registered"] for row in plan_entrypoint_checks(claude, codex, [plugin]))


def test_stop_bridge_orders_absorb_before_export_reminder(tmp_path):
    _, claude, codex, _, _, _ = browser_home(tmp_path)
    entries = []
    for name, tail in [("pw_export_guard.py", []), ("pw-auth.py", ["absorb"])]:
        script = claude / "scripts" / name
        put(script, 'print("synthetic output")\n')
        entries.append({"type": "command", "command": subprocess.list2cmdline([sys.executable, str(script), *tail])})
    put_json(claude / "settings.json", {"hooks": {"Stop": [{"hooks": entries}]}})
    outputs, _ = plan_hooks(claude, codex)
    bridge = outputs[codex / "imports/claude-hooks/stop_bridge.py"]
    assert bridge.index(b"'name': 'pw-auth.py'") < bridge.index(b"'name': 'pw_export_guard.py'")
