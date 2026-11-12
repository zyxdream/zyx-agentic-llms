"""Real SQL checkpoints with provider/broker I/O mocked at their boundaries."""
import io
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest

from backend.database import db
from backend.models import DocumentSummarySegment, TextEntry
from backend import case_assistant_tasks as tasks
from backend import case_assistant_routes as routes
from backend.util import case_assistant_workflow as service
from backend.util import document_preparation as prep
from backend.util import prepared_retrieval as retrieval
from backend.util import attachment_parser as parser
from backend.util.case_assistant_workflow import Workflow, Work
from tests.test_document_preparation import app, admit, receive, run


@pytest.fixture
def workflow(app, monkeypatch):
    app.register_blueprint(routes.bp)
    for endpoint, fn in list(app.view_functions.items()):
        if endpoint.startswith('case_assistant_workflows.'):
            app.view_functions[endpoint] = fn.__wrapped__
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=1))
    for task in (tasks.plan_prompt, tasks.process_section, tasks.after_extraction, tasks.answer_prompt):
        monkeypatch.setattr(task, 'apply_async', lambda *a, **kw: SimpleNamespace(id=kw.get('task_id')))
        monkeypatch.setattr(task, 'update_state', lambda *a, **kw: None)
    monkeypatch.setattr(tasks, 'chord', lambda *a, **kw: SimpleNamespace(apply_async=lambda **kw: None))
    entry = receive(admit('A'))
    assert run(entry)['ok']
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}]})
    row = service.submit(1, {'draftId': 'draft-1', 'query': 'Find every deadline.', 'document_snapshot': snapshot})
    row.execution_version = 0
    # submit() admits the current agent and prepares its initial delivery. This
    # fixture represents an older request, before a version-0 task ID was saved.
    row.planning_task_id = None
    # Exercise already-accepted requests throughout this legacy compatibility
    # suite. New-admission behavior is covered by test_source_context.
    data = json.loads(row.data_json)
    data.pop('_source_context_version')
    row.data_json = service.encode(data)
    selected = [{'file_id': entry['document_id'], 'role': 'supporting_context', 'method': 'relevant'}]
    payload = {'document_snapshot': snapshot, 'intent_decision': {'selected_files': selected, 'coverage_mode': 'whole_document'},
        'user_message': 'Find every deadline.', 'display_user_message': 'Find every deadline.', 'selected_model': 'gpt-6-luna',
        'draft_action': 'answer_only', 'revision_mode': 'general_revision', 'file_selection': {'selected_files': selected},
        'prompt_turn_meta': {'workflow_run_id': row.id}, 'verbatim_file_unique_ids': [],
        'doc_id': row.draft_id, 'user_id': row.user_id, 'workflow_run_id': row.id}
    row.payload_json, row.phase = service.encode(payload), 'extract'
    db.session.commit()
    tasks.plan_sections(row, payload)
    return row


def provider(prompt, schema, **kwargs):
    if schema is retrieval.EvidenceResult:
        material = json.loads(prompt.split('Source blocks:\n')[1])
        return schema(excerpts=[{'block_id': material[0]['block_id'], 'quote': material[0]['text']}])
    material = json.loads(prompt.split('Evidence:\n')[1])
    part = material[0]
    references = ([{'file_id': part['file_id'], **part['excerpts'][0]}] if 'excerpts' in part
                  else part['findings'][0]['references'])
    references = [{k: ref[k] for k in ('file_id', 'block_id', 'quote')} for ref in references]
    return schema(findings=[{'statement': 'The source establishes a deadline.', 'references': references}])


def complete(row, monkeypatch):
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    for item in tasks.work_rows(row).filter_by(state='queued').all():
        tasks.process_section.run(item.id)


@pytest.mark.parametrize('override,expected', [(None, 64000), (24000, 24000)])
def test_new_prompt_saves_effective_evidence_budget(app, override, expected):
    if override is not None:
        app.config['CASE_ASSISTANT_EVIDENCE_TOKENS'] = override
    entry = receive(admit('budget'))
    assert run(entry)['ok']
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}]})
    row = service.submit(1, {'draftId': 'draft-1', 'query': 'Find deadlines.', 'document_snapshot': snapshot})
    db.session.expire_all()
    assert db.session.get(Workflow, row.id).evidence_budget == expected


def test_workflow_model_saves_sixty_four_thousand_without_explicit_budget(workflow):
    row = Workflow(id='wf_model_default', user_id=workflow.user_id, draft_id=workflow.draft_id,
                   data_json=workflow.data_json, draft_hash=workflow.draft_hash,
                   created_at=service.now(), updated_at=service.now(), last_progress_at=service.now(),
                   deadline=workflow.deadline)
    db.session.add(row)
    db.session.commit()
    db.session.expire_all()
    assert db.session.get(Workflow, row.id).evidence_budget == 64000


@pytest.mark.parametrize('budget', [24000, 12000, 4000])
@pytest.mark.parametrize('resume', ['resubmit', 'retry', 'confirmation', 'failure_check'])
def test_saved_budget_survives_new_default(workflow, app, budget, resume):
    workflow.evidence_budget = budget
    app.config['CASE_ASSISTANT_EVIDENCE_TOKENS'] = 64000
    db.session.commit()
    uid = workflow.id
    if resume == 'resubmit':
        data = json.loads(workflow.data_json)
        assert service.submit(1, {**data, 'workflow_run_id': uid}).id == uid
    elif resume == 'retry':
        service.stop(workflow, 'Retry saved work.')
        response = app.test_client().post(f'/api/everything/workflows/{uid}/retry')
        assert response.status_code == 200 and response.json['workflow_run_id'] == uid
    elif resume == 'confirmation':
        tasks.work_rows(workflow).delete()
        workflow.payload_json = None
        workflow.status, workflow.phase = 'awaiting_user_confirmation', 'confirm'
        workflow.planning_task_id = 'previous-confirmation-task'
        workflow.result_json = service.encode({'confirmation': {'reason': 'Which documents?'}})
        db.session.commit()
        response = app.test_client().post(f'/api/everything/workflows/{uid}/confirm',
            json={'decision': {'reclassify_note': 'Use all included documents.'}})
        assert response.status_code == 200 and response.json['workflow_run_id'] == uid
        assert workflow.planning_task_id and workflow.planning_task_id != 'previous-confirmation-task'
    else:
        workflow.updated_at = service.now() - timedelta(minutes=2)
        workflow.dispatch_pending = True
        db.session.commit()
        tasks.reconcile_case_assistant_workflows.run()
        assert workflow.group_id is None  # Status checks never dispatch work.
    db.session.expire_all()
    saved = db.session.get(Workflow, uid)
    assert saved.evidence_budget == budget and saved.status == 'running'


def test_submitted_prompt_blocks_second_prompt_and_retry_identity(workflow):
    data = json.loads(workflow.data_json)
    data['workflow_run_id'] = workflow.id
    assert service.submit(1, data).id == workflow.id
    data.pop('workflow_run_id')
    with pytest.raises(ValueError, match='Another request'):
        service.submit(1, data)
    db.session.rollback()


