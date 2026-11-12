"""Focused follow-up retrieval with real SQL, mocked models and Celery messages."""
import json
from datetime import timedelta

import pytest

from backend.database import db
from backend.models import DocumentChunk, DocumentPreparedArtifact, DocumentSummarySegment, UploadedFiles
from backend.util import attachment_parser as parser
from backend.util import document_preparation as prep
from backend.util import case_assistant_workflow as service
from backend.util import prepared_retrieval as retrieval
from backend.util import retrieval_review as review
from backend import case_assistant_tasks as tasks
from tests.test_case_assistant_workflows import workflow
from tests.test_document_preparation import app, admit, receive, run


def focus(row, question='What is the termination notice period and its exceptions?'):
    tasks.work_rows(row).delete()
    payload = json.loads(row.payload_json)
    payload['intent_decision']['coverage_mode'] = 'focused'
    payload['user_message'] = question
    for key in ('retrieval_review', '_planned_file_ids', '_sections_planned'):
        payload.pop(key, None)
    row.payload_json = service.encode(payload)
    db.session.commit()
    return payload


def source(row, texts):
    """Install deterministic section records so the decisive clause ranks >12."""
    uid = json.loads(row.payload_json)['document_snapshot']['files'][0]['file_id']
    file = UploadedFiles.query.filter_by(unique_id=uid).one()
    manifest = parser.manifest_from_blocks([{'text': text, 'paragraph': i + 1} for i, text in enumerate(texts)])
    file.content = manifest['text']
    artifact = db.session.get(DocumentPreparedArtifact, json.loads(row.payload_json)['document_snapshot']['files'][0]['artifact_id'])
    data = json.loads(artifact.result_json)
    keys = []
    for i, block in enumerate(manifest['blocks']):
        key = f'focused-test-{i:03}'
        record = {**block, 'definitions': [], 'summary': texts[i]}
        db.session.add(DocumentSummarySegment(document_id=uid, segment_key=key, kind='section', state='ready',
                        result_json=service.encode(record), search_text=texts[i]))
        keys.append(key)
    data['section_keys'] = keys
    artifact.result_json, artifact.manifest_json = service.encode(data), service.encode({k: v for k, v in manifest.items() if k != 'text'})
    DocumentChunk.query.filter_by(source_unique_id=uid).delete()
    for i, text in enumerate(texts):
        db.session.add(DocumentChunk(uploaded_file_id=file.id, draft_id=file.draft_id, project_id=file.project_id,
                                    source_unique_id=uid, chunk_index=i, content=text, token_count=20,
                                    char_count=len(text), source_filename=file.original_filename))
    db.session.commit()
    return uid, manifest


