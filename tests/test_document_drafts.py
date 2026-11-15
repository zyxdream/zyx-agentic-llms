"""Markdown authoring retains source and immutable revisions through agent steps."""
import copy
import json

import pytest

from backend import agent_tasks as tasks
from backend.workspace import state as everything
from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact
from backend.util import document_drafts as documents, draft_structure
from backend.util import workspace_service as service
from tests.test_agent_controller import agent, call, last_result, operation, state, tick
from tests.test_document_preparation import app


SOURCE = '# Reconciliation\n\n| Invoice | Balance |\n| --- | ---: |\n| 000001 | 25.50 |\n\n```javascript\nconst amount = 25.50;\nconsole.log(amount);\n```\n'


def written(markdown=SOURCE, *, title='Reconciliation', update=False):
    return documents.MarkdownDraftResult(title=title, markdown=markdown, update_court_format=update)


def test_markdown_source_survives_legacy_storage_adapters_without_normalization(monkeypatch):
    draft = {'format_version': 2, 'source_format': 'markdown', 'title': 'Table',
             'markdown': SOURCE, 'revision_id': 'saved', 'parent_revision_id': None,
             'export_settings': {'court': {'layout': {'font_size_pt': 12}}}}
    monkeypatch.setattr(draft_structure, 'structure_section_content', lambda *a, **kw: pytest.fail('Do not rebuild Markdown into paragraphs'))
    for adapter in (draft_structure.sanitize_draft_structure, draft_structure.normalize_draft_structure):
        saved = adapter(draft)
        assert saved == draft and saved is not draft
        assert saved['markdown'].encode() == SOURCE.encode()
    assert not draft_structure.draft_needs_normalization(draft)
    assert documents.draft_to_markdown(draft) == SOURCE
    assert documents.draft_text(draft) == SOURCE


def test_markdown_writer_uses_complete_assignment_context_and_target(monkeypatch):
    captured = []
    def provider(prompt, schema, **kwargs):
        captured.append((json.loads(prompt), schema, kwargs))
        return documents.MarkdownRevisionResult(mode='rewrite', title='Reconciliation', markdown=SOURCE,
            replacements=[], update_court_format=False)
    monkeypatch.setattr(base, 'chat_completion_parse', provider)
    result = documents.write_document(assignment='Reconcile all invoices.', instructions='Preserve leading zeros.',
        conversation={'text': 'Earlier discussion'}, materials=[{'kind': 'source', 'text': 'Complete source rows'}],
        target={'markdown': 'Old text'}, answers=[{'answer': 'Use all rows.'}], model='selected-model', reasoning_effort='high')
    assert result.markdown == SOURCE
    payload, schema, options = captured[0]
    assert payload['assignment'] == 'Reconcile all invoices.'
    assert payload['target_document']['markdown'] == 'Old text'
    assert payload['materials'][0]['text'] == 'Complete source rows'
    assert payload['user_clarifications'][0]['answer'] == 'Use all rows.'
    assert options['model'] == 'selected-model' and options['reasoning_effort'] == 'high'
    assert schema is documents.MarkdownRevisionResult and options['raise_on_incomplete']


def test_create_revise_and_publish_keep_immutable_markdown_and_court_settings(agent, monkeypatch):
    before = copy.deepcopy(state(agent))
    requests, formats = [], []
    def writer(**kwargs):
        requests.append(kwargs)
        return written(SOURCE + ('\nRevised.' if len(requests) > 1 else ''), update=len(requests) == 3)
    def resolve(draft, **kwargs):
        formats.append((copy.deepcopy(draft), kwargs))
        return {'structural': {'require_caption_page': False}, 'layout': {'font_size_pt': 10 + len(formats)}}
    monkeypatch.setattr(documents, 'write_document', writer)
    monkeypatch.setattr(documents, 'resolve_court_settings', resolve)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Write a reconciliation report with a table and code example.', 'material_ids': []})
    first = last_result(agent)['artifact_id']
    original = db.session.get(AgentArtifact, first).content_json
    draft = json.loads(original)['draft']
    assert draft['markdown'] == SOURCE and draft['revision_id'] == first
    assert draft['parent_revision_id'] is None and len(formats) == 1
    assert requests[0]['assignment'] == json.loads(agent.data_json)['query']
    assert state(agent) == before

    operation(agent, monkeypatch, 'revise_draft', {'target': first, 'instructions': 'Add one sentence.',
        'material_ids': [], 'revision_mode': 'general_revision'})
    second = last_result(agent)['artifact_id']
    second_draft = json.loads(db.session.get(AgentArtifact, second).content_json)['draft']
    assert second != first and second_draft['parent_revision_id'] == first
    assert second_draft['export_settings'] == draft['export_settings'] and len(formats) == 1
    assert db.session.get(AgentArtifact, first).content_json == original
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': second, 'ranges': [], 'start': 0, 'end': None})
    assert json.loads(last_result(agent)['text'])['draft']['markdown'] == second_draft['markdown']

    operation(agent, monkeypatch, 'revise_draft', {'target': second, 'instructions': 'Change the court page layout.',
        'material_ids': [], 'revision_mode': 'general_revision'})
    third = last_result(agent)['artifact_id']
    third_draft = json.loads(db.session.get(AgentArtifact, third).content_json)['draft']
    assert third_draft['parent_revision_id'] == second and len(formats) == 2
    assert third_draft['export_settings'] != second_draft['export_settings']
    operation(agent, monkeypatch, 'complete_task', {'response': 'The report is ready.', 'candidate_id': third})
    assert state(agent)['draft'] == third_draft
    assert state(agent)['draft_before'] == before['draft']
    assert db.session.get(AgentArtifact, first).content_json == original
    assert len(state(agent)['chat_history']) == 1


