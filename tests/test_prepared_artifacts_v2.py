"""Coverage, evidence provenance and immutable preparation integration."""
import io
import json

import pytest
from werkzeug.datastructures import FileStorage

from backend.database import db
from backend.models import DocumentPreparation, DocumentPreparedArtifact, DocumentSummarySegment, UploadedFiles
from backend.util import attachment_parser as parser
from backend.util import document_preparation as prep
from backend.util import preparation_summary as summaries
from backend.util import prepared_retrieval as retrieval
from backend import document_preparation_tasks as tasks
from backend.util.document_metadata import parse_metadata, serialize_metadata
from tests.test_document_preparation import app, admit, receive, run, prepared_provider


def selected_snapshot(entry, *, acknowledgments=None):
    return prep.build_snapshot(1, 'draft-1', {'promptAttachments': [{'file_id': entry['document_id']}],
                                             'acceptedCoverageGaps': acknowledgments or []})


def test_preparation_cannot_be_disabled_by_old_flag(app):
    app.config['DOCUMENT_PREPARATION_ENABLED'] = False
    assert prep.enabled()
    response = app.test_client().get('/api/workspace/document-preparation?conversation_id=draft-1')
    assert response.status_code == 200 and response.json['enabled'] is True


def test_txt_numbers_and_margin_lines_are_preserved(tmp_path):
    source = 'MARGIN CONTRACT TITLE\n\n123456\n\n$5,000.00\n\nClause: café 漢字 😺.\n\nFinal exception.'
    path = tmp_path / 'source.txt'; path.write_text(source)
    manifest = parser.parse_attachment(path)
    assert all(part in manifest['text'] for part in ('123456', '$5,000.00', 'café 漢字 😺.', 'Final exception.'))
    assert manifest['extraction']['status'] == 'complete'
    assert all(manifest['text'][block['start']:block['end']] for block in manifest['blocks'])


def test_search_chunks_preserve_unicode_and_unbroken_paragraphs():
    text = '  Numeric table: 123456\t$5,000\n\n' + '漢字😺' * 5000 + '\n  Footnote exception.\n'
    chunks = parser.search_chunks(text)
    assert ''.join(chunk['content'] for chunk in chunks) == text
    assert max(chunk['token_count'] for chunk in chunks) <= 800
    assert all('\ufffd' not in chunk['content'] for chunk in chunks)


def test_document_card_includes_path_for_duplicate_filenames():
    card = json.loads(retrieval.compact_card({'document_id': 'document-1', 'filename': 'contract.txt',
        'relative_path': 'Supplier A/contract.txt', 'metadata': {'status': 'ready'}}, 'matter', False))
    assert card['filename'] == 'contract.txt' and card['relative_path'] == 'Supplier A/contract.txt'
    assert summaries.token_count(card) <= retrieval.CARD_LIMIT


def test_docx_tables_footnotes_and_headers_are_preserved(tmp_path):
    from docx import Document
    from zipfile import ZipFile
    doc = Document()
    doc.add_heading('Payment obligations', 1)
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = 'Due date'; table.cell(0, 1).text = 'Amount'
    table.cell(1, 0).text = '2030-12-31'; table.cell(1, 1).text = '123456'
    doc.sections[0].header.paragraphs[0].text = 'SUBSTANTIVE HEADER CONDITION'
    path = tmp_path / 'contract.docx'; doc.save(path)
    with ZipFile(path, 'a') as archive:
        archive.writestr('word/footnotes.xml',
            '<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:footnote w:id="1"><w:p><w:r><w:t>Unless the notice exception applies.</w:t></w:r></w:p></w:footnote></w:footnotes>')
    manifest = parser.parse_attachment(path)
    for expected in ('2030-12-31', '123456', 'SUBSTANTIVE HEADER CONDITION', 'Unless the notice exception applies.'):
        assert expected in manifest['text']
    assert manifest['blocks'][0]['heading_path'] == ['Payment obligations']


