"""Original-range context and compact exhaustive findings, with mocked providers."""
import json
from pathlib import Path

import pytest

from backend.database import db
from backend.models import DocumentPreparedArtifact, DocumentSummarySegment, UploadedFiles
from backend import case_assistant_tasks as tasks
from backend.util import case_assistant_workflow as service
from backend.util import document_preparation as prep
from backend.util import prepared_retrieval as retrieval
from backend.util import source_context as context
from backend.util import attachment_parser as parser
from backend.util.preparation_summary import token_count
from tests.test_case_assistant_workflows import workflow
from tests.test_document_preparation import app, admit, receive, run
from tests.test_retrieval_follow_up import model as review_model


@pytest.fixture
def modern(workflow):
    tasks.work_rows(workflow).delete()
    data = json.loads(workflow.data_json)
    data['_source_context_version'] = context.VERSION
    workflow.data_json = service.encode(data)
    payload = json.loads(workflow.payload_json)
    for key in ('_sections_planned', '_planned_file_ids', 'source_context', 'retrieval_review'):
        payload.pop(key, None)
    payload['intent_decision']['selection_confidence'] = 'high'
    workflow.payload_json = service.encode(payload)
    db.session.commit()
    return workflow


def install(row, texts, *, uid=None, manifest=None):
    payload = json.loads(row.payload_json)
    entry = next(e for e in payload['document_snapshot']['files'] if uid is None or e['file_id'] == uid)
    file = UploadedFiles.query.filter_by(unique_id=entry['file_id']).one()
    manifest = manifest or parser.manifest_from_blocks([{'text': text, 'paragraph': i + 1}
                                                       for i, text in enumerate(texts)])
    file.content = manifest['text']
    artifact = db.session.get(DocumentPreparedArtifact, entry['artifact_id'])
    data = json.loads(artifact.result_json)
    keys = []
    for i, block in enumerate(manifest['blocks']):
        key = f'original-test-{i:04}'
        record = {**block, 'definitions': [], 'summary': manifest['text'][block['start']:block['end']]}
        db.session.add(DocumentSummarySegment(document_id=entry['file_id'], segment_key=key,
            kind='section', state='ready', result_json=service.encode(record), search_text=record['summary']))
        keys.append(key)
    data['section_keys'] = keys
    artifact.result_json = service.encode(data)
    artifact.manifest_json = service.encode({k: v for k, v in manifest.items() if k != 'text'})
    db.session.commit()
    return entry['file_id'], manifest


def focused(row, question='What is the termination notice period?'):
    payload = json.loads(row.payload_json)
    payload['user_message'] = question
    payload['intent_decision']['coverage_mode'] = 'focused'
    payload['intent_decision']['retrieval_queries'] = ['termination notice']
    row.payload_json = service.encode(payload)
    db.session.commit()
    return payload


def finish(row):
    if not row.group_id:
        tasks.dispatch_group(row)
    return tasks.after_extraction.run(row.id, row.group_id)


def add_files(row, names):
    if names:
        for offset in range(0, len(names), 5):
            group = admit(*names[offset:offset + 5], key=f'additional-original-documents-{offset}')
            for index in range(len(group['documents'])):
                entry = receive(group, index)
                assert run(entry)['ok']
    payload = json.loads(row.payload_json)
    files = UploadedFiles.query.filter_by(draft_id=row.draft_id).all()
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': f.unique_id} for f in files]})
    selected = [{'file_id': f.unique_id, 'role': 'supporting_context', 'method': 'relevant'} for f in files]
    data = json.loads(row.data_json)
    data['document_snapshot'] = snapshot
    row.data_json = service.encode(data)
    payload['document_snapshot'] = snapshot
    payload['intent_decision']['selected_files'] = selected
    payload['file_selection']['selected_files'] = selected
    row.payload_json = service.encode(payload)
    db.session.commit()
    return [f.unique_id for f in files]


