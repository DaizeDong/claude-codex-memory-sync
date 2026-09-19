"""Generate T07 synthetic inputs in a caller-owned temporary home.

No fixture payload is copied from a profile or committed as generated data.
The concurrent T03 generator is deliberately independent of these fixtures.
"""
import json
from pathlib import Path


def write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def json_file(path, value):
    return write(path, json.dumps(value))


def skill(path, name="example"):
    write(Path(path) / "SKILL.md", f"---\nname: {name}\ndescription: Synthetic skill instructions.\n---\nUse the synthetic input.\n")
    return Path(path)


def home(base):
    claude, codex, shared = base / ".claude", base / ".codex", base / ".agents/skills"
    for root in (claude / "skills", codex / "skills", shared):
        root.mkdir(parents=True)
    json_file(claude / "settings.json", {"enabledPlugins": {}})
    json_file(claude / "plugins/installed_plugins.json", {"plugins": {}})
    return claude, codex, shared


def external(base):
    checkout = skill(base / "checkouts/repo-a/nested/canonical", "frontmatter-name")
    vendored = skill(base / "vendored/installed-alias", "vendored-frontmatter")
    installer = skill(base / "vendored/cc-setup", "setup-frontmatter")
    manifest = json_file(base / "external.json", {
        "skillRepoRoot": "checkouts", "vendoredRoot": "vendored",
        "repos": [{"dir": "repo-a", "url": "https://example.com/fixtures/repo-a.git",
                   "branch": "stable", "commit": "a" * 40,
                   "skills": [{"name": "checkout-alias", "subPath": "nested/canonical"}]}],
        "vendored": [
            {"name": "installed-alias", "upstream": "https://example.com/fixtures/repo-b.git",
             "subPath": "upstream/path", "commit": "b" * 40, "vendored": "2026-01-01"},
            {"name": "cc-setup", "upstream": "https://example.com/fixtures/setup.git",
             "subPath": ".", "commit": "c" * 40, "vendored": "2026-01-01"}],
    })
    return manifest, checkout, vendored, installer


def plugin(base, claude, *, enabled=True, key="kit@market-a", version="1"):
    root = base / "plugins" / key / version
    skill(root / "skills/installed-alias", "different-frontmatter")
    write(root / "commands/nested/review.md", "---\ndescription: Review synthetic input.\n---\nReview.\n")
    write(root / "custom/inspector.md", "---\nname: inspector\ndescription: Inspect synthetic input.\n---\nRead [rules](rules.md).\n")
    write(root / "custom/rules.md", "Synthetic resource.\n")
    json_file(root / ".claude-plugin/plugin.json", {"agents": ["./custom/inspector.md"]})
    json_file(root / ".mcp.json", {"servers-one": {"command": "synthetic-one"},
                                   "servers-two": {"command": "synthetic-two"}})
    registry = claude / "plugins/installed_plugins.json"
    data = json.loads(registry.read_text())
    data["plugins"][key] = [{"scope": "user", "installPath": str(root), "version": version}]
    json_file(registry, data)
    settings = claude / "settings.json"
    data = json.loads(settings.read_text())
    data["enabledPlugins"][key] = enabled
    json_file(settings, data)
    return root