def test_pdf_page_outcomes_and_ocr_failures_are_explicit(tmp_path, monkeypatch):
    import fitz
    pdf = fitz.open()
    page = pdf.new_page(); page.insert_text((30, 20), 'SUBSTANTIVE MARGIN'); page.insert_text((50, 70), '123456')
    pdf.new_page()
    page = pdf.new_page(); page.draw_rect(fitz.Rect(30, 30, 100, 100), color=(0, 0, 0))
    path = tmp_path / 'record.pdf'; pdf.save(path); pdf.close()
    monkeypatch.setattr(parser, '_ocr', lambda data: (_ for _ in ()).throw(TimeoutError()))
    manifest = parser.parse_attachment(path)
    assert 'SUBSTANTIVE MARGIN' in manifest['text'] and '123456' in manifest['text']
    assert [o['status'] for o in manifest['extraction']['outcomes']] == ['native', 'verified_blank', 'unreadable']
    assert manifest['extraction']['gaps'][0]['page'] == 3
    assert manifest['extraction']['gap_fingerprint']


def test_native_pdf_keeps_text_without_treating_vector_drawings_as_missing_text(tmp_path):
    import fitz
    pdf = fitz.open()
    page = pdf.new_page()
    page.insert_text((50, 70), 'Native contract language.')
    page.draw_rect(fitz.Rect(30, 100, 200, 200), color=(0, 0, 0))
    path = tmp_path / 'diagram.pdf'; pdf.save(path); pdf.close()
    manifest = parser.parse_attachment(path)
    assert 'Native contract language.' in manifest['text']
    assert manifest['extraction']['outcomes'][0]['status'] == 'native'
    assert manifest['extraction']['status'] == 'complete'
    assert manifest['extraction']['gaps'] == []


@pytest.mark.parametrize('ocr_fails', [False, True])
def test_scanned_pages_use_ocr_and_keep_other_readable_pages_on_failure(tmp_path, monkeypatch, ocr_fails):
    import fitz
    from PIL import Image
    image = io.BytesIO()
    Image.new('RGB', (100, 100), 'white').save(image, format='PNG')
    pdf = fitz.open()
    page = pdf.new_page(); page.insert_text((50, 70), 'Native obligations and $5000.')
    page = pdf.new_page(); page.insert_image(page.rect, stream=image.getvalue())
    path = tmp_path / 'scan.pdf'; pdf.save(path); pdf.close()
    calls = []
    def ocr(data):
        calls.append(data)
        if ocr_fails:
            raise TimeoutError('OCR timed out')
        return 'Scanned deadline is December 31.'
    monkeypatch.setattr(parser, '_ocr', ocr)
    manifest = parser.parse_attachment(path)
    assert len(calls) == 1
    assert 'Native obligations and $5000.' in manifest['text']
    assert manifest['extraction']['outcomes'][1]['status'] == ('unreadable' if ocr_fails else 'ocr')
    assert ('Scanned deadline is December 31.' in manifest['text']) is not ocr_fails


