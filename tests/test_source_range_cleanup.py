"""Direct-source records and maintenance use isolated SQL, never live sources."""
import json
from datetime import timedelta

import pytest

from backend.database import db
from backend.models import DocumentSummarySegment as Segment
from backend import case_assistant_tasks as tasks
from backend.util import case_assistant_workflow as service
from backend.util import source_context as context
from backend.util.source_range_cleanup import cleanup_source_ranges
from tests.test_document_preparation import app
from tests.test_case_assistant_workflows import workflow
from tests.test_source_context import modern, install, focused, finish


def source_work(row):
    install(row, ['Delivery is due June 30, unless an extension is agreed.'])
    tasks.plan_sections(row, json.loads(row.payload_json))
    return tasks.work_rows(row, 'query').one()


def legacy_range(row, item, *, key='old-range', value=None, kind='query'):
    value = value if value is not None else context.read_work_value(row, item)
    saved = Segment(document_id=item.document_id, segment_key=service.fingerprint([row.id, item.id, key]),
                    kind=kind, state='ready', result_json=service.encode(value))
    db.session.add(saved)
    db.session.flush()
    item.result_id = saved.id
    db.session.commit()
    return saved.id


def terminal(row, status='completed'):
    row.status, row.active_draft_id = status, None
    db.session.commit()


def shared_reference(row, item, *, status='completed'):
    clone = service.Workflow(id='wf_shared_reference', user_id=row.user_id, draft_id=row.draft_id,
        data_json=row.data_json, payload_json=row.payload_json, draft_hash=row.draft_hash, status=status,
        created_at=service.now(), updated_at=service.now(), last_progress_at=service.now(),
        deadline=service.now() + timedelta(hours=1))
    other = service.Work(workflow_run_id=clone.id, work_key=item.work_key, kind=item.kind,
        document_id=item.document_id, artifact_id=item.artifact_id, material_json=item.material_json,
        state='ready', result_id=item.result_id, updated_at=service.now())
    db.session.add_all([clone, other])
    db.session.commit()
    return clone, other


def test_new_source_saves_once_and_reuses_work_after_reload_and_retry(modern, monkeypatch):
    item = source_work(modern)
    wid, material = item.id, item.material_json
    assert item.result_id is None and item.state == 'ready'
    assert Segment.query.filter_by(kind='query').count() == 0
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    assert tasks.work_rows(modern, 'query').count() == 1
    service.stop(modern, 'Stopped until explicit Retry.')
    service.retry(modern)
    db.session.expire_all()
    saved = db.session.get(service.Work, wid)
    assert saved.material_json == material and saved.result_id is None
    assert finish(modern)['answer_scheduled']
    assert 'Delivery is due June 30' in json.loads(modern.payload_json)['file_extractions_str']
    assert Segment.query.filter_by(kind='query').count() == 0


def test_old_linked_source_remains_readable_and_evidence_is_unchanged(modern, app):
    item = source_work(modern)
    expected = context.read_work_value(modern, item)
    before = app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence').json
    saved_id = legacy_range(modern, item)
    assert context.read_work_value(modern, item) == expected
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    assert item.result_id == saved_id  # Ordinary reads/planning never run cleanup.
    after = app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence').json
    assert before == after
    assert finish(modern)['answer_scheduled']


def test_mixed_source_records_paginate_and_merge_without_copying_source(modern, app):
    original = source_work(modern)
    payload = json.loads(modern.payload_json)
    doc = context.document(payload['document_snapshot'], original.document_id, original.artifact_id)
    end = len(doc['manifest']['text'])
    first = context.save_source(modern, payload, doc, {'start': 0, 'end': 20})
    context.save_source(modern, payload, doc, {'start': 20, 'end': end})
    legacy_range(modern, first)
    expected = {item.id for item in tasks.work_rows(modern, 'query')}
    seen, after = set(), ''
    while True:
        response = app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence',
                                        query_string={'limit': 1, 'after': after})
        assert response.status_code == 200
        page = response.json
        assert len(page['sections']) == 1
        section = page['sections'][0]
        assert section['processing_method'] == context.SOURCE
        assert section['excerpts'][0]['quote'] in doc['manifest']['text']
        assert section['section_id'] not in seen
        seen.add(section['section_id'])
        after = page['next']
        if not after:
            break
    assert seen == expected
    assert tasks.prepare_answer(modern)
    assert json.loads(modern.payload_json)['file_extractions_str'].count('Delivery is due June 30') == 1