def test_admission_uses_server_owned_source_policy_and_preserves_it_on_resubmit(app):
    entry = receive(admit('admission'))
    assert run(entry)['ok']
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}]})
    data = {'draftId': 'draft-1', 'query': 'Compare the sources.', 'document_snapshot': snapshot,
            '_source_context_version': -1}
    row = service.submit(1, data)
    assert json.loads(row.data_json)['_source_context_version'] == context.VERSION
    assert service.submit(1, {**data, 'workflow_run_id': row.id}).id == row.id


def test_complete_source_bypasses_all_extraction_review_and_reduction(modern, monkeypatch, app):
    uid, manifest = install(modern, ['The deadline is June 30.', 'The admission concerns the blue vehicle.'])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Original source required no provider processing'))
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    item = tasks.work_rows(modern, 'query').one()
    assert item.state == 'ready' and item.attempts == 0
    assert item.result_id is None
    assert DocumentSummarySegment.query.filter(DocumentSummarySegment.result_json.contains(context.SOURCE)).count() == 0
    assert finish(modern)['answer_scheduled']
    payload = json.loads(modern.payload_json)
    assert payload['source_context']['mode'] == 'full'
    assert payload['file_extractions_str'].count('The deadline is June 30.') == 1
    assert all(text in payload['file_extractions_str'] for text in [manifest['text'].split('\n')[0], 'blue vehicle'])
    assert payload['source_context']['assembled_tokens'] == token_count(payload['file_extractions_str'])
    assert not tasks.work_rows(modern).filter(service.Work.kind.in_(['reduce', 'retrieval_review'])).count()
    response = app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence')
    assert response.status_code == 200
    result = response.json['sections'][0]
    assert result['processing_method'] == context.SOURCE and result['findings'] == []
    assert result['excerpts'][0]['quote'] == manifest['text']


def test_section_match_is_not_its_entire_large_source_range(modern, monkeypatch):
    uid, manifest = install(modern, ['Unrelated financial history. ' * 1000] * 12
                            + ['Termination notice is thirty days.'] + ['Appendix context. ' * 500] * 2
                            + ['UNIQUE_FAR_APPENDIX_SENTINEL ' * 500])
    artifact_id = json.loads(modern.payload_json)['document_snapshot']['files'][0]['artifact_id']
    artifact = db.session.get(DocumentPreparedArtifact, artifact_id)
    data = json.loads(artifact.result_json)
    first = DocumentSummarySegment.query.filter_by(document_id=uid, segment_key=data['section_keys'][0]).one()
    record = {'start': 0, 'end': len(manifest['text']), 'summary': 'Termination notice', 'definitions': []}
    first.result_json, first.search_text = service.encode(record), 'termination notice'
    data['section_keys'] = [first.segment_key]
    artifact.result_json = service.encode(data)
    db.session.commit()
    payload = focused(modern)
    tasks.plan_sections(modern, payload)
    spans = [json.loads(w.material_json) for w in tasks.work_rows(modern).all()]
    assert all(token_count(manifest['text'][s['start']:s['end']]) <= context.WINDOW_LIMIT for s in spans)
    assert sum(token_count(manifest['text'][s['start']:s['end']]) for s in spans) < 5000
    # Neighbor windows may be clipped; treat that as a weak search, not a reason
    # to copy the entire source section into a new extraction job.
    monkeypatch.setattr(retrieval, 'chat_completion_parse', review_model([], sufficient=True))
    for _ in range(5):
        for w in tasks.work_rows(modern, 'retrieval_review').filter_by(state='queued').all():
            tasks.process_section.run(w.id)
        if finish(modern).get('answer_scheduled'):
            break
    result = json.loads(modern.payload_json)
    assert 'Termination notice is thirty days.' in result['file_extractions_str']
    assert 'UNIQUE_FAR_APPENDIX_SENTINEL' not in result['file_extractions_str']
    assert result['source_context']['mode'] == 'focused'


