"""Session launch adoption tests use generated homes and never start a browser."""
import json
import tomllib

import pytest

from profile_bridge import ownership
from profile_config import plan_config, plan_playwright_config
from tools.make_fixtures import make_browser_session_fixture


def parse(data):
    return tomllib.loads(data.decode("utf-8-sig"))


def plan(fixture, *, adopt=True):
    return plan_playwright_config(fixture["claude"], fixture["codex"], [fixture["plugin"]],
                                  adopt_playwright=adopt)


def save_source(fixture):
    (fixture["plugin"] / ".mcp.json").write_text(json.dumps({"playwright": fixture["source"]}), encoding="utf-8")


def test_session_launcher_adopts_launch_and_timeout_and_preserves_native_init_page(tmp_path):
    fixture = make_browser_session_fixture(tmp_path)
    original = fixture["original"]
    assert plan(fixture, adopt=False)[0] == original
    merged, report = plan(fixture)
    expected = parse(original)
    native = expected["mcp_servers"]["playwright"]
    native["command"] = fixture["source"]["command"]
    native["args"] = [*fixture["source"]["args"][:3], *native["args"], *fixture["source"]["args"][4:]]
    native["startup_timeout_sec"] = 120
    assert parse(merged) == expected
    assert original.split(b'tool_timeout_sec = 90')[1] in merged
    assert report["mcp"][0]["ownership"] == "playwright_command_args_startup_timeout"
    assert ownership.verify_members({".codex/config.toml": merged})["status"] == "verified"
    (fixture["codex"] / "config.toml").write_bytes(merged)
    assert plan(fixture, adopt=False)[0] == merged
    # Ordinary future sync uses the same alignment implementation.
    ordinary, _ = plan_config(fixture["claude"], fixture["codex"], [fixture["plugin"]])
    assert parse(ordinary)["mcp_servers"]["playwright"] == native


@pytest.mark.parametrize("tampered_field", ["command", "startup_timeout_sec"])
def test_legacy_owned_args_upgrade_and_new_owned_field_tampering(tmp_path, tampered_field):
    fixture = make_browser_session_fixture(tmp_path, wrapped=False)
    legacy, _ = plan(fixture)
    (fixture["codex"] / "config.toml").write_bytes(legacy)
    source = fixture["source"]
    import sys
    fixture["source"] = {"command": sys.executable, "args": [str(fixture["wrapper"]), "--", source["command"], *source["args"]]}
    save_source(fixture)
    merged, report = plan(fixture, adopt=False)
    assert merged != legacy
    assert report["mcp"][0]["ownership"] == "playwright_command_args_startup_timeout"
    assert parse(merged)["mcp_servers"]["playwright"]["startup_timeout_sec"] == 120
    assert merged.count(b'startup_timeout_sec =') == 1
    config = fixture["codex"] / "config.toml"
    config.write_bytes(merged)
    assert plan(fixture, adopt=False)[0] == merged
    damaged = (merged.replace(b'command = ', b'command = "tampered" # ', 1)
               if tampered_field == "command" else
               merged.replace(b'startup_timeout_sec = 120', b'startup_timeout_sec = 240', 1))
    config.write_bytes(damaged)
    result, report = plan(fixture)
    assert result == damaged
    assert report["mcp"][0]["status"] == "conflict"


@pytest.mark.parametrize("timeout,expected", [
    (None, 120), (0, 120), (-5, 120), (45, 120), (119.5, 120),
    (120, 120), (120.0, 120.0), (300, 300), (300.5, 300.5),
])
def test_session_startup_timeout_has_a_floor_and_preserves_larger_values(tmp_path, timeout, expected):
    fixture = make_browser_session_fixture(tmp_path, startup_timeout=timeout)
    merged, report = plan(fixture)
    assert report["changed"]
    assert parse(merged)["mcp_servers"]["playwright"]["startup_timeout_sec"] == expected
    assert merged.count(b'startup_timeout_sec =') == 1
    config = fixture["codex"] / "config.toml"
    config.write_bytes(merged)
    repeated, report = plan(fixture, adopt=False)
    assert repeated == merged
    assert not report["changed"]
    ordinary, _ = plan_config(fixture["claude"], fixture["codex"], [fixture["plugin"]])
    assert parse(ordinary)["mcp_servers"]["playwright"] == parse(merged)["mcp_servers"]["playwright"]


@pytest.mark.parametrize("timeout", [True, False, "45", float('nan'), float('inf'), float('-inf')])
def test_invalid_native_startup_timeout_withholds_session_adoption(tmp_path, timeout):
    fixture = make_browser_session_fixture(tmp_path, startup_timeout=timeout)
    merged, report = plan(fixture)
    assert merged == fixture["original"]
    assert not report["changed"]
    assert report["mcp"][0]["status"] == "conflict"


