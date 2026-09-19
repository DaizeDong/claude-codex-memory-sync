"""Archive retirement tests use only producer-generated synthetic profiles."""
import hashlib
import json

import pytest

import profile_memory as memory
from profile_bridge import memory_outbox as outbox
from profile_bridge.memory import archive as hygiene
from tools.make_fixtures import make_archive_hygiene, make_retirement_backup


@pytest.fixture
def archive(tmp_path):
    return make_archive_hygiene(tmp_path / 'home')


def test_source_deletion_plans_owned_retirement_without_writes(archive):
    archive['source'].unlink()
    destination = archive['destination']
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert destination in plans and plans[destination] is None
    assert report['archive_hygiene']['retirements'][0]['reason'] == 'source_absent'
    assert destination.read_bytes() == archive['payloads']['original']


def test_modified_destination_preserved_on_update_and_retirement(archive):
    archive['destination'].write_bytes(archive['payloads']['edited'])
    archive['source'].write_bytes(archive['payloads']['updated'])
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert archive['destination'] not in plans
    assert report['status'] == 'partial'
    archive['source'].unlink()
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert archive['destination'] not in plans
    assert report['archive_hygiene']['preserved'][0]['reason'] == 'destination_modified'


def test_legacy_unindexed_risk_requires_exact_review(archive):
    risk = archive['payloads']['risk']
    destination = archive['destination'].with_name('legacy.md')
    destination.write_bytes(risk)
    source = archive['source'].with_name('legacy.md')
    source.write_bytes(risk)
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert destination not in plans
    row = next(r for r in report['archive_hygiene']['preserved'] if r['path'].endswith('legacy.md'))
    assert row['reason'] == 'unowned_archive_copy'
    decision = dict(path='synthetic-project/legacy.md', sha256=hashlib.sha256(risk).hexdigest(),
                    action='quarantine', source_root=str(source.parents[2]), review_id='synthetic-review')
    plans, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_archive=[decision])
    assert destination in plans and plans[destination] is None
    assert risk.decode().strip() not in json.dumps(report)
    assert 'Z' * 32 not in json.dumps(report)
    row = next(r for r in hygiene.profile_changes(plans, archive['codex']) if r.get(hygiene.RETIREMENT))
    backup = make_retirement_backup(archive['codex'], row)
    outbox.prepare(plans.delivery)
    assert hygiene.apply_retirement(row, archive['codex'], backup)
    assert not destination.exists()
    assert next(backup.glob('quarantine-*.bin')).read_bytes() == risk
    assert hygiene.preserve_on_rollback(row)
    state = (outbox.state_root(archive['codex']) / 'history.json').read_bytes()
    assert risk not in state and b'Z' * 32 not in state


def test_explicit_scope_mapping_preserves_active_conflicts(archive):
    decision = dict(project='synthetic-project', scope_path=str(archive['claude'].parent / 'workspace'),
                    source_root=str(archive['claude'] / 'projects'), evidence_sha256='a' * 64)
    _, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_scopes=[decision])
    assert report['scope']['synthetic-project'] == decision['scope_path']
    assert report['scope_reviews'] == [dict(decision, status='reviewed')]
    conflicting = dict(decision, scope_path=str(archive['claude'].parent / 'another-workspace'))
    _, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_scopes=[decision, conflicting])
    assert report['scope']['synthetic-project'] is None
    assert any(r['reason'] == 'scope_mapping_conflict' for r in report['skipped'])


def retirement(archive):
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    row = next(r for r in hygiene.profile_changes(plans, archive['codex']) if r.get(hygiene.RETIREMENT))
    backup = make_retirement_backup(archive['codex'], row)
    outbox.prepare(plans.delivery)
    return row, backup