def test_sections_persist_validated_results_and_duplicate_delivery_is_safe(workflow, monkeypatch):
    calls = []
    def tracked(*args, **kwargs):
        calls.append(1)
        return provider(*args, **kwargs)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', tracked)
    item = tasks.work_rows(workflow, 'query').one()
    first = tasks.process_section.run(item.id)
    second = tasks.process_section.run(item.id)
    assert first == second and len(calls) == 1
    assert item.state == 'ready'
    assert DocumentSummarySegment.query.filter_by(kind='query').count() == 1
    result = service.status(workflow)
    assert result['info']['counts']['query:ready'] == 1
    assert result['report_available'] and 'excerpts' not in json.dumps(result)


def test_no_hit_is_success_and_is_available_in_report(workflow, monkeypatch):
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw: schema(excerpts=[]))
    item = tasks.work_rows(workflow).one()
    tasks.process_section.run(item.id)
    assert item.state == 'ready'
    response = __import__('flask').current_app.test_client().get(f'/api/everything/workflows/{workflow.id}/evidence')
    assert response.status_code == 200
    assert response.json['sections'][0]['excerpts'] == []
    assert response.json['complete'] is False


def test_failure_does_not_look_like_empty_completed_extraction(workflow, monkeypatch):
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: None)
    item = tasks.work_rows(workflow).one()
    item.attempts = 2
    db.session.commit()
    with pytest.raises(ValueError):
        tasks.process_section.run(item.id)
    assert item.state == 'failed' and item.result_id is None
    assert workflow.status == 'failed'
    progress = service.status(workflow)
    assert progress['ready'] and not progress['successful']
    assert progress['value']['error_message'] == workflow.error_message
    service.retry(workflow)
    assert item.state == 'queued' and item.attempts == 0


def test_retry_keeps_successful_results_and_replaces_only_unfinished_tasks(workflow, monkeypatch):
    complete(workflow, monkeypatch)
    item = tasks.work_rows(workflow).one()
    result_id = item.result_id
    extra = tasks.add_work(workflow, 'query', {'start': 1, 'end': 10}, item.document_id, item.artifact_id)
    extra.state = 'failed'
    db.session.commit()
    service.stop(workflow, 'A worker failed')
    service.retry(workflow)
    tasks.dispatch_group(workflow)
    assert item.result_id == result_id and item.state == 'ready'
    assert extra.state == 'queued' and extra.task_id
    assert workflow.group_id and workflow.answer_task_id


def test_forged_quotes_are_never_published(workflow, monkeypatch):
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw: schema(excerpts=[{'block_id': 'invented', 'quote': 'fake'}]))
    item = tasks.work_rows(workflow).one()
    item.attempts = 2
    db.session.commit()
    with pytest.raises(ValueError):
        tasks.process_section.run(item.id)
    assert DocumentSummarySegment.query.filter_by(kind='query').count() == 0


def test_oversized_output_saves_children_before_callback_can_answer(workflow, monkeypatch):
    from backend.ai.token_budget import IncompleteStructuredOutput
    item = tasks.work_rows(workflow).one()
    item.material_json = service.encode({'start': 0, 'end': 1000})
    db.session.commit()
    monkeypatch.setattr(tasks, 'extraction_result', lambda *a: (_ for _ in ()).throw(IncompleteStructuredOutput()))
    assert tasks.process_section.run(item.id)['split']
    assert item.state == 'split'
    children = tasks.work_rows(workflow).filter_by(state='queued').all()
    assert len(children) == 2
    spans = [json.loads(child.material_json) for child in children]
    assert min(s['start'] for s in spans) == 0 and max(s['end'] for s in spans) == 1000


def test_aggregation_uses_saved_results_and_keeps_original_evidence(workflow, monkeypatch):
    complete(workflow, monkeypatch)
    item = tasks.work_rows(workflow).one()
    saved = db.session.get(DocumentSummarySegment, item.result_id)
    value = json.loads(saved.result_json)
    # Distinct supported passages exercise aggregation; exact overlap duplicates are removed.
    source = value['excerpts'][0]
    text = source['quote']
    value['excerpts'] = [{'block_id': source['block_id'], 'quote': text[start:end]}
        for start in range(15) for end in range(start+1, min(len(text), start+8))][:80]
    saved.result_json = service.encode(value)
    workflow.evidence_budget = 700
    db.session.commit()
    monkeypatch.setattr(retrieval, 'authorized_artifacts', lambda *a, **kw: pytest.fail('Aggregation re-read original sources'))
    assert not tasks.prepare_answer(workflow)
    assert workflow.reduction_round == 1
    for reduction in tasks.work_rows(workflow, 'reduce').all():
        tasks.process_section.run(reduction.id)
    assert tasks.prepare_answer(workflow)
    payload = json.loads(workflow.payload_json)
    assert payload['saved_evidence_only']
    assert 'compact overview' in payload['file_extractions_str']
    assert len(json.loads(saved.result_json)['excerpts']) == 80


def prepare_long_source_results(workflow, monkeypatch, block_count):
    """Save genuine, individually bounded source quotes for aggregation tests."""
    from backend import document_preparation_tasks
    manifest = parser.manifest_from_blocks([
        {'text': f'Deadline section {index}. ' + 'The filing deadline is June 1, 2027. ' * 250}
        for index in range(block_count)])
    monkeypatch.setattr(document_preparation_tasks, 'parse_attachment', lambda *a, **kw: manifest)
    entry = receive(admit('long-source', key='overflow-source'))
    assert run(entry)['ok']
    uid = entry['document_id']
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': uid}]})
    data, payload = json.loads(workflow.data_json), json.loads(workflow.payload_json)
    data['document_snapshot'] = payload['document_snapshot'] = snapshot
    payload.update(use_uploaded_files=True, assistant_mode='deep',
                   create_new_draft=False, change_draft_requested=False)
    selected = [{'file_id': uid, 'role': 'supporting_context', 'method': 'relevant'}]
    payload['intent_decision']['selected_files'] = payload['file_selection']['selected_files'] = selected
    workflow.data_json, workflow.payload_json = service.encode(data), service.encode(payload)
    tasks.work_rows(workflow).delete()
    for block in manifest['blocks']:
        tasks.add_work(workflow, 'query', {'start': block['start'], 'end': block['end']}, uid, snapshot['files'][0]['artifact_id'])
    db.session.commit()
    complete(workflow, monkeypatch)
    return uid


def estimated_evidence_tokens(workflow):
    from backend.util.preparation_summary import token_count
    # Count complete units including references/locations, the coverage notice,
    # and a separator allowance, as required by the persisted evidence budget.
    return token_count(tasks.evidence_header(workflow, json.loads(workflow.payload_json))) + sum(
        token_count(service.encode(value)) + 1 for value in tasks.evidence_items(workflow))