def test_definition_origin_range_is_searched_not_copied(modern):
    uid, manifest = install(modern, ['Business Day means a weekday excluding bank holidays.']
        + ['Unrelated equipment details. ' * 100] * 18 + ['Termination notice requires five Business Days.'])
    artifact_id = json.loads(modern.payload_json)['document_snapshot']['files'][0]['artifact_id']
    artifact = db.session.get(DocumentPreparedArtifact, artifact_id)
    data = json.loads(artifact.result_json)
    last = DocumentSummarySegment.query.filter_by(document_id=uid, segment_key=data['section_keys'][-1]).one()
    record = json.loads(last.result_json)
    record['definitions'] = [{'term': 'Business Day', 'meaning': 'weekdays',
                             'source_ranges': [{'start': 0, 'end': len(manifest['text'])}]}]
    last.result_json = service.encode(record)
    db.session.commit()
    spans = context.selected_spans(manifest, {**data, '_document_id': uid}, ['termination notice'], hit_state={})
    text = '\n'.join(manifest['text'][s['start']:s['end']] for s in spans)
    assert 'Business Day means' in text and 'five Business Days' in text
    assert token_count(text) < 4000


def test_selected_spans_keep_intervening_defense_line(modern):
    lines = [
        '183. Plaintiff’s claims are barred by the doctrine of waiver.',
        'Sixteenth Affirmative Defense',
        '(License)',
        '184. On information and belief, some or all of Plaintiff’s products are licensed under',
        'the ’922, ’936, ’273, and ’464 patents, and/or are subject to the doctrines of patent exhaustion and',
        'expressed and/or implied license.',
        '27',
    ]
    uid, manifest = install(modern, [], manifest=parser.manifest_from_blocks(
        [{'text': line, 'page': 22} for line in lines]))
    snapshot = json.loads(modern.payload_json)['document_snapshot']
    doc = context.document(snapshot, uid)
    # The heading and final continuation rank, but their one-block neighbors
    # leave the opening assertion between the two retrieved windows.
    hits = {'blocks': [manifest['blocks'][i]['id'] for i in (1, 5)]}
    spans = context.selected_spans(manifest, {**doc['artifact'], '_document_id': uid},
        ['Sixteenth Affirmative Defense License'], section_limit=0, block_limit=2, hit_state=hits)
    rendered = context.format_document(doc, spans, 'D1')
    assert rendered.count('\n\n'.join(lines[3:6])) == 1
    assert '[D1 page 22]' in rendered
    assert len(spans) == 1
    assert token_count(manifest['text'][spans[0]['start']:spans[0]['end']]) <= context.WINDOW_LIMIT


@pytest.mark.parametrize('delta,merged', [(0, True), (-1, False)])
def test_gap_bridge_respects_complete_passage_token_budget(delta, merged):
    text = 'Opening passage.\n\nAn intervening qualification applies.\n\nClosing passage.'
    spans = [{'start': 0, 'end': len('Opening passage.')},
             {'start': text.index('Closing passage.'), 'end': len(text)}]
    # Overlapping fragments must be unioned before deciding if bridging fits.
    fragments = [spans[0], {**spans[1], 'end': len(text) - len('passage.')},
                 {**spans[1], 'start': spans[1]['start'] + 4}]
    result = context.merge_ranges(text, fragments, bridge_token_limit=token_count(text) + delta)
    assert result == ([{'start': 0, 'end': len(text)}] if merged else spans)


def test_default_range_merge_keeps_gaps():
    text = 'Opening passage.\n\nAn intervening qualification applies.\n\nClosing passage.'
    spans = [{'start': 0, 'end': len('Opening passage.')},
             {'start': text.index('Closing passage.'), 'end': len(text)}]
    assert context.merge_ranges(text, spans) == spans


def test_unicode_windows_and_range_union_do_not_duplicate_source():
    manifest = parser.manifest_from_blocks([{'text': '§ 定義 😀 deadline June 30.\n\n' * 1200, 'page': 1}])
    text = manifest['text']
    spans = list(context.windows(text, 0, len(text)))
    assert ''.join(text[s['start']:s['end']] for s in spans) == text
    assert all(token_count(text[s['start']:s['end']]) <= context.WINDOW_TARGET for s in spans)
    doc = {'filename': 'unicode.pdf', 'manifest': manifest}
    once = context.format_document(doc, [{'start': 0, 'end': len(text)}], 'D1')
    overlapping = context.format_document(doc, [{'start': 0, 'end': len(text) - 10},
                                               {'start': 5, 'end': len(text)}], 'D1')
    assert overlapping == once and overlapping.count('§ 定義 😀 deadline June 30.') == 1200