@pytest.mark.parametrize('real_gap', [False, True])
def test_historical_vector_warnings_are_normalized_without_repreparation_or_artifact_changes(app, monkeypatch, real_gap):
    entry = receive(admit('A')); assert run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    artifact = db.session.get(DocumentPreparedArtifact, doc.artifact_id)
    gaps = [{'code': 'visual_content', 'page': page,
             'message': 'Vector graphics are not interpreted; native text is retained.'} for page in range(1, 6)]
    if real_gap:
        gaps.append({'code': 'ocr_unreadable', 'page': 6, 'message': 'Unreadable page'})
    historical = {'status': 'partial', 'gaps': gaps, 'gap_fingerprint': 'old-gap-fingerprint',
                  'outcomes': [{'page': page, 'status': 'partial'} for page in range(1, 6)]}
    manifest = json.loads(artifact.manifest_json)
    manifest['extraction'] = historical
    coverage = json.loads(artifact.coverage_json)
    coverage['extraction'] = historical
    artifact.manifest_json, artifact.coverage_json = json.dumps(manifest), json.dumps(coverage)
    doc.coverage_json = artifact.coverage_json
    # Model a compact response written by the old code, with only vector examples.
    doc.coverage_summary_json = json.dumps({'extraction': {**historical, 'gaps': gaps[:3], 'gap_count': len(gaps)}})
    doc.warning = 'This document has extraction coverage gaps.'
    db.session.commit()
    stored = (artifact.id, artifact.manifest_json, artifact.coverage_json, doc.coverage_json, doc.coverage_summary_json)
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: pytest.fail('Ready document was reparsed'))
    monkeypatch.setattr(summaries, '_call', lambda *a, **kw: pytest.fail('Ready document was summarized again'))
    view = prep.catalog(1, 'draft-1')['documents'][0]
    assert view['status'] == 'ready' and view['artifact_id'] == stored[0]
    assert view['coverage']['extraction']['gap_count'] == int(real_gap)
    assert bool(view['warning']) is real_gap
    response = app.test_client().get(f'/api/workspace/sources/{doc.id}/summary')
    assert response.status_code == 200
    assert len(response.json['coverage']['extraction']['gaps']) == int(real_gap)
    snapshot = selected_snapshot(entry)
    authorized = retrieval.authorized_artifacts(snapshot, include_source=True)[doc.id]
    extraction = authorized['artifact']['parse_manifest']['extraction']
    assert extraction['status'] == ('partial' if real_gap else 'complete')
    assert all(outcome['status'] == 'native' for outcome in extraction['outcomes'])
    from backend.util import source_context
    heading = source_context.document_heading(source_context.document(snapshot, doc.id), 'D1')
    assert 'accepted' not in heading.lower()
    assert 'Vector graphics' not in heading
    if real_gap:
        assert 'Unreadable page' in heading and '"page": 6' in heading
    else:
        assert 'Original extraction:' not in heading
    db.session.refresh(artifact); db.session.refresh(doc)
    assert (artifact.id, artifact.manifest_json, artifact.coverage_json, doc.coverage_json, doc.coverage_summary_json) == stored


@pytest.mark.parametrize('text', ['', '  \n\t'])
def test_no_usable_text_remains_a_preparation_failure(app, monkeypatch, text):
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw:
        parser.manifest_from_blocks([{'text': text}], gaps=[{'code': 'ocr_unreadable', 'page': 1, 'message': 'Unreadable page'}]))
    entry = receive(admit('A'))
    assert run(entry) == {'ok': False, 'reason': 'parsing_failed'}
    doc = prep.catalog(1, 'draft-1')['documents'][0]
    assert doc['status'] == 'failed' and doc['error_code'] == 'parsing_failed'
    with pytest.raises(prep.PreparationError, match='preparation'):
        selected_snapshot(entry)


def test_filtering_vector_warnings_keeps_real_gaps_on_the_same_page_and_unknown_coverage():
    original = parser.manifest_from_blocks([{'text': 'Readable words.', 'page': 1}], verified=False,
        outcomes=[{'page': 1, 'status': 'partial'}], gaps=[
            {'code': 'visual_content', 'page': 1,
             'message': 'Vector graphics are not interpreted; native text is retained.'},
            {'code': 'ocr_unreadable', 'page': 1, 'message': 'Some content could not be read.'}])['extraction']
    saved = json.dumps(original, sort_keys=True)
    normalized = parser.normalized_extraction(original)
    assert normalized['status'] == 'unverified'
    assert normalized['outcomes'][0]['status'] == 'partial'
    assert {gap['code'] for gap in normalized['gaps']} == {'ocr_unreadable', 'original_coverage_unverified'}
    assert json.dumps(original, sort_keys=True) == saved
    assert parser.normalized_extraction(normalized) == normalized


def test_pdf_parse_continues_without_repeating_completed_pages(tmp_path, monkeypatch):
    import fitz
    pdf = fitz.open()
    for number in range(12):
        page = pdf.new_page(); page.insert_text((50, 70), f'Page {number + 1} complete text')
    path = tmp_path / 'long.pdf'; pdf.save(path); pdf.close()
    saved = {}
    def checkpoint(key, result=None):
        if result is not None:
            assert key not in saved
            saved[key] = result
        return saved.get(key)
    with pytest.raises(parser.WorkPending):
        parser.parse_attachment(path, checkpoint=checkpoint, max_units=8)
    assert len(saved) == 8
    manifest = parser.parse_attachment(path, checkpoint=checkpoint, max_units=8)
    assert len(saved) == 12 and len(manifest['extraction']['outcomes']) == 12


