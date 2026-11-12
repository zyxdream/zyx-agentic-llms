"""Loss-preserving attachment text and explicit extraction coverage.

This deliberately does not use the complaint parser's header/number cleanup.
All offsets refer to the exact canonical text returned here, not rendered pages.
"""
from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile
from lxml import etree as ET

PARSER_VERSION = 'attachment-blocks-v3'


class WorkPending(Exception):
    """A bounded unit finished; continue from durable checkpoints in another task."""


def fingerprint(value):
    import json
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def normalized_extraction(extraction):
    """Read historical vector warnings without rewriting published artifacts.

    A PDF drawing can be a border or rule beside perfectly readable text.
    Only this old, specific warning is discarded; OCR failures and unknown
    original coverage still qualify what the assistant can claim.
    """
    extraction = dict(extraction or {})
    gaps, vector_pages = [], set()
    for gap in extraction.get('gaps', []):
        if (gap.get('code') == 'visual_content'
                and gap.get('message') == 'Vector graphics are not interpreted; native text is retained.'):
            vector_pages.add(gap.get('page'))
        else:
            gaps.append(gap)
    if not vector_pages:
        return extraction
    remaining_pages = {gap.get('page') for gap in gaps}
    extraction['outcomes'] = [
        {**outcome, 'status': 'native'}
        if outcome.get('status') == 'partial' and outcome.get('page') in vector_pages - remaining_pages
        else outcome for outcome in extraction.get('outcomes', [])]
    extraction['gaps'] = gaps
    if not gaps and extraction.get('status') == 'partial':
        extraction['status'] = 'complete'
    extraction.pop('gap_fingerprint', None)
    extraction['gap_fingerprint'] = fingerprint(extraction) if gaps else None
    return extraction


def normalized_coverage(coverage):
    return {**coverage, 'extraction': normalized_extraction(coverage.get('extraction'))}


def extraction_warning(extraction):
    extraction = normalized_extraction(extraction)
    if extraction.get('status') in {'partial', 'unverified'}:
        return 'Some original content could not be read or its extraction could not be verified. Available text will be used.'
    return None


def manifest_from_blocks(items, *, outcomes=None, gaps=None, verified=True):
    blocks, parts, offset, heading_path, previous_part = [], [], 0, [], None
    for item in items:
        material = item.get('text', '')
        if not material.strip():
            continue
        if item.get('part') != previous_part:
            heading_path = []
            previous_part = item.get('part')
        if parts:
            parts.append('\n\n')
            offset += 2
        heading = item.get('heading')
        if heading:
            level = max(1, min(6, item.get('level', 1)))
            heading_path = heading_path[:level - 1] + [heading]
        block = {k: v for k, v in item.items() if k != 'text'}
        block.update(id=f'b{len(blocks) + 1:06d}', start=offset, end=offset + len(material),
                     heading_path=list(heading_path))
        blocks.append(block)
        parts.append(material)
        offset += len(material)
    text = ''.join(parts)
    gaps = list(gaps or [])
    if not verified:
        gaps.append({'code': 'original_coverage_unverified',
                     'message': 'Original extraction completeness cannot be verified.'})
    extraction = {'status': 'partial' if gaps and verified else 'unverified' if not verified else 'complete',
                  'outcomes': outcomes or [], 'gaps': gaps}
    extraction['gap_fingerprint'] = fingerprint(extraction) if gaps else None
    return {'parser_version': PARSER_VERSION, 'text': text, 'blocks': blocks,
            'parsed_hash': hashlib.sha256(text.encode()).hexdigest(), 'extraction': extraction}


def unverified_manifest(text):
    return manifest_from_blocks([{'text': text, 'location': 'Previously extracted text'}], verified=False)