@pytest.mark.parametrize('delta,ready', [(0, True), (-1, False)])
def test_actual_formatted_budget_boundary_includes_labels(modern, delta, ready):
    install(modern, ['An original deadline with source identity.'])
    payload = json.loads(modern.payload_json)
    tasks.plan_sections(modern, payload)
    expected = context.source_context(modern, json.loads(modern.payload_json))
    modern.evidence_budget = token_count(expected) + delta
    db.session.commit()
    assert tasks.prepare_answer(modern) is ready
    if ready:
        assert json.loads(modern.payload_json)['file_extractions_str'] == expected
    else:
        assert json.loads(modern.payload_json)['source_context']['mode'] == 'findings'
        assert tasks.work_rows(modern, 'query').filter_by(state='queued').count() > 0


def test_sufficient_focused_search_goes_directly_to_answer(modern, monkeypatch):
    install(modern, ['Termination notice is thirty days.', 'A short unrelated paragraph.'])
    payload = focused(modern)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Unnecessary extraction/review'))
    tasks.plan_sections(modern, payload)
    assert finish(modern)['answer_scheduled']
    assert json.loads(modern.payload_json)['retrieval_review']['state'] == 'skipped'


def test_weak_search_expands_once_and_reads_new_passage_without_extraction(modern, monkeypatch):
    uid, manifest = install(modern, ['Termination notice is thirty days.'] * 20
        + ['Unrelated equipment provisions.'] * 15 + ['Upon fraud, immediate cessation applies.'])
    payload = focused(modern)
    payload['intent_decision']['selection_confidence'] = 'low'
    tasks.plan_sections(modern, payload)
    original = {w.id: w.result_id for w in tasks.work_rows(modern).all()}
    calls = []
    provider = review_model(calls)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for _ in range(12):
        for item in tasks.work_rows(modern).filter_by(state='queued').all():
            tasks.process_section.run(item.id)
        if finish(modern).get('answer_scheduled'):
            break
    payload = json.loads(modern.payload_json)
    assert modern.phase == 'draft', modern.error_message
    assert 'Upon fraud, immediate cessation applies.' in payload['file_extractions_str']
    assert payload['retrieval_review']['round'] == 1 and payload['retrieval_review']['state'] == 'complete'
    assert sum(schema is tasks.retrieval_review.RetrievalReview for schema, _, _ in calls) == 1
    assert all(schema is not retrieval.EvidenceResult and schema is not context.FindingsResult for schema, _, _ in calls)
    assert not DocumentSummarySegment.query.filter(DocumentSummarySegment.result_json.contains(context.SOURCE)).count()
    assert all(item.result_id is None and item.attempts == 0 for item in tasks.work_rows(modern, 'query'))
    assert all(db.session.get(service.Work, wid).result_id == result_id for wid, result_id in original.items())
    hits = payload['retrieval_review']['hits'][uid]
    assert len(hits['sections']) <= 6 and len(hits['blocks']) <= 24


def test_no_hits_never_start_full_document_extraction(modern, monkeypatch):
    install(modern, ['This source concerns equipment purchases.'])
    payload = focused(modern, 'Find the nonexistentword restriction.')
    payload['intent_decision']['retrieval_queries'] = ['nonexistentword']
    tasks.plan_sections(modern, payload)
    def provider(prompt, schema, **kw):
        if schema is retrieval.DocumentReview:
            docs = json.loads(prompt.split('Documents:\n')[1])
            return schema(documents=[{'file_id': d['file_id'], 'relevant': True, 'reason': 'Requested source'} for d in docs])
        assert schema is tasks.retrieval_review.RetrievalReview
        return schema(issues=[{'part': 'Restriction', 'reason': 'Not located in the supplied material.'}], queries=['nonexistentword'])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for _ in range(12):
        for item in tasks.work_rows(modern).filter_by(state='queued').all():
            tasks.process_section.run(item.id)
        if finish(modern).get('answer_scheduled'):
            break
    assert modern.phase == 'draft'
    assert all(json.loads(w.material_json)['start'] == json.loads(w.material_json)['end'] == 0
               for w in tasks.work_rows(modern, 'query').all())
    assert 'do not infer absence' in json.loads(modern.payload_json)['file_extractions_str']