def test_section_ranges_cover_every_character_and_overlap_unicode(monkeypatch):
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 100)
    source = ('Heading\n\n漢字😺 café section 12345\n\n' * 80)
    ranges = summaries.sections(source)
    assert ''.join(source[r['core_start']:r['core_end']] for r in ranges) == source
    assert all(r['start'] <= r['core_start'] < r['core_end'] == r['end'] for r in ranges)
    assert all(ranges[i]['start'] < ranges[i]['core_start'] for i in range(1, len(ranges)))
    assert all('\ufffd' not in source[r['start']:r['end']] for r in ranges)


def test_publication_is_atomic_and_summary_keeps_more_than_three_sentences(app):
    entry = receive(admit('A'))
    first = tasks.prepare_document.run(entry['document_id'], entry['generation'])
    assert first['continuing']
    assert DocumentPreparedArtifact.query.count() == 0
    assert prep.catalog(1, 'draft-1')['documents'][0]['status'] == 'summarizing'
    assert run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    artifact = db.session.get(DocumentPreparedArtifact, doc.artifact_id)
    result = json.loads(artifact.result_json)
    assert 'It contains exceptions.' in result['detailed_digest']
    assert 'detailed_digest' not in prep.catalog(1, 'draft-1')['documents'][0]['metadata']
    meta = {'schema_version': 2, 'preview': 'One. Two. Three. Four. Five.', 'status': 'ready'}
    assert parse_metadata(serialize_metadata(meta))['summary'].endswith('Five.')
    assert len(result['section_keys']) == 1
    assert DocumentSummarySegment.query.filter_by(kind='section', state='ready').one().lease_token is None


def test_partial_extraction_needs_no_approval_but_still_validates_scope_and_versions(app, monkeypatch):
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw:
        parser.manifest_from_blocks([{'text': 'Readable contract.'}], gaps=[{'code': 'ocr_unreadable', 'page': 4, 'message': 'Unreadable page'}]))
    entry = receive(admit('A')); assert run(entry)['ok']
    snapshot = selected_snapshot(entry)
    assert snapshot['version'] == 2
    assert 'accepted_coverage_gaps' not in snapshot
    assert prep.validate_snapshot(1, 'draft-1', snapshot)
    for owner, chat in [(2, 'draft-1'), (1, 'draft-2')]:
        with pytest.raises(prep.PreparationError):
            prep.validate_snapshot(owner, chat, snapshot)
    stale = {**snapshot, 'files': [{**snapshot['files'][0], 'artifact_id': 'old-version'}]}
    with pytest.raises(prep.PreparationError, match='changed'):
        prep.validate_snapshot(1, 'draft-1', stale)
    # Older clients and saved requests remain compatible; approval fields have no effect.
    assert selected_snapshot(entry, acknowledgments='obsolete') == snapshot
    legacy = {**snapshot, 'accepted_coverage_gaps': [{'document_id': 'obsolete', 'artifact_id': 'old'}]}
    assert prep.validate_snapshot(1, 'draft-1', legacy)


def test_summary_endpoint_authorizes_document_and_requested_artifact(app):
    first = receive(admit('A')); run(first)
    second = receive(admit('B', key='second')); run(second)
    a = db.session.get(DocumentPreparation, first['document_id'])
    b = db.session.get(DocumentPreparation, second['document_id'])
    client = app.test_client()
    result = client.get(f'/api/workspace/sources/{a.id}/summary?artifactId={a.artifact_id}')
    assert result.status_code == 200
    assert 'detailed_digest' in result.json and 'parse_manifest' not in result.json
    assert client.get(f'/api/workspace/sources/{a.id}/summary?artifactId={b.artifact_id}').status_code == 404
    prep.cancel_document(a)
    assert client.get(f'/api/workspace/sources/{a.id}/summary').status_code == 404