def model(calls, *, sufficient=False, unresolved=False):
    def respond(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        calls.append((schema, prompt, kwargs))
        if schema is retrieval.EvidenceResult:
            material = json.loads(prompt.split('Source blocks:\n')[1])
            return schema(excerpts=[{'block_id': b['block_id'], 'quote': b['text']} for b in material
                                    if 'termination notice' in b['text'] or 'fraud' in b['text']])
        if schema is retrieval.DocumentReview:
            material = json.loads(prompt.split('Documents:\n')[1])
            return schema(documents=[{'file_id': d['file_id'], 'relevant': True, 'reason': 'Potential amendment or notice clause'}
                                     for d in material])
        assert schema is review.RetrievalReview
        values = json.loads(prompt.split('Saved extraction or child reviews:\n')[1])
        refs = []
        for v in values:
            if 'excerpt_index' in v:
                refs.append({'work_id': v['work_id'], 'excerpt_index': v['excerpt_index']})
            else:
                refs.extend(r for p in v.get('supported', []) for r in p['references'])
        found = sufficient or ('fraud' in prompt and not unresolved)
        return schema(supported=[{'part': 'Notice period', 'detail': 'See the cited original passages.', 'references': refs[:1]}] if refs else [],
                      issues=[] if found else [{'part': 'Exceptions', 'reason': 'The immediate cessation exception needs a source lookup.'}],
                      queries=[] if found else ['fraud immediate cessation'])
    return respond


def finish(row, monkeypatch, provider, max_rounds=30):
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for _ in range(max_rounds):
        for item in tasks.work_rows(row).filter_by(state='queued').all():
            tasks.process_section.run(item.id)
        if row.phase == 'draft':
            return json.loads(row.payload_json)
        if not row.group_id:
            tasks.dispatch_group(row)
        tasks.after_extraction.run(row.id, row.group_id)
    pytest.fail('Focused workflow did not finish its bounded follow-up')


def test_follow_up_finds_exception_beyond_initial_hits_and_keeps_results(workflow, monkeypatch):
    uid, manifest = source(workflow, ['termination notice is thirty days. ' * 8 for _ in range(22)]
                           + ['Unrelated equipment provisions. ' * 8 for _ in range(14)]
                           + ['Upon fraud, immediate cessation applies.'] + ['Appendix financial data.'] * 3)
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    hit_state = payload['retrieval_review']['hits'][uid]
    assert len(hit_state['sections']) == 3 and len(hit_state['blocks']) == 12
    assert not any(json.loads(w.material_json)['start'] <= manifest['blocks'][36]['start'] < json.loads(w.material_json)['end']
                   for w in tasks.work_rows(workflow, 'query').all())
    calls = []
    provider = model(calls)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    initial = {w.id: w.result_id for w in tasks.work_rows(workflow, 'query').all()}
    result = finish(workflow, monkeypatch, provider)
    assert 'Upon fraud, immediate cessation applies.' in result['file_extractions_str']
    assert result['retrieval_review']['round'] == 1 and result['retrieval_review']['state'] == 'complete'
    assert result['retrieval_review']['remaining_issues'] == []
    hits = result['retrieval_review']['hits'][uid]
    assert len(hits['sections']) <= 6 and len(hits['blocks']) <= 24
    assert set(hit_state['sections']) <= set(hits['sections'])
    assert all(db.session.get(service.Work, key).result_id == value for key, value in initial.items())
    assert all(kw['reasoning_effort'] == 'medium' for schema, _, kw in calls if schema is review.RetrievalReview)
    assert all(retrieval.request_tokens(prompt, schema) <= 40000 for schema, prompt, _ in calls)
    assert len(tasks.work_rows(workflow, review.KIND).all()) > 0
    assert workflow.evidence_budget == 64000 and workflow.reduction_round == 0
    assert 'source_location' in result['file_extractions_str']


def test_sufficient_evidence_does_not_schedule_follow_up(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    calls = []
    result = finish(workflow, monkeypatch, model(calls, sufficient=True))
    assert result['retrieval_review']['round'] == 0
    assert not any(schema is retrieval.DocumentReview for schema, _, _ in calls)
    assert result['retrieval_review']['state'] == 'complete'


def test_unresolved_after_one_round_reaches_final_context_and_report(workflow, monkeypatch, app):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    calls = []
    result = finish(workflow, monkeypatch, model(calls, unresolved=True))
    state = result['retrieval_review']
    assert state['round'] == 1 and state['state'] == 'complete'
    assert 'Exceptions' in result['file_extractions_str'] and 'not factual evidence' in result['file_extractions_str']
    info = service.status(workflow)['info']
    assert info['retrieval']['remaining_issues'] == state['remaining_issues']
    assert 'excerpts' not in service.encode(info) and 'supported' not in service.encode(info)
    response = app.test_client().get(f'/api/everything/workflows/{workflow.id}/evidence')
    assert response.status_code == 200 and response.json['retrieval']['round'] == 1
    assert response.json['coverage_mode'] == 'focused'
    before = len(calls)
    assert not review.advance(workflow, result)
    tasks.prepare_answer(workflow)
    assert len(calls) == before


def test_legacy_request_keeps_previous_policy_on_retry(workflow, monkeypatch):
    payload = focus(workflow)
    data = json.loads(workflow.data_json)
    data.pop('_retrieval_policy_version')
    workflow.data_json = service.encode(data)
    db.session.commit()
    tasks.plan_sections(workflow, payload)
    result = finish(workflow, monkeypatch, model([], sufficient=True))
    assert 'retrieval_review' not in result
    service.stop(workflow, 'Retry')
    service.retry(workflow)
    assert '_retrieval_policy_version' not in json.loads(workflow.data_json)


def test_review_inputs_are_references_and_duplicate_delivery_reuses_result(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    calls = []
    monkeypatch.setattr(retrieval, 'chat_completion_parse', model(calls, sufficient=True))
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    assert review.advance(workflow, json.loads(workflow.payload_json))
    item = tasks.work_rows(workflow, review.KIND).one()
    assert 'thirty days' not in item.material_json
    first = tasks.process_section.run(item.id)
    before = len(calls)
    assert tasks.process_section.run(item.id) == first and len(calls) == before


def test_review_cannot_cite_another_request(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', model([], sufficient=True))
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    review.advance(workflow, json.loads(workflow.payload_json))
    item = tasks.work_rows(workflow, review.KIND).one()
    item.attempts = 2
    db.session.commit()
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: review.RetrievalReview(supported=[{
        'part': 'Invented support', 'detail': 'Wrong source', 'references': [{'work_id': 'other-request', 'excerpt_index': 0}]}]))
    with pytest.raises(ValueError, match='outside its supplied inputs'):
        tasks.process_section.run(item.id)
    assert item.result_id is None and workflow.status == 'failed'


def test_saved_review_cannot_bypass_changed_artifact(workflow, monkeypatch):
    from backend.models import DocumentPreparation
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', model([], sufficient=True))
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    review.advance(workflow, json.loads(workflow.payload_json))
    item = tasks.work_rows(workflow, review.KIND).one()
    tasks.process_section.run(item.id)
    doc = db.session.get(DocumentPreparation, item.document_id)
    doc.generation += 1
    db.session.commit()
    with pytest.raises(prep.PreparationError):
        review.result(workflow, item, json.loads(workflow.payload_json))


def test_uncovered_ranges_keep_context_without_rescheduling_covered_sources():
    assert review.uncovered_spans([{'start': 0, 'end': 3000}], [(0, 3000)], 3000) == []
    assert review.uncovered_spans([{'start': 0, 'end': 3000}], [(0, 2000)], 3000) == [{'start': 1488, 'end': 3000}]


def test_query_round_robin_preserves_second_query_and_deduplicates():
    items = [{'id': str(i), 'content': 'notice ' * 20} for i in range(20)]
    items += [{'id': 'exception', 'content': 'fraud immediate cessation'}]
    hits = retrieval.ranked_matches(items, ['notice', 'fraud immediate cessation'], separate=True)
    assert hits[1]['id'] == 'exception'
    assert len(hits) == len({h['id'] for h in hits})


def test_query_round_robin_is_unchanged_by_repeated_words():
    items = [{'id': f'notice-{i}', 'content': 'notice ' * 20} for i in range(4)]
    items += [{'id': 'exception', 'content': 'fraud immediate cessation'}]
    expected = retrieval.ranked_matches(items, ['notice', 'fraud immediate cessation'], separate=True)
    actual = retrieval.ranked_matches(items, ['notice notice NOTICE', 'fraud fraud immediate cessation'], separate=True)
    assert actual == expected
    assert [row['id'] for row in actual[:2]] == ['notice-0', 'exception']
    assert len(actual) == len({row['id'] for row in actual}) == len(items)


def test_overlapping_query_terms_select_sections_and_blocks_without_full_fallback(workflow):
    uid, manifest = source(workflow, ['The filing deadline is June 30.', 'Unrelated equipment schedule.'])
    entry = json.loads(workflow.payload_json)['document_snapshot']['files'][0]
    artifact = json.loads(db.session.get(DocumentPreparedArtifact, entry['artifact_id']).result_json)
    artifact['_document_id'] = uid
    expected_hits = {}
    expected = retrieval._selected_spans(manifest, artifact, ['deadline'], False,
                                         hit_state=expected_hits)
    actual_hits = {}
    actual = retrieval._selected_spans(manifest, artifact,
        ['deadline', 'deadline deadline'], False, hit_state=actual_hits)
    assert actual == expected and actual_hits == expected_hits
    assert actual_hits['sections'] == [artifact['section_keys'][0]]
    assert actual_hits['blocks'] == [manifest['blocks'][0]['id']]
    assert not actual_hits.get('full_document')


def test_zero_score_passages_do_not_displace_hits_from_later_queries(workflow):
    uid, _ = source(workflow, ['termination notice is thirty days.'] * 4)
    assert retrieval.raw_review_passages(uid, ['unmatchedword']) == []
    assert retrieval.raw_review_passages(uid, ['termination notice'])


def test_expanded_limits_count_initial_hits_and_keep_neighbors_and_definitions(workflow):
    uid, manifest = source(workflow, [f'termination notice clause {i}.' for i in range(40)])
    entry = json.loads(workflow.payload_json)['document_snapshot']['files'][0]
    artifact = json.loads(db.session.get(DocumentPreparedArtifact, entry['artifact_id']).result_json)
    artifact['_document_id'] = uid
    first = DocumentSummarySegment.query.filter_by(document_id=uid, segment_key=artifact['section_keys'][0]).one()
    record = json.loads(first.result_json)
    record['definitions'] = [{'references': [{'block_id': manifest['blocks'][39]['id']}]}]
    first.result_json = service.encode(record)
    db.session.commit()
    hits = {}
    initial = retrieval._selected_spans(manifest, artifact, ['termination notice'], False, hit_state=hits)
    previous = {key: set(value) for key, value in hits.items()}
    spans = retrieval._selected_spans(manifest, artifact, ['termination notice'], False,
        section_limit=6, block_limit=24, hit_state=hits, separate_queries=True)
    assert len(hits['sections']) == 6 and len(hits['blocks']) == 24
    assert previous['sections'] <= set(hits['sections']) and previous['blocks'] <= set(hits['blocks'])
    assert all(any(s['start'] <= old['start'] and s['end'] >= old['end'] for s in spans) for old in initial)
    assert any(s['start'] <= manifest['blocks'][39]['start'] < s['end'] for s in spans)
    assert len({b for b in hits['blocks']}) == 24


def test_admission_policy_is_server_owned_and_override_only_affects_new_requests(workflow, app):
    saved = json.loads(workflow.data_json)
    assert saved['_retrieval_policy_version'] == 1
    app.config['CASE_ASSISTANT_RETRIEVAL_REVIEW_ENABLED'] = False
    assert service.submit(1, {**saved, 'workflow_run_id': workflow.id, '_retrieval_policy_version': 0}).id == workflow.id
    assert json.loads(workflow.data_json)['_retrieval_policy_version'] == 1
    service.stop(workflow, 'Create another prompt')
    new = service.submit(1, {**saved, '_retrieval_policy_version': 1})
    assert json.loads(new.data_json)['_retrieval_policy_version'] == 0


def test_amendment_added_from_snapshot_preserves_primary_edit_target(workflow, monkeypatch):
    from backend import document_preparation_tasks
    primary = json.loads(workflow.payload_json)['document_snapshot']['files'][0]['file_id']
    monkeypatch.setattr(document_preparation_tasks, 'parse_attachment', lambda *a, **kw:
                        parser.manifest_from_blocks([{'text': 'Upon fraud, immediate cessation applies.'}]))
    amendment = receive(admit('Amendment', scope='matter', key='amendment'))
    assert run(amendment)['ok']
    # This chat document is searchable in the database but outside the snapshot.
    outside = receive(admit('OtherChatFile', key='outside'))
    assert run(outside)['ok']
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': primary}]})
    payload = focus(workflow)
    payload['document_snapshot'] = snapshot
    payload['draft_action'], payload['revision_mode'] = 'revise_uploaded_draft', 'preserve_original'
    selected = [{'file_id': primary, 'role': 'primary_draft', 'method': 'verbatim'}]
    payload['intent_decision']['selected_files'] = payload['file_selection']['selected_files'] = selected
    payload['file_selection']['primary_draft_file_id'] = primary
    data = json.loads(workflow.data_json)
    data['document_snapshot'] = snapshot
    workflow.data_json, workflow.payload_json = service.encode(data), service.encode(payload)
    db.session.commit()
    tasks.plan_sections(workflow, payload)
    calls = []
    result = finish(workflow, monkeypatch, model(calls))
    assert result['file_selection']['primary_draft_file_id'] == primary
    assert result['file_selection']['selected_files'][0] == selected[0]
    assert amendment['document_id'] in {d['file_id'] for d in result['file_selection']['selected_files']}
    assert result['draft_action'] == 'revise_uploaded_draft' and result['revision_mode'] == 'preserve_original'
    assert amendment['document_id'] in result['file_extractions_str']
    assert all(outside['document_id'] not in prompt for _, prompt, _ in calls)


def test_no_hit_fallback_finishes_before_review_without_repeating_it(workflow, monkeypatch):
    source(workflow, ['Unrelated equipment provisions.'] * 4)
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    calls = []
    result = finish(workflow, monkeypatch, model(calls, unresolved=True))
    uid = result['document_snapshot']['files'][0]['file_id']
    assert uid in result['_full_fallback_ids']
    assert result['retrieval_review']['hits'][uid]['full_document']
    assert result['retrieval_review']['round'] == 1
    assert all(not json.loads(db.session.get(DocumentSummarySegment, w.result_id).result_json)['excerpts']
               for w in tasks.work_rows(workflow, 'query').filter_by(state='ready').all())


def test_no_supporting_documents_skips_review(workflow, monkeypatch):
    payload = focus(workflow)
    primary = payload['file_selection']['selected_files'][0]['file_id']
    payload['draft_action'] = 'revise_uploaded_draft'
    payload['file_selection']['primary_draft_file_id'] = primary
    payload['intent_decision']['selected_files'][0]['role'] = 'primary_draft'
    workflow.payload_json = service.encode(payload)
    db.session.commit()
    tasks.plan_sections(workflow, payload)
    result = finish(workflow, monkeypatch, model([]))
    assert result['retrieval_review']['state'] == 'skipped'
    assert tasks.work_rows(workflow, review.KIND).count() == 0


def test_exhaustive_request_still_processes_every_required_section(workflow, monkeypatch):
    _, manifest = source(workflow, ['termination notice is thirty days.'] * 30)
    payload = focus(workflow)
    payload['intent_decision']['coverage_mode'] = 'whole_document'
    tasks.plan_sections(workflow, payload)
    result = finish(workflow, monkeypatch, model([], sufficient=True))
    spans = [json.loads(w.material_json) for w in tasks.work_rows(workflow, 'query').all()]
    assert review.uncovered_spans([{'start': 0, 'end': len(manifest['text'])}],
                                 [(s['start'], s['end']) for s in spans], len(manifest['text'])) == []
    assert tasks.work_rows(workflow, review.KIND).count() == 0
    assert 'Every required supporting-document section was processed' in result['file_extractions_str']


def test_search_suggestions_remain_uncertain_after_round_limit(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    normal = model([], sufficient=True)
    def queries_only(prompt, schema, **kwargs):
        if schema is review.RetrievalReview:
            return schema(queries=['Look for an amendment or exception'])
        return normal(prompt, schema, **kwargs)
    result = finish(workflow, monkeypatch, queries_only)
    assert result['retrieval_review']['round'] == 1
    assert 'Look for an amendment or exception' in result['file_extractions_str']


def test_answer_overflow_reuses_completed_retrieval_review(workflow, monkeypatch):
    from backend import everything
    from backend.ai.token_budget import ContextBudgetExceeded
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    finish(workflow, monkeypatch, model([], sufficient=True))
    saved = {w.id: w.result_id for w in tasks.work_rows(workflow).all()}
    def overflow(*args, **kwargs):
        raise ContextBudgetExceeded('Reduce supporting evidence')
    monkeypatch.setattr(everything, '_everything_phase2_body', overflow)
    tasks.dispatch_answer(workflow)
    assert tasks.answer_prompt.run(workflow.id, workflow.group_id)['smaller_evidence']
    assert workflow.evidence_budget == 32000
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Completed retrieval repeated'))
    assert tasks.after_extraction.run(workflow.id, workflow.group_id)['answer_scheduled']
    assert {w.id: w.result_id for w in tasks.work_rows(workflow).all()} == saved


def test_review_split_children_survive_missing_callback_and_explicit_retry(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'] * 4)
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    good = model([], sufficient=True)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', good)
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    review.advance(workflow, json.loads(workflow.payload_json))
    parent = tasks.work_rows(workflow, review.KIND).one()
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: review.RetrievalReview(complete=False))
    assert tasks.process_section.run(parent.id)['split']
    children = json.loads(parent.material_json)['children']
    assert len(children) == 2 and all(db.session.get(service.Work, uid).state == 'queued' for uid in children)
    workflow.last_progress_at = service.now() - timedelta(minutes=31)
    workflow.dispatch_pending = True
    db.session.commit()
    tasks.reconcile_case_assistant_workflows.run()
    assert workflow.status == 'failed' and workflow.group_id is None
    assert all(db.session.get(service.Work, uid).state == 'cancelled' for uid in children)
    service.retry(workflow)
    tasks.dispatch_group(workflow)
    assert workflow.group_id
    finish(workflow, monkeypatch, good)
    assert parent.state == 'split'
    assert all(db.session.get(service.Work, uid).state == 'ready' for uid in children)


def test_crash_while_saving_document_reviews_preserves_search_parameters(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    provider = model([], unresolved=True)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    review.advance(workflow, json.loads(workflow.payload_json))
    for item in tasks.work_rows(workflow, review.KIND).all():
        tasks.process_section.run(item.id)
    original = review._queue
    def crash(row, payload, mode, refs, level=0):
        roots = original(row, payload, mode, refs, level)
        db.session.commit()
        if mode == 'documents':
            raise RuntimeError('Worker crashed before saving document-review roots')
        return roots
    monkeypatch.setattr(review, '_queue', crash)
    with pytest.raises(RuntimeError):
        review.advance(workflow, json.loads(workflow.payload_json))
    assert json.loads(workflow.payload_json)['retrieval_review']['follow_up_queries']
    monkeypatch.setattr(review, '_queue', original)
    finish(workflow, monkeypatch, provider)
    assert json.loads(workflow.payload_json)['retrieval_review']['state'] == 'complete'


def test_review_failure_retries_saved_work_and_cancellation_fences_publication(workflow, monkeypatch):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    good = model([], sufficient=True)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', good)
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    original = tasks.work_rows(workflow, 'query').one().result_id
    review.advance(workflow, json.loads(workflow.payload_json))
    item = tasks.work_rows(workflow, review.KIND).one()
    item.attempts = 2
    db.session.commit()
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: None)
    with pytest.raises(ValueError):
        tasks.process_section.run(item.id)
    assert workflow.status == 'failed'
    service.retry(workflow)
    assert tasks.work_rows(workflow, 'query').one().result_id == original
    def cancel(*args, **kwargs):
        answer = good(*args, **kwargs)
        service.cancel(workflow)
        return answer
    monkeypatch.setattr(retrieval, 'chat_completion_parse', cancel)
    assert tasks.process_section.run(item.id)['stopped']
    assert item.result_id is None and workflow.status == 'cancelled'


def test_final_answer_cannot_bypass_pending_review(workflow, monkeypatch):
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', model([], sufficient=True))
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    with pytest.raises(service.WorkflowStopped, match='retrieval review'):
        tasks.prepare_answer(workflow)


@pytest.mark.parametrize('mutation', ['user', 'chat', 'matter', 'version'])
def test_review_reauthorizes_scope_before_using_cached_results(workflow, monkeypatch, mutation):
    source(workflow, ['termination notice is thirty days.'])
    payload = focus(workflow)
    tasks.plan_sections(workflow, payload)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', model([], sufficient=True))
    for item in tasks.work_rows(workflow, 'query').all():
        tasks.process_section.run(item.id)
    review.advance(workflow, json.loads(workflow.payload_json))
    item = tasks.work_rows(workflow, review.KIND).one()
    tasks.process_section.run(item.id)
    payload = json.loads(workflow.payload_json)
    snapshot = payload['document_snapshot']
    if mutation == 'user': snapshot['owner_id'] = 2
    elif mutation == 'chat': snapshot['draft_id'] = '2'
    elif mutation == 'matter': snapshot['files'][0]['scope'] = 'matter'
    else: snapshot['files'][0]['artifact_id'] = 'old-version'
    with pytest.raises(prep.PreparationError):
        review.result(workflow, item, payload)


def test_all_one_hundred_documents_participate_in_bounded_review(workflow, monkeypatch):
    from backend import document_preparation_tasks
    text = 'termination notice is thirty days. ' + 'term ' * 450
    source(workflow, [text])
    ids = [json.loads(workflow.payload_json)['document_snapshot']['files'][0]['file_id']]
    monkeypatch.setattr(document_preparation_tasks, 'parse_attachment', lambda *a, **kw:
                        parser.manifest_from_blocks([{'text': text}]))
    for i in range(99):
        entry = receive(admit(str(i), key=f'follow-up-scale-{i}'))
        assert run(entry)['ok']
        ids.append(entry['document_id'])
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': uid} for uid in ids]})
    payload = focus(workflow, 'What notice period applies?')
    data = json.loads(workflow.data_json)
    data['document_snapshot'] = payload['document_snapshot'] = snapshot
    selected = [{'file_id': uid, 'role': 'supporting_context', 'method': 'relevant'} for uid in ids]
    payload['intent_decision']['selected_files'] = payload['file_selection']['selected_files'] = selected
    workflow.data_json, workflow.payload_json = service.encode(data), service.encode(payload)
    db.session.commit()
    tasks.plan_sections(workflow, payload)
    calls = []
    result = finish(workflow, monkeypatch, model(calls, sufficient=True))
    reviews = tasks.work_rows(workflow, review.KIND).all()
    leaves = [w for w in reviews if json.loads(w.material_json)['mode'] == 'evidence']
    represented = {ref['work_id'] for w in leaves for ref in json.loads(w.material_json)['inputs']}
    assert represented == {w.id for w in tasks.work_rows(workflow, 'query').all()}
    assert len(leaves) > 1 and any(json.loads(w.material_json)['mode'] == 'combine' for w in reviews)
    assert all(retrieval.request_tokens(prompt, schema) <= 40000 for schema, prompt, _ in calls)
    assert all(retrieval.token_count(json.loads(db.session.get(DocumentSummarySegment, w.result_id).result_json)) <= 4000
               for w in reviews if w.state == 'ready')
    assert len(service.status(workflow)['info']['files']) == 100
    assert result['retrieval_review']['state'] == 'complete'