def test_large_exhaustive_request_saves_compact_findings_and_source_refs(modern, monkeypatch, app):
    uid, manifest = install(modern, ['The delivery deadline is June 30. ' * 250,
                                    'The allegation is denied except as expressly admitted. ' * 200])
    modern.evidence_budget = 500
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    assert json.loads(modern.payload_json)['source_context']['mode'] == 'findings'
    calls = []
    def provider(prompt, schema, **kw):
        calls.append((prompt, schema))
        assert schema is context.FindingsResult
        assert not db.session().in_transaction()
        return schema(findings=[{'statement': 'Delivery is due June 30.', 'block_ids': [manifest['blocks'][0]['id']]},
                                {'statement': 'The party denies the allegation, subject to its express admissions.',
                                 'block_ids': [manifest['blocks'][1]['id']]}])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for item in tasks.work_rows(modern).filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    assert finish(modern)['answer_scheduled']
    payload = json.loads(modern.payload_json)
    assert token_count(payload['file_extractions_str']) <= 500
    assert 'Delivery is due June 30.' in payload['file_extractions_str']
    assert payload['file_extractions_str'].count('Delivery is due June 30.') == 1
    assert '"quote"' not in payload['file_extractions_str']
    result = app.test_client().get(f'/api/everything/workflows/{modern.id}/evidence').json['sections'][0]
    assert result['processing_method'] == context.FINDINGS
    assert result['excerpts'] == [] and len(result['findings']) == 2
    assert result['findings'][0]['references'][0]['source_location']['paragraph'] == 1
    assert len(calls) == 1


def test_complete_list_overflow_asks_to_narrow_without_reduction(modern, monkeypatch):
    uid, manifest = install(modern, ['A deadline appears here. ' * 300])
    modern.evidence_budget = 150
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw: schema(findings=[
        {'statement': f'Deadline {i} is due on a distinct date after a different triggering event.',
         'block_ids': [manifest['blocks'][0]['id']]} for i in range(50)]))
    for item in tasks.work_rows(modern).filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    with pytest.raises(context.ContextBudgetExceeded, match='Narrow'):
        tasks.prepare_answer(modern)
    assert not tasks.work_rows(modern, 'reduce').count()
    assert tasks.work_rows(modern, 'query').filter_by(state='ready').count() == 1


def test_findings_reject_unavailable_source_ids_on_first_attempt(modern, monkeypatch):
    install(modern, ['The deadline is June 30. ' * 400])
    modern.evidence_budget = 150
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw:
        schema(findings=[{'statement': 'Invented support', 'block_ids': ['not-a-source-block']}]))
    item = tasks.work_rows(modern).filter_by(state='queued').one()
    with pytest.raises(ValueError, match='outside'):
        tasks.process_section.run(item.id)
    assert modern.status == 'failed' and item.state == 'failed' and item.attempts == 1


def test_saved_waymo_sources_fit_directly_without_query_model_calls(modern, monkeypatch):
    root = Path(__file__).resolve().parents[1] / 'artifacts/live-case-assistant/waymo-20261026-164035'
    paths = sorted((root / 'prepared').glob('*.json'))
    if len(paths) != 3:
        pytest.skip('Saved local Waymo fixture is not distributed with the repository')
    ids = add_files(modern, ['FAC', 'ANSWER'])
    expected_texts = []
    for uid, path in zip(ids, paths):
        saved = json.loads(path.read_text())
        manifest = saved['manifest_json']
        manifest = json.loads(manifest) if isinstance(manifest, str) else manifest
        manifest['text'] = (root / 'sources' / (saved['document_id'] + '.txt')).read_text()
        expected_texts.append(manifest['text'])
        install(modern, [], uid=uid, manifest=manifest)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Waymo source was copied by a model'))
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    assert finish(modern)['answer_scheduled']
    payload = json.loads(modern.payload_json)
    assert payload['source_context']['mode'] == 'full'
    assert token_count(payload['file_extractions_str']) < 50000
    for text in expected_texts:
        assert text[-150:] in payload['file_extractions_str']
    assert tasks.work_rows(modern, 'query').filter_by(state='ready').count() == 3
    assert all(item.result_id is None for item in tasks.work_rows(modern, 'query'))
    assert not DocumentSummarySegment.query.filter(DocumentSummarySegment.result_json.contains(context.SOURCE)).count()
    assert not tasks.work_rows(modern, 'retrieval_review').count()
    assert not tasks.work_rows(modern, 'reduce').count()


