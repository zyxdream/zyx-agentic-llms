"""Extracted, application-owned helpers for the Workspace agent."""

import json

import re

from backend.models import DocumentSummarySegment, UploadedFiles

from backend.util import workspace_service as service

from backend.util import prepared_retrieval as retrieval

from backend.util.preparation_summary import token_count

WINDOW_TARGET = 1024


WINDOW_LIMIT = 1536


def document(snapshot, uid, artifact_id=None):
    """Reauthorize even ready ranges/cache hits before reading canonical text."""
    info = retrieval.authorized_artifacts(snapshot, include_source=True, file_ids=[uid])[uid]
    if artifact_id is not None and artifact_id != info['entry']['artifact_id']:
        raise service.WorkflowStopped('The source document version changed.')
    file = UploadedFiles.query.filter_by(unique_id=uid).one()
    return {**info, 'file_id': uid, 'artifact_id': info['entry']['artifact_id'],
            'filename': file.original_filename, 'relative_path': file.relative_path,
            'manifest': {**info['artifact']['parse_manifest'], 'text': file.content}}


def merge_ranges(text, spans, *, bridge_token_limit=None):
    """Union ranges in one document, optionally filling gaps within a token limit."""
    result = []
    for span in sorted(spans, key=lambda s: (s['start'], s['end'])):
        start, end = span['start'], span['end']
        if (type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text)):
            raise ValueError('Source range is outside canonical text')
        if result and start <= result[-1]['end']:
            result[-1]['end'] = max(end, result[-1]['end'])
        else:
            result.append({'start': start, 'end': end})
    if bridge_token_limit is None:
        return result
    bridged = []
    for span in result:
        if bridged and token_count(text[bridged[-1]['start']:span['end']]) <= bridge_token_limit:
            bridged[-1]['end'] = span['end']
        else:
            bridged.append(span)
    return bridged


def _end(text, start, end, limit):
    """Find a token-bounded end using Python character (Unicode) boundaries."""
    if token_count(text[start:end]) <= limit:
        return end
    low, high = start + 1, end
    while low < high:
        middle = (low + high + 1) // 2
        if token_count(text[start:middle]) <= limit:
            low = middle
        else:
            high = middle - 1
    return low


def windows(text, start, end, limit=WINDOW_TARGET):
    """Disjoint windows prefer complete paragraphs/sentences where possible."""
    while start < end:
        stop = _end(text, start, end, limit)
        if stop < end:
            boundaries = list(re.finditer(r'\n\s*\n|(?<=[.!?;])\s+', text[start:stop]))
            if boundaries and boundaries[-1].end() >= (stop - start) // 2:
                stop = start + boundaries[-1].end()
        yield {'start': start, 'end': stop}
        start = stop