def test_sixty_thousand_tokens_reach_final_payload_without_reduction(workflow, monkeypatch):
    from backend.util.preparation_summary import token_count
    uid = prepare_long_source_results(workflow, monkeypatch, 19)
    expected = list(tasks.evidence_items(workflow))
    assert 57000 < estimated_evidence_tokens(workflow) < 64000
    # Changing deployment configuration cannot lower an already accepted budget.
    from flask import current_app
    current_app.config['CASE_ASSISTANT_EVIDENCE_TOKENS'] = 24000
    assert tasks.prepare_answer(workflow)
    context = json.loads(workflow.payload_json)['file_extractions_str']
    assert token_count(context) <= 64000 and uid in context
    assert all(service.encode(value) in context for value in expected)
    assert context.startswith(tasks.evidence_header(workflow, json.loads(workflow.payload_json)))
    assert workflow.reduction_round == 0 and tasks.work_rows(workflow, 'reduce').count() == 0


def fit_saved_quotes_to_estimate(workflow, target):
    """Choose source-valid quote prefixes to hit an exact estimated boundary.

    Each saved result stays below 6K. This only constructs test responses; the
    production aggregator must retain or reduce complete results, never slice.
    """
    assert estimated_evidence_tokens(workflow) > target
    for item in reversed(tasks.work_rows(workflow, 'query').order_by(Work.id).all()):
        saved = db.session.get(DocumentSummarySegment, item.result_id)
        original = json.loads(saved.result_json)
        quote = original['excerpts'][0]['quote']
        def prefix(length, start=0):
            refs = [{**original['excerpts'][0], 'quote': quote[start:length]}] if length > start else []
            saved.result_json = service.encode({**original, 'excerpts': refs})
        prefix(0)
        if estimated_evidence_tokens(workflow) > target:
            continue
        # Some source prefixes skip a token count because of merges with JSON
        # framing. Random UUID lengths can expose that gap. Try nearby source
        # starts too: every candidate remains an exact substring of the source.
        for start in range(17):
            low, high = start, len(quote)
            while low < high:
                middle = (low + high) // 2
                prefix(middle, start)
                if estimated_evidence_tokens(workflow) < target:
                    low = middle + 1
                else:
                    high = middle
            for length in range(max(start, low - 64), min(len(quote), low + 64) + 1):
                prefix(length, start)
                if estimated_evidence_tokens(workflow) == target:
                    db.session.commit()
                    return
        pytest.fail('Could not construct the exact token boundary from source text')
    pytest.fail('Source results cannot reach the requested token boundary')


@pytest.mark.parametrize('estimated_size,ready', [(64000, True), (64001, False)])
def test_combined_evidence_boundary_includes_framing_and_references(workflow, monkeypatch, estimated_size, ready):
    from backend.util.preparation_summary import token_count
    prepare_long_source_results(workflow, monkeypatch, 22)
    fit_saved_quotes_to_estimate(workflow, estimated_size)
    expected = list(tasks.evidence_items(workflow))
    originals = {w.id: db.session.get(DocumentSummarySegment, w.result_id).result_json
                 for w in tasks.work_rows(workflow, 'query').all()}
    assert estimated_evidence_tokens(workflow) == estimated_size
    assert all(token_count(value) < 6000 for value in expected)
    assert tasks.prepare_answer(workflow) is ready
    if ready:
        context = json.loads(workflow.payload_json)['file_extractions_str']
        assert all(service.encode(value) in context for value in expected)
        assert workflow.reduction_round == 0
    else:
        assert not json.loads(workflow.payload_json).get('file_extractions_str')
        assert workflow.reduction_round == 1 and tasks.work_rows(workflow, 'reduce').count() > 0
    for item in tasks.work_rows(workflow, 'query').all():
        assert item.state == 'ready'
        assert db.session.get(DocumentSummarySegment, item.result_id).result_json == originals[item.id]


def test_final_context_overflow_reduces_saved_evidence_and_publishes_once(workflow, monkeypatch):
    from backend import everything
    from backend.util.preparation_summary import token_count
    from backend.util.db import DBMemory
    # Source results fit 64K aggregation but exceed the smaller final-call window.
    uid = prepare_long_source_results(workflow, monkeypatch, 12)
    assert tasks.prepare_answer(workflow)
    saved_input = json.loads(workflow.payload_json)['file_extractions_str']
    assert 32000 < token_count(saved_input) < 64000
    originals = {w.id: (w.result_id, db.session.get(DocumentSummarySegment, w.result_id).result_json)
                 for w in tasks.work_rows(workflow, 'query').all()}
    snapshot_json = workflow.data_json
    monkeypatch.setitem(everything.MODEL_CONTEXT_LIMITS, 'gpt-6.1-sol', 128000 + 2048 + 32000)
    answers = []
    monkeypatch.setattr(everything, 'multichat_completion_text', lambda **kw:
                        (answers.append(kw) or 'The filing deadline is June 1, 2027.'))
    monkeypatch.setattr(everything, 'retrieve_relevant_chat_turns', lambda *a, **kw: '')
    monkeypatch.setattr(everything, 'finalize_llm_response_for_display', lambda text, *a, **kw: (text, {}, {}))
    monkeypatch.setattr(everything, '_enqueue_brief_update', lambda *a, **kw: None)
    monkeypatch.setattr(everything, '_record_successful_chat_answer', lambda *a: True)
    monkeypatch.setattr(tasks, 'extraction_result', lambda *a: pytest.fail('Answer recovery repeated source extraction'))
    workflow.group_id = 'initial'
    db.session.commit()
    tasks.dispatch_answer(workflow)
    assert tasks.answer_prompt.run(workflow.id, workflow.group_id) == {'smaller_evidence': True}
    assert workflow.evidence_budget == 32000 and workflow.status == 'running'
    assert workflow.active_draft_id == workflow.draft_id and workflow.lease_token is None
    memory = DBMemory(db=db, doc_id=workflow.draft_id)
    assert not everything.load_state_from_memory(memory)['chat_history'] and not answers
    assert not tasks.after_extraction.run(workflow.id, workflow.group_id)['answer_scheduled']
    assert workflow.reduction_round == 1
    for item in tasks.work_rows(workflow, 'reduce').filter_by(state='queued').all():
        tasks.process_section.run(item.id)
    assert tasks.after_extraction.run(workflow.id, workflow.group_id)['answer_scheduled']
    tasks.answer_prompt.run(workflow.id, workflow.group_id)
    assert workflow.status == 'completed' and workflow.active_draft_id is None
    assert len(answers) == 1 and token_count(answers[0]['new_prompt']) < 32000
    assert 'compact overview' in answers[0]['new_prompt'] and uid in answers[0]['new_prompt']
    assert tasks.answer_prompt.run(workflow.id, workflow.group_id)['stale']
    assert len(everything.load_state_from_memory(memory)['chat_history']) == 1
    assert workflow.data_json == snapshot_json
    for item in tasks.work_rows(workflow, 'query').all():
        assert item.state == 'ready'
        assert (item.result_id, db.session.get(DocumentSummarySegment, item.result_id).result_json) == originals[item.id]


