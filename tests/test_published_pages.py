"""Explicit published pagination, with no inference from offsets or PDF pages."""
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.ai import base
from backend.database import db
from backend.models import AgentArtifact
from backend.domains.legal import authority_research as research, authority_sources as sources, published_pages as pages
from backend.util import document_drafts as drafts
from tests.test_authority_research import ops, lookup_args, read_args, case_result, record
from tests.test_agent_controller import agent, operation, last_result, call, tick
from tests.test_document_preparation import app

CITATION = '365 B.R. 401'
QUOTE = 'flexible approach and no one factor is determinative.'
FIXTURE = Path(__file__).parent / 'fixtures/authority-pinpoints/calpine-lawbox.html'


def calpine():
    html = FIXTURE.read_text()
    assert hashlib.sha256(html.encode()).hexdigest() == '02451aee517393806eb82e2f576a8bdc46879a24afffeb59797b01c269964b00'
    text, ranges = pages.extract(html, [CITATION, '2007 WL 950090'], field='html_lawbox')
    return {'status': 'read', 'text': text, 'published_page_ranges': ranges,
            'url': 'https://www.courtlistener.com/opinion/1833489/',
            'retrieval_url': sources.API_BASE + 'opinions/1833489/',
            'opinion_id': 1833489, 'sha256': hashlib.sha256(html.encode()).hexdigest(),
            'blocks': [{'start': 0, 'end': len(text), 'location': 'CourtListener opinion 1833489'}]}


def test_calpine_boundary_outside_excerpt_and_exact_original_text():
    fetched = calpine()
    text = fetched['text']
    assert text == sources._html_text(FIXTURE.read_text())[1]
    start = text.index(QUOTE)
    result = pages.for_passage(fetched, start - 1000, start + 11000, start, start + len(QUOTE))
    assert result['matched_published_pages'] == ['409']
    evidence = next(p for p in result['page_evidence'] if p['page'] == '409')
    assert evidence['boundary_start'] < start - 1000
    assert evidence['boundary_text'] == '*409'
    assert evidence['next_boundary']['boundary_text'] == '*410'
    assert evidence['source_url'] == fetched['retrieval_url']
    assert evidence['citation'] == CITATION
    notes = text.index('NOTES')
    assert not pages.for_passage(fetched, notes, len(text), notes + 10, notes + 20)['matched_published_pages']


@pytest.mark.parametrize('html,citations', [
    ('<p>*409 arbitrary text</p>', [CITATION]),
    ('<span class="star-pagination">*409</span>text', [CITATION, '1 F.3d 2']),
    ('<span class="star-pagination" label="410">*409</span>text', [CITATION]),
    ('<span class="star-pagination" citation-index="1">*409</span>text'
     '<span class="star-pagination" citation-index="2">*12</span>text', [CITATION]),
    ('<span class="star-pagination">*409</span>text<span class="star-pagination">*408</span>text', [CITATION]),
    ('<span class="star-pagination">*409</span>text<span class="star-pagination">*411</span>text', [CITATION]),
])
def test_unsupported_or_conflicting_boundaries_remain_unverified(html, citations):
    text, ranges = pages.extract(html, citations, field='html_with_citations')
    assert text and not ranges
    assert pages.for_passage({'text': text}, 0, len(text), 0, 1) == {
        'matched_published_pages': None, 'page_evidence': []}


def test_footnote_scope_and_cross_page_span():
    html = ('<span class="star-pagination" label="409">*409</span>main wording'
        '<span class="star-pagination" label="410">*410</span>next wording'
        '<div class="footnotes"><div class="footnote" id="fn1">unlocated note'
        '<span class="star-pagination" label="408">*408</span>located note</div></div>')
    text, ranges = pages.extract(html, [CITATION], field='html_with_citations')
    fetched = {'published_page_ranges': ranges}
    cross = pages.for_passage(fetched, 0, len(text), text.index('main wording'), text.index('next wording') + 4)
    assert cross['matched_published_pages'] == ['409', '410']
    lo = text.index('unlocated note')
    assert pages.for_passage(fetched, 0, len(text), lo, lo+5)['matched_published_pages'] is None
    lo = text.index('located note', text.index('*408'))
    assert pages.for_passage(fetched, 0, len(text), lo, lo+5)['matched_published_pages'] == ['408']