def search_chunks(text, target_tokens=800):
    """Index exact contiguous source ranges without tokenizing a giant paragraph."""
    import tiktoken
    if not text or not text.strip():
        return []
    encoder = tiktoken.get_encoding('cl100k_base')
    result, start = [], 0
    while start < len(text):
        probe_end = min(len(text), start + target_tokens * 4)
        while True:
            tokens = encoder.encode(text[start:probe_end])
            if len(tokens) >= target_tokens or probe_end == len(text):
                break
            probe_end = min(len(text), start + 2 * (probe_end - start))
        take = min(target_tokens, len(tokens))
        while take:
            try:
                material = encoder.decode_bytes(tokens[:take]).decode('utf-8')
                break
            except UnicodeDecodeError:
                take -= 1
        if not take:
            raise ValueError('Cannot find a complete Unicode indexing boundary')
        end = start + len(material)
        if end < len(text):
            lower = len(material) * 3 // 4
            for pattern in (r'\n\s*\n', r'\n', r'(?<=[.;])\s+'):
                boundaries = list(re.finditer(pattern, material[lower:]))
                if boundaries:
                    end = start + lower + boundaries[-1].end()
                    break
        content = text[start:end]
        result.append({'chunk_index': len(result), 'content': content,
                       'token_count': len(encoder.encode(content)), 'char_count': len(content)})
        start = end
    return result


def _paragraphs(text, **location):
    # Keep every nonempty character, including isolated numbers and margin text.
    result = []
    for index, match in enumerate(re.finditer(r'\S[\s\S]*?(?=\n\s*\n|\Z)', text)):
        material = match.group(0)
        first = material.splitlines()[0].strip()
        heading = first if len(material.splitlines()) == 1 and (
            re.match(r'^(?:#{1,6}\s|(?:ARTICLE|SECTION|Article|Section)\s+[\w.]+)', first)
            or (first.isupper() and len(first) < 150 and any(c.isalpha() for c in first))) else None
        result.append({'text': material, 'paragraph': index + 1, **location,
                       **({'heading': heading} if heading else {})})
    return result


def _ocr(image_bytes):
    from backend.constants import PDF_OCR_BACKEND
    if PDF_OCR_BACKEND == 'cloud':
        from backend.util.aws_textract import detect_text_from_image_bytes
        return detect_text_from_image_bytes(image_bytes)
    from PIL import Image
    import pytesseract
    return pytesseract.image_to_string(Image.open(BytesIO(image_bytes)), timeout=60)


