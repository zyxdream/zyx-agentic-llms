"""Utilities for turning parsed legal-document text into editable draft structure.

The LLM is used only to classify boundaries. Final block text is assembled from
the original line records so downstream editing can preserve source content.
"""

from __future__ import annotations

import argparse
import concurrent.futures
from contextvars import copy_context
import json
import re
from pathlib import Path
from typing import Callable, Literal

from pydantic import BaseModel
from celery.exceptions import SoftTimeLimitExceeded

from backend.ai.base import chat_completion_parse


BLOCK_TYPES = {"heading", "paragraph"}
DEFAULT_MODEL = "gpt-5.5"
# Per-section block structuring is one LLM call per section and the calls are independent, so
# they are fanned out across a thread pool. Sized to cover most documents (complaints/briefs
# rarely exceed ~30-40 sections) in a single parallel round; in line with the drafters' 25-50
# worker pools. The base Responses helper retries transient provider errors.
STRUCTURE_SECTION_MAX_WORKERS = 40


class _HeadingLine(BaseModel):
    line: int


class _HeadingSelection(BaseModel):
    headings: list[_HeadingLine]


class _BlockRange(BaseModel):
    type: Literal["heading", "paragraph"]
    start_line: int
    end_line: int
    paragraph_number: str | None
    level: int | None


class _SectionBlocks(BaseModel):
    blocks: list[_BlockRange]


def _parallel_map(items: list, worker: Callable, max_workers: int = STRUCTURE_SECTION_MAX_WORKERS) -> list:
    """Run worker(item) across a thread pool and return results in input order.

    On a per-item exception the result is None (callers substitute a fallback). Mirrors the
    ordered-results pattern used in everything.py's threaded file extraction. Used to
    parallelize the independent per-section structuring LLM calls.
    """
    items = list(items or [])
    if not items:
        return []
    if len(items) == 1:
        try:
            return [worker(items[0])]
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            return [None]
    results = [None] * len(items)
    workers = min(max(len(items), 1), max_workers)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers)
    timed_out = False
    try:
        future_map = {
            pool.submit(copy_context().run, worker, item): idx
            for idx, item in enumerate(items)
        }
        for fut in concurrent.futures.as_completed(future_map):
            idx = future_map[fut]
            try:
                results[idx] = fut.result()
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                print(f"_parallel_map: item {idx} failed: {exc}")
                results[idx] = None
    except SoftTimeLimitExceeded:
        # Do not wait for other LLM calls after the run's soft limit expires.
        timed_out = True
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        if not timed_out:
            pool.shutdown(wait=True)
    return results

_PAGE_ARTIFACT_RE = re.compile(r"^\s*==\s*End of page\s+\d+\s*==\s*$", re.I)
_BOLD_LINE_RE = re.compile(r"^\s*<B>(.*?)</B>\s*$", re.I | re.S)
_MARKDOWN_BOLD_LINE_RE = re.compile(r"^\s*\*\*(.*?)\*\*\s*$", re.S)
_MARKDOWN_HASH_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s*(?=\S)(.*?)\s*$")
_NUMBERED_HEADING_RE = re.compile(
    r"^\s*(?:[IVXLCDM]{1,8}\.|[A-Z]\.|\d{1,3}\.)\s+\S+",
    re.I,
)
_PAREN_NUM_RE = re.compile(r"^\s*\((\d{1,4})\)\s+")
_PAREN_LETTER_RE = re.compile(r"^\s*\(([A-Za-z])\)\s+")
_PARA_SYMBOL_NUM_RE = re.compile(r"^\s*\u00b6\s*(\d{1,4})(?:[\.)])?\s+")
_NUM_DOT_RE = re.compile(r"^\s*(\d{1,4})[\.)]\s+")
_NUM_SPACE_RE = re.compile(r"^\s*(\d{1,4})\s+[A-Z]")
_LETTER_DOT_RE = re.compile(r"^\s*([A-Z])\.\s+")


def _strip_structure_markup(text: str) -> str:
    """Remove parser-only structure markers from one line."""
    text = str(text or "").strip()
    bold_match = _BOLD_LINE_RE.match(text)
    if bold_match:
        text = bold_match.group(1).strip()
    markdown_bold_match = _MARKDOWN_BOLD_LINE_RE.match(text)
    if markdown_bold_match:
        text = markdown_bold_match.group(1).strip()
    return text.replace("<B>", "").replace("</B>", "").strip()


def _is_standalone_bold_line(text: str) -> bool:
    return bool(_MARKDOWN_BOLD_LINE_RE.match(str(text or "").strip()))