def test_100_focused_documents_share_one_budget_and_disclose_omissions(modern, monkeypatch):
    ids = add_files(modern, [f'Attachment-{i:03}' for i in range(99)])
    for index, uid in enumerate(ids):
        install(modern, [f'Unique document {index}: termination notice is thirty days. ' * 18], uid=uid)
    payload = focused(modern)
    tasks.plan_sections(modern, payload)
    # Every source was considered before assembly; this is not 64K per file.
    assert tasks.work_rows(modern, 'query').filter_by(state='ready').count() == 100
    assert not tasks.retrieval_review.advance(modern, json.loads(modern.payload_json))
    assert tasks.prepare_answer(modern)
    payload = json.loads(modern.payload_json)
    assert token_count(payload['file_extractions_str']) <= 64000
    assert len(payload['source_context']['included_ranges']) == 100
    assert payload['source_context']['omitted_windows'] == 0
    modern.evidence_budget = 4000
    db.session.commit()
    assert tasks.prepare_answer(modern)
    payload = json.loads(modern.payload_json)
    assert token_count(payload['file_extractions_str']) <= 4000
    assert 'Some retrieved source ranges do not fit' in payload['file_extractions_str']
    assert payload['source_context']['omitted_windows'] > 0
    assert not tasks.work_rows(modern, 'reduce').count()


def test_original_formatter_retains_accepted_gap_warning(modern):
    uid, manifest = install(modern, [], manifest=parser.manifest_from_blocks(
        [{'text': 'Readable content.', 'page': 1}], gaps=[{'code': 'unreadable', 'page': 2}]))
    rendered = context.format_document(context.document(json.loads(modern.payload_json)['document_snapshot'], uid),
        [{'start': 0, 'end': len(manifest['text'])}], 'D1')
    assert 'Original extraction: partial' in rendered and 'Do not infer absence' in rendered
    assert '[D1 page 1]' in rendered and 'page 2]' not in rendered


def test_missing_explicit_source_location_makes_search_weak(modern):
    uid, manifest = install(modern, ['Termination notice is thirty days.'])
    doc = context.document(json.loads(modern.payload_json)['document_snapshot'], uid)
    hits = {}
    context.selected_spans(manifest, {**doc['artifact'], '_document_id': uid},
                           ['termination notice in paragraph 42'], hit_state=hits)
    assert any('paragraph 42' in reason for reason in hits['weak_reasons'])