def test_raw_source_search_finds_fact_absent_from_preview_and_scope_is_exact(app, monkeypatch):
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: parser.manifest_from_blocks(
        [{'text': 'Ordinary content. The zephyr deadline is December 31, 2030.'}]))
    first = receive(admit('A')); run(first)
    other = receive(admit('B', key='other')); run(other)
    snapshot = selected_snapshot(first)
    def provider(prompt, schema, **kwargs):
        assert other['document_id'] not in prompt
        assert not db.session().in_transaction()
        payload = json.loads(prompt.split('Documents:\n')[1]) if 'Documents:\n' in prompt else []
        return retrieval.DocumentReview(documents=[retrieval.SelectedDocument(file_id=item['file_id'], relevant=True, reason='Deadline in source') for item in payload])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    decision = retrieval.refine_selection(snapshot,
        {'selected_files': [], 'retrieval_queries': ['zephyr deadline'], 'coverage_mode': 'focused', 'selection_confidence': 'high'},
        'What is the zephyr deadline?', model='gpt-6-luna')
    assert [f['file_id'] for f in decision['selected_files']] == [first['document_id']]
    assert 'zephyr' not in prep.catalog(1, 'draft-1')['documents'][0]['metadata']['preview']


@pytest.mark.parametrize('coverage_mode,expected_count', [
    ('whole_document', 3), ('focused', 3), ('corpus_wide', 25),
])
def test_selection_respects_relevance_unless_corpus_wide(app, monkeypatch, coverage_mode, expected_count):
    group = admit(*(f'source-{i:02}' for i in range(25)))
    entries = [receive(group, index=i) for i in range(25)]
    for entry in entries:
        assert run(entry)['ok']
    ids = [entry['document_id'] for entry in entries]
    snapshot = prep.build_snapshot(1, 'draft-1', {
        'promptAttachments': [{'file_id': uid} for uid in ids],
    })
    selected = [{'file_id': uid, 'scope': 'chat', 'role': 'citation_source', 'method': 'verbatim'}
                for uid in ids[:3]]
    # Two overlapping search result sets produce 25 candidates, as in the
    # recorded failure. Being a search hit does not make a document relevant.
    monkeypatch.setattr(retrieval, '_rank_documents', lambda model, *a, **kw:
        ids[5:] if model is DocumentSummarySegment else ids[:20])
    reviewed = []
    def provider(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        documents = json.loads(prompt.split('Documents:\n')[1])
        reviewed.extend(item['file_id'] for item in documents)
        return retrieval.DocumentReview(documents=[retrieval.SelectedDocument(
            file_id=item['file_id'], relevant=item['file_id'] in ids[:3],
            reason='Requested evidence' if item['file_id'] in ids[:3] else 'Unrelated case',
        ) for item in documents])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    decision = retrieval.refine_selection(snapshot, {
        'selected_files': selected, 'retrieval_queries': [], 'coverage_mode': coverage_mode,
        'selection_confidence': 'high', 'context_policy': 'refresh',
    }, 'Compare the pleadings.', model='gpt-6-luna')
    result = {item['file_id']: item for item in decision['selected_files']}
    assert len(reviewed) == 25 and set(reviewed) == set(ids)
    assert len(result) == expected_count
    assert set(result) == set(ids if coverage_mode == 'corpus_wide' else ids[:3])
    for item in selected:
        assert result[item['file_id']] == item
    assert all(decision['selection_reasons'][uid] == 'Unrelated case' for uid in ids[3:])


def test_exhaustive_evidence_reads_every_section_and_cache_revalidates(app, monkeypatch):
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 30)
    source = 'Alpha fact.\n\n' * 80 + 'RARE_MIDDLE_DEADLINE December 31, 2030.\n\n' + 'Omega fact.\n\n' * 80
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: parser.manifest_from_blocks([{'text': source}]))
    entry = receive(admit('A')); run(entry)
    snapshot = selected_snapshot(entry)
    calls = []
    def provider(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        material = json.loads(prompt.split('Source blocks:\n')[1])
        calls.append(''.join(b['text'] for b in material))
        block = next((b for b in material if 'RARE_MIDDLE_DEADLINE' in b['text']), None)
        return retrieval.EvidenceResult(excerpts=([summaries.SourceReference(block_id=block['block_id'], quote='RARE_MIDDLE_DEADLINE')] if block else []))
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    evidence, key, coverage = retrieval.extract_evidence(snapshot, [entry['document_id']], 'Find every deadline.',
                                                        model='gpt-6-luna', exhaustive=True)
    assert 'RARE_MIDDLE_DEADLINE' in evidence
    ranges = coverage[0]['completed_ranges']
    assert ''.join(source[r['core_start']:r['core_end']] for r in ranges) == source
    previous = len(calls)
    assert retrieval.extract_evidence(snapshot, [entry['document_id']], 'Find every deadline.',
                                     model='gpt-6-luna', exhaustive=True)[1] == key
    assert len(calls) == previous
    prep.cancel_document(db.session.get(DocumentPreparation, entry['document_id']))
    with pytest.raises(prep.PreparationError):
        retrieval.extract_evidence(snapshot, [entry['document_id']], 'Find every deadline.', model='gpt-6-luna', exhaustive=True)


def test_card_budget_preserves_exact_identity_and_headline():
    info = {'unique_id': 'file-123', 'name': '合同 file.pdf', 'artifact_id': 'a' * 64,
            'metadata': {'status': 'ready', 'headline': 'Agreement and payment deadlines',
                         'preview': 'Some detailed background. ' * 400, 'parties': ['Party'] * 400, 'topics': ['Topic'] * 400}}
    card = retrieval.compact_card(info, 'current', True)
    assert summaries.token_count(card) <= retrieval.CARD_LIMIT
    result = json.loads(card)
    assert result['file_id'] == 'file-123' and result['artifact_id'] == 'a' * 64
    assert result['headline'] == 'Agreement and payment deadlines'



def test_chat_per_file_limit_is_admitted_before_transfer(app):
    client = app.test_client()
    def submit(key, size):
        return client.post('/api/workspace/upload-groups', json={'scope': 'chat', 'scopeId': 'draft-1',
            'selectionId': key, 'documents': [{'client_upload_id': key, 'filename': key + '.pdf', 'file_size': size}]})
    assert submit('at-limit', 50 * 1024**2).status_code == 201
    assert submit('over-limit', 50 * 1024**2 + 1).status_code == 400
    assert UploadedFiles.query.count() == 0


def test_retrying_extraction_publishes_a_new_immutable_coverage_artifact(app, monkeypatch):
    partial = parser.manifest_from_blocks([{'text': 'Readable contract.'}],
        gaps=[{'code': 'ocr_unreadable', 'page': 4, 'message': 'Unreadable page'}])
    complete = parser.manifest_from_blocks([{'text': 'Readable contract.'}])
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: partial)
    entry = receive(admit('A')); run(entry)
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    old_id = doc.artifact_id
    retried = prep.retry_document(doc)
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: complete)
    assert run(retried)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert doc.artifact_id != old_id
    assert DocumentPreparedArtifact.query.count() == 2
    assert json.loads(db.session.get(DocumentPreparedArtifact, old_id).coverage_json)['extraction']['status'] == 'partial'
    assert 'accepted_coverage_gaps' not in selected_snapshot(retried)


