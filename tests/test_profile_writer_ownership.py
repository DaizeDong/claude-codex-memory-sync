"""Writer regressions generated entirely in synthetic temporary homes."""
import json
from pathlib import Path
import shutil
import tomllib
import pytest
import profile_sync as sync
from profile_agents import plan_agents
from profile_config import plan_config
from profile_hooks import plan_hooks
from profile_bridge import ownership


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def profile(tmp_path):
    home = tmp_path / 'synthetic-home'
    claude, codex, skills = home/'.claude', home/'.codex', home/'.agents/skills'
    put(claude/'CLAUDE.md', b'Use synthetic examples.\n')
    put(claude/'settings.json', b'{"enabledPlugins": {}}')
    put(claude/'plugins/installed_plugins.json', b'{"plugins": {}}')
    put(codex/'config.toml', '# utilisateur café\nmodel = "synthetic"\n'.encode())
    return home, claude, codex, skills


def role_source(claude, name):
    path = claude/'agents'/(name+'.md')
    put(path, f'---\nname: {name}\ndescription: Synthetic role\n---\nRead synthetic input.\n'.encode())
    return path


def apply_roles(codex, result):
    files, config, report = result
    for path, data in files.items():
        put(path, data)
    put(codex/'config.toml', config)
    for path in report['deletions']:
        Path(path).unlink()


@pytest.mark.parametrize('damage', ['edited', 'missing', 'schema', 'alias'])
def test_role_dependency_failure_withholds_intact_sibling(tmp_path, damage):
    _, claude, codex, _ = profile(tmp_path)
    role_source(claude, 'alpha')
    beta = role_source(claude, 'beta')
    apply_roles(codex, plan_agents(claude, codex, [], (codex/'config.toml').read_bytes()))
    original = (codex/'config.toml').read_bytes()
    roles = tomllib.loads(original.decode())['agents']
    alpha = next(row for name, row in roles.items() if 'alpha' in name)
    path = codex/alpha['config_file']
    if damage == 'edited':
        put(path, path.read_bytes()+b'# user edit\n')
    elif damage == 'missing':
        path.unlink()
    elif damage == 'schema':
        path = codex/'claude-sync/managed-agents.json'
        obj = json.loads(path.read_bytes()); obj['version'] = 99
        put(path, ownership.encoded(obj))
    else:
        original += ('[agents.manual]\nconfig_file="./'+alpha['config_file']+'"\n').encode()
        put(codex/'config.toml', original)
    put(beta, beta.read_bytes()+b'Changed source instructions.\n')
    files, merged, report = plan_agents(claude, codex, [], original)
    assert files == {}
    assert merged == original
    assert report['deletions'] == []
    assert report['registered'] == 0
    if damage != 'alias':
        assert plan_config(claude, codex, [])[0] == original


@pytest.mark.parametrize('damage', ['edited', 'missing', 'schema', 'manifest_missing'])
def test_adapter_dependency_cannot_be_recreated_or_rehashed(tmp_path, damage):
    _, claude, codex, skills = profile(tmp_path)
    for name in ('alpha', 'beta'):
        put(claude/'commands'/(name+'.md'), b'Synthetic command.\n')
    links, files, _ = sync.plan_skills(claude, skills, [], codex)
    assert links == {}
    for path, data in files.items():
        put(path, data)
    alpha = skills/'claude-user-alpha/SKILL.md'
    manifest = codex/'claude-sync/managed-artifacts.json'
    if damage == 'edited':
        put(alpha, alpha.read_bytes()+b'User addition.\n')
    elif damage == 'missing':
        alpha.unlink()
    elif damage == 'schema':
        obj = json.loads(manifest.read_bytes()); obj['version'] = 99
        put(manifest, ownership.encoded(obj))
    else:
        manifest.unlink()
        put(codex/'claude-sync/managed-skills.json', b'{}')
    put(claude/'commands/beta.md', b'Updated synthetic command.\n')
    links, files, report = sync.plan_skills(claude, skills, [], codex)
    assert links == {}
    assert not any(path == alpha or path.name == 'managed-artifacts.json' for path in files)
    assert any(row['status'] == 'conflict' for row in report)