def test_one_consolidation_preserves_refs_and_saved_original_findings(modern, monkeypatch):
    uid, manifest = install(modern, ['Termination notice is thirty days. ' * 300])
    focused(modern, 'Analyze termination notice obligations.')
    payload = json.loads(modern.payload_json)
    payload['intent_decision']['coverage_mode'] = 'whole_document'
    modern.payload_json = service.encode(payload)
    modern.evidence_budget = 400
    db.session.commit()
    tasks.plan_sections(modern, payload)
    calls = []
    def provider(prompt, schema, **kw):
        calls.append(schema)
        if schema is context.FindingsResult:
            return schema(findings=[{'statement': f'Termination item {i}: a separate triggering condition applies.',
                'block_ids': [manifest['blocks'][0]['id']]} for i in range(45)])
        assert schema is context.CombinedResult
        return schema(findings=[{'statement': 'Termination requires thirty days notice, subject to the recorded conditions.',
            'references': [{'file_id': uid, 'block_id': manifest['blocks'][0]['id']}]}])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for item in tasks.work_rows(modern, 'query').filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    original_ids = [item.result_id for item in tasks.work_rows(modern, 'query').filter_by(state='ready')]
    assert not tasks.prepare_answer(modern)
    assert modern.reduction_round == 1
    for item in tasks.work_rows(modern, 'reduce').filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    assert tasks.prepare_answer(modern)
    assert 'paragraph 1' in json.loads(modern.payload_json)['file_extractions_str']
    assert len(list(context.all_findings(modern, original=True))) == 45
    assert original_ids == [item.result_id for item in tasks.work_rows(modern, 'query').filter_by(state='ready')]
    assert calls == [context.FindingsResult, context.CombinedResult]
    modern.evidence_budget = 10
    db.session.commit()
    with pytest.raises(context.ContextBudgetExceeded, match='Narrow'):
        tasks.prepare_answer(modern)
    assert modern.reduction_round == 1


def test_subdivision_has_disjoint_required_ranges_and_preserves_unicode(modern, monkeypatch):
    uid, manifest = install(modern, ['§ 定義 😀 deadline June 30. ' * 400])
    modern.evidence_budget = 100
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    item = tasks.work_rows(modern, 'query').filter_by(state='queued').one()
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw: schema(findings=[], complete=False))
    assert tasks.process_section.run(item.id)['split']
    children = [json.loads(w.material_json) for w in tasks.work_rows(modern, 'query').filter_by(state='queued')]
    children.sort(key=lambda s: s['core_start'])
    assert children[0]['core_start'] == 0 and children[-1]['core_end'] == len(manifest['text'])
    assert children[0]['core_end'] == children[1]['core_start']
    assert children[0]['end'] > children[0]['core_end']
    assert children[1]['start'] < children[1]['core_start']
    assert ''.join(manifest['text'][s['core_start']:s['core_end']] for s in children) == manifest['text']


def test_explicit_retry_reuses_completed_new_findings(modern, monkeypatch):
    uid, manifest = install(modern, ['Deadline June 30. ' * 400])
    modern.evidence_budget = 100
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw:
        schema(findings=[{'statement': 'Deadline June 30.', 'block_ids': [manifest['blocks'][0]['id']]}]))
    item = tasks.work_rows(modern, 'query').filter_by(state='queued').one()
    tasks.process_section.run(item.id)
    saved_id, attempts = item.result_id, item.attempts
    service.stop(modern, 'Answer worker failed.')
    assert modern.status == 'failed'
    service.retry(modern)
    assert context.enabled(modern) and item.result_id == saved_id
    tasks.process_section.run(item.id)
    assert item.attempts == attempts and item.result_id == saved_id


def test_cancellation_during_new_findings_rejects_late_publication(modern, monkeypatch):
    uid, manifest = install(modern, ['Deadline June 30. ' * 400])
    modern.evidence_budget = 100
    db.session.commit()
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    def cancelled(prompt, schema, **kw):
        service.cancel(modern)
        return schema(findings=[{'statement': 'Deadline June 30.', 'block_ids': [manifest['blocks'][0]['id']]}])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', cancelled)
    item = tasks.work_rows(modern, 'query').filter_by(state='queued').one()
    assert tasks.process_section.run(item.id)['stopped']
    assert modern.status == 'cancelled' and item.result_id is None
    assert DocumentSummarySegment.query.filter(DocumentSummarySegment.result_json.contains(context.FINDINGS)).count() == 0


@pytest.mark.parametrize('change', ['excluded', 'version', 'other_chat', 'other_user'])
def test_raw_source_lookup_rejects_changed_scope(modern, change, app):
    uid, manifest = install(modern, ['Original source.'])
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    payload = json.loads(modern.payload_json)
    if change == 'excluded':
        payload['document_snapshot']['files'] = []
        modern.payload_json = service.encode(payload)
        db.session.commit()
    elif change == 'version':
        entry = payload['document_snapshot']['files'][0]
        artifact = db.session.get(DocumentPreparedArtifact, entry['artifact_id'])
        artifact.document_id = 'different-document'
        db.session.commit()
    else:
        file = UploadedFiles.query.filter_by(unique_id=uid).one()
        file.draft_id = 2
        if change == 'other_user':
            file.project_id = 2
        db.session.commit()
    with pytest.raises((service.WorkflowStopped, prep.PreparationError)):
        tasks.prepare_answer(modern)