def test_reduction_budget_retry_is_checkpointed_across_deliveries(app, monkeypatch):
    calls = []
    def provider(prompt, schema, **kwargs):
        calls.append((schema, prompt))
        if schema is summaries.PreparedSummary and 'target 3000 total output tokens' in prompt:
            raise summaries.RecordTooLarge('Simulated overlong output')
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', provider)
    entry = receive(admit('A'))
    assert run(entry)['ok']
    root_calls = [prompt for schema, prompt in calls if schema is summaries.PreparedSummary]
    assert len(root_calls) == 2
    assert sum('target 3000 total output tokens' in prompt for prompt in root_calls) == 1
    assert DocumentSummarySegment.query.filter_by(kind='budget_retry', state='ready').count() == 1


def test_oversized_section_subdivides_without_repeating_failed_parent(app, monkeypatch):
    source = ('Source paragraph with payment duties and exceptions.\n\n' * 100)
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: parser.manifest_from_blocks([{'text': source}]))
    calls = []
    def provider(prompt, schema, **kwargs):
        if schema is summaries.SectionRecord:
            payload = json.loads(prompt.rsplit('\n', 1)[-1])
            bounds = payload['range']; calls.append((bounds['core_start'], bounds['core_end']))
            if bounds['core_end'] - bounds['core_start'] > 2000:
                raise summaries.RecordTooLarge('Dense record')
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', provider)
    entry = receive(admit('A'))
    assert run(entry)['ok']
    coverage = json.loads(db.session.get(DocumentPreparation, entry['document_id']).coverage_json)
    assert coverage['completed_segments'] > 1 and coverage['covered_characters'] == len(source)
    assert all(calls.count(bounds) == 1 for bounds in calls)