def test_repeated_context_overflow_stops_at_four_thousand_tokens(workflow, monkeypatch):
    from backend import everything
    from backend.ai.token_budget import ContextBudgetExceeded
    complete(workflow, monkeypatch)
    assert tasks.prepare_answer(workflow)
    original = tasks.work_rows(workflow, 'query').one().result_id
    def fail(*a, **kw):
        raise ContextBudgetExceeded('The final request still does not fit. Select a smaller section.')
    monkeypatch.setattr(everything, '_everything_phase2_body', fail)
    monkeypatch.setattr(tasks, 'extraction_result', lambda *a: pytest.fail('Completed sections must be reused'))
    workflow.group_id = 'initial'
    db.session.commit()
    tasks.dispatch_answer(workflow)
    for expected in (32000, 16000, 8000, 4000):
        assert tasks.answer_prompt.run(workflow.id, workflow.group_id)['smaller_evidence']
        assert workflow.status == 'running' and workflow.evidence_budget == expected
        assert workflow.active_draft_id == workflow.draft_id and workflow.lease_token is None
        assert tasks.after_extraction.run(workflow.id, workflow.group_id)['answer_scheduled']
    with pytest.raises(ContextBudgetExceeded, match='Select a smaller section'):
        tasks.answer_prompt.run(workflow.id, workflow.group_id)
    assert workflow.status == 'failed' and workflow.active_draft_id is None
    assert tasks.work_rows(workflow, 'query').one().result_id == original
    assert 'Select a smaller section' in workflow.error_message


def test_exact_draft_overflow_stops_without_reducing_evidence(workflow, monkeypatch):
    from backend import everything
    from backend.ai.token_budget import ContextBudgetExceeded
    complete(workflow, monkeypatch)
    assert tasks.prepare_answer(workflow)
    def fail(*a, **kw):
        raise ContextBudgetExceeded(everything.DRAFT_CONTEXT_TOO_LONG_MESSAGE, can_reduce_evidence=False)
    monkeypatch.setattr(everything, '_everything_phase2_body', fail)
    monkeypatch.setattr(tasks, 'dispatch_group', lambda *a: pytest.fail('Exact source cannot be reduced'))
    with pytest.raises(ContextBudgetExceeded, match='select a smaller section'):
        tasks.answer_prompt.run(workflow.id, workflow.group_id)
    assert workflow.status == 'failed' and workflow.evidence_budget == 64000
    assert workflow.reduction_round == 0 and workflow.active_draft_id is None
    assert service.status(workflow)['report_available']


def test_preserve_original_still_requires_complete_input_below_104k(monkeypatch):
    from backend import everything
    from backend.ai.token_budget import ContextBudgetExceeded
    original = 'Original deadline clause. ' * 17000
    payload = {'editable_blocks': [{'anchor': 'p_0001', 'text': original}], 'context_blocks': []}
    evidence = 'Record statement. ' * 20000
    assert everything.num_tokens_from_string(evidence) < 64000
    requests = []
    def provider(prompt, schema, **kw):
        requests.append(prompt)
        return schema(operations=[], response='No change required.')
    monkeypatch.setattr(everything, 'chat_completion_parse', provider)
    with pytest.raises(ContextBudgetExceeded) as error:
        everything.edit_preserve_original_blocks('Update the deadline.', payload, supporting_evidence=evidence)
    assert error.value.can_reduce_evidence and not requests
    # The next attempt carries less evidence, retaining the entire editable text.
    everything.edit_preserve_original_blocks('Update the deadline.', payload,
                                             supporting_evidence='Record statement. ' * 10000)
    assert len(requests) == 1 and original in requests[0]
    complete_input = everything.num_tokens_from_string(everything.SYSTEM_PROMPT + requests[0] +
        json.dumps(everything.PreserveOriginalEditResult.model_json_schema())) + 512
    assert complete_input <= 104000


def test_general_revision_marks_exact_primary_source_separately(workflow, monkeypatch):
    from backend import everything
    from backend.models import UploadedFiles
    complete(workflow, monkeypatch)
    item = tasks.work_rows(workflow, 'query').one()
    payload = json.loads(workflow.payload_json)
    payload.update(draft_action='revise_uploaded_draft', revision_mode='general_revision')
    payload['file_selection']['primary_draft_file_id'] = item.document_id
    workflow.payload_json = service.encode(payload)
    db.session.commit()
    assert tasks.prepare_answer(workflow)
    context = json.loads(workflow.payload_json)['file_extractions_str']
    exact, evidence = everything.split_file_extractions_str(context)
    source = UploadedFiles.query.filter_by(unique_id=item.document_id).one().content
    assert context.startswith(everything.VERBATIM_BLOCK_HEADER)
    assert exact and source in exact and item.document_id in exact
    assert evidence and 'Source-backed extracted evidence' in evidence
    assert json.loads(workflow.payload_json)['saved_evidence_only']


@pytest.mark.parametrize('reason', ['round_limit', 'no_shrink'])
def test_evidence_reduction_stops_when_it_cannot_progress(workflow, monkeypatch, reason):
    from backend.ai.token_budget import ContextBudgetExceeded
    complete(workflow, monkeypatch)
    original = tasks.work_rows(workflow, 'query').one().result_id
    workflow.evidence_budget = 1
    if reason == 'round_limit':
        workflow.reduction_round = 8
    else:
        payload = json.loads(workflow.payload_json)
        payload['_previous_evidence_size'] = 0
        workflow.payload_json = service.encode(payload)
    db.session.commit()
    monkeypatch.setattr(tasks, 'dispatch_answer', lambda *a: pytest.fail('Incomplete evidence must not be answered'))
    with pytest.raises(ContextBudgetExceeded, match='cannot be reduced safely'):
        tasks.after_extraction.run(workflow.id, workflow.group_id)
    assert workflow.status == 'failed' and workflow.active_draft_id is None
    assert tasks.work_rows(workflow, 'reduce').count() == 0
    assert tasks.work_rows(workflow, 'query').one().result_id == original


def test_cancelled_request_rejects_late_chat_publication(workflow, monkeypatch):
    from backend.util.db import DBMemory
    from backend.everything import save_state_to_memory
    monkeypatch.setattr(__import__('celery').current_app.control, 'revoke', lambda *a, **kw: None)
    token = tasks.claim_workflow(workflow.id)
    service.cancel(workflow)
    with service.execution(workflow.id, token), pytest.raises(service.WorkflowStopped):
        save_state_to_memory({'draft': {}, 'chat_history': []}, DBMemory(db=db, doc_id=1))
    assert TextEntry.query.filter_by(draftId=1, textName='state').first() is None


def test_stale_callback_cannot_start_answer(workflow):
    workflow.group_id = 'current'
    db.session.commit()
    assert tasks.after_extraction.run(workflow.id, 'old')['stale']
    assert workflow.phase == 'extract'


