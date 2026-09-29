"""Host guidance is installed through the verified, user-preserving adapter."""
import profile_sync as sync
from tools.make_fixtures import make_instruction_home


def test_host_guidance_is_managed_and_idempotent(tmp_path):
    claude, codex = make_instruction_home(tmp_path)
    target = codex / 'AGENTS.md'
    original = target.read_bytes()
    payload, report = sync.plan_instructions(claude, codex)
    assert report['status'] == 'updated'
    assert payload.startswith(original)
    text = payload.decode()
    assert 'profile_bridge.resources' in text
    assert (codex / 'claude-sync/runtime-artifacts/installed/current.json').as_posix() in text
    assert 'resource_context.source_root' in text
    assert 'Claude settings do not configure Codex' in text
    assert 'Installing a skill does not establish runtime capability' in text
    assert 'explicitly names that persona' in text
    assert 'one applicable general cleanup skill' in text
    assert 'llmcall.call(prompt, mode="agent")' in text
    target.write_bytes(payload)
    assert sync.plan_instructions(claude, codex) == (payload, {
        'status': 'unchanged', 'bytes': len(payload), 'source': str(claude / 'CLAUDE.md')})


def test_user_edits_to_host_guidance_are_not_overwritten(tmp_path):
    claude, codex = make_instruction_home(tmp_path)
    payload, _ = sync.plan_instructions(claude, codex)
    edited = payload.replace(b'## Imported skill compatibility', b'## User-adjusted compatibility')
    assert edited != payload
    (codex / 'AGENTS.md').write_bytes(edited)
    result, report = sync.plan_instructions(claude, codex)
    assert result is None
    assert report['reason'] == 'managed_instructions_were_edited'
    assert (codex / 'AGENTS.md').read_bytes() == edited