def test_oversized_answer_context_reads_complete_sources_in_bounded_batches(app, monkeypatch):
    paragraphs = [f'Unique source clause {i}.' for i in range(100)]
    paragraphs[47] += ' CRITICAL DEADLINE 2030-12-31.'
    monkeypatch.setattr(summaries, 'SOURCE_SEGMENT_TOKENS', 50)
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: parser.manifest_from_blocks(
        [{'text': paragraph, 'paragraph': i + 1} for i, paragraph in enumerate(paragraphs)]))
    entry = receive(admit('A')); assert run(entry)['ok']
    snapshot = selected_snapshot(entry)
    seen = []
    def provider(prompt, schema, **kwargs):
        assert not db.session().in_transaction()
        payload = json.loads(prompt.split('Batch:\n')[1])
        seen.extend(block['text'] for block in payload['blocks'])
        refs = [retrieval.ScopedReference(file_id=payload['file_id'], block_id=block['block_id'],
                                        quote='CRITICAL DEADLINE 2030-12-31.')
                for block in payload['blocks'] if 'CRITICAL DEADLINE 2030-12-31.' in block['text']]
        return retrieval.EvidenceDigest(findings=[retrieval.ResponsiveFinding(statement='The deadline is December 31, 2030.',
                                                                              references=refs)] if refs else [])
    monkeypatch.setattr(retrieval, 'chat_completion_parse', provider)
    context = retrieval.bounded_answer_context(snapshot, [entry['document_id']], 'Find every deadline.',
                                                budget=4500, model='gpt-6-luna')
    assert 'CRITICAL DEADLINE 2030-12-31.' in context
    material = '\n'.join(seen)
    assert all(f'Unique source clause {i}.' in material for i in range(100))
    assert summaries.token_count(context) < 4500
    previous = len(seen)
    retrieval.bounded_answer_context(snapshot, [entry['document_id']], 'Find every deadline.',
                                      budget=4500, model='gpt-6-luna')
    assert len(seen) == previous


def test_token_preflight_rejects_oversized_request_before_provider(monkeypatch):
    from backend.ai import base
    from backend.ai import token_budget
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'gpt-6-luna', 2000)
    monkeypatch.setattr(token_budget, 'SAFETY_MARGIN', 32)
    monkeypatch.setattr(base, 'get_openai_client', lambda: pytest.fail('Oversized request reached provider'))
    with pytest.raises(token_budget.ContextBudgetExceeded):
        base.chat_completion_parse('source material ' * 4000, summaries.PreparedSummary,
                                   model='gpt-6-luna', reasoning_effort='medium', max_output_tokens=128)


def test_incomplete_provider_output_is_exposed_for_section_subdivision(monkeypatch):
    from types import SimpleNamespace
    from backend.ai import base
    from backend.ai.token_budget import IncompleteStructuredOutput
    calls = []
    def parse(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id='incomplete', status='incomplete', usage=SimpleNamespace(total_tokens=40),
                               incomplete_details=SimpleNamespace(reason='max_output_tokens'), output=[])
    monkeypatch.setattr(base, 'get_openai_client', lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse)))
    with pytest.raises(IncompleteStructuredOutput):
        base.chat_completion_parse('Some content', summaries.SectionRecord, model='gpt-6-luna',
                                   reasoning_effort='medium', max_output_tokens=32768, raise_on_incomplete=True)
    assert len(calls) == 1