def _window(manifest, block, queries):
    text = manifest['text']
    choices = list(windows(text, block['start'], block['end']))
    if len(choices) > 1:
        ranked = retrieval.ranked_matches([{'id': str(i), 'content': text[s['start']:s['end']], 'span': s}
                                           for i, s in enumerate(choices)], queries)
        span = dict(ranked[0]['span'] if ranked else choices[0])
    else:
        span = dict(choices[0])
    original = dict(span)
    # At most one neighboring block on either side, bounded by the same window
    # ceiling. Further qualifications/definitions are separate source windows.
    blocks = manifest['blocks']
    index = next(i for i, b in enumerate(blocks) if b['id'] == block['id'])
    if index:
        left = blocks[index - 1]['start']
        while left < span['start'] and token_count(text[left:span['end']]) > WINDOW_LIMIT:
            left += max(1, (span['start'] - left) // 2)
        span['start'] = left
    if index + 1 < len(blocks):
        span['end'] = _end(text, span['start'], blocks[index + 1]['end'], WINDOW_LIMIT)
    span['end'] = max(span['end'], original['end'])
    clipped = (original['start'] > block['start'] or original['end'] < block['end']
               or (index and span['start'] > blocks[index - 1]['start'])
               or (index + 1 < len(blocks) and span['end'] < blocks[index + 1]['end']))
    return span, clipped


def selected_spans(manifest, artifact, queries, *, section_limit=3, block_limit=12,
                   hit_state=None, separate_queries=False):
    """A section is a search location, not a 28K-token reading instruction."""
    hits = hit_state if hit_state is not None else {}
    rows = DocumentSummarySegment.query.filter_by(document_id=artifact['_document_id'],
        kind='section', state='ready').filter(DocumentSummarySegment.segment_key.in_(artifact['section_keys'])).all()
    section_items = [{'id': r.segment_key, 'content': r.search_text or '',
                      'record': json.loads(r.result_json)} for r in rows]
    blocks = [{'id': b['id'], 'content': manifest['text'][b['start']:b['end']], 'block': b}
              for b in manifest['blocks']]
    def retain(field, items, ranked, limit):
        by_id = {v['id']: v for v in items}
        ids = list(dict.fromkeys(hits.get(field, [])))
        if len(ids) > limit or any(uid not in by_id for uid in ids):
            raise ValueError('Saved retrieval hits do not match the source version or limits')
        ids.extend(v['id'] for v in ranked if v['id'] not in ids)
        hits[field] = ids[:limit]
        return [by_id[uid] for uid in hits[field]]
    ranked_sections = retrieval.ranked_matches(section_items, queries, separate=separate_queries)
    chosen_sections = retain('sections', section_items, ranked_sections, section_limit)
    within = []
    for item in chosen_sections:
        record = item['record']
        within.append(retrieval.ranked_matches([b for b in blocks if
            b['block']['end'] > record['start'] and b['block']['start'] < record['end']],
            queries, separate=separate_queries))
    ranked = retrieval.round_robin_hits([
        retrieval.ranked_matches(blocks, queries, separate=separate_queries), *within], len(blocks))
    chosen = retain('blocks', blocks, ranked, block_limit)
    spans, clipped = [], False
    for item in chosen:
        span, cut = _window(manifest, item['block'], queries)
        spans.append(span)
        clipped |= cut
    # Definitions can carry an entire orientation-section range. Locate the
    # definition's original words inside that range instead of copying it all.
    definition_spans = []
    for item in chosen_sections:
        record = item['record']
        selected_text = ' '.join(manifest['text'][s['start']:s['end']] for s in spans).casefold()
        for definition in record.get('definitions', []):
            term = definition.get('term', '')
            if not term or term.casefold() not in selected_text:
                continue
            origins = definition.get('source_ranges') or record.get('definition_ranges') or [record]
            candidates = [b for b in blocks if any(b['block']['end'] > s['start'] and
                          b['block']['start'] < s['end'] for s in origins)]
            definitions = [b for b in candidates if term.casefold() in b['content'].casefold()
                           and re.search(r'\b(means|defined|shall mean|refers to)\b', b['content'], re.I)]
            for match in retrieval.ranked_matches(definitions or candidates, [term])[:1]:
                span, cut = _window(manifest, match['block'], [term])
                definition_spans.append(span)
                clipped |= cut
    hits['weak_reasons'] = ([] if chosen else ['No matching original passage was located.'])
    if chosen_sections and not chosen:
        hits['weak_reasons'].append('Section summaries matched but original passages did not.')
    if clipped:
        hits['weak_reasons'].append('A relevant source block or neighboring clause continues beyond its window.')
    for location, number in re.findall(r'\b(page|paragraph)\s+(\d+)\b', ' '.join(queries), re.I):
        if not any(item['block'].get(location.lower()) == int(number) for item in chosen):
            hits['weak_reasons'].append(f'The requested {location.lower()} {number} was not located in the selected passages.')
    # Never turn a no-hit focused search into full-document extraction.
    return merge_ranges(manifest['text'], spans + definition_spans, bridge_token_limit=WINDOW_LIMIT)


def document_heading(doc, label):
    """Emit the filename and extraction warning once per document."""
    manifest, filename = doc['manifest'], doc['filename']
    path = doc.get('relative_path')
    heading = f'[{label}: {filename}' + (f' ({path})' if path and path != filename else '') + ']\n'
    extraction = manifest['extraction']
    if extraction['status'] != 'complete':
        heading += f'[Original extraction: {extraction["status"]}; only readable content is available. Do not infer absence in unreadable content.]\n'
        heading += '[Extraction details: ' + json.dumps({
            'gap_count': len(extraction.get('gaps', [])),
            'example_gaps': extraction.get('gaps', [])[:3]}, ensure_ascii=False) + ']\n'
    return heading


def format_ranges(manifest, spans, label, *, filename=None):
    text, blocks = manifest['text'], manifest['blocks']
    parts = []
    for span in merge_ranges(text, spans):
        cursor, previous = span['start'], None
        if span['start'] or span['end'] < len(text):
            parts.append(f'[Selected source range {span["start"]}:{span["end"]}]\n')
        for b in blocks:
            if b['end'] <= span['start'] or b['start'] >= span['end']:
                continue
            location = (('sheet', f"{b['sheet']}!{b.get('range', b.get('cell', ''))}") if b.get('sheet') is not None else
                        ('page', b['page']) if b.get('page') is not None else
                        ('paragraph', b['paragraph']) if b.get('paragraph') is not None else ('block', b['id']))
            position = max(span['start'], b['start'])
            if location != previous and location[1] is not None:
                header = (f'\n--- SOURCE EXCERPT ---\nFILE: {filename or label}\n'
                          f'PDF PAGE: {location[1]}\n\n' if location[0] == 'page' else
                          f'\n[{label} {location[0]} {location[1]}]\n')
                parts.extend([text[cursor:position], header])
                cursor, previous = position, location
        parts.append(text[cursor:span['end']])
        parts.append('\n')
    return ''.join(parts)


def format_document(doc, spans, label):
    """Label each PDF excerpt explicitly without changing canonical text/offsets."""
    return document_heading(doc, label) + format_ranges(
        doc['manifest'], spans, label, filename=doc['filename'])