def parse_attachment(path, filename=None, checkpoint=None, *, max_units=None):
    filename = filename or Path(path).name
    suffix = Path(filename).suffix.lower()
    outcomes, gaps, items = [], [], []
    units = 0

    def unit(key, work):
        nonlocal units
        saved = checkpoint(key) if checkpoint else None
        if saved is not None:
            return saved
        if max_units is not None and units >= max_units:
            raise WorkPending()
        result = work()
        if checkpoint:
            checkpoint(key, {**result, '_kind': 'parse'})
        units += 1
        return result

    if suffix in {'.xlsx', '.csv'}:
        from backend.util.spreadsheet_ingestion import parse_spreadsheet, VERSION as SPREADSHEET_VERSION
        saved = unit(f'parse:{SPREADSHEET_VERSION}',
                     lambda: dict(zip(('items', 'spreadsheet', 'outcomes', 'gaps'),
                                      parse_spreadsheet(path, filename))))
        manifest = manifest_from_blocks(saved['items'], outcomes=saved['outcomes'], gaps=saved['gaps'])
        manifest['parser_version'] = SPREADSHEET_VERSION
        manifest['spreadsheet'] = saved['spreadsheet']
        return manifest
    if suffix == '.pdf':
        import fitz
        with fitz.open(path) as pdf:
            if not pdf.page_count:
                raise ValueError('PDF has no pages')
            for number in range(pdf.page_count):
                def read_page():
                    page = pdf[number]
                    native = page.get_text('text', sort=True)
                    images = bool(page.get_images(full=True))
                    drawings = bool(page.get_drawings())
                    page_items = _paragraphs(native, page=number + 1, origin='native')
                    page_gaps = []
                    status = 'native'
                    if not native.strip() and not images and not drawings:
                        status = 'verified_blank'
                    elif images or not native.strip():
                        try:
                            data = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).tobytes('png')
                            ocr = _ocr(data)
                            if not str(ocr or '').strip():
                                raise ValueError('OCR produced no readable text')
                            # Native text is authoritative where available. Preserve additional
                            # OCR text separately rather than replacing readable source text.
                            if ocr.strip() != native.strip():
                                page_items.extend(_paragraphs(ocr, page=number + 1, origin='ocr'))
                            status = 'ocr'
                            if images:
                                page_gaps.append({'code': 'visual_content', 'page': number + 1,
                                    'message': 'Image text was OCR processed; non-text visual content is not interpreted.'})
                        except Exception:
                            status = 'unreadable' if not native.strip() else 'partial'
                            page_gaps.append({'code': 'ocr_unreadable', 'page': number + 1,
                                              'message': 'Some content on this page could not be read.'})
                    return {'items': page_items, 'outcome': {'page': number + 1, 'status': status}, 'gaps': page_gaps}
                saved = unit(f'parse:{PARSER_VERSION}:page:{number}', read_page)
                items.extend(saved['items']); outcomes.append(saved['outcome']); gaps.extend(saved['gaps'])
    elif suffix == '.docx':
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        with ZipFile(path) as archive:
            names = archive.namelist()
            parts = ['word/document.xml'] + sorted(name for name in names if re.match(
                r'word/(?:footnotes|endnotes|header\d*|footer\d*)\.xml$', name))
            for name in parts:
                root = ET.fromstring(archive.read(name), parser=ET.XMLParser(resolve_entities=False, no_network=True))
                for index, paragraph in enumerate(root.iter(f'{{{ns["w"]}}}p')):
                    text = ''.join(('\t' if node.tag.endswith('}tab') else '\n' if node.tag.endswith('}br')
                                    else node.text or '') for node in paragraph.iter()
                                   if node.tag.endswith(('}t', '}delText', '}tab', '}br')))
                    style = paragraph.find('w:pPr/w:pStyle', ns)
                    style_name = style.get(f'{{{ns["w"]}}}val', '') if style is not None else ''
                    heading = re.search(r'heading\s*(\d)', style_name, re.I)
                    location = {}
                    for ancestor in paragraph.iterancestors():
                        if ancestor.tag.endswith('}tc'):
                            location['cell'] = len(list(ancestor.itersiblings(preceding=True, tag=f'{{{ns["w"]}}}tc'))) + 1
                        elif ancestor.tag.endswith('}tr'):
                            location['row'] = len(list(ancestor.itersiblings(preceding=True, tag=f'{{{ns["w"]}}}tr'))) + 1
                        elif ancestor.tag.endswith('}tbl'):
                            location['table_path'] = root.getroottree().getpath(ancestor)
                        elif ancestor.tag.endswith(('}footnote', '}endnote')):
                            location['note_id'] = ancestor.get(f'{{{ns["w"]}}}id')
                    if paragraph.find('.//w:delText', ns) is not None:
                        location['revision'] = 'includes_deleted_text'
                        gaps.append({'code': 'tracked_deletion', 'part': name, 'paragraph': index + 1,
                                     'message': 'Tracked deleted text is retained and requires review.'})
                    items.append({'text': text, 'part': name, 'paragraph': index + 1, **location,
                                  **({'heading': text, 'level': int(heading[1])} if heading else {})})
            for name in names:
                if name.startswith(('word/media/', 'word/embeddings/')):
                    gaps.append({'code': 'embedded_content', 'part': name,
                                 'message': 'Embedded visual/object content needs review.'})
        outcomes = [{'part': name, 'status': 'native'} for name in parts]
    elif suffix == '.doc':
        result = subprocess.run(['antiword', str(path)], capture_output=True, check=True, timeout=120)
        items = _paragraphs(result.stdout.decode('utf-8'), origin='antiword')
        return manifest_from_blocks(items, verified=False)
    elif suffix == '.txt':
        raw = Path(path).read_bytes()
        try:
            text = raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            text = raw.decode('cp1252')
            gaps.append({'code': 'encoding_assumed', 'message': 'Text encoding was inferred as Windows-1252.'})
        items = _paragraphs(text, origin='text')
    else:
        raise ValueError('Unsupported attachment format')
    return manifest_from_blocks(items, outcomes=outcomes, gaps=gaps)