def test_readiness_is_checked_again_before_answer(workflow):
    workflow.group_id = 'current'
    db.session.commit()
    assert tasks.answer_prompt.run(workflow.id, 'current')['stopped']
    assert workflow.status == 'failed'


def test_expired_section_fails_without_losing_ready_results(workflow, monkeypatch):
    complete(workflow, monkeypatch)
    ready = tasks.work_rows(workflow).one()
    pending = tasks.add_work(workflow, 'query', {'start': 1, 'end': 10}, ready.document_id, ready.artifact_id)
    pending.state, pending.lease_token = 'running', 'dead'
    pending.lease_until, pending.attempts = service.now()-timedelta(seconds=1), 1
    workflow.updated_at = service.now()-timedelta(minutes=30)
    db.session.commit()
    tasks.reconcile_case_assistant_workflows.run()
    assert workflow.status == 'failed' and workflow.active_draft_id is None
    assert pending.state == 'failed' and pending.task_id is None
    assert ready.state == 'ready' and ready.result_id


def test_scope_checks_apply_to_progress_and_paginated_evidence(workflow, app, monkeypatch):
    complete(workflow, monkeypatch)
    client = app.test_client()
    path = f'/api/everything/workflows/{workflow.id}'
    assert client.get(path).status_code == 200
    response = client.get(path + '/evidence?limit=1')
    assert response.status_code == 200 and len(response.json['sections']) == 1
    assert response.json['sections'][0]['excerpts'][0]['source_location']
    monkeypatch.setattr(routes, 'current_user', SimpleNamespace(id=2))
    assert client.get(path).status_code == 404
    assert client.get(path + '/evidence').status_code == 404


def test_missing_workflow_progress_returns_not_found(workflow, app):
    response = app.test_client().get('/api/everything/workflows/wf_missing')
    assert response.status_code == 404
    assert response.json == {'msg': 'Request not found'}


def test_internal_progress_key_error_is_a_server_error(workflow, app, monkeypatch):
    def broken_status(row):
        raise KeyError('unexpected-document')
    monkeypatch.setattr(service, 'status', broken_status)
    app.config['PROPAGATE_EXCEPTIONS'] = False
    response = app.test_client().get(f'/api/everything/workflows/{workflow.id}')
    assert response.status_code == 500
    assert b'Request not found' not in response.data


def test_changed_artifact_is_rejected_on_report_and_retry(workflow, app, monkeypatch):
    complete(workflow, monkeypatch)
    from backend.models import DocumentPreparation
    item = tasks.work_rows(workflow).one()
    document = db.session.get(DocumentPreparation, item.document_id)
    document.generation += 1
    db.session.commit()
    response = app.test_client().get(f'/api/everything/workflows/{workflow.id}/evidence')
    assert response.status_code == 409
    service.stop(workflow, 'Failed')
    with pytest.raises(prep.PreparationError):
        service.retry(workflow)


def test_request_deadline_is_independent_of_task_timeout(workflow):
    workflow.deadline = service.now()-timedelta(seconds=1)
    db.session.commit()
    tasks.reconcile_case_assistant_workflows.run()
    assert workflow.status == 'failed' and workflow.active_draft_id is None
    assert 'overall deadline' in workflow.error_message


def test_planning_resumes_saved_reviews_without_reclassifying(workflow, monkeypatch):
    from backend import everything
    selected = json.loads(workflow.payload_json)['intent_decision']['selected_files']
    decision = everything._normalize_intent_decision({'draft_action': 'answer_only', 'selected_files': selected,
        'coverage_mode': 'whole_document'}, matter_files=[], previous_chat_files=[],
        last_new_chat_files=[{'unique_id': selected[0]['file_id']}])
    classifications = []
    monkeypatch.setattr(everything, 'classify_intent_decision', lambda *a, **kw: (classifications.append(1) or decision))
    monkeypatch.setattr(tasks.plan_prompt, 'update_state', lambda *a, **kw: None)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw:
        schema(documents=[{'file_id': selected[0]['file_id'], 'relevant': True, 'reason': 'Relevant source'}]))
    tasks.work_rows(workflow).delete()
    workflow.payload_json, workflow.phase = None, 'classify'
    db.session.commit()
    for _ in range(10):
        result = tasks.plan_prompt.run(workflow.id)
        if not result.get('planning_continues'):
            break
    assert workflow.status == 'running', workflow.error_message
    assert workflow.phase == 'extract' and workflow.payload_json
    assert len(classifications) == 1
    assert tasks.work_rows(workflow, 'query').count() == 1
    assert DocumentSummarySegment.query.filter_by(kind='review', state='ready').count() >= 1


def test_paginated_report_preserves_all_sections_and_checks_each_version(workflow, app, monkeypatch):
    complete(workflow, monkeypatch)
    source = tasks.work_rows(workflow).one()
    for i in range(3):
        extra = tasks.add_work(workflow, 'query', {'start': i+1, 'end': 20}, source.document_id, source.artifact_id)
        extra.state, extra.result_id = 'ready', source.result_id
    db.session.commit()
    path = f'/api/everything/workflows/{workflow.id}/evidence?limit=2'
    first = app.test_client().get(path).json
    second = app.test_client().get(path+'&after='+first['next']).json
    assert first['total_sections'] == second['total_sections'] == 4
    assert len({s['section_id'] for s in first['sections']+second['sections']}) == 4
    assert second['next'] is None


def test_queue_wait_deadline_does_not_reset_on_status_check(workflow):
    workflow.last_progress_at = service.now()-timedelta(hours=1)
    workflow.updated_at = service.now()
    db.session.commit()
    tasks.reconcile_case_assistant_workflows.run()
    assert workflow.status == 'failed' and 'queue-wait' in workflow.error_message


def test_draft_change_blocks_final_publication(workflow):
    from backend import everything
    from backend.util.db import DBMemory
    from backend.util import workspace_store
    token = tasks.claim_workflow(workflow.id)
    state = workspace_store.load(workflow.draft_id)
    workspace_store.update_draft(workflow.draft_id, {'title': 'User changed this'},
                                 expected_version=state['draft_version'])
    db.session.commit()
    with service.execution(workflow.id, token), pytest.raises(service.WorkflowStopped, match='draft changed'):
        everything.save_state_to_memory({'draft': {'title': 'Old generated version'}, 'chat_history': []}, DBMemory(db=db, doc_id=1))
    saved = workspace_store.load(workflow.draft_id)
    assert saved['draft']['title'] == 'User changed this'


def test_mysql_migration_has_longtext_and_single_active_prompt_constraint():
    from importlib import import_module
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    output = io.StringIO()
    context = MigrationContext.configure(dialect_name='mysql', opts={'as_sql': True, 'output_buffer': output})
    with Operations.context(context):
        import_module('migrations.versions.f7a8b9c0d1e2_case_assistant_extraction_tasks').upgrade()
    sql = output.getvalue()
    assert 'material_json LONGTEXT' in sql and 'data_json LONGTEXT' in sql
    assert 'UNIQUE (active_draft_id)' in sql