@pytest.mark.parametrize('bad_range', [{'start': True, 'end': 5}, {'start': 2, 'end': 2},
    {'start': -1, 'end': 5}, {'start': 0, 'end': 100000}, {'start': '0', 'end': 5}])
def test_invalid_direct_positions_fail_instead_of_returning_text(modern, bad_range, app):
    item = source_work(modern)
    item.material_json = service.encode({**bad_range, 'context_method': 'source', 'format_version': 1})
    db.session.commit()
    with pytest.raises(ValueError, match='range'):
        tasks.prepare_answer(modern)
    assert app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence').status_code == 409


@pytest.mark.parametrize('kind,method', [('query', 'findings'), ('retrieval_review', None), ('reduce', None), ('query', None)])
def test_null_result_is_only_allowed_for_recognized_direct_source(modern, kind, method):
    item = source_work(modern)
    item.kind = kind
    material = json.loads(item.material_json)
    material['context_method'] = method
    item.material_json = service.encode(material)
    db.session.commit()
    with pytest.raises(service.WorkflowStopped, match='generated result'):
        context.read_work_value(modern, item)


@pytest.mark.parametrize('change', ['positions', 'format', 'boolean'])
def test_linked_source_mismatch_is_not_silently_ignored(modern, change, app):
    item = source_work(modern)
    value = context.read_work_value(modern, item)
    if change == 'positions':
        value['range']['end'] -= 1
    elif change == 'format':
        value['format'] = context.FINDINGS
    else:
        # True == 1 in Python; integer validation must still reject it.
        value['range']['start'] = False
    legacy_range(modern, item, value=value)
    with pytest.raises(ValueError):
        context.read_work_value(modern, item)
    assert app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence').status_code == 409


def test_unknown_source_version_is_rejected(modern):
    item = source_work(modern)
    item.material_json = service.encode({**json.loads(item.material_json), 'format_version': 999})
    db.session.commit()
    with pytest.raises(ValueError, match='format'):
        context.read_work_value(modern, item)


def test_cleanup_dry_run_apply_and_repeat_preserve_visible_evidence(modern, app):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    client = app.test_client()
    evidence = client.get(f'/api/everything/workflows/{modern.id}/evidence').json
    dry = cleanup_source_ranges()
    assert dry['eligible_records'] == dry['eligible_references'] == 1
    assert dry['deleted_records'] == dry['cleared_references'] == 0
    assert db.session.get(Segment, sid) and item.result_id == sid
    applied = cleanup_source_ranges(apply=True)
    assert applied['deleted_records'] == applied['cleared_references'] == 1
    db.session.expire_all()
    assert db.session.get(Segment, sid) is None
    assert db.session.get(service.Work, item.id).result_id is None
    assert client.get(f'/api/everything/workflows/{modern.id}/evidence').json == evidence
    again = cleanup_source_ranges(apply=True)
    assert again['deleted_records'] == again['cleared_references'] == 0


@pytest.mark.parametrize('status', ['running', 'awaiting_user_confirmation', 'future_unknown_status'])
def test_cleanup_skips_active_and_unknown_requests(modern, status):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    modern.status = status
    db.session.commit()
    result = cleanup_source_ranges(apply=True)
    assert result['skipped'] == {'active_or_unknown_request': 1}
    assert result['deleted_records'] == 0 and item.result_id == sid
    assert db.session.get(Segment, sid)


def test_cleanup_shared_results_clear_all_refs_or_skip_all_when_any_active(modern):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    other_row, other_work = shared_reference(modern, item, status='running')
    assert cleanup_source_ranges(apply=True)['deleted_records'] == 0
    assert item.result_id == other_work.result_id == sid
    terminal(other_row, status='failed')
    result = cleanup_source_ranges(apply=True)
    assert result['deleted_records'] == 1 and result['cleared_references'] == 2
    assert item.result_id is None and other_work.result_id is None


