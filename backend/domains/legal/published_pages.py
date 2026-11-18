"""Explicit CourtListener HTML pagination, tied to the text extracted with it.

Only provider-marked boundaries are evidence. Plain-text asterisks, PDF page
positions and citation pinpoints are never promoted to published pagination.
"""
from __future__ import annotations

import copy
import re

from bs4 import BeautifulSoup


def _normal(value):
    return ' '.join(value.split())


def extract(html, citations, *, field):
    """Return the usual readable HTML text and conservative, original-text ranges.

    Supported provider markup is span.star-pagination, optionally with label and
    citation-index. Multiple pagination streams without an explicit reporter
    association remain unsupported. Footnotes never inherit main-text pages.
    """
    soup = BeautifulSoup(html, 'html.parser')
    for element in soup.find_all(['script', 'style', 'noscript', 'template', 'svg', 'canvas', 'iframe']):
        element.decompose()
    root = soup.body or soup
    # Match BeautifulSoup.get_text('\n', strip=True), including its exclusion of
    # comments. Build offsets during extraction, never by aligning another format.
    nodes = list(root.strings)
    parts, offsets, cursor = [], {}, 0
    for node in nodes:
        value = str(node).strip()
        if not value:
            continue
        if parts:
            cursor += 1
        offsets[id(node)] = (cursor, cursor + len(value))
        parts.append(value)
        cursor += len(value)
    text = '\n'.join(parts)

    def bounds(element):
        spans = [offsets[id(n)] for n in element.descendants if id(n) in offsets]
        return (spans[0][0], spans[-1][1]) if spans else None

    # Lawbox identifies the publication in its centered introductory heading.
    # Otherwise a single supplied citation is unambiguous; do not guess among
    # parallel citations or treat citation-index as the order of API citations.
    headers = [e.get_text(' ', strip=True) for e in root.select('center b')]
    identified = [c for c in citations if any(
        re.fullmatch(re.escape(_normal(c)) + r'(?: \(\d{4}\))?', _normal(h)) for h in headers)]
    citation = identified[0] if len(identified) == 1 else (citations[0] if len(citations) == 1 else None)
    tags = root.select('span.star-pagination')
    if not tags or not citation:
        return text, []
    streams = {tag.get('citation-index') for tag in tags}
    if len(streams) != 1:
        return text, []

    # Lawbox's explicit NOTES heading starts separately assembled notes. Their
    # original page positions cannot be inherited from the final main-text page.
    notes_start = min((b[0] for e in root.find_all(['h2', 'h3'])
                       if e.get_text(' ', strip=True).upper() in {'NOTES', 'FOOTNOTES'}
                       if (b := bounds(e)) is not None), default=len(text))
    groups, scope_bounds = {}, {}
    for tag in tags:
        span = bounds(tag)
        if span is None:
            continue
        footnote = next((p for p in tag.parents if p.name in {'div', 'aside', 'section'}
                         and ('footnote' in p.get('class', []) or p.get('role') == 'doc-endnote')), None)
        notes = next((p for p in tag.parents if 'footnotes' in p.get('class', [])
                      or p.get('role') == 'doc-endnotes'), None)
        if notes is not None and footnote is None:
            continue
        if footnote is not None:
            key = ('footnote', id(footnote))
            scope_bounds[key] = bounds(footnote)
        elif span[0] < notes_start:
            key = ('main', 0)
            # A structured footnotes container is also a main-text boundary.
            footnote_starts = [b[0] for e in root.select('.footnotes, [role="doc-endnotes"]')
                               if (b := bounds(e)) is not None]
            scope_bounds[key] = (0, min([notes_start, *footnote_starts]))
        else:
            continue
        marker = text[span[0]:span[1]]
        label = tag.get('label') or marker.lstrip('*').strip()
        # Require explicit marker text and attribute, when present, to agree.
        if not re.fullmatch(r'\d+', label) or _normal(marker) != '*' + label:
            groups.setdefault(key, []).append(None)
            continue
        groups.setdefault(key, []).append({'page': label, 'boundary_start': span[0],
            'boundary_end': span[1], 'boundary_text': marker})
    ranges = []
    for key, markers in groups.items():
        if any(m is None for m in markers):
            continue
        if any(int(b['page']) != int(a['page']) + 1 for a, b in zip(markers, markers[1:])):
            continue
        for index, marker in enumerate(markers):
            following = markers[index + 1] if index + 1 < len(markers) else None
            end = following['boundary_start'] if following else scope_bounds[key][1]
            if end <= marker['boundary_end']:
                continue
            item = {**marker, 'start': marker['boundary_start'], 'end': end,
                'citation': citation, 'scope': key[0], 'field': field,
                'next_boundary': copy.deepcopy(following),
                'end_basis': 'next_published_boundary' if following else 'scope_end'}
            pieces = [item]
            if key[0] == 'main':
                for node in root.select('div.footnote, aside.footnote, section.footnote, [role="doc-endnote"]'):
                    span = bounds(node)
                    if span is None:
                        continue
                    split = []
                    for piece in pieces:
                        if span[0] >= piece['end'] or span[1] <= piece['start']:
                            split.append(piece)
                        else:
                            if piece['start'] < span[0]:
                                split.append({**piece, 'end': span[0]})
                            if piece['end'] > span[1]:
                                split.append({**piece, 'start': span[1]})
                    pieces = split
            ranges.extend(pieces)
    return text, sorted(ranges, key=lambda value: value['start'])


def for_passage(fetched, lo, hi, match_start, match_end):
    """Return boundary evidence for this excerpt; map only the actual matched span."""
    evidence = []
    for page in fetched.get('published_page_ranges') or []:
        if page['start'] < hi and page['end'] > lo:
            evidence.append({**copy.deepcopy(page),
                'source_url': fetched.get('retrieval_url') or fetched.get('url'),
                'source_sha256': fetched.get('sha256'), 'opinion_id': fetched.get('opinion_id')})
    matched = None
    if match_start is not None and match_end is not None:
        pages = [p for p in evidence if p['start'] < match_end and p['end'] > match_start]
        cursor = match_start
        for page in pages:
            if page['start'] > cursor:
                break
            cursor = max(cursor, page['end'])
        identities = {(p['citation'], p['scope']) for p in pages}
        if cursor >= match_end and len(identities) == 1:
            matched = list(dict.fromkeys(p['page'] for p in pages))
    return {'matched_published_pages': matched, 'page_evidence': evidence}