def _normalize_hash_heading_line(line: str) -> str:
    """Convert Markdown heading markers to the draft's strict bold-line format."""
    match = _MARKDOWN_HASH_HEADING_RE.match(line)
    if not match:
        return line.rstrip()
    heading_text = match.group(1).strip()
    # Remove optional closing hashes used by ATX headings ("## Heading ##").
    heading_text = re.sub(r"\s+#+\s*$", "", heading_text).strip()
    if not heading_text:
        return line.rstrip()
    return f"**{heading_text}**"


def normalize_section_content_markdown(section_content: str) -> str:
    """Normalize generated draft body Markdown to bold headings and italic case names only.

    The frontend and Word exporter both handle ``**bold**`` and ``*italic*`` reliably. Hash
    headings are accepted from older drafts or imperfect LLM output, then converted into
    standalone bold lines with blank lines around them.
    """
    if section_content is None:
        return ""
    text = str(section_content).replace("\r\n", "\n").replace("\r", "\n")
    if not text:
        return ""

    normalized_lines = [_normalize_hash_heading_line(line).strip() if line.strip() else "" for line in text.split("\n")]

    output: list[str] = []
    for idx, line in enumerate(normalized_lines):
        if not line:
            if output and output[-1] != "":
                output.append("")
            continue

        if _is_standalone_bold_line(line):
            if output and output[-1] != "":
                output.append("")
            output.append(line)
            next_lines = normalized_lines[idx + 1 :]
            next_nonempty = next((next_line for next_line in next_lines if next_line), "")
            if next_nonempty and output[-1] != "":
                output.append("")
            continue

        output.append(line)

    while output and output[0] == "":
        output.pop(0)
    while output and output[-1] == "":
        output.pop()
    return "\n".join(output)


def normalize_heading_block_text(text: str) -> str:
    """Return plain heading text for a heading block, stripping supported display markers."""
    normalized = normalize_section_content_markdown(str(text or "").strip()).strip()
    bold_match = _MARKDOWN_BOLD_LINE_RE.match(normalized)
    if bold_match:
        return bold_match.group(1).strip()
    return normalized