def test_preserve_editor_receives_supporting_evidence_and_counts_it_in_budget(monkeypatch):
    from backend import everything
    seen = []
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda prompt, schema, **kw:
        (seen.append(prompt) or schema(operations=[], response='No change required.')))
    everything.edit_preserve_original_blocks('Update the deadline.', {'editable_blocks': [], 'context_blocks': []},
        supporting_evidence='[file_id]: letter-2\n[block_id]: b1\nThe extension was rejected.')
    assert 'The extension was rejected.' in seen[0] and 'read-only source data' in seen[0]
    monkeypatch.setattr(everything, 'PRESERVE_ORIGINAL_MAX_EDIT_TOKENS', 100)
    result = everything.edit_preserve_original_blocks('Update.', {}, supporting_evidence='evidence ' * 1000)
    assert result['error'] and len(seen) == 1


def test_cancellation_during_provider_call_does_not_publish_a_result(workflow, monkeypatch):
    def cancelled(prompt, schema, **kwargs):
        service.stop(workflow, cancelled=True)
        return provider(prompt, schema, **kwargs)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', cancelled)
    item = tasks.work_rows(workflow).one()
    assert tasks.process_section.run(item.id)['stopped']
    assert workflow.status == 'cancelled' and item.state == 'cancelled'
    assert DocumentSummarySegment.query.filter_by(kind='query').count() == 0


def test_old_worker_cannot_fail_a_retried_request(workflow, monkeypatch):
    def superseded(prompt, schema, **kwargs):
        service.stop(workflow, 'Worker was replaced')
        service.retry(workflow)
        tasks.dispatch_group(workflow)
        return provider(prompt, schema, **kwargs)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', superseded)
    item = tasks.work_rows(workflow).one()
    assert tasks.process_section.run(item.id)['stopped']
    assert workflow.status == 'running' and item.state == 'queued'
    assert item.result_id is None


def test_retry_cannot_take_chat_away_from_a_newer_request(workflow):
    service.stop(workflow, 'Failed section')
    newer = service.submit(workflow.user_id, json.loads(workflow.data_json))
    with pytest.raises(ValueError, match='Another request'):
        service.retry(workflow)
    db.session.rollback()
    assert newer.active_draft_id == workflow.draft_id and newer.status == 'running'
    assert workflow.status == 'failed'


@pytest.mark.parametrize('task_name,field', [('plan_prompt', 'planning_task_id'), ('answer_prompt', 'answer_task_id'),
                                          ('after_extraction', 'answer_task_id')])
def test_replaced_celery_task_cannot_claim_current_attempt(workflow, task_name, field):
    workflow.group_id = 'same-group'
    setattr(workflow, field, 'replacement-task')
    db.session.commit()
    task = getattr(tasks, task_name)
    task.push_request(id='old-task')
    try:
        args = [workflow.id] if task_name == 'plan_prompt' else [workflow.id, workflow.group_id]
        assert task.run(*args)['stale']
    finally:
        task.pop_request()
    assert workflow.status == 'running' and workflow.lease_token is None


def test_changed_document_is_rechecked_at_chat_publication(workflow):
    from backend.models import DocumentPreparation
    from backend import everything
    from backend.util.db import DBMemory
    token = tasks.claim_workflow(workflow.id)
    item = tasks.work_rows(workflow).one()
    db.session.get(DocumentPreparation, item.document_id).generation += 1
    db.session.commit()
    with service.execution(workflow.id, token), pytest.raises(prep.PreparationError):
        everything.save_state_to_memory({'draft': {}, 'chat_history': []}, DBMemory(db=db, doc_id=1))
    assert TextEntry.query.filter_by(draftId=1, textName='state').first() is None


