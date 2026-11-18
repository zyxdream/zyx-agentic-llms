"""Explicit CourtListener HTML pagination, tied to the text extracted with it.

Only provider-marked boundaries are evidence. Plain-text asterisks, PDF page
positions and citation pinpoints are never promoted to published pagination.
"""
from __future__ import annotations

import copy
import re
import unicodedata

from bs4 import BeautifulSoup


def _normal(value):
    return ' '.join(value.split())


def inspect_html(html, citations, *, field):
    """Retain readable text and validated boundaries without guessing a reporter.

    Unassociated boundaries are not published-page evidence. They use the same
    text and scope checks, so a metadata-only association cannot change positions.
    Footnotes never inherit main-text pages.
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
    headers = [e.get_text(' ', strip=True) for e in root.select('center b')
               if e.get_text(' ', strip=True)]
    citations = list(dict.fromkeys(citations))
    identified = [c for c in citations if any(_header_matches(h, c) for h in headers)]
    citation_headers = [h for h in headers if _looks_like_citation_header(h)]
    citation = identified[0] if len(identified) == 1 else (citations[0] if len(citations) == 1 else None)
    if citation and any(not _header_matches(h, citation) for h in citation_headers):
        citation = None
    tags = root.select('span.star-pagination')
    streams = list(dict.fromkeys(tag.get('citation-index') for tag in tags))
    result = {'text': text, 'published_page_ranges': [], 'unassociated_page_ranges': [],
              'pagination_headers': headers, 'pagination_streams': streams}
    if not tags or len(streams) != 1:
        return result

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
                'scope': key[0], 'field': field,
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
    result['unassociated_page_ranges'] = sorted(ranges, key=lambda value: value['start'])
    if citation:
        result['published_page_ranges'] = [{**copy.deepcopy(item), 'citation': citation}
                                           for item in result['unassociated_page_ranges']]
    return result


def extract(html, citations, *, field):
    """Compatibility wrapper returning only associated published-page evidence."""
    result = inspect_html(html, citations, field=field)
    return result['text'], result['published_page_ranges']


def _header_matches(header, citation):
    return bool(re.fullmatch(re.escape(_normal(citation)) + r'(?: \(\d{4}\))?', _normal(header)))


def _looks_like_citation_header(header):
    return bool(re.fullmatch(r'\d+\s+.+\s+\d+(?: \(\d{4}\))?', _normal(header)))


def _slug(value):
    # Django slugify's default ASCII algorithm, used by CAP's Reporter and Volume
    # models. Avoid a Django dependency in the Flask application.
    value = unicodedata.normalize('NFKD', str(value)).encode('ascii', 'ignore').decode('ascii')
    value = re.sub(r'[^\w\s-]', '', value.lower())
    return re.sub(r'[-\s]+', '-', value).strip('-_')


def _record_id(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, dict):
        value = value.get('id')
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        match = re.fullmatch(r'(?:https://www\.courtlistener\.com/api/rest/v[34]/opinions/)?(\d+)/?', value)
        return int(match[1]) if match and int(match[1]) > 0 else None
    return None


def resolve_cap_publication(fetched, cluster, *, metadata_url, metadata_sha256):
    """Associate retained CAP stream 1 using its archive locator and case identity.

    CAP writes stream 1 for its scanned publication. CourtListener's archive
    locator names that publication by reporter slug, volume and first page. No
    API citation ordering, page-number proximity, or text alignment is used.
    This function performs no I/O and never changes either supplied record.
    """
    result = {'published_page_ranges': copy.deepcopy(fetched.get('published_page_ranges') or []),
              'publication_source': None,
              'pagination_resolution': {'status': 'unverified', 'reason': None}}

    def unresolved(reason):
        result['pagination_resolution']['reason'] = reason
        return result

    if not isinstance(cluster, dict) or _record_id(cluster.get('id')) != _record_id(fetched.get('cluster_id')) \
            or _record_id(fetched.get('cluster_id')) is None:
        return unresolved('invalid_cluster_metadata')
    opinion_id = _record_id(fetched.get('opinion_id'))
    members = cluster.get('sub_opinions')
    if opinion_id is None or not isinstance(members, list) or opinion_id not in {_record_id(v) for v in members}:
        return unresolved('opinion_not_in_cluster')
    locator = cluster.get('filepath_json_harvard')
    if not isinstance(locator, str) or not locator:
        return unresolved('missing_cap_locator')
    parsed = re.fullmatch(r'law\.free\.cap\.([a-z0-9_-]+)\.([a-z0-9_-]+)/([0-9]+)\.([0-9]+)\.json', locator)
    if not parsed:
        return unresolved('invalid_cap_locator')
    reporter_slug, volume_slug, first_page, cap_id = parsed.groups()
    if int(cap_id) < 1:
        return unresolved('invalid_cap_locator')
    matches = {}
    citations = cluster.get('citations')
    if not isinstance(citations, list):
        return unresolved('unsupported_citation_metadata')
    for item in citations:
        if not isinstance(item, dict):
            continue
        identity = tuple(_normal(str(item.get(key) or '')) for key in ('volume', 'reporter', 'page'))
        volume, reporter, page = identity
        if volume and reporter and page and (_slug(reporter), _slug(volume), page) == (reporter_slug, volume_slug, first_page):
            matches[identity] = ' '.join(identity)
    if len(matches) != 1:
        return unresolved('citation_not_unique')
    citation = next(iter(matches.values()))
    if fetched.get('pagination_streams') != ['1']:
        return unresolved('unsupported_pagination_stream')
    headers = fetched.get('pagination_headers') or []
    if any(_looks_like_citation_header(h) and not _header_matches(h, citation) for h in headers):
        return unresolved('conflicting_publication_header')
    ranges = fetched.get('unassociated_page_ranges') or []
    if not ranges:
        return unresolved('no_valid_boundaries')
    result['published_page_ranges'] = [{**copy.deepcopy(item), 'citation': citation} for item in ranges]
    result['publication_source'] = {'field': 'filepath_json_harvard', 'value': locator,
        'url': metadata_url, 'sha256': metadata_sha256, 'cluster_id': cluster['id'],
        'citation': citation, 'pagination_identifier': '1'}
    result['pagination_resolution'] = {'status': 'resolved', 'reason': 'cap_archive_publication'}
    return result


def for_passage(fetched, lo, hi, match_start, match_end):
    """Return boundary evidence for this excerpt; map only the actual matched span."""
    evidence = []
    for page in fetched.get('published_page_ranges') or []:
        if page['start'] < hi and page['end'] > lo:
            evidence.append({**copy.deepcopy(page),
                'source_url': fetched.get('retrieval_url') or fetched.get('url'),
                'source_sha256': fetched.get('sha256'), 'opinion_id': fetched.get('opinion_id'),
                **({'publication_source': copy.deepcopy(fetched['publication_source'])}
                   if fetched.get('publication_source') else {})})
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