@pytest.mark.parametrize("timeout,expected", [(None, 120), (45, 120), (300.5, 300.5)])
def test_old_owned_command_args_upgrade_adds_one_timeout_without_rewriting_other_values(tmp_path, timeout, expected):
    fixture = make_browser_session_fixture(tmp_path, startup_timeout=timeout, legacy_owned_command_args=True)
    original = fixture["original"]
    assert ownership.verify_members({".codex/config.toml": original})["status"] == "verified"
    expected_document = parse(original)
    expected_document["mcp_servers"]["playwright"]["startup_timeout_sec"] = expected
    merged, report = plan(fixture, adopt=False)
    assert report["changed"]
    assert parse(merged) == expected_document
    assert merged.count(b'startup_timeout_sec =') == 1
    assert ownership.verify_members({".codex/config.toml": merged})["status"] == "verified"
    (fixture["codex"] / "config.toml").write_bytes(merged)
    repeated, report = plan(fixture, adopt=False)
    assert repeated == merged
    assert not report["changed"]


@pytest.mark.parametrize("timeout", [None, 45, 300.5])
def test_direct_npx_does_not_adopt_or_change_native_startup_timeout(tmp_path, timeout):
    fixture = make_browser_session_fixture(tmp_path, wrapped=False, startup_timeout=timeout)
    merged, report = plan(fixture)
    native = parse(merged)["mcp_servers"]["playwright"]
    assert native.get("startup_timeout_sec") == timeout
    assert report["mcp"][0]["ownership"] == "playwright_args_only"
    if timeout is None:
        assert "startup_timeout_sec" not in native
    else:
        assert b'startup_timeout_sec = ' + json.dumps(timeout).encode() + b'\n' in merged


@pytest.mark.parametrize("mutation", ["foreign_wrapper", "missing_wrapper", "missing_hook", "relative_python", "extra_python_arg", "persistent", "duplicate", "remote"])
def test_unreviewed_or_unsafe_session_sources_are_withheld(tmp_path, mutation):
    fixture = make_browser_session_fixture(tmp_path)
    source = fixture["source"]
    if mutation == "foreign_wrapper":
        source["args"][0] = str(fixture["wrapper"].with_name("other.py"))
    elif mutation == "missing_wrapper":
        fixture["wrapper"].unlink()
    elif mutation == "missing_hook":
        fixture["wrapper"].with_suffix(".cjs").unlink()
    elif mutation == "relative_python":
        source["command"] = "python"
    elif mutation == "extra_python_arg":
        source["args"].insert(0, "-c")
    elif mutation == "persistent":
        source["args"] += ["--user-data-dir", "synthetic-profile"]
    elif mutation == "duplicate":
        source["args"] += ["--isolated"]
    else:
        source["url"] = "https://example.com/mcp"
    save_source(fixture)
    result, report = plan(fixture)
    assert result == fixture["original"]
    assert report["mcp"][0]["status"] == "conflict"


def test_session_launcher_never_silently_downgrades_to_direct_npx(tmp_path):
    fixture = make_browser_session_fixture(tmp_path)
    merged, _ = plan(fixture)
    (fixture["codex"] / "config.toml").write_bytes(merged)
    fixture["source"] = {"command": "npx", "args": fixture["source"]["args"][3:]}
    save_source(fixture)
    result, report = plan(fixture, adopt=False)
    assert result == merged
    assert report["mcp"][0]["reason"] == "playwright_session_launcher_downgrade_refused"


def test_scoped_planner_does_not_add_project_fallback_or_other_mcp(tmp_path):
    fixture = make_browser_session_fixture(tmp_path)
    (fixture["claude"] / ".claude.json").write_text(json.dumps({"mcpServers": {
        "new-source-server": {"command": "synthetic-new-command"}}}), encoding="utf-8")
    merged, report = plan(fixture)
    assert "project_doc_fallback_filenames" not in parse(merged)
    assert set(parse(merged)["mcp_servers"]) == {"playwright", "other"}
    assert [row["name"] for row in report["mcp"]] == ["playwright"]


@pytest.mark.parametrize("owned_shape", ["native", "args", "command_args"])
def test_launch_layout_preserves_bom_crlf_quoted_timeout_and_embedded_fake_assignments(tmp_path, owned_shape):
    fixture = make_browser_session_fixture(tmp_path, wrapped=owned_shape != "args",
        legacy_owned_command_args=owned_shape == "command_args", complex_toml=True)
    original = fixture["original"]
    config = fixture["codex"] / "config.toml"
    config.write_bytes(original)
    if owned_shape == "args":
        old, _ = plan(fixture)
        config.write_bytes(old)
        source = fixture["source"]
        import sys
        fixture["source"] = {"command": sys.executable, "args": [str(fixture["wrapper"]), "--", source["command"], *source["args"]]}
        save_source(fixture)
    merged, report = plan(fixture)
    assert report["changed"]
    assert parse(merged)["note"] == parse(original)["note"]
    assert parse(merged)["mcp_servers"]["playwright"]["startup_timeout_sec"] == 120
    assert merged.startswith(b'\xef\xbb\xbf')
    assert b'\n' not in merged.replace(b'\r\n', b'')
    config.write_bytes(merged)
    assert plan(fixture, adopt=False)[0] == merged