def test_inline_footnote_does_not_inherit_main_page():
    text, ranges = pages.extract('<span class="star-pagination">*409</span>main'
        '<div class="footnote">unknown location</div>continued', [CITATION], field='html')
    start = text.index('unknown')
    assert pages.for_passage({'published_page_ranges': ranges}, 0, len(text), start, start+7)['matched_published_pages'] is None


def test_reader_saves_page_evidence_and_reuses_cache(ops, monkeypatch):
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **k: ([case_result(citation=CITATION)], None))
    found = research.lookup(ops, lookup_args(CITATION))
    fetched, calls = calpine(), []
    def fetch(*a, **k):
        calls.append(1)
        return fetched
    monkeypatch.setattr(sources, 'fetch_source', fetch)
    args = read_args(found['research_id'], found['sources'][0]['id'], query=QUOTE)
    result = research.read(ops, args)
    assert result['matched_published_pages'] == ['409']
    assert result['text'] == fetched['text'][result['start']:result['end']]
    db.session.commit()
    db.session.expire_all()
    saved = record(result['passage_artifact_id'])
    for key in ('page_evidence', 'matched_published_pages', 'text', 'match_start', 'match_end'):
        assert saved[key] == result[key]
    args.research_id = result['research_id']
    args.query = None
    plain = research.read(ops, args)
    assert plain['matched_published_pages'] is None and plain['page_evidence']
    args.research_id = plain['research_id']
    args.query = 'missing wording'
    missed = research.read(ops, args)
    assert missed['matched_published_pages'] is None and missed['page_evidence'] == []
    assert calls == [1]


@pytest.mark.parametrize('tool', ['complete_task', 'create_draft', 'revise_draft', 'analyze_material', 'verify_draft'])
def test_evidence_reaches_general_answer_and_drafting(agent, monkeypatch, tool):
    monkeypatch.setattr(research, '_discover_mentions', lambda *a, **k: [])
    monkeypatch.setattr(sources, 'lookup_citations', lambda *a, **k: ([case_result(citation=CITATION)], None))
    operation(agent, monkeypatch, 'lookup_authorities', {'targets': [{'file_id': None, 'draft_id': None,
        'text': CITATION, 'start': None, 'end': None}], 'mode': 'overview', 'purpose': 'Check quotation.', 'research_id': None})
    found = last_result(agent)
    monkeypatch.setattr(sources, 'fetch_source', lambda *a, **k: calpine())
    operation(agent, monkeypatch, 'read_authority', {'research_id': found['research_id'],
        'source_id': found['sources'][0]['id'], 'opinion_id': None, 'query': QUOTE, 'start': 0})
    observed = last_result(agent)
    assert observed['matched_published_pages'] == ['409']
    db.session.expire_all()
    values = {'instructions': 'Check this quotation.', 'material_ids': [observed['passage_artifact_id']]}
    if tool in {'revise_draft', 'verify_draft'}:
        values['target'] = 'current_draft'
    if tool == 'revise_draft':
        values['revision_mode'] = 'general_revision'
    if tool == 'verify_draft':
        values['check_citations'] = False
    if tool == 'complete_task':
        values = {'response': 'The quotation is on page 409.', 'candidate_id': None}
    def check(materials):
        assert materials[0]['page_evidence'] == observed['page_evidence']
        assert materials[0]['matched_published_pages'] == ['409']
        assert materials[0]['text'] == observed['text']
    def writer(prompt, schema, **options):
        check(json.loads(prompt)['materials'])
        if schema is drafts.MarkdownDraftResult:
            return schema(title='Review', markdown='Quotation checked.', update_court_format=False)
        return schema(mode='patch', title=None, markdown=None,
            replacements=[{'old_text': 'Original facts.', 'new_text': 'Reviewed facts.'}], update_court_format=False)
    def reviewer(prompt, **options):
        marker = '\nSupporting materials:\n' if tool == 'verify_draft' else '\nMaterials (source evidence and prior generated work are labeled):\n'
        check(json.loads(prompt.split(marker)[1]))
        return 'Quotation checked.'
    monkeypatch.setattr(base, 'chat_completion_parse', writer)
    monkeypatch.setattr(base, 'chat_completion', reviewer)
    def controller(request):
        results = [json.loads(x['output']) for x in request['input'] if x.get('type') == 'function_call_output']
        assert results[-1]['page_evidence'] == observed['page_evidence']
        assert results[-1]['matched_published_pages'] == ['409']
        return call(tool, values)
    monkeypatch.setattr(base, 'responses_completion', controller)
    tick(agent)
    tick(agent)
    assert not last_result(agent).get('error'), last_result(agent)