def test_final_prompt_overflow_switches_full_source_to_compact_findings(modern, monkeypatch):
    from backend import everything
    uid, manifest = install(modern, ['Deadline June 30. ' * 400])
    tasks.plan_sections(modern, json.loads(modern.payload_json))
    assert finish(modern)['answer_scheduled']
    calls = []
    def final(*args):
        calls.append(1)
        if len(calls) == 1:
            raise context.ContextBudgetExceeded('Final prompt exceeded its smaller window.')
        return {'response': 'Delivery is due June 30.'}
    monkeypatch.setattr(everything, '_everything_phase2_body', final)
    monkeypatch.setattr(everything, '_record_successful_chat_answer', lambda *a: None)
    assert tasks.answer_prompt.run(modern.id, modern.group_id)['smaller_evidence']
    assert modern.evidence_budget == 32000
    assert json.loads(modern.payload_json)['source_context']['mode'] == 'findings'
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw:
        schema(findings=[{'statement': 'Deadline June 30.', 'block_ids': [manifest['blocks'][0]['id']]}]))
    for item in tasks.work_rows(modern, 'query').filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    assert tasks.after_extraction.run(modern.id, modern.group_id)['answer_scheduled']
    tasks.answer_prompt.run(modern.id, modern.group_id)
    assert modern.status == 'completed' and len(calls) == 2
    assert tasks.answer_prompt.run(modern.id, modern.group_id)['stale']


def test_focused_final_overflow_repacks_ranges_without_extraction(modern, monkeypatch):
    from backend import everything
    install(modern, ['Termination notice is thirty days.'])
    tasks.plan_sections(modern, focused(modern))
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Unnecessary provider work'))
    assert finish(modern)['answer_scheduled']
    original = tasks.work_rows(modern, 'query').one().result_id
    def overflow(*args):
        raise context.ContextBudgetExceeded('Final window too small.')
    monkeypatch.setattr(everything, '_everything_phase2_body', overflow)
    for budget in (32000, 16000, 8000, 4000):
        assert tasks.answer_prompt.run(modern.id, modern.group_id)['smaller_evidence']
        assert modern.evidence_budget == budget
        assert tasks.after_extraction.run(modern.id, modern.group_id)['answer_scheduled']
        assert tasks.work_rows(modern, 'query').one().result_id == original
    with pytest.raises(context.ContextBudgetExceeded, match='Final window'):
        tasks.answer_prompt.run(modern.id, modern.group_id)
    assert modern.status == 'failed' and not tasks.work_rows(modern, 'reduce').count()


@pytest.mark.parametrize('mode', ['preserve_original', 'general_revision'])
def test_primary_draft_is_kept_exact_and_not_duplicated_in_support(modern, mode):
    ids = add_files(modern, ['Supporting'])
    primary, support = ids
    install(modern, ['Exact primary draft, keep its wording.'], uid=primary)
    install(modern, ['Additional source material.'], uid=support)
    payload = json.loads(modern.payload_json)
    payload.update(draft_action='revise_uploaded_draft', revision_mode=mode)
    payload['file_selection']['primary_draft_file_id'] = primary
    modern.payload_json = service.encode(payload)
    db.session.commit()
    tasks.plan_sections(modern, payload)
    assert finish(modern)['answer_scheduled']
    payload = json.loads(modern.payload_json)
    evidence = payload['file_extractions_str']
    assert evidence.count('Additional source material.') == 1
    assert evidence.count('Exact primary draft, keep its wording.') == (0 if mode == 'preserve_original' else 1)
    assert [w.document_id for w in tasks.work_rows(modern, 'query').all()] == [support]
    assert payload['verbatim_file_unique_ids'] == [primary]