def test_provider_calls_do_not_hold_sql_transactions(workflow, monkeypatch):
    def checked(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        return provider(prompt, schema, **kwargs)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', checked)
    item = tasks.work_rows(workflow).one()
    tasks.process_section.run(item.id)
    assert item.state == 'ready'


@pytest.mark.parametrize('existing_work', [False, True])
def test_progress_stays_consistent_when_a_worker_adds_a_document(workflow, app, monkeypatch, existing_work):
    from sqlalchemy.orm import Query
    first = tasks.work_rows(workflow).one()
    first.material_json = service.encode({'start': 0, 'end': 20})
    first_id, workflow_id = first.document_id, workflow.id
    if not existing_work:
        tasks.work_rows(workflow).delete()
    db.session.commit()
    late = receive(admit('late', key='late-document'))
    assert run(late)['ok']
    original_all, inserted = Query.all, []
    def all_with_worker_write(query):
        rows = original_all(query)
        if query.column_descriptions[0].get('entity') is Work and not inserted:
            # A worker commits a new document after progress fetched its work.
            # Use another connection without changing the polling transaction.
            inserted.append(str(uuid.uuid4()))
            with db.engine.begin() as connection:
                connection.execute(Work.__table__.insert().values(
                    id=inserted[0], workflow_run_id=workflow_id, work_key='progress-new-document',
                    kind='query', document_id=late['document_id'],
                    material_json=service.encode({'start': 0, 'end': 10}),
                    state='queued', attempts=0, updated_at=service.now()))
        return rows
    monkeypatch.setattr(Query, 'all', all_with_worker_write)
    path = f'/api/everything/workflows/{workflow_id}'
    client = app.test_client()
    response = client.get(path)
    assert inserted and response.status_code == 200
    info = response.json['info']
    assert info['counts'] == ({'query:queued': 1} if existing_work else {})
    files = {item['file_id']: item for item in info['files']}
    assert set(files) == ({first_id} if existing_work else set())
    if existing_work:
        assert files[first_id]['total'] == 1 and files[first_id]['completed'] == 0
        assert files[first_id]['required_characters'] == 20 and files[first_id]['completed_characters'] == 0
    response = client.get(path)
    assert response.status_code == 200
    info = response.json['info']
    assert info['counts'] == {'query:queued': 1 + existing_work}
    files = {item['file_id']: item for item in info['files']}
    assert set(files) == ({first_id, late['document_id']} if existing_work else {late['document_id']})
    assert files[late['document_id']]['total'] == 1 and files[late['document_id']]['completed'] == 0
    assert files[late['document_id']]['required_characters'] == 10
    assert files[late['document_id']]['completed_characters'] == 0


def test_progress_counts_source_union_without_double_counting_overlap(workflow, monkeypatch):
    first = tasks.work_rows(workflow).one()
    first.material_json = service.encode({'start': 0, 'end': 20})
    complete(workflow, monkeypatch)
    second = tasks.add_work(workflow, 'query', {'start': 10, 'end': 30}, first.document_id, first.artifact_id)
    db.session.commit()
    progress = service.status(workflow)['info']['files'][0]
    assert progress['required_characters'] == 30 and progress['completed_characters'] == 20
    tasks.process_section.run(second.id)
    parent = tasks.add_work(workflow, 'query', {'start': 0, 'end': 100}, first.document_id, first.artifact_id)
    parent.state = 'split'
    db.session.commit()
    progress = service.status(workflow)['info']['files'][0]
    assert progress['total'] == progress['completed'] == 2
    assert progress['completed_characters'] == progress['required_characters'] == 30


def test_lost_answer_dispatch_fails_until_explicit_retry(workflow, monkeypatch):
    complete(workflow, monkeypatch)
    result_id = tasks.work_rows(workflow).one().result_id
    assert tasks.prepare_answer(workflow)
    monkeypatch.setattr(tasks.answer_prompt, 'apply_async', lambda *a, **kw: (_ for _ in ()).throw(ConnectionError('broker unavailable')))
    tasks.dispatch_answer(workflow)
    assert not workflow.dispatch_pending and workflow.status == 'failed'
    scheduled = []
    monkeypatch.setattr(tasks.answer_prompt, 'apply_async', lambda *a, **kw: scheduled.append(kw))
    tasks.reconcile_case_assistant_workflows.run()
    assert scheduled == []
    service.retry(workflow)
    assert tasks.prepare_answer(workflow)
    tasks.dispatch_answer(workflow)
    assert len(scheduled) == 1 and not workflow.dispatch_pending
    assert tasks.work_rows(workflow).one().result_id == result_id


def test_primary_draft_stays_original_and_supporting_passages_reach_both_edit_steps(workflow, monkeypatch):
    from backend import everything
    complete(workflow, monkeypatch)
    item = tasks.work_rows(workflow).one()
    payload = json.loads(workflow.payload_json)
    payload.update(draft_action='revise_uploaded_draft', revision_mode='preserve_original')
    payload['file_selection']['primary_draft_file_id'] = 'primary-draft'
    workflow.payload_json = service.encode(payload)
    db.session.commit()
    assert tasks.prepare_answer(workflow)
    evidence = json.loads(workflow.payload_json)['file_extractions_str']
    assert item.document_id in evidence and item.artifact_id in evidence
    original = 'Original deadline.\n\nUntouched paragraph.'
    monkeypatch.setattr(everything, 'load_primary_uploaded_original_text', lambda *a, **kw:
        {'content': original, 'filename': 'complaint.txt'})
    calls = []
    def edit(prompt, schema, **kw):
        calls.append(prompt)
        assert 'A contract about the middle event.' in prompt
        assert item.document_id in prompt and item.artifact_id in prompt
        if schema is everything.PreserveOriginalTargetResult:
            return schema(edit_scope='targeted_blocks', anchors=['p_0001'], confidence='high', reason='Deadline', needs_confirmation=False)
        return schema(operations=[{'op': 'edit_preserved_block', 'anchor': 'p_0001',
                                  'replacement_text': 'Updated deadline.', 'change_summary': 'Updated from supporting evidence.', 'citation_changes': []},
                                 {'op': 'edit_preserved_block', 'anchor': 'p_0002',
                                  'replacement_text': 'Forbidden change to context.', 'change_summary': '', 'citation_changes': []}],
                      response='Updated from the source.')
    monkeypatch.setattr(everything, 'chat_completion_parse', edit)
    monkeypatch.setattr(everything, 'structure_draft_text', lambda text, **kw:
        {'title': 'Complaint', 'sections': [{'section_id': '1', 'section_name': 'Text', 'section_content': text}]})
    result = everything.run_preserve_original_uploaded_revision(user_message='Update the deadline only.',
        primary_draft_file_id='primary-draft', doc_id=workflow.draft_id, project_id=1, supporting_evidence=evidence)
    assert len(calls) == 2 and result['draft']['sections'][0]['section_content'] == 'Updated deadline.\n\nUntouched paragraph.'
    assert {'anchor': 'p_0002', 'reason': 'read_only_anchor'} in result['operations'][1]['edits_rejected']


def test_preserve_budget_includes_framing_schema_and_supporting_evidence(monkeypatch):
    from backend import everything
    monkeypatch.setattr(everything, 'PRESERVE_ORIGINAL_MAX_EDIT_TOKENS', 18000)
    monkeypatch.setattr(everything, 'chat_completion_parse', lambda *a, **kw: pytest.fail('Oversized request reached provider'))
    with pytest.raises(everything.ContextBudgetExceeded):
        everything.edit_preserve_original_blocks('Update.', {}, supporting_evidence='source ' * 1000)


def test_one_hundred_attachments_produce_saved_independent_section_results(workflow, monkeypatch):
    # Admission/preparation limits are covered separately; exercise the new prompt path at its file limit.
    ids = [tasks.work_rows(workflow).one().document_id]
    for index in range(99):
        entry = receive(admit(str(index), key='scale-' + str(index)))
        assert run(entry)['ok']
        ids.append(entry['document_id'])
    snapshot = prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': uid} for uid in ids]})
    data = json.loads(workflow.data_json)
    data['document_snapshot'] = snapshot
    payload = json.loads(workflow.payload_json)
    payload['document_snapshot'] = snapshot
    selected = [{'file_id': uid, 'role': 'supporting_context', 'method': 'relevant'} for uid in ids]
    payload['intent_decision']['selected_files'] = payload['file_selection']['selected_files'] = selected
    payload.pop('_planned_file_ids', None)
    tasks.work_rows(workflow).delete()
    workflow.data_json, workflow.payload_json = service.encode(data), service.encode(payload)
    db.session.commit()
    tasks.plan_sections(workflow, payload)
    assert tasks.work_rows(workflow, 'query').count() == 100
    complete(workflow, monkeypatch)
    progress = service.status(workflow)
    assert progress['info']['counts']['query:ready'] == 100
    assert len(progress['info']['files']) == 100
    assert all(f['completed_characters'] == f['required_characters'] for f in progress['info']['files'])


def test_polling_and_saved_evidence_do_not_load_original_document_text(workflow, app, monkeypatch):
    from sqlalchemy import event
    complete(workflow, monkeypatch)
    statements = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement.lower())
    event.listen(db.engine, 'before_cursor_execute', capture)
    try:
        assert service.status(workflow)['info']['files'][0]['filename']
        assert tasks.prepare_answer(workflow)
        response = app.test_client().get(f'/api/everything/workflows/{workflow.id}/evidence')
        assert response.status_code == 200
    finally:
        event.remove(db.engine, 'before_cursor_execute', capture)
    assert statements and not any('uploaded_files.content' in sql for sql in statements)


def test_single_large_evidence_item_can_be_split_without_losing_any_quotes(workflow):
    original = tasks.work_rows(workflow).one()
    material = {'round': 1, 'items': [{'file_id': original.document_id, 'artifact_id': original.artifact_id,
        'filename': 'record.txt', 'excerpts': [{'block_id': 'b1', 'quote': str(i)} for i in range(8)]}]}
    item = tasks.add_work(workflow, 'reduce', material, original.document_id)
    db.session.commit()
    tasks.split_section(item, workflow)
    children = tasks.work_rows(workflow, 'reduce').filter_by(state='queued').all()
    assert len(children) == 2
    quotes = [q['quote'] for child in children for q in json.loads(child.material_json)['items'][0]['excerpts']]
    assert sorted(quotes, key=int) == [str(i) for i in range(8)]