def test_quarantine_retains_exact_original_outside_search_and_rollback(archive):
    archive['source'].write_bytes(archive['payloads']['risk'])
    row, backup = retirement(archive)
    assert row[hygiene.RETIREMENT]['reason'] == 'source_excluded_credential'
    assert hygiene.apply_retirement(row, archive['codex'], backup)
    assert not archive['destination'].exists()
    retained = next(backup.glob('quarantine-*.bin'))
    assert retained.read_bytes() == archive['payloads']['original']
    assert not retained.is_relative_to(archive['codex'] / 'imports')
    assert hygiene.preserve_on_rollback(row)
    assert not hygiene.preserve_on_rollback({'path': 'unrelated'})
    # A repeat of the same hook proves identity and bytes at the retained object.
    assert hygiene.apply_retirement(row, archive['codex'], backup)
    retained.write_bytes(archive['payloads']['edited'])
    with pytest.raises(ValueError, match='archive_retirement_evidence_missing'):
        hygiene.apply_retirement(row, archive['codex'], backup)


def test_interrupted_detach_retry_preserves_history(archive, monkeypatch):
    archive['source'].unlink()
    row, backup = retirement(archive)

    class Interrupted(BaseException):
        pass

    def interrupt(boundary):
        if boundary == 'archive_retired':
            raise Interrupted()

    monkeypatch.setattr(outbox, 'checkpoint', interrupt)
    with pytest.raises(Interrupted):
        hygiene.apply_retirement(row, archive['codex'], backup)
    history_before = (outbox.state_root(archive['codex']) / 'history.json').read_bytes()
    assert hygiene.apply_retirement(row, archive['codex'], backup)
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert not plans.retirements
    assert (outbox.state_root(archive['codex']) / 'history.json').read_bytes() == history_before
    assert archive['codex'] / 'imports/claude-memory/index.md' in plans


@pytest.mark.parametrize('change', ['edit', 'replace', 'backup'])
def test_retirement_cas_rejects_changed_bytes_identity_and_backup(archive, change):
    archive['source'].unlink()
    row, backup = retirement(archive)
    destination = archive['destination']
    if change == 'edit':
        destination.write_bytes(archive['payloads']['edited'])
    elif change == 'replace':
        destination.rename(destination.with_suffix('.saved'))
        destination.write_bytes(archive['payloads']['original'])
    else:
        (backup / '00000.bin').write_bytes(archive['payloads']['edited'])
    with pytest.raises(ValueError, match='archive_retirement_(cas_conflict|backup_changed)'):
        hygiene.apply_retirement(row, archive['codex'], backup)
    assert destination.exists()
    assert not list(backup.glob('quarantine-*.bin'))


def test_unavailable_source_never_implies_deletion(archive, monkeypatch):
    def unavailable(root, skipped):
        skipped.append({'path': str(root), 'reason': 'unreadable_directory'})
        return iter(())
    monkeypatch.setattr(memory, '_source_files', unavailable)
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert archive['destination'] not in plans
    assert report['archive_hygiene']['preserved'][0]['reason'] == 'source_snapshot_incomplete'


def test_legacy_review_wrong_hash_and_action_fail_closed(archive):
    destination = archive['destination'].with_name('legacy.md')
    destination.write_bytes(archive['payloads']['risk'])
    decision = dict(path='synthetic-project/legacy.md', sha256='0' * 64, action='quarantine',
                    source_root=str(archive['claude'] / 'projects'), review_id='synthetic-review')
    plans, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_archive=[decision])
    assert not plans and report['status'] == 'unsupported'
    assert report['delivery']['delivery_state'] == 'archive_conflict'
    assert destination.exists()


def test_update_after_outbox_prepare_interruption_recovers_old_owned_bytes(archive):
    archive['source'].write_bytes(archive['payloads']['updated'])
    plans, _ = memory.plan_memory(archive['claude'], archive['codex'])
    outbox.prepare(plans.delivery)
    # Crash before archive publication: old and new exact hashes remain known.
    retry, _ = memory.plan_memory(archive['claude'], archive['codex'])
    assert retry[archive['destination']] == archive['payloads']['updated']
    memory.apply_memory_plan(retry, archive['claude'], archive['codex'])
    assert archive['destination'].read_bytes() == archive['payloads']['updated']


