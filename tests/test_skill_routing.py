"""Discovery routing preserves native policy and never enables blocked execution."""
import tomllib
from pathlib import Path
from profile_config import plan_skill_routing


def runtime(path):
    return {'status': 'ready', 'selection_policy': {'entries': []}, 'entries': {
        'skill': {'entrypoint': {'kind': 'skill', 'path': str(path)}, 'policy_ids': ['selected'],
                  'overlay': {'status': 'blocked'}},
        'role': {'entrypoint': {'kind': 'agent_template', 'path': str(path.with_name('agent.md'))}, 'policy_ids': ['role']}}}


def test_opt_in_and_idempotence_keep_unrelated_config(tmp_path):
    codex=tmp_path/'.codex'; codex.mkdir()
    skill=tmp_path/'skill/SKILL.md'
    original=b'model = "synthetic"\n[mcp_servers.local]\nurl = "http://127.0.0.1:1/mcp"\n'
    state=runtime(skill)
    assert plan_skill_routing(original,codex,state)[0] == original
    value,report=plan_skill_routing(original,codex,state,adopt=True)
    parsed=tomllib.loads(value.decode())
    assert parsed['model']=='synthetic' and parsed['mcp_servers']==tomllib.loads(original.decode())['mcp_servers']
    assert parsed['skills']['config']==[{'path':str(skill.resolve()),'enabled':False}]
    assert report['native_roles_changed'] is False
    assert state['entries']['skill']['overlay']['status']=='blocked'
    assert plan_skill_routing(value,codex,state)[0]==value


def test_modified_owned_preference_preserved(tmp_path):
    codex=tmp_path/'.codex';codex.mkdir();state=runtime(tmp_path/'SKILL.md')
    value,_=plan_skill_routing(b'',codex,state,adopt=True)
    edited=value.replace(b'enabled = false',b'enabled = true')
    actual,report=plan_skill_routing(edited,codex,state)
    assert actual==edited and report['status']=='conflict'


def test_native_enable_is_conflict_and_explicit_disable_reused(tmp_path):
    import json
    codex=tmp_path/'.codex';codex.mkdir();path=tmp_path/'SKILL.md';state=runtime(path)
    prefix=('[[skills.config]]\npath = '+json.dumps(str(path))+'\nenabled = ').encode()
    original=prefix+b'true\n'
    assert plan_skill_routing(original,codex,state,adopt=True)[1]['status']=='conflict'
    original=prefix+b'false\n'
    assert plan_skill_routing(original,codex,state,adopt=True)[0]==original


def test_missing_policy_does_not_reenable_raw_skills(tmp_path):
    codex=tmp_path/'.codex';codex.mkdir();state=runtime(tmp_path/'SKILL.md')
    value,_=plan_skill_routing(b'',codex,state,adopt=True)
    actual,report=plan_skill_routing(value,codex,{'status':'unsupported'})
    assert actual==value and report['status']=='conflict'


def test_explicit_removed_policy_retires_only_own_preferences(tmp_path):
    codex=tmp_path/'.codex';codex.mkdir();state=runtime(tmp_path/'SKILL.md')
    original=b'model="synthetic"\n'
    value,_=plan_skill_routing(original,codex,state,adopt=True)
    state['entries']={}
    actual,report=plan_skill_routing(value,codex,state)
    assert actual==original and report['status']=='updated'