def test_cleanup_bounds_reference_count_before_locking_owners(modern, monkeypatch):
    from backend.util import source_range_cleanup as cleanup
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    shared_reference(modern, item)
    monkeypatch.setattr(cleanup, 'MAX_REFERENCES', 1)
    result = cleanup_source_ranges(apply=True)
    assert result['skipped'] == {'too_many_references': 1}
    assert db.session.get(Segment, sid) and item.result_id == sid


@pytest.mark.parametrize('change', ['range', 'method', 'kind', 'document', 'version'])
def test_cleanup_preserves_mismatched_references(modern, change):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    material = json.loads(item.material_json)
    if change == 'range':
        material['end'] -= 1
    elif change == 'method':
        material['context_method'] = 'findings'
    elif change == 'kind':
        item.kind = 'reduce'
    elif change == 'document':
        item.document_id = 'unrelated-document'
    else:
        material['format_version'] = 999
    item.material_json = service.encode(material)
    db.session.commit()
    result = cleanup_source_ranges(apply=True)
    assert result['skipped'] == {'mismatched_reference': 1}
    assert db.session.get(Segment, sid) and item.result_id == sid


def test_cleanup_removes_orphans_in_bounded_pages_and_keeps_real_outputs(modern):
    item = source_work(modern)
    source = context.read_work_value(modern, item)
    uid = item.document_id
    remove_ids, keep_ids = [], []
    for i in range(5):
        saved = Segment(document_id=uid, segment_key=f'orphan-{i}', kind='query',
            state='ready', result_json=service.encode(source))
        db.session.add(saved)
        db.session.flush()
        remove_ids.append(saved.id)
    for kind, value in [('section', source), ('query', {'format': context.FINDINGS, 'findings': []}),
                        ('retrieval_review', {'supported': []}), ('answer', {'format': context.CONSOLIDATED, 'findings': []}),
                        ('query', {'format': context.SOURCE, 'range': source['range'], 'summary': 'Real data: keep.'}),
                        ('query', {'excerpts': [{'quote': context.SOURCE, 'block_id': 'b1'}]})]:
        saved = Segment(document_id=uid, segment_key=f'keep-{len(keep_ids)}', kind=kind,
            state='ready', result_json=service.encode(value))
        db.session.add(saved)
        db.session.flush()
        keep_ids.append(saved.id)
    db.session.commit()
    result = cleanup_source_ranges(apply=True, batch_size=2)
    assert result['deleted_records'] == 5 and result['cleared_references'] == 0
    assert all(db.session.get(Segment, sid) is None for sid in remove_ids)
    assert all(db.session.get(Segment, sid) is not None for sid in keep_ids)


def test_cleanup_clearing_references_and_deletion_are_atomic(modern, monkeypatch):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    def fail_after_unlink(saved):
        assert item.result_id is None
        raise RuntimeError('Delete interrupted')
    monkeypatch.setattr(db.session, 'delete', fail_after_unlink)
    with pytest.raises(RuntimeError, match='interrupted'):
        cleanup_source_ranges(apply=True)
    db.session.expire_all()
    assert db.session.get(service.Work, item.id).result_id == sid
    assert db.session.get(Segment, sid)


def test_cleanup_command_defaults_to_dry_run(modern, app):
    item = source_work(modern)
    sid = legacy_range(modern, item)
    terminal(modern)
    runner = app.test_cli_runner()
    preview = runner.invoke(args=['case-assistant-cleanup-source-ranges'])
    assert preview.exit_code == 0, preview.output
    assert 'Dry-run: would delete 1' in preview.output
    assert db.session.get(Segment, sid) and item.result_id == sid
    apply = runner.invoke(args=['case-assistant-cleanup-source-ranges', '--apply', '--batch-size', '1'])
    assert apply.exit_code == 0, apply.output
    assert 'Deleted 1' in apply.output
    assert db.session.get(Segment, sid) is None