def test_scoped_apply_changes_only_config_and_repeats_without_writes(tmp_path, monkeypatch):
    from profile_bridge.browser import build_playwright_plan
    import profile_sync as sync

    fixture = make_browser_session_fixture(tmp_path)
    monkeypatch.setattr(sync, "build_plan", lambda *a, **k: pytest.fail("full profile planner must not run"))
    roots = [fixture[key] for key in ("claude", "codex", "skills")]
    before = {path: path.read_bytes() for path in fixture["home"].rglob("*") if path.is_file()}
    changes, report = build_playwright_plan(*roots, adopt_playwright=True)
    assert len(changes) == 1
    config = fixture["codex"] / "config.toml"
    assert changes[0]["path"] == str(config)
    assert {path: path.read_bytes() for path in before} == before
    result = sync.apply_plan(changes, report, fixture["codex"], fixture["skills"])
    assert result["status"] == "applied"
    assert config.read_bytes() == changes[0]["data"]
    assert all(path.read_bytes() == data for path, data in before.items() if path != config)
    assert not (fixture["codex"] / "imports").exists()
    assert not (fixture["codex"] / "memories").exists()
    assert not (fixture["codex"] / "hooks.json").exists()
    repeated, report = build_playwright_plan(*roots)
    assert repeated == []
    assert report["status"] == "no_changes"


@pytest.mark.parametrize("target", ["wrapper", "settings", "preferred_source", "destination"])
def test_scoped_apply_rejects_intervening_source_or_destination_edits(tmp_path, target):
    from profile_bridge.browser import build_playwright_plan
    import profile_sync as sync

    fixture = make_browser_session_fixture(tmp_path)
    changes, report = build_playwright_plan(fixture["claude"], fixture["codex"], fixture["skills"],
                                            adopt_playwright=True)
    paths = {"wrapper": fixture["wrapper"], "settings": fixture["claude"] / "settings.json",
             "preferred_source": fixture["claude"] / ".claude.json", "destination": fixture["codex"] / "config.toml"}
    changed = paths[target]
    changed.write_bytes(changed.read_bytes() + b'\n' if changed.exists() else b'{}\n')
    before = (fixture["codex"] / "config.toml").read_bytes()
    with pytest.raises(ValueError, match="[Cc]hanged"):
        sync.apply_plan(changes, report, fixture["codex"], fixture["skills"])
    assert (fixture["codex"] / "config.toml").read_bytes() == before


def test_scoped_cli_reports_conflict_without_applying(tmp_path, monkeypatch, capsys):
    from profile_bridge import browser

    fixture = make_browser_session_fixture(tmp_path)
    fixture["wrapper"].unlink()
    monkeypatch.setattr(browser.profile_sync, "apply_plan", lambda *a, **k: pytest.fail("conflict must not apply"))
    code = browser.main(["--claude-home", str(fixture["claude"]), "--codex-home", str(fixture["codex"]),
                         "--skills-home", str(fixture["skills"]), "--adopt-playwright", "--apply"])
    result = json.loads(capsys.readouterr().out)
    assert code != 0
    assert result["status"] == "blocked"


def test_scoped_apply_revalidates_plugin_availability_after_entrypoint_disappears(tmp_path):
    from profile_bridge.browser import build_playwright_plan
    import profile_sync as sync

    fixture = make_browser_session_fixture(tmp_path, declared_agent=True)
    changes, report = build_playwright_plan(fixture["claude"], fixture["codex"], fixture["skills"],
                                            adopt_playwright=True)
    assert len(changes) == 1
    (fixture["plugin"] / "agents/example.md").unlink()
    with pytest.raises(ValueError, match="[Ss]election|[Cc]hanged"):
        sync.apply_plan(changes, report, fixture["codex"], fixture["skills"])
    assert (fixture["codex"] / "config.toml").read_bytes() == fixture["original"]


def test_scoped_cli_returns_nonzero_when_apply_leaves_a_rollback_copy(tmp_path, monkeypatch, capsys):
    from profile_bridge import browser

    fixture = make_browser_session_fixture(tmp_path)
    monkeypatch.setattr(browser.profile_sync, "apply_plan", lambda changes, report, *roots:
                        {**report, "status": "applied", "cleanup_pending": str(fixture["codex"] / "pending-copy")})
    code = browser.main(["--claude-home", str(fixture["claude"]), "--codex-home", str(fixture["codex"]),
                         "--skills-home", str(fixture["skills"]), "--adopt-playwright", "--apply"])
    result = json.loads(capsys.readouterr().out)
    assert code != 0
    assert result["status"] == "applied"
    assert result["cleanup_pending"]