def test_regular_archive_rollback_uses_existing_backup_and_preserves_edits(archive):
    import profile_sync as sync
    archive['source'].write_bytes(archive['payloads']['updated'])
    plan, _ = memory.plan_memory(archive['claude'], archive['codex'])
    memory.apply_memory_plan(plan, archive['claude'], archive['codex'])
    backup = sorted((archive['codex'] / 'claude-sync/backups').iterdir())[-1]
    archive['destination'].write_bytes(archive['payloads']['edited'])
    result = sync.rollback(backup, archive['codex'], archive['codex'].parent / '.agents/skills')
    assert archive['destination'].read_bytes() == archive['payloads']['edited']
    assert any(r['path'] == str(archive['destination']) for r in result['preserved'])
    assert outbox._read_history(archive['codex'])[0][hygiene.FIELD]


def test_active_exact_mapping_and_collision_cannot_be_overridden(archive):
    # These path spellings encode to the same key; neither is inferred from it.
    project = 'C--synthetic-project'
    original = archive['source'].parents[1]
    target = original.with_name(project)
    original.rename(target)
    config = archive['claude'] / '.claude.json'
    config.write_text(json.dumps({'projects': {'C:\\synthetic\\project': {}}}))
    decision = dict(project=project, scope_path=str(archive['claude'].parent / 'another'),
                    source_root=str(archive['claude'] / 'projects'), evidence_sha256='a' * 64)
    _, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_scopes=[decision])
    assert report['scope'][project] == 'C:\\synthetic\\project'
    config.write_text(json.dumps({'projects': {'C:\\synthetic\\project': {}, 'C:\\synthetic-project': {}}}))
    _, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_scopes=[decision])
    assert report['scope'][project] is None


def test_direct_apply_requires_integration_hooks_before_any_retirement_write(archive, monkeypatch):
    import profile_sync as sync
    monkeypatch.delattr(sync, 'MEMORY_ARCHIVE_HYGIENE_VERSION', raising=False)
    archive['source'].unlink()
    plans, _ = memory.plan_memory(archive['claude'], archive['codex'])
    before = (outbox.state_root(archive['codex']) / 'history.json').read_bytes()
    with pytest.raises(ValueError, match='requires_profile_transaction_hooks'):
        memory.apply_memory_plan(plans, archive['claude'], archive['codex'])
    assert archive['destination'].exists()
    assert (outbox.state_root(archive['codex']) / 'history.json').read_bytes() == before


def test_modified_index_requires_exact_adoption_and_does_not_hide_edits(archive):
    index = archive['codex'] / 'imports/claude-memory/index.md'
    index.write_bytes(index.read_bytes() + archive['payloads']['edited'])
    plans, report = memory.plan_memory(archive['claude'], archive['codex'])
    assert not plans and plans.delivery is None
    assert report['archive_hygiene']['blocked']
    decision = dict(path='index.md', sha256=hashlib.sha256(index.read_bytes()).hexdigest(),
                    action='adopt', source_root=str(archive['claude'] / 'projects'), review_id='review-index')
    plans, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_archive=[decision])
    assert index in plans
    memory.apply_memory_plan(plans, archive['claude'], archive['codex'])
    backups = archive['codex'] / 'claude-sync/backups'
    assert any(archive['payloads']['edited'] in p.read_bytes() for p in backups.rglob('*.bin'))


def test_unavailable_active_scope_map_does_not_approve_unknown_override(archive):
    (archive['claude'] / '.claude.json').write_bytes(b'{invalid')
    decision = dict(project='synthetic-project', scope_path=str(archive['claude'].parent / 'workspace'),
                    source_root=str(archive['claude'] / 'projects'), evidence_sha256='a' * 64)
    _, report = memory.plan_memory(archive['claude'], archive['codex'], reviewed_scopes=[decision])
    assert report['scope']['synthetic-project'] is None
    assert any(r['reason'] == 'scope_mapping_unavailable' for r in report['skipped'])