def _is_page_artifact(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(_PAGE_ARTIFACT_RE.match(stripped)) or stripped == "<CoverPage>"


def line_records_from_text(raw_text: str) -> list[dict]:
    """Return 1-based line records from parsed upload text."""
    records = []
    for index, raw_line in enumerate((raw_text or "").splitlines(), start=1):
        records.append({
            "line": index,
            "raw_text": raw_line,
            "text": _strip_structure_markup(raw_line),
            "is_artifact": _is_page_artifact(raw_line),
        })
    return records


def _has_alpha(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]", text or ""))


def _is_all_caps_heading(text: str) -> bool:
    stripped = str(text or "").strip()
    if not stripped or len(stripped) > 180 or not _has_alpha(stripped):
        return False
    letters = [ch for ch in stripped if ch.isalpha()]
    if not letters:
        return False
    upper_ratio = sum(1 for ch in letters if ch.isupper()) / len(letters)
    return upper_ratio > 0.8


def _is_heading_candidate_record(record: dict) -> tuple[bool, str]:
    raw_text = str(record.get("raw_text") or "").strip()
    text = str(record.get("text") or "").strip()
    if record.get("is_artifact") or not text:
        return False, ""
    if _BOLD_LINE_RE.match(raw_text):
        return True, "bold_line"
    if _MARKDOWN_BOLD_LINE_RE.match(raw_text):
        return True, "bold_line"
    if _is_all_caps_heading(text):
        return True, "all_caps"
    if _NUMBERED_HEADING_RE.match(text) and len(text) <= 220:
        return True, "numbered_heading"
    return False, ""


def detect_heading_candidates(lines: list[dict]) -> list[dict]:
    """Detect raw heading candidates from parsed text lines."""
    candidates = []
    for record in lines or []:
        is_candidate, reason = _is_heading_candidate_record(record)
        if not is_candidate:
            continue
        candidates.append({
            "line": record.get("line"),
            "text": record.get("text", ""),
            "reason": reason,
        })
    return candidates


def _lines_by_number(lines: list[dict]) -> dict[int, dict]:
    return {
        int(record["line"]): record
        for record in lines or []
        if isinstance(record, dict) and isinstance(record.get("line"), int)
    }


def _non_artifact_nonempty_lines(lines: list[dict]) -> list[int]:
    return [
        int(record["line"])
        for record in lines or []
        if not record.get("is_artifact") and str(record.get("text") or "").strip()
    ]


def _json_object(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}


def _safe_int(value, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _call_llm_json(
    llm_json_fn: Callable | None,
    prompt: str,
    model: str,
    *,
    response_model: type[BaseModel],
    reasoning_effort: str | None,
) -> dict:
    if llm_json_fn is None:
        parsed = chat_completion_parse(
            prompt,
            response_model,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        return parsed.model_dump() if parsed is not None else {}
    try:
        raw = llm_json_fn(prompt=prompt, model=model)
    except TypeError:
        try:
            raw = llm_json_fn(prompt, model=model)
        except TypeError:
            raw = llm_json_fn(prompt)
    return _json_object(raw)


def _context_for_line(lines_by_no: dict[int, dict], line_no: int, radius: int = 2) -> str:
    context = []
    for current in range(line_no - radius, line_no + radius + 1):
        record = lines_by_no.get(current)
        if not record or record.get("is_artifact"):
            continue
        text = str(record.get("text") or "").strip()
        if text:
            context.append(f"{current}: {text}")
    return "\n".join(context)


def _select_headings_with_llm(
    lines: list[dict],
    candidates: list[dict],
    *,
    model: str,
    reasoning_effort: str | None,
    llm_json_fn: Callable | None,
) -> list[dict]:
    if not candidates:
        return []

    lines_by_no = _lines_by_number(lines)
    candidate_payload = []
    for candidate in candidates:
        line_no = int(candidate.get("line"))
        candidate_payload.append({
            "line": line_no,
            "text": candidate.get("text", ""),
            "reason": candidate.get("reason", ""),
            "context": _context_for_line(lines_by_no, line_no),
        })

    prompt = f"""
You are identifying top-level section headings in a parsed legal document.

Candidate heading lines with nearby context:
{json.dumps(candidate_payload, ensure_ascii=False, indent=2)}

Return JSON only:
{{
  "headings": [
    {{"line": 12}}
  ]
}}

Rules:
- Select only real section headings.
- Use only exact line numbers from the candidates.
- Do not invent headings or line numbers.
- If none are reliable, return an empty headings list.
"""
    parsed = _call_llm_json(
        llm_json_fn,
        prompt,
        model,
        response_model=_HeadingSelection,
        reasoning_effort=reasoning_effort,
    )
    return _normalize_selected_headings(parsed, candidates)


def _normalize_selected_headings(parsed: dict, candidates: list[dict]) -> list[dict]:
    valid_by_line = {
        int(candidate["line"]): candidate
        for candidate in candidates
        if isinstance(candidate.get("line"), int)
    }
    selected_lines: list[int] = []

    raw_headings = parsed.get("headings", [])
    if isinstance(raw_headings, list):
        for item in raw_headings:
            if isinstance(item, dict):
                value = item.get("line", item.get("line_number", item.get("start_line")))
            else:
                value = item
            try:
                selected_lines.append(int(value))
            except Exception:
                continue
    elif isinstance(raw_headings, str):
        selected_lines.extend(int(match) for match in re.findall(r"\((\d+)\)", raw_headings))

    raw_heading_lines = parsed.get("heading_lines", [])
    if isinstance(raw_heading_lines, list):
        for value in raw_heading_lines:
            try:
                selected_lines.append(int(value))
            except Exception:
                continue

    deduped = []
    seen = set()
    for line_no in sorted(selected_lines):
        if line_no in seen or line_no not in valid_by_line:
            continue
        seen.add(line_no)
        candidate = valid_by_line[line_no]
        deduped.append({"line": line_no, "text": candidate.get("text", "")})
    return deduped


def build_section_ranges(lines: list[dict], selected_headings: list[dict]) -> list[dict]:
    """Build internal section ranges from selected heading lines."""
    content_lines = _non_artifact_nonempty_lines(lines)
    if not content_lines:
        return []

    lines_by_no = _lines_by_number(lines)
    first_line = min(content_lines)
    last_line = max(content_lines)
    heading_lines = []
    for heading in selected_headings or []:
        try:
            line_no = int(heading.get("line"))
        except Exception:
            continue
        if first_line <= line_no <= last_line and line_no in lines_by_no:
            heading_lines.append(line_no)
    heading_lines = sorted(set(heading_lines))

    if not heading_lines:
        return [{
            "section_id": "s_0001",
            "section_name": "",
            "level": 1,
            "start_line": first_line,
            "content_start_line": first_line,
            "end_line": last_line,
        }]

    ranges = []
    section_index = 1
    if first_line < heading_lines[0]:
        ranges.append({
            "section_id": f"s_{section_index:04d}",
            "section_name": "",
            "level": 1,
            "start_line": first_line,
            "content_start_line": first_line,
            "end_line": heading_lines[0] - 1,
        })
        section_index += 1

    for idx, heading_line in enumerate(heading_lines):
        next_heading = heading_lines[idx + 1] if idx + 1 < len(heading_lines) else last_line + 1
        ranges.append({
            "section_id": f"s_{section_index:04d}",
            "section_name": lines_by_no[heading_line].get("text", ""),
            "level": 1,
            "start_line": heading_line,
            "content_start_line": heading_line + 1,
            "end_line": next_heading - 1,
        })
        section_index += 1
    return ranges


def _records_in_range(lines: list[dict], start_line: int, end_line: int) -> list[dict]:
    return [
        record
        for record in lines or []
        if start_line <= int(record.get("line", -1)) <= end_line
    ]


def _join_source_lines(records: list[dict]) -> str:
    parts = [
        str(record.get("text") or "").strip()
        for record in records
        if not record.get("is_artifact") and str(record.get("text") or "").strip()
    ]
    return re.sub(r"\s+", " ", " ".join(parts)).strip()


def assemble_block_text(lines: list[dict], start_line: int, end_line: int) -> str:
    """Assemble source text from inclusive line range."""
    return _join_source_lines(_records_in_range(lines, int(start_line), int(end_line)))


def detect_paragraph_number(text: str) -> str | None:
    """Return the source paragraph marker at the start of text, if present."""
    value = str(text or "")
    for pattern in (_PARA_SYMBOL_NUM_RE, _PAREN_NUM_RE, _NUM_DOT_RE, _PAREN_LETTER_RE, _LETTER_DOT_RE):
        match = pattern.match(value)
        if match:
            return match.group(1)
    match = _NUM_SPACE_RE.match(value)
    if match:
        number = int(match.group(1))
        if 0 < number < 1000:
            return str(number)
    return None


def _is_strict_number_line(text: str) -> bool:
    return bool(re.match(r"^\s*(?:\u00b6\s*)?(?:\d{1,4}|[A-Za-z])[\.)]?\s*$", str(text or "")))


def _is_numbered_paragraph_start_line(text: str) -> bool:
    stripped = str(text or "").strip()
    return bool(
        _PARA_SYMBOL_NUM_RE.match(stripped)
        or _PAREN_NUM_RE.match(stripped)
        or _NUM_DOT_RE.match(stripped)
    )


def _is_subheading_line(record: dict) -> bool:
    is_candidate, reason = _is_heading_candidate_record(record)
    return is_candidate and reason in {"bold_line", "all_caps", "numbered_heading"}


def _fallback_blocks_for_range(lines: list[dict], start_line: int, end_line: int) -> list[dict]:
    records = _records_in_range(lines, start_line, end_line)
    blocks = []
    current_start = None
    current_end = None

    def flush_paragraph():
        nonlocal current_start, current_end
        if current_start is not None and current_end is not None:
            blocks.append({"type": "paragraph", "start_line": current_start, "end_line": current_end})
        current_start = None
        current_end = None

    for record in records:
        line_no = int(record.get("line"))
        text = str(record.get("text") or "").strip()
        if record.get("is_artifact"):
            continue
        if not text:
            flush_paragraph()
            continue
        if _is_subheading_line(record):
            flush_paragraph()
            blocks.append({"type": "heading", "start_line": line_no, "end_line": line_no, "level": 2})
            continue
        if current_start is not None and _is_numbered_paragraph_start_line(text):
            flush_paragraph()
        if current_start is None:
            current_start = line_no
        current_end = line_no

    flush_paragraph()
    return blocks


def _section_lines_text(lines: list[dict], start_line: int, end_line: int) -> str:
    rendered = []
    for record in _records_in_range(lines, start_line, end_line):
        if record.get("is_artifact"):
            continue
        rendered.append(f"{record.get('line')}: {record.get('text', '')}")
    return "\n".join(rendered)


def _llm_blocks_for_section(
    lines: list[dict],
    section_range: dict,
    *,
    model: str,
    reasoning_effort: str | None,
    llm_json_fn: Callable | None,
) -> list[dict]:
    start_line = int(section_range.get("content_start_line", section_range.get("start_line", 1)))
    end_line = int(section_range.get("end_line", start_line - 1))
    if end_line < start_line:
        return []

    prompt = f"""
You are identifying paragraph and subsection-heading ranges in a legal document section.

Section name: {section_range.get("section_name", "")}
Line-numbered source text:
{_section_lines_text(lines, start_line, end_line)}

Return JSON only:
{{
  "blocks": [
    {{"type": "paragraph", "start_line": 10, "end_line": 14, "paragraph_number": null, "level": null}},
    {{"type": "heading", "start_line": 15, "end_line": 15, "paragraph_number": null, "level": 2}}
  ]
}}

Rules:
- Use only "heading" and "paragraph" block types.
- Return line ranges only. Do not rewrite or summarize text.
- Cover all non-empty body lines in this section.
- Exclude page artifacts.
- Preserve printed paragraph numbers in paragraph_number when visible.
"""
    parsed = _call_llm_json(
        llm_json_fn,
        prompt,
        model,
        response_model=_SectionBlocks,
        reasoning_effort=reasoning_effort,
    )
    raw_blocks = parsed.get("blocks", [])
    if not isinstance(raw_blocks, list):
        return []
    return raw_blocks


def _normalize_range_blocks(raw_blocks: list[dict], start_line: int, end_line: int) -> list[dict]:
    normalized = []
    for raw in raw_blocks or []:
        if not isinstance(raw, dict):
            continue
        try:
            block_start = int(raw.get("start_line"))
            block_end = int(raw.get("end_line", block_start))
        except Exception:
            continue
        if block_end < block_start:
            block_start, block_end = block_end, block_start
        block_start = max(block_start, start_line)
        block_end = min(block_end, end_line)
        if block_start > block_end:
            continue
        block_type = raw.get("type") if raw.get("type") in BLOCK_TYPES else "paragraph"
        normalized.append({
            "type": block_type,
            "start_line": block_start,
            "end_line": block_end,
            "level": int(raw.get("level", 2) or 2) if block_type == "heading" else None,
            "paragraph_number": raw.get("paragraph_number") if block_type == "paragraph" else None,
        })
    return normalized


def _repair_coverage(lines: list[dict], blocks: list[dict], start_line: int, end_line: int) -> list[dict]:
    coverage_lines = [
        int(record.get("line"))
        for record in _records_in_range(lines, start_line, end_line)
        if not record.get("is_artifact") and str(record.get("text") or "").strip()
    ]
    if not coverage_lines:
        return []

    clean_blocks = []
    last_end = start_line - 1
    for block in sorted(blocks or [], key=lambda item: (item.get("start_line", 0), item.get("end_line", 0))):
        block_start = max(int(block["start_line"]), start_line)
        block_end = min(int(block["end_line"]), end_line)
        if block_start <= last_end:
            block_start = last_end + 1
        if block_start > block_end:
            continue
        if last_end + 1 < block_start:
            clean_blocks.extend(_fallback_blocks_for_range(lines, last_end + 1, block_start - 1))
        new_block = dict(block)
        new_block["start_line"] = block_start
        new_block["end_line"] = block_end
        clean_blocks.append(new_block)
        last_end = block_end

    if last_end < end_line:
        clean_blocks.extend(_fallback_blocks_for_range(lines, last_end + 1, end_line))

    covered = set()
    for block in clean_blocks:
        covered.update(range(int(block["start_line"]), int(block["end_line"]) + 1))
    missing = [line_no for line_no in coverage_lines if line_no not in covered]
    for line_no in missing:
        clean_blocks.extend(_fallback_blocks_for_range(lines, line_no, line_no))

    return sorted(clean_blocks, key=lambda item: (item.get("start_line", 0), item.get("end_line", 0)))


def _split_multinumber_paragraphs(lines: list[dict], block: dict) -> list[dict]:
    if block.get("type") != "paragraph":
        return [block]
    records = [
        record
        for record in _records_in_range(lines, int(block["start_line"]), int(block["end_line"]))
        if not record.get("is_artifact") and str(record.get("text") or "").strip()
    ]
    split_points = [
        int(record["line"])
        for idx, record in enumerate(records)
        if idx > 0 and _is_numbered_paragraph_start_line(record.get("text", ""))
    ]
    if not split_points:
        return [block]

    ranges = []
    segment_start = int(block["start_line"])
    for split_line in split_points:
        ranges.append({"type": "paragraph", "start_line": segment_start, "end_line": split_line - 1})
        segment_start = split_line
    ranges.append({"type": "paragraph", "start_line": segment_start, "end_line": int(block["end_line"])})
    return ranges


def validate_and_repair_paragraph_numbers(blocks: list[dict], source_lines: list[dict] | None = None) -> list[dict]:
    """Repair paragraph-number boundaries and recompute paragraph indexes."""
    source_lines = source_lines or []
    repaired = []
    for block in blocks or []:
        repaired.extend(_split_multinumber_paragraphs(source_lines, block))

    merged = []
    idx = 0
    while idx < len(repaired):
        block = repaired[idx]
        text = assemble_block_text(source_lines, int(block["start_line"]), int(block["end_line"]))
        if (
            block.get("type") == "paragraph"
            and _is_strict_number_line(text)
            and idx + 1 < len(repaired)
            and repaired[idx + 1].get("type") == "paragraph"
        ):
            next_block = dict(repaired[idx + 1])
            next_block["start_line"] = int(block["start_line"])
            repaired[idx + 1] = next_block
            idx += 1
            continue
        merged.append(block)
        idx += 1

    final_blocks = []
    paragraph_index = 1
    for block in merged:
        block_type = block.get("type") if block.get("type") in BLOCK_TYPES else "paragraph"
        text = assemble_block_text(source_lines, int(block["start_line"]), int(block["end_line"]))
        if not text:
            continue
        output = {
            "block_id": f"b_{len(final_blocks) + 1:04d}",
            "type": block_type,
            "text": text,
        }
        if block_type == "heading":
            output["level"] = int(block.get("level", 2) or 2)
        else:
            output["paragraph_index"] = paragraph_index
            output["paragraph_number"] = detect_paragraph_number(text)
            paragraph_index += 1
        final_blocks.append(output)
    return final_blocks


def assemble_section_content(blocks: list[dict]) -> str:
    """Assemble compatibility section_content from block list.

    Heading blocks are emitted as standalone bold lines so they render reliably in the frontend
    and export as centered bold paragraph headers in Word.
    """
    parts = []
    for block in blocks or []:
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        if block.get("type") == "heading":
            text = normalize_heading_block_text(text)
            parts.append(f"**{text}**")
        elif block.get("type") == "paragraph":
            parts.append(text)
    return normalize_section_content_markdown("\n\n".join(parts))


def normalize_existing_blocks(blocks: list[dict]) -> list[dict]:
    """Return valid section-local blocks with repaired paragraph metadata."""
    normalized = []
    paragraph_index = 1
    for raw in blocks or []:
        if not isinstance(raw, dict):
            continue
        block_type = raw.get("type") if raw.get("type") in BLOCK_TYPES else None
        text = str(raw.get("text") or "").strip()
        if block_type == "heading":
            text = normalize_heading_block_text(text)
        if not block_type or not text:
            continue
        output = {
            "block_id": str(raw.get("block_id") or f"b_{len(normalized) + 1:04d}"),
            "type": block_type,
            "text": text,
        }
        if block_type == "heading":
            try:
                output["level"] = int(raw.get("level", 2) or 2)
            except Exception:
                output["level"] = 2
        else:
            output["paragraph_index"] = paragraph_index
            output["paragraph_number"] = detect_paragraph_number(text)
            paragraph_index += 1
        normalized.append(output)
    return normalized


def section_blocks_match_content(blocks: list[dict], section_content: str) -> bool:
    """Return true when blocks are valid and assemble to the section content."""
    normalized = normalize_existing_blocks(blocks)
    if not normalized:
        return False
    content = str(section_content or "").strip()
    return assemble_section_content(normalized).strip() == content


def sanitize_draft_structure(draft: dict) -> dict:
    """Cheaply clean a draft for storage without making LLM calls.

    Valid blocks are preserved. Stale blocks are dropped so they can be rebuilt by
    lazy phase-2 normalization after a user or frontend edit changes section text.
    """
    from backend.util.document_drafts import is_markdown_draft, sanitize_markdown_draft
    if is_markdown_draft(draft):
        return sanitize_markdown_draft(draft)
    if not isinstance(draft, dict):
        draft = {}

    cleaned_sections = []
    raw_sections = draft.get("sections", [])
    if not isinstance(raw_sections, list):
        raw_sections = []

    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            continue
        section_name = str(raw_section.get("section_name") or "").strip()
        section_content = normalize_section_content_markdown(raw_section.get("section_content") or "")
        blocks = normalize_existing_blocks(raw_section.get("blocks", []))
        if blocks:
            assembled = normalize_section_content_markdown(assemble_section_content(blocks)).strip()
            if section_content and assembled != section_content:
                blocks = []
            else:
                section_content = assembled

        if not section_name and not section_content and not blocks:
            continue

        section = {
            "section_id": str(raw_section.get("section_id") or f"{len(cleaned_sections) + 1}"),
            "section_name": section_name,
            "level": _safe_int(raw_section.get("level", 1) or 1, 1),
            "section_content": section_content,
        }
        if blocks:
            section["blocks"] = blocks
        cleaned_sections.append(section)

    return {
        "title": str(draft.get("title") or ""),
        "sections": cleaned_sections,
    }


def draft_needs_normalization(draft: dict) -> bool:
    """Return true when a substantive draft has sections without valid blocks."""
    from backend.util.document_drafts import is_markdown_draft
    if is_markdown_draft(draft):
        return False
    sanitized = sanitize_draft_structure(draft)
    for section in sanitized.get("sections", []):
        if not section.get("blocks") and (
            str(section.get("section_name") or "").strip()
            or str(section.get("section_content") or "").strip()
        ):
            return True
    return False


def structure_section_content(
    section_content: str,
    *,
    section_name: str = "",
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = None,
    llm_json_fn: Callable | None = None,
) -> list[dict]:
    """Structure one existing section body into paragraph/heading blocks."""
    lines = line_records_from_text(section_content or "")
    content_lines = _non_artifact_nonempty_lines(lines)
    if not content_lines:
        return []
    section_range = {
        "section_name": section_name or "",
        "start_line": min(content_lines),
        "content_start_line": min(content_lines),
        "end_line": max(content_lines),
    }
    return _structure_blocks_for_section(
        lines,
        section_range,
        model=model,
        reasoning_effort=reasoning_effort,
        llm_json_fn=llm_json_fn,
    )


def normalize_draft_structure(
    draft: dict,
    *,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = None,
    llm_json_fn: Callable | None = None,
) -> dict:
    """Normalize an existing structured draft into sections with blocks.

    Existing valid blocks are preserved. Missing or stale blocks are rebuilt from
    section_content while preserving section_id and section_name.
    """
    from backend.util.document_drafts import is_markdown_draft
    sanitized = sanitize_draft_structure(draft)
    if is_markdown_draft(sanitized):
        return sanitized
    input_sections = sanitized.get("sections", [])

    def _blocks_for_section(section: dict) -> list:
        blocks = section.get("blocks", [])
        if not blocks and str(section.get("section_content") or "").strip():
            blocks = structure_section_content(
                section.get("section_content", ""),
                section_name=section.get("section_name", ""),
                model=model,
                reasoning_effort=reasoning_effort,
                llm_json_fn=llm_json_fn,
            )
        return blocks

    # The per-section block structuring (one LLM call each when blocks are missing) is
    # independent across sections, so fan it out across a thread pool. Section id assignment
    # and the empty-section filter stay serial below to preserve exact ordering/numbering.
    blocks_by_index = _parallel_map(input_sections, _blocks_for_section)

    normalized_sections = []
    for idx, section in enumerate(input_sections):
        blocks = blocks_by_index[idx]
        if blocks is None:
            blocks = section.get("blocks", [])
        normalized_section = {
            "section_id": section.get("section_id") or f"{len(normalized_sections) + 1}",
            "section_name": section.get("section_name", ""),
            "level": _safe_int(section.get("level", 1) or 1, 1),
            "blocks": blocks,
            "section_content": assemble_section_content(blocks) if blocks else section.get("section_content", ""),
        }
        if (
            normalized_section.get("section_name")
            or normalized_section.get("section_content")
            or normalized_section.get("blocks")
        ):
            normalized_sections.append(normalized_section)

    return {
        "title": sanitized.get("title", ""),
        "sections": normalized_sections,
    }


def validate_draft_structure(structure: dict, source_lines: list[dict]) -> dict:
    """Return validation warnings for a final draft structure."""
    warnings = []
    valid_block_types = True
    paragraph_numbers = []

    for section in structure.get("sections", []) if isinstance(structure, dict) else []:
        seen_numbers = set()
        expected_index = 1
        for block in section.get("blocks", []):
            if block.get("type") not in BLOCK_TYPES:
                valid_block_types = False
                warnings.append(f"invalid block type: {block.get('type')}")
            if block.get("type") == "paragraph":
                if block.get("paragraph_index") != expected_index:
                    warnings.append("paragraph_index sequence repaired or inconsistent")
                expected_index += 1
                number = block.get("paragraph_number")
                if number:
                    if number in seen_numbers:
                        warnings.append(f"repeated paragraph_number: {number}")
                    seen_numbers.add(number)
                    paragraph_numbers.append(number)
        if assemble_section_content(section.get("blocks", [])) != section.get("section_content", ""):
            warnings.append("section_content does not match assembled blocks")

    return {
        "valid": valid_block_types and not any("section_content" in warning for warning in warnings),
        "warnings": warnings,
        "paragraph_numbers": paragraph_numbers,
        "source_non_artifact_lines": len(_non_artifact_nonempty_lines(source_lines)),
    }


def _structure_blocks_for_section(
    lines: list[dict],
    section_range: dict,
    *,
    model: str,
    reasoning_effort: str | None,
    llm_json_fn: Callable | None,
) -> list[dict]:
    start_line = int(section_range.get("content_start_line", section_range.get("start_line", 1)))
    end_line = int(section_range.get("end_line", start_line - 1))
    if end_line < start_line:
        return []

    raw_blocks = _llm_blocks_for_section(
        lines,
        section_range,
        model=model,
        reasoning_effort=reasoning_effort,
        llm_json_fn=llm_json_fn,
    )
    normalized = _normalize_range_blocks(raw_blocks, start_line, end_line)
    if not normalized:
        normalized = _fallback_blocks_for_range(lines, start_line, end_line)
    covered = _repair_coverage(lines, normalized, start_line, end_line)
    return validate_and_repair_paragraph_numbers(covered, lines)


def structure_draft_text(
    raw_text: str,
    *,
    title: str = "",
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = None,
    llm_json_fn: Callable | None = None,
) -> dict:
    """Structure parsed draft text into sections and paragraph-level blocks."""
    lines = line_records_from_text(raw_text)
    candidates = detect_heading_candidates(lines)
    selected_headings = _select_headings_with_llm(
        lines,
        candidates,
        model=model,
        reasoning_effort=reasoning_effort,
        llm_json_fn=llm_json_fn,
    )
    section_ranges = build_section_ranges(lines, selected_headings)

    # Per-section block structuring is independent across sections, so fan the LLM calls out
    # across a thread pool; results are reassembled strictly in input order below.
    blocks_by_index = _parallel_map(
        section_ranges,
        lambda section_range: _structure_blocks_for_section(
            lines,
            section_range,
            model=model,
            reasoning_effort=reasoning_effort,
            llm_json_fn=llm_json_fn,
        ),
    )

    sections = []
    for section_index, section_range in enumerate(section_ranges, start=1):
        blocks = blocks_by_index[section_index - 1] or []
        section_id = section_range.get("section_id") or f"s_{section_index:04d}"
        section = {
            "section_id": section_id,
            "section_name": section_range.get("section_name", ""),
            "level": int(section_range.get("level", 1) or 1),
            "blocks": blocks,
            "section_content": assemble_section_content(blocks),
        }
        if section.get("section_name") or section.get("section_content") or section.get("blocks"):
            sections.append(section)

    return {
        "title": title or "",
        "sections": sections,
    }


def structure_draft_file(
    file_path: str | Path,
    *,
    title: str | None = None,
    model: str = DEFAULT_MODEL,
    reasoning_effort: str | None = None,
    max_lines: int | None = None,
    pdf_ocr_backend: str | None = None,
    llm_json_fn: Callable | None = None,
) -> dict:
    """Parse a source file and structure it with the real LLM by default."""
    path = Path(file_path)
    if pdf_ocr_backend:
        import backend.constants as constants

        constants.PDF_OCR_BACKEND = pdf_ocr_backend

    from backend.util.data_tools import get_text_from_extended_formats

    parsed_text, metadata, is_image = get_text_from_extended_formats(str(path))
    if max_lines is not None and max_lines > 0:
        parsed_text = "\n".join(parsed_text.splitlines()[:max_lines])

    result = structure_draft_text(
        parsed_text,
        title=title if title is not None else path.stem,
        model=model,
        reasoning_effort=reasoning_effort,
        llm_json_fn=llm_json_fn,
    )
    result["source"] = {
        "path": str(path),
        "metadata": metadata,
        "is_image": is_image,
        "max_lines": max_lines,
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse a draft file and print structured JSON using the real LLM.",
    )
    parser.add_argument("file_path", help="Path to the uploaded draft source file.")
    parser.add_argument("--title", default=None, help="Optional title for the structured draft.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"LLM model to use. Default: {DEFAULT_MODEL}.")
    parser.add_argument(
        "--max-lines",
        type=int,
        default=None,
        help="Optional line limit for quicker and cheaper live testing.",
    )
    parser.add_argument(
        "--pdf-ocr-backend",
        choices=("local", "cloud"),
        default=None,
        help="Optional override for backend.constants.PDF_OCR_BACKEND before parsing PDFs.",
    )
    parser.add_argument("--indent", type=int, default=2, help="JSON indentation. Default: 2.")
    parser.add_argument("--output", default=None, help="Optional path to write the JSON instead of printing.")
    args = parser.parse_args(argv)

    result = structure_draft_file(
        args.file_path,
        title=args.title,
        model=args.model,
        max_lines=args.max_lines,
        pdf_ocr_backend=args.pdf_ocr_backend,
    )
    rendered = json.dumps(result, indent=args.indent, ensure_ascii=False)

    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