def test_revise_legacy_editor_adapts_to_markdown_without_rewriting_old_draft(agent, monkeypatch):
    original = copy.deepcopy(state(agent)['draft'])
    targets = []
    monkeypatch.setattr(documents, 'write_document', lambda **kw: targets.append(kw['target']) or written())
    operation(agent, monkeypatch, 'revise_draft', {'target': 'current_draft', 'instructions': 'Add a table.',
        'material_ids': [], 'revision_mode': 'general_revision'})
    saved = json.loads(db.session.get(AgentArtifact, last_result(agent)['artifact_id']).content_json)
    assert saved['draft']['format_version'] == 2 and saved['draft']['parent_revision_id'] is None
    assert targets[0]['markdown'] == '# Original\n\n## Facts\n\nOriginal facts.'
    assert state(agent)['draft'] == original


def test_published_markdown_is_discoverable_and_readable_in_next_assignment(agent, monkeypatch):
    monkeypatch.setattr(documents, 'write_document', lambda **kw: written())
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Make a report.', 'material_ids': []})
    revision = last_result(agent)['artifact_id']
    operation(agent, monkeypatch, 'complete_task', {'response': 'Ready.', 'candidate_id': revision})
    next_run = service.submit(agent.user_id, json.loads(agent.data_json))
    tasks.dispatch(next_run)
    requests = []
    monkeypatch.setattr(base, 'responses_completion', lambda request: requests.append(request) or call('read_sources',
        {'artifact_id': 'current_draft', 'ranges': [], 'start': 0, 'end': None}))
    tick(next_run)
    summary = json.loads(requests[0]['input'][1]['content'])['current_draft']
    assert summary['source_format'] == 'markdown' and summary['revision_id'] == revision
    tick(next_run)
    assert json.loads(last_result(next_run)['text'])['draft']['markdown'] == SOURCE


def test_markdown_verification_and_authority_lookup_receive_complete_document(agent, monkeypatch):
    monkeypatch.setattr(documents, 'write_document', lambda **kw: written())
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Make a report.', 'material_ids': []})
    revision = last_result(agent)['artifact_id']
    prompts, audited = [], []
    monkeypatch.setattr(base, 'chat_completion', lambda prompt, **kw: prompts.append(prompt) or 'The table retains the requested identifiers.')
    from backend.ai import agent_tools
    def lookup(ops, args):
        assert args.targets[0].draft_id == revision
        audited.append(ops.target(args.targets[0].draft_id)['markdown'])
        uid = ops.save('authority_research', {'kind': 'authority_research', 'purpose': args.purpose,
            'targets': [], 'authorities': [], 'sources': [], 'references': [], 'coverage': {}})
        return {'research_id': uid, 'artifact_id': uid, 'error': False}
    monkeypatch.setattr(agent_tools.Operations, 'lookup_authorities', lookup)
    operation(agent, monkeypatch, 'verify_draft', {'target': revision, 'instructions': 'Check all cells and code.',
        'material_ids': [], 'check_citations': True})
    assert audited == [SOURCE]
    assert '000001' in prompts[0] and 'console.log' in prompts[0]
    report = json.loads(db.session.get(AgentArtifact, last_result(agent)['artifact_id']).content_json)
    assert report['target'] == revision and not report['error']


def test_cancelled_document_writer_cannot_save_a_candidate(agent, monkeypatch):
    def cancelled(**kwargs):
        service.stop(agent, cancelled=True)
        return written()
    monkeypatch.setattr(documents, 'write_document', cancelled)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Make a report.', 'material_ids': []})
    assert agent.status == 'cancelled'
    assert AgentArtifact.query.filter_by(run_id=agent.id, kind='draft').count() == 0
    assert state(agent)['draft']['title'] == 'Original'
