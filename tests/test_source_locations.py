"""Source locations remain tied to exact prepared text through drafting handoffs."""
import copy
import json
import re

import pytest

from backend import agent_tasks as tasks
from backend.ai import agent_tools, base
from backend.database import db
from backend.models import AgentArtifact
from backend.util import document_drafts as drafts, source_context
from backend.util.attachment_parser import manifest_from_blocks
from tests.test_agent_controller import agent, call, last_result, operation, tick
from tests.test_document_preparation import app


def pdf_manifest():
    return manifest_from_blocks([
        {'text': 'Cover text. Printed page 10.\nKeep  these spaces.\n', 'page': 1},
        {'text': 'First page continues.', 'page': 1},
        {'text': 'Second PDF page. Printed page 11.\n', 'page': 2},
    ])


@pytest.mark.parametrize('selection,expected_pages', [
    ('whole', [1, 2]), ('middle', [1]), ('cross_page', [1, 2]), ('separate', [1, 1]),
])
def test_pdf_headers_follow_selected_pages_without_changing_source_text(selection, expected_pages):
    manifest = pdf_manifest()
    original = copy.deepcopy(manifest)
    text, blocks = manifest['text'], manifest['blocks']
    spans = {
        'whole': [{'start': 0, 'end': len(text)}],
        'middle': [{'start': 3, 'end': 19}],
        'cross_page': [{'start': blocks[1]['start'] + 4, 'end': blocks[2]['start'] + 12}],
        'separate': [{'start': 2, 'end': 12}, {'start': 20, 'end': 31}],
    }[selection]
    doc = {'filename': 'original.pdf', 'manifest': manifest}
    rendered = source_context.format_document(doc, spans, 'D1')
    assert [int(n) for n in re.findall(r'^PDF PAGE: (\d+)$', rendered, re.M)] == expected_pages
    assert rendered.count('FILE: original.pdf') == len(expected_pages)
    assert 'FILE: D1' not in rendered
    body = rendered.removeprefix(source_context.document_heading(doc, 'D1'))
    body = re.sub(r'\n--- SOURCE EXCERPT ---\nFILE: original\.pdf\nPDF PAGE: \d+\n\n', '', body)
    body = re.sub(r'\[Selected source range \d+:\d+\]\n', '', body)
    assert body == ''.join(text[s['start']:s['end']] + '\n' for s in spans)
    assert manifest == original


def test_extraction_warnings_and_non_pdf_locations_remain_available():
    manifest = pdf_manifest()
    manifest['extraction'] = {'status': 'partial', 'gaps': [
        {'code': 'ocr_unreadable', 'page': 3, 'message': 'Unreadable page'}]}
    rendered = source_context.format_document({'filename': 'partial.pdf', 'manifest': manifest},
        [{'start': 0, 'end': len(manifest['text'])}], 'D1')
    assert 'Original extraction: partial' in rendered and 'Unreadable page' in rendered
    assert 'PDF PAGE: 3' not in rendered
    for item, expected in [
        ({'text': 'Paragraph text.', 'paragraph': 7}, '[D1 paragraph 7]'),
        ({'text': '0001,25', 'sheet': 'Payments', 'range': 'A4:B4'}, '[D1 sheet Payments!A4:B4]'),
        ({'text': 'No page is known.'}, '[D1 block b000001]'),
    ]:
        manifest = manifest_from_blocks([item])
        rendered = source_context.format_document({'filename': 'source', 'manifest': manifest},
            [{'start': 0, 'end': len(manifest['text'])}], 'D1')
        assert expected in rendered and 'PDF PAGE:' not in rendered


@pytest.mark.parametrize('legacy_label', [False, True])
def test_page_locations_survive_reload_and_reach_creation_revision_and_controller(agent, monkeypatch, legacy_label):
    snapshot = json.loads(agent.data_json)['document_snapshot']
    uid = snapshot['files'][0]['file_id']
    doc = source_context.document(snapshot, uid)
    manifest = pdf_manifest()
    with monkeypatch.context() as scoped:
        scoped.setattr(source_context, 'document', lambda *a, **kw: {
            **doc, 'filename': 'original.pdf', 'manifest': manifest})
        operation(agent, monkeypatch, 'read_sources', {'artifact_id': None,
            'ranges': [{'file_id': uid, 'start': 0, 'end': len(manifest['text'])}],
            'start': 0, 'end': None})
    result = last_result(agent)
    source_id = result['artifact_id']
    saved = db.session.get(AgentArtifact, source_id)
    if legacy_label:
        content = json.loads(saved.content_json)
        content['text'] = '[original.pdf page 1]\nCover text. Printed page 10.'
        saved.content_json = json.dumps(content)
        db.session.commit()
    original = saved.content_json
    db.session.expire_all()
    source_text = json.loads(db.session.get(AgentArtifact, source_id).content_json)['text']
    marker = '[original.pdf page 1]' if legacy_label else 'FILE: original.pdf\nPDF PAGE: 1'
    assert marker in source_text
    calls = []

    def writer(prompt, schema, **options):
        payload = json.loads(prompt)
        assert payload['materials'][0]['text'] == source_text
        assert drafts.SOURCE_LOCATION_GUIDANCE in options['sys_prompt']
        calls.append(schema)
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Brief', markdown='Supported summary (original.pdf, PDF p. 1).',
                update_court_format=False)
        assert schema is drafts.MarkdownRevisionResult
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text': 'Supported summary', 'new_text': 'Revised supported summary'}],
            update_court_format=False)

    monkeypatch.setattr(base, 'chat_completion_parse', writer)
    operation(agent, monkeypatch, 'create_draft', {'instructions': 'Draft a summary.', 'material_ids': [source_id]})
    assert not last_result(agent).get('error')
    operation(agent, monkeypatch, 'revise_draft', {'target': 'working_draft',
        'instructions': 'Revise the opening.', 'material_ids': [source_id], 'revision_mode': 'general_revision'})
    assert not last_result(agent).get('error')
    assert calls == [drafts.MarkdownDraftResult, drafts.MarkdownRevisionResult]
    operation(agent, monkeypatch, 'read_sources', {'artifact_id': source_id,
        'ranges': [], 'start': 0, 'end': None})
    assert json.loads(last_result(agent)['text'])['text'] == source_text

    def controller(request):
        system = next(item['content'] for item in request['input'] if item.get('role') == 'system')
        assert drafts.SOURCE_LOCATION_GUIDANCE in system
        assert 'Before publishing, compare findings' in system
        outputs = [json.loads(item['output']) for item in request['input']
                   if item.get('type') == 'function_call_output']
        assert json.loads(outputs[-1]['text'])['text'] == source_text
        return call('complete_task', {'response': 'Summary saved.', 'candidate_id': None})

    monkeypatch.setattr(base, 'responses_completion', controller)
    assert tick(agent) == {'continued': True}
    assert db.session.get(AgentArtifact, source_id).content_json == original
    description = next(t['description'] for t in agent_tools.definitions() if t['name'] == 'read_sources')
    assert '1-based page position' in description and 'printed page numbers' in description