def test_compact_source_markers_keep_large_coherent_sections(app, monkeypatch):
    blocks = [{'text': 'This administrative background paragraph repeats correspondence context and adds no operative deadlines, amounts, duties, or exceptions.',
               'paragraph': i + 1} for i in range(1600)]
    manifest = parser.manifest_from_blocks(blocks)
    monkeypatch.setattr(summaries, '_call', prepared_provider)
    saved = {}
    def checkpoint(key, result=None):
        if result is not None and '_work' not in result: saved[key] = result
        return saved.get(key)
    result, coverage = summaries.summarize_document(manifest['text'], 'long-record.txt', checkpoint,
                                                   source_hash='long', manifest=manifest)
    assert coverage['source_tokens'] > 30_000
    assert coverage['completed_segments'] <= 3
    for section in result['section_outline'][:-1]:
        assert summaries.token_count(manifest['text'][section['core_start']:section['core_end']]) >= 24_000



def test_manual_retry_resets_exhausted_reduction_attempts(app, monkeypatch):
    def oversized(prompt, schema, **kwargs):
        if schema is summaries.PreparedSummary:
            raise summaries.RecordTooLarge('Simulated overlong reduction')
        return prepared_provider(prompt, schema)
    monkeypatch.setattr(summaries, '_call', oversized)
    entry = receive(admit('A'))
    assert not run(entry)['ok']
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    assert DocumentSummarySegment.query.filter_by(kind='budget_retry').count() == 3
    ready_sections = [row.segment_key for row in DocumentSummarySegment.query.filter_by(kind='section', state='ready')]
    retried = prep.retry_document(doc)
    assert DocumentSummarySegment.query.filter_by(kind='budget_retry').count() == 0
    monkeypatch.setattr(summaries, '_call', prepared_provider)
    assert run(retried)['ok']
    assert [row.segment_key for row in DocumentSummarySegment.query.filter_by(kind='section', state='ready')] == ready_sections






def test_late_publisher_cannot_replace_a_newer_continuation_task(app, monkeypatch):
    entry = receive(admit('A'))
    doc = db.session.get(DocumentPreparation, entry['document_id'])
    doc.dispatch_pending = True
    db.session.commit()
    published = []
    def publish(**kwargs):
        published.append(kwargs['task_id'])
        if len(published) == 1:
            active = db.session.get(DocumentPreparation, entry['document_id'])
            active.status = 'summarizing'
            active.dispatch_pending = True
            db.session.commit()
            prep.dispatch_document(active.id)
    monkeypatch.setattr(tasks.prepare_document, 'apply_async', publish)
    prep.dispatch_document(doc.id)
    current = DocumentPreparation.query.filter_by(id=doc.id).populate_existing().one()
    assert len(published) == 2
    assert current.task_id == published[-1] and current.status == 'summarizing'
    assert current.dispatch_pending is False


def test_chat_evidence_keeps_extraction_gaps_without_approval_and_absence_qualified(app, monkeypatch):
    manifest = parser.manifest_from_blocks([{'text': 'Readable contract text.', 'page': 1}],
        gaps=[{'code': 'ocr_unreadable', 'page': 4, 'message': 'This page could not be read.'}])
    monkeypatch.setattr(tasks, 'parse_attachment', lambda *a, **kw: manifest)
    entry = receive(admit('A')); run(entry)
    doc = prep.catalog(1, 'draft-1')['documents'][0]
    snapshot = selected_snapshot(entry)
    monkeypatch.setattr(retrieval, 'chat_completion_parse', lambda *a, **kw: retrieval.EvidenceResult(excerpts=[]))
    evidence, _, progress = retrieval.extract_evidence(snapshot, [entry['document_id']],
        'Does this contain a deadline?', model='gpt-6-luna')
    assert '"status": "partial"' in evidence and '"page": 4' in evidence
    assert 'No responsive passages were found in the readable content.' in evidence
    assert progress[0]['mode'] == 'exhaustive_fallback'