def test_ownership_restore_validation_is_pure_and_rejects_invalid_hash(archive):
    from tools.make_fixtures import t03_state_bytes
    state = t03_state_bytes(archive['codex'])
    outbox.validate_restore_state(archive['codex'], state)
    key = 'claude-sync/memory-outbox/history.json'
    history = json.loads(state[key])
    history[hygiene.FIELD]['roots'][outbox._root(archive['claude'] / 'projects')]['synthetic-project/fact.md'] = ['invalid']
    state[key] = outbox.encoded(history)
    with pytest.raises(outbox.HistoryConflict):
        outbox.validate_restore_state(archive['codex'], state)


def test_integrated_profile_quarantines_legacy_risk_and_rollback_keeps_it_hidden(archive):
    import profile_sync as sync
    destination = archive['destination'].with_name('legacy.md')
    destination.write_bytes(archive['payloads']['risk'])
    archive['source'].with_name('legacy.md').write_bytes(archive['payloads']['risk'])
    decision = dict(path='synthetic-project/legacy.md', sha256=hashlib.sha256(destination.read_bytes()).hexdigest(),
                    action='quarantine', source_root=str(archive['claude'] / 'projects'), review_id='integrated-review')
    skills = archive['codex'].parent / '.agents/skills'
    changes, report = sync.build_plan(archive['claude'], archive['codex'], skills, reviewed_archive=[decision])
    assert any(hygiene.RETIREMENT in row for row in changes)
    result = sync.apply_plan(changes, report, archive['codex'], skills)
    from pathlib import Path
    backup = Path(result['backup'])
    assert not destination.exists()
    assert next(backup.glob('quarantine-*.bin')).read_bytes() == archive['payloads']['risk']
    assert 'Z' * 32 not in json.dumps(result)
    assert b'Z' * 32 not in (backup / 'manifest.json').read_bytes()
    rolled = sync.rollback(backup, archive['codex'], skills)
    assert not destination.exists()
    assert any(r['reason'] == 'quarantined_memory_evidence' for r in rolled['preserved'])


@pytest.mark.parametrize('boundary', ['outbox_prepared', 'archive_retired'])
def test_integrated_retirement_restart_converges(archive, monkeypatch, boundary):
    archive['source'].unlink()
    plans, _ = memory.plan_memory(archive['claude'], archive['codex'])

    class Interrupted(BaseException):
        pass

    def interrupt(point):
        if point == boundary:
            raise Interrupted()

    monkeypatch.setattr(outbox, 'checkpoint', interrupt)
    with pytest.raises(Interrupted):
        memory.apply_memory_plan(plans, archive['claude'], archive['codex'])
    monkeypatch.setattr(outbox, 'checkpoint', lambda _: None)
    retry, _ = memory.plan_memory(archive['claude'], archive['codex'])
    memory.apply_memory_plan(retry, archive['claude'], archive['codex'])
    assert not archive['destination'].exists()
    assert not memory.plan_memory(archive['claude'], archive['codex'])[0]
    assert any(p.read_bytes() == archive['payloads']['original']
               for p in (archive['codex'] / 'claude-sync/backups').rglob('quarantine-*.bin'))


def test_cross_home_restore_rebinds_only_reviewed_archive_root(archive, tmp_path):
    from profile_bridge.memory.restore import migrate_restore_state
    from tools.make_fixtures import t03_state_bytes
    source = outbox._root(archive['claude'] / 'projects')
    target_root = outbox._root(tmp_path / 'restored-claude/projects')
    original = t03_state_bytes(archive['codex'])
    result = migrate_restore_state(archive['codex'], tmp_path / 'restored-codex', original,
                                   scope_evidence=[dict(source_root=source, target_root=target_root, scope=['*'])])
    key = 'claude-sync/memory-outbox/history.json'
    before = json.loads(original[key])[hygiene.FIELD]['roots']
    after = json.loads(result['files'][key])[hygiene.FIELD]['roots']
    assert set(after) == {target_root} and after[target_root] == before[source]
    assert result['report']['native_notes_written'] == 0