def test_unicode_source_subdivision_preserves_full_readable_coverage(monkeypatch):
    manifest = parser.manifest_from_blocks([{'text': ('§ 數額 $123.45 — obligation.\n\n' * 500)}])
    original_size = tasks.request_size
    def narrow(prompt, schema):
        material = json.loads(prompt.split('Source blocks:\n')[1])
        return 40001 if sum(len(b['text']) for b in material) > 1000 else original_size(prompt, schema)
    monkeypatch.setattr(tasks, 'request_size', narrow)
    spans = list(tasks.bounded_spans(manifest, [{'start': 0, 'end': len(manifest['text'])}], 'Find amounts and obligations.'))
    end = 0
    for span in sorted(spans, key=lambda s: s['start']):
        assert span['start'] <= end and span['end'] > span['start']
        end = max(end, span['end'])
    assert end == len(manifest['text']) and len(spans) > 1


def test_case_assistant_workers_stay_inside_isolated_test_queues(app):
    from flask import current_app
    current_app.config['E2E_CELERY_QUEUE'] = current_app.config['CELERY']['task_default_queue']
    get_queue = tasks.queue_name
    assert get_queue().startswith(current_app.config['E2E_CELERY_QUEUE'] + '.')
    current_app.config['CASE_ASSISTANT_EXTRACTION_QUEUE'] = 'production'
    with pytest.raises(ValueError, match='isolated'):
        get_queue()


def test_confirmation_can_be_cancelled_after_a_document_changes(workflow, app):
    from backend.models import DocumentPreparation
    workflow.status, workflow.phase = 'awaiting_user_confirmation', 'confirm'
    item = tasks.work_rows(workflow).one()
    db.session.get(DocumentPreparation, item.document_id).generation += 1
    db.session.commit()
    response = app.test_client().post(f'/api/everything/workflows/{workflow.id}/confirm', json={'decision': {'cancelled': True}})
    assert response.status_code == 200 and response.json['cancelled']
    assert workflow.status == 'cancelled' and workflow.active_draft_id is None


def test_model_reported_incomplete_section_is_not_saved_as_complete(workflow, monkeypatch):
    from backend.ai.token_budget import IncompleteStructuredOutput
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda prompt, schema, **kw: schema(excerpts=[], complete=False))
    item = tasks.work_rows(workflow).one()
    with pytest.raises(IncompleteStructuredOutput):
        tasks.extraction_result(workflow, item, json.loads(workflow.payload_json))
    assert DocumentSummarySegment.query.filter_by(kind='query').count() == 0


@pytest.mark.skipif(__import__('os').environ.get('LEXINTEL_RUN_CELERY_INTEGRATION') != '1', reason='Opt-in isolated Redis/Celery integration')
@pytest.mark.parametrize('transport', ['memory', 'redis'])
def test_native_redis_chord_completes_sql_workflow(workflow, app, monkeypatch, tmp_path, transport):
    """Own Redis instance, real threaded worker, actual chord; no provider traffic."""
    import shutil
    import subprocess
    import tempfile
    import time
    import celery
    import redis
    from celery.contrib.testing.worker import start_worker
    from backend import everything
    from backend.util import workspace_store
    temporary = process = broker = None
    socket_path = ''
    if transport == 'redis':
        binary = shutil.which('redis-server')
        if not binary:
            pytest.skip('redis-server is unavailable')
        temporary = tempfile.TemporaryDirectory(prefix='case-assistant-redis-', dir='/tmp')
        socket_path = temporary.name + '/redis.sock'
        process = subprocess.Popen([binary, '--port', '0', '--unixsocket', socket_path, '--save', '',
            '--appendonly', 'no', '--dir', temporary.name], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        broker = redis.Redis(unix_socket_path=socket_path)
    old_app = celery.current_app._get_current_object()
    test_celery = None
    try:
        for _ in range(100) if broker else []:
            try:
                broker.ping()
                break
            except redis.ConnectionError:
                if process.poll() is not None:
                    output = process.communicate(timeout=1)[0].decode(errors='replace')
                    if 'Operation not permitted' in output or 'Permission denied' in output:
                        pytest.skip('Sandbox prevents starting an isolated Redis socket')
                    pytest.fail('Isolated Redis did not start: ' + output[-500:])
                time.sleep(.05)
        class FlaskTask(celery.Task):
            def __call__(self, *args, **kwargs):
                with app.app_context():
                    return self.run(*args, **kwargs)
        test_celery = celery.Celery('case-assistant-integration', task_cls=FlaskTask,
            broker=f'redis+socket://{socket_path}?virtual_host=0' if broker else 'memory://',
            backend=f'redis+socket://{socket_path}?virtual_host=1' if broker else 'cache+memory://')
        test_celery.conf.update(task_default_queue='case-assistant-integration', worker_prefetch_multiplier=1)
        test_celery.set_default()
        test_celery.set_current()
        monkeypatch.setattr(tasks, 'chord', celery.chord)
        monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
        def answer(task, payload, draft_id):
            state = everything.load_state_from_memory(everything.DBMemory(db=db, doc_id=draft_id))
            state['active_workflow'] = {'workflow_run_id': workflow.id, 'status': 'completed'}
            everything.add_chat_message(state, (payload['user_message'], 'A source-backed answer.'), meta=payload['prompt_turn_meta'])
            everything.save_state_to_memory(state, everything.DBMemory(db=db, doc_id=draft_id))
            return {'response': 'A source-backed answer.', 'text': state['draft']}
        monkeypatch.setattr(everything, '_everything_phase2_body', answer)
        monkeypatch.setattr(everything, '_record_successful_chat_answer', lambda *a: None)
        with start_worker(test_celery, pool='threads', concurrency=2, perform_ping_check=False,
                          queues=['case-assistant-integration', tasks.queue_name()], shutdown_timeout=15):
            tasks.dispatch_group(workflow)
            timeout = time.monotonic()+30
            while time.monotonic() < timeout:
                db.session.rollback()
                current = db.session.get(Workflow, workflow.id)
                if current.status != 'running':
                    break
                time.sleep(.1)
            assert current.status == 'completed', current.error_message
            assert json.loads(current.result_json)['response'] == 'A source-backed answer.'
            assert tasks.work_rows(current).filter_by(state='ready').count() == 1
            state = workspace_store.load(current.draft_id)
            assert len(state['chat_history']) == 1
            assert state['chat_history_meta'][0]['workflow_completed']
    finally:
        if test_celery:
            test_celery.close()
        old_app.set_default()
        old_app.set_current()
        if process:
            process.terminate()
            process.wait(timeout=10)
        if temporary:
            temporary.cleanup()