def test_poisoned_backup_output_never_regains_role_ownership(tmp_path):
    home, claude, codex, _ = profile(tmp_path)
    role_source(claude, 'alpha')
    apply_roles(codex, plan_agents(claude, codex, [], (codex/'config.toml').read_bytes()))
    members = {p.relative_to(home).as_posix(): p.read_bytes() for p in codex.rglob('*') if p.is_file()}
    damaged = dict(members)
    role = next(k for k in members if k.startswith('.codex/claude-sync/agents/'))
    damaged[role] += b'# redaction removes this user edit\n'
    transformed, conflicts = ownership.remap_members(damaged, members, home=home)
    assert conflicts
    for key, data in transformed.items():
        put(home/key, data)
    original = (codex/'config.toml').read_bytes()
    assert plan_agents(claude, codex, [], original)[:2] == ({}, original)


def test_unknown_instruction_marker_does_not_become_initial_import(tmp_path):
    _, claude, codex, _ = profile(tmp_path)
    original = b'User prefix\n<!-- claude-profile-sync:future version=99 -->\nUser suffix\n'
    put(codex/'AGENTS.md', original)
    result, report = sync.plan_instructions(claude, codex)
    assert result is None
    assert report['status'] == 'conflict'
    assert (codex/'AGENTS.md').read_bytes() == original


@pytest.mark.parametrize('damage', ['bridge', 'handler', 'manifest', 'schema'])
def test_hook_dependency_failure_preserves_user_groups(tmp_path, damage):
    from test_ownership_contract import make_hooks
    _, claude, codex, _ = profile(tmp_path)
    members = make_hooks()
    document = json.loads(members['.codex/hooks.json'])
    user_group = {'hooks': [{'type': 'command', 'command': 'synthetic-user-hook'}]}
    document['hooks']['Stop'].insert(0, user_group)
    members['.codex/hooks.json'] = ownership.encoded(document)
    for key, data in members.items():
        put(codex.parent/key, data)
    manifest = codex/'imports/claude-hooks/manifest.json'
    if damage in {'bridge', 'manifest'}:
        (manifest if damage == 'manifest' else codex/'imports/claude-hooks/stop_bridge.py').unlink()
    elif damage == 'handler':
        document['hooks']['Stop'].pop()
        put(codex/'hooks.json', ownership.encoded(document))
    else:
        obj = json.loads(manifest.read_bytes()); obj['version'] = 99
        put(manifest, ownership.encoded(obj))
    before = (codex/'hooks.json').read_bytes()
    assert plan_hooks(claude, codex)[0] == {}
    assert (codex/'hooks.json').read_bytes() == before
    assert json.loads(before)['hooks']['Stop'][0] == user_group


def test_actual_plan_apply_after_backup_remap_preserves_user_bytes(tmp_path):
    home, claude, codex, skills = profile(tmp_path)
    role_source(claude, 'alpha')
    put(claude/'commands/example.md', b'Synthetic command.\n')
    prefix = 'Préface utilisateur\r\n'.encode()
    put(codex/'AGENTS.md', prefix)
    put(codex/'user-owned.txt', b'Untouched user file.\n')
    changes, report = sync.build_plan(claude, codex, skills)
    sync.apply_plan(changes, report, codex, skills)
    members = {p.relative_to(home).as_posix(): p.read_bytes()
               for root in (codex, skills) for p in root.rglob('*')
               if p.is_file() and 'backups' not in p.parts}
    envelope = ownership.encoded({'home': str(home), 'members': {k: v.hex() for k, v in members.items()}})
    new_home = tmp_path/'restored-home'
    remapped = json.loads(ownership.remap(envelope, 'profile', {str(home): str(new_home)}))
    shutil.copytree(claude, new_home/'.claude')
    for key, value in remapped['members'].items():
        put(new_home/key, bytes.fromhex(value))
    claude, codex, skills = new_home/'.claude', new_home/'.codex', new_home/'.agents/skills'
    role_source(claude, 'beta')
    changes, report = sync.build_plan(claude, codex, skills)
    assert report['agents']['registered'] == 2
    sync.apply_plan(changes, report, codex, skills)
    assert (codex/'AGENTS.md').read_bytes().startswith(prefix)
    assert (codex/'user-owned.txt').read_bytes() == b'Untouched user file.\n'
    assert sync.build_plan(claude, codex, skills)[0] == []
