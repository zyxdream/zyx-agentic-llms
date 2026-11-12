import json
import logging
import os
import re
import uuid
from html import escape
from importlib import import_module
from pathlib import Path
from typing import Any

try:
    shared_task = getattr(import_module("celery"), "shared_task")
    SoftTimeLimitExceeded = getattr(import_module("celery.exceptions"), "SoftTimeLimitExceeded")
except ImportError:  # pragma: no cover - used only in stripped-down test envs
    class SoftTimeLimitExceeded(Exception):
        pass

    def shared_task(*_args, **_kwargs):
        def _decorator(func):
            return func

        return _decorator

from backend.database import db
from backend.ai import base as ai_base
from backend.models import DocumentChunk, ChatThread, UploadedFiles
from backend.util.analytics import guided_step_operation, submit_with_context
from backend.ai.complaint_parser import get_last_ocr_warning, get_pagenum_from_pdf_fitz
from backend.util.chunker import chunk_text
from backend.util.data_tools import extract_from_json, get_complaint_text_from_file
from backend.util.db import DBMemory
from backend.util.freelaw_cache import (
    _normalize_cluster_id,
    _snapshot_from_cache_row,
    build_freelaw_cluster_cache_snapshot,
    get_freelaw_cluster_cache,
    prune_old_freelaw_cluster_cache_rows,
    record_freelaw_cluster_cache_hit,
    upsert_freelaw_cluster_cache,
)
from backend.util.free_law_tools import (
    LOOKUP_TAG_AMBIGUOUS_CLUSTER,
    LOOKUP_TAG_CASE_NAME_MISMATCH,
    LOOKUP_TAG_CITE_MISMATCH,
    LOOKUP_TAG_API_ERROR_OR_PARTIAL,
    LOOKUP_TAG_LACKS_OPINION_DATA,
    LOOKUP_TAG_NO_CITATION_PARSED,
    LOOKUP_TAG_VERIFIED_FL,
    LOOKUP_TAGS_ELIGIBLE_FOR_LINKS,
    LOOKUP_TAGS_VALIDATED_CASES,
    build_caseviewer_href,
    extract_holding_from_opinion,
    extract_validated_cases_from_citations,
    get_cluster,
    get_cluster_opinions,
    get_court,
    get_docket,
    get_opinion,
    normalize_text_for_citation_lookup,
    verify_citations_with_lookup_tags_lookup_safe,
)


logger = logging.getLogger(__name__)

DOC_TYPE = "Hallucination Guard"
RUN_STEP = 0
STANDALONE_HG_DRAFT_TYPE = 9
STANDALONE_HG_MAX_FILE_SIZE = 15 * 1024 * 1024
STANDALONE_HG_ALLOWED_EXTENSIONS = {"txt", "pdf", "doc", "docx"}
STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY = "standalone_hg/active_upload_task_id"
STANDALONE_HG_ACTIVE_HG_TASK_KEY = "standalone_hg/active_hg_task_id"
LOCAL_CASES_HALLUCINATION_PROMPT_PATH = Path("backend/ai/prompts/hg/HALLUCINATION_GUARD_CASES")
LOCAL_STATUTES_PROMPT_PATH = Path("backend/ai/prompts/hg/HALLUCINATION_GUARD_STATUTES")
LOCAL_CITATION_WEBSEARCH_PROMPT_PATH = Path("backend/ai/prompts/hg/HALLUCINATION_GUARD_CITATION_WEBSEARCH")
HG_ANALYSIS_MODEL = ai_base.DEFAULT_MODEL
HG_ANALYSIS_REASONING_EFFORT = "high"
# Cap how many unvalidated citations we send to the (per-citation, web-enabled) follow-up pass.
WEB_SEARCH_MAX_CITATIONS = 25
WEB_SEARCH_MAX_WORKERS = 4


def standalone_hg_allowed_file(filename: str | None) -> bool:
    return bool(
        filename
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in STANDALONE_HG_ALLOWED_EXTENSIONS
    )


def _delete_memory_key(mem: DBMemory, key: str) -> None:
    try:
        del mem[key]
    except Exception:
        pass


def _reset_standalone_hg_validated_cases(mem: DBMemory) -> None:
    try:
        from backend.everything import load_state_from_memory, save_state_to_memory

        state = load_state_from_memory(mem)
        state["validated_cases"] = {}
        save_state_to_memory(state, mem, auxiliary_fields=('validated_cases',))
    except Exception:
        pass


def clear_standalone_hg_result_state(mem: DBMemory, *, reset_validated_cases: bool = True) -> None:
    for key in (
        "hallucination_check",
        "standalone_hg/results", "standalone_hg/results_html",
        "standalone_hg/case_results", "standalone_hg/case_results_html",
        "standalone_hg/statute_results", "standalone_hg/statute_results_html",
        "standalone_hg/filtered_citations", "standalone_hg/lookup_diagnostics",
        "standalone_hg/error_message", "standalone_hg/token_limit",
        "standalone_hg/current_tokens", "standalone_hg/prompt_token_counts",
    ):
        _delete_memory_key(mem, key)

    if reset_validated_cases:
        _reset_standalone_hg_validated_cases(mem)


def clear_standalone_hg_active_task(mem: DBMemory, key: str, expected_task_id: str | None = None) -> None:
    current_task_id = mem.get(key, "") or ""
    if expected_task_id and current_task_id and current_task_id != expected_task_id:
        return
    _delete_memory_key(mem, key)


def _current_task_id(task) -> str | None:
    return getattr(getattr(task, "request", None), "id", None)


def get_active_standalone_hg_task_id(doc_id: int, key: str) -> str | None:
    mem = DBMemory(db=db, doc_id=doc_id)
    task_id = (mem.get(key, "") or "").strip()
    if not task_id:
        return None

    try:
        AsyncResult = getattr(import_module("celery.result"), "AsyncResult")
        result = AsyncResult(task_id)
        if result.ready():
            clear_standalone_hg_active_task(mem, key)
            return None
    except Exception:
        # If the broker cannot be reached while rendering state, keep the id so
        # the frontend can still attempt normal /result polling.
        return task_id

    return task_id


def _standalone_hg_uploaded_file_payload(uploaded_file: UploadedFiles) -> dict:
    return {
        "unique_id": uploaded_file.unique_id,
        "filename": uploaded_file.original_filename,
        "file_size": uploaded_file.file_size,
        "upload_time": uploaded_file.upload_time.isoformat() if uploaded_file.upload_time else None,
        "page_count": uploaded_file.page_count,
    }


def _cluster_id(entry: dict) -> Any:
    return entry.get("clusterId") or entry.get("cluster_id") or entry.get("id")


# --- Quote verification (does a quoted passage actually appear in the opinion?) ---
# Design: bias to precision. A false "found" (asserting a fabricated/misattributed
# quote exists) is dangerous; a false "not found" is a safe annoyance. So use exact
# same-opinion anchors first; fallback only ignores punctuation between identical words.

_QUOTE_MAX_OPINION_CHARS = 1_000_000  # cap normalized opinion size for safety
_QUOTE_MIN_SEGMENT_LEN = 12  # ignore trivial ellipsis fragments shorter than this
_QUOTE_FALLBACK_MIN_LEN = 8  # minimum length to match a short single-segment quote
_QUOTE_ELLIPSIS_RE = re.compile(r"\s*(?:\.\s*\.\s*\.|…)\s*")  # "...", ". . .", "…"
_QUOTE_BRACKET_RE = re.compile(r"\[([^\[\]]*)\]")
_QUOTE_BRACKET_ELLIPSIS_RE = re.compile(r"^(?:\.|\s|…)+$")
_QUOTE_PUNCT_SOFT_WORD_RE = re.compile(r"[a-z0-9]+")
_QUOTE_LOG_QUOTE_PREVIEW_CHARS = 240
_QUOTE_LOG_ANCHOR_PREVIEW_CHARS = 180
_QUOTE_LOG_OPINION_PREVIEW_CHARS = 320
_HTML_TAG_RE = re.compile(r"<[^>]+>")
# Drafter-added editorial notes that are NOT part of the opinion text.
_QUOTE_NOISE_PATTERNS = [
    r"\(emphasis added\)",
    r"\(emphasis in (?:the )?original\)",
    r"\(internal quotation marks? omitted\)",
    r"\(internal citations? omitted\)",
    r"\(citations? omitted\)",
    r"\(quotations? omitted\)",
    r"\(quotation marks? omitted\)",
    r"\(cleaned up\)",
    r"\(alterations? in (?:the )?original\)",
    r"\(quoting [^)]*\)",
    r"\(citing [^)]*\)",
]
_QUOTE_NOISE_RE = re.compile("|".join(_QUOTE_NOISE_PATTERNS), re.IGNORECASE)
_SMART_CHAR_MAP = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ",
}


def _smart_chars_to_ascii(s: str) -> str:
    for src, dst in _SMART_CHAR_MAP.items():
        s = s.replace(src, dst)
    return s


def _normalize_for_match(text: str) -> str:
    """Normalize opinion or quote text for verbatim comparison.

    Strips HTML, folds smart quotes/dashes to ASCII, lowercases, then reuses the
    shared citation-lookup normalizer (joins line-wrap hyphenation, collapses
    whitespace). Caps very large opinions for safety.
    """
    if not text:
        return ""
    s = _HTML_TAG_RE.sub(" ", str(text))
    s = _smart_chars_to_ascii(s).lower()
    s = s.replace('"', '')  # quote marks are delimiters, not content — drop on both sides
    s = normalize_text_for_citation_lookup(s)
    if len(s) > _QUOTE_MAX_OPINION_CHARS:
        s = s[:_QUOTE_MAX_OPINION_CHARS]
    return s


def _bracketed_quote_edit_to_match_text(match: re.Match) -> str:
    text = match.string
    start, end = match.span()
    inner = match.group(1) or ""
    prev_char = text[start - 1] if start > 0 else ""
    next_char = text[end] if end < len(text) else ""

    if prev_char.isalpha() or next_char.isalpha():
        return inner
    if _QUOTE_BRACKET_ELLIPSIS_RE.fullmatch(inner.strip()):
        return "..."
    return "..."


def _clean_drafted_quote(quote: str) -> str:
    """Strip wrapping quotes and drafter notes; preserve in-word brackets, wildcard standalone edits."""
    if not quote:
        return ""
    q = _smart_chars_to_ascii(str(quote)).strip()
    q = q.strip('"\'').strip()
    q = _QUOTE_NOISE_RE.sub(" ", q)
    q = _QUOTE_BRACKET_RE.sub(_bracketed_quote_edit_to_match_text, q)
    return q.strip()


def _quote_segments(quote: str) -> list[str]:
    """Clean a drafted quote and split flexible gaps into substantive normalized anchors."""
    cleaned = _clean_drafted_quote(quote)
    raw_segments = _QUOTE_ELLIPSIS_RE.split(cleaned) if cleaned else []
    segments = [
        seg
        for seg in (_normalize_for_match(s.strip(" \t\n\r,;:.")) for s in raw_segments)
        if seg
    ]
    substantive = [s for s in segments if len(s) >= _QUOTE_MIN_SEGMENT_LEN]
    if substantive:
        return substantive
    # Whole quote is short; keep the single longest segment so short verbatim quotes still
    # match — but only if it is long enough to be meaningful (avoid trivial words like "the").
    if segments:
        longest = max(segments, key=len)
        if len(longest) >= _QUOTE_FALLBACK_MIN_LEN:
            return [longest]
    return []


def _quote_log_preview(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return f"{text[:max(limit - 3, 0)].rstrip()}..."


def _quote_opinion_record(opinion: Any, index: int) -> dict[str, Any]:
    if isinstance(opinion, dict):
        opinion_text = (
            opinion.get("normalized_text")
            or opinion.get("text")
            or opinion.get("normalized_opinion")
            or ""
        )
        return {
            "index": opinion.get("index", index),
            "id": opinion.get("id") or opinion.get("opinion_id"),
            "url": opinion.get("url") or opinion.get("opinion_url"),
            "type": opinion.get("type") or opinion.get("opinion_type") or "Opinion",
            "text": _normalize_for_match(opinion_text),
        }

    if isinstance(opinion, (tuple, list)):
        opinion_type = opinion[0] if len(opinion) > 0 else "Opinion"
        opinion_text = opinion[1] if len(opinion) > 1 else ""
        return {
            "index": index,
            "id": None,
            "url": None,
            "type": opinion_type or "Opinion",
            "text": _normalize_for_match(opinion_text),
        }

    return {
        "index": index,
        "id": None,
        "url": None,
        "type": "Opinion",
        "text": _normalize_for_match(opinion),
    }


def _quote_segments_in_text_diagnostic(segments: list[str], op_text: str) -> dict[str, Any]:
    pos = 0
    for anchor_index, seg in enumerate(segments):
        idx = op_text.find(seg, pos)
        if idx == -1:
            first_idx = op_text.find(seg)
            return {
                "matched": False,
                "method": "exact",
                "reason": "anchors_out_of_order" if first_idx != -1 else "missing_exact_anchor",
                "failed_anchor_index": anchor_index + 1,
                "failed_anchor": seg,
                "failed_anchor_words": _punctuation_soft_words_for_match(seg),
            }
        pos = idx + len(seg)
    return {"matched": True, "method": "exact", "reason": "matched"}


def _quote_segments_in_text(segments: list[str], op_text: str) -> bool:
    return bool(_quote_segments_in_text_diagnostic(segments, op_text)["matched"])


def _punctuation_soft_words_for_match(text: str) -> list[str]:
    return _QUOTE_PUNCT_SOFT_WORD_RE.findall(_normalize_for_match(text))


def _find_word_sequence(haystack: list[str], needle: list[str], start: int) -> int:
    if not needle:
        return start
    last_start = len(haystack) - len(needle)
    for idx in range(start, last_start + 1):
        if haystack[idx:idx + len(needle)] == needle:
            return idx
    return -1


def _quote_segments_in_punctuation_soft_text_diagnostic(segments: list[str], op_text: str) -> dict[str, Any]:
    op_words = _punctuation_soft_words_for_match(op_text)
    segment_words = [_punctuation_soft_words_for_match(seg) for seg in segments]
    if not op_words:
        return {
            "matched": False,
            "method": "punctuation_soft",
            "reason": "no_opinions",
            "failed_anchor_index": None,
            "failed_anchor": "",
            "failed_anchor_words": [],
        }
    for anchor_index, words in enumerate(segment_words):
        if not words:
            return {
                "matched": False,
                "method": "punctuation_soft",
                "reason": "no_usable_anchors",
                "failed_anchor_index": anchor_index + 1,
                "failed_anchor": segments[anchor_index],
                "failed_anchor_words": [],
            }

    pos = 0
    for anchor_index, words in enumerate(segment_words):
        idx = _find_word_sequence(op_words, words, pos)
        if idx == -1:
            first_idx = _find_word_sequence(op_words, words, 0)
            return {
                "matched": False,
                "method": "punctuation_soft",
                "reason": (
                    "anchors_out_of_order"
                    if first_idx != -1
                    else "missing_punctuation_soft_word_sequence"
                ),
                "failed_anchor_index": anchor_index + 1,
                "failed_anchor": segments[anchor_index],
                "failed_anchor_words": words,
            }
        pos = idx + len(words)
    return {"matched": True, "method": "punctuation_soft", "reason": "matched"}


def _quote_segments_in_punctuation_soft_text(segments: list[str], op_text: str) -> bool:
    return bool(_quote_segments_in_punctuation_soft_text_diagnostic(segments, op_text)["matched"])


def _log_quote_check_start(
    quote: str,
    segments: list[str],
    opinion: dict[str, Any],
    *,
    citation: Any = None,
    cluster_id: Any = None,
) -> None:
    logger.info(
        "standalone_hg quote-check checking citation=%r cluster_id=%r "
        "opinion_index=%s opinion_id=%r opinion_url=%r opinion_type=%r "
        "anchors=%s quote_preview=%r opinion_length=%s opinion_preview=%r",
        citation,
        cluster_id,
        opinion.get("index"),
        opinion.get("id"),
        opinion.get("url"),
        opinion.get("type") or "Opinion",
        [_quote_log_preview(seg, _QUOTE_LOG_ANCHOR_PREVIEW_CHARS) for seg in segments],
        _quote_log_preview(quote, _QUOTE_LOG_QUOTE_PREVIEW_CHARS),
        len(opinion.get("text") or ""),
        _quote_log_preview(opinion.get("text"), _QUOTE_LOG_OPINION_PREVIEW_CHARS),
    )


def _log_quote_check_found(
    method: str,
    quote: str,
    opinion: dict[str, Any],
    *,
    citation: Any = None,
    cluster_id: Any = None,
    exact_reason: str | None = None,
) -> None:
    logger.info(
        "standalone_hg quote-check found method=%s citation=%r cluster_id=%r "
        "opinion_index=%s opinion_id=%r opinion_url=%r opinion_type=%r "
        "exact_reason=%s quote_preview=%r",
        method,
        citation,
        cluster_id,
        opinion.get("index"),
        opinion.get("id"),
        opinion.get("url"),
        opinion.get("type") or "Opinion",
        exact_reason,
        _quote_log_preview(quote, _QUOTE_LOG_QUOTE_PREVIEW_CHARS),
    )


def _log_quote_check_failure(
    quote: str,
    opinion: dict[str, Any] | None,
    diagnostic: dict[str, Any],
    *,
    citation: Any = None,
    cluster_id: Any = None,
    exact_reason: str | None = None,
    opinion_count: int = 0,
) -> None:
    logger.warning(
        "standalone_hg quote-check not_found reason=%s method=%s citation=%r cluster_id=%r "
        "opinion_count=%s opinion_index=%s opinion_id=%r opinion_url=%r opinion_type=%r "
        "failed_anchor_index=%s failed_anchor_preview=%r failed_anchor_words=%s "
        "exact_reason=%s quote_preview=%r",
        diagnostic.get("reason"),
        diagnostic.get("method"),
        citation,
        cluster_id,
        opinion_count,
        opinion.get("index") if opinion else None,
        opinion.get("id") if opinion else None,
        opinion.get("url") if opinion else None,
        (opinion.get("type") or "Opinion") if opinion else None,
        diagnostic.get("failed_anchor_index"),
        _quote_log_preview(diagnostic.get("failed_anchor"), _QUOTE_LOG_ANCHOR_PREVIEW_CHARS),
        diagnostic.get("failed_anchor_words") or [],
        exact_reason,
        _quote_log_preview(quote, _QUOTE_LOG_QUOTE_PREVIEW_CHARS),
    )


def _quote_in_opinions(
    quote: str,
    normalized_opinions: list[Any],
    *,
    citation: Any = None,
    cluster_id: Any = None,
) -> dict:
    """Check whether `quote` appears verbatim, in order, within a single opinion.

    `normalized_opinions` is a list of (opinion_type, normalized_text) tuples.
    Returns {"status": "found"|"not_found", "opinion_type": <type>|None}.
    """
    segments = _quote_segments(quote)
    if not segments:
        _log_quote_check_failure(
            quote,
            None,
            {
                "method": "precheck",
                "reason": "no_usable_anchors",
                "failed_anchor_index": None,
                "failed_anchor": "",
                "failed_anchor_words": [],
            },
            citation=citation,
            cluster_id=cluster_id,
        )
        return {"status": "not_found", "opinion_type": None}
    opinion_records = [
        record
        for record in (
            _quote_opinion_record(opinion, index)
            for index, opinion in enumerate(normalized_opinions or [])
        )
        if record.get("text")
    ]
    if not opinion_records:
        _log_quote_check_failure(
            quote,
            None,
            {
                "method": "precheck",
                "reason": "no_opinions",
                "failed_anchor_index": None,
                "failed_anchor": "",
                "failed_anchor_words": [],
            },
            citation=citation,
            cluster_id=cluster_id,
        )
        return {"status": "not_found", "opinion_type": None}

    exact_failures: dict[int, dict[str, Any]] = {}
    for record_index, opinion in enumerate(opinion_records):
        _log_quote_check_start(quote, segments, opinion, citation=citation, cluster_id=cluster_id)
        op_text = opinion.get("text") or ""
        if not op_text:
            continue
        diagnostic = _quote_segments_in_text_diagnostic(segments, op_text)
        if diagnostic["matched"]:
            _log_quote_check_found(
                "exact",
                quote,
                opinion,
                citation=citation,
                cluster_id=cluster_id,
            )
            return {"status": "found", "opinion_type": opinion.get("type") or "Opinion"}
        exact_failures[record_index] = diagnostic

    soft_failures: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    for record_index, opinion in enumerate(opinion_records):
        op_text = opinion.get("text") or ""
        if not op_text:
            continue
        diagnostic = _quote_segments_in_punctuation_soft_text_diagnostic(segments, op_text)
        exact_diagnostic = exact_failures.get(record_index)
        if diagnostic["matched"]:
            _log_quote_check_found(
                "punctuation_soft",
                quote,
                opinion,
                citation=citation,
                cluster_id=cluster_id,
                exact_reason=exact_diagnostic.get("reason") if exact_diagnostic else None,
            )
            return {"status": "found", "opinion_type": opinion.get("type") or "Opinion"}
        soft_failures.append((opinion, diagnostic, exact_diagnostic))

    if soft_failures:
        opinion, diagnostic, exact_diagnostic = soft_failures[0]
        _log_quote_check_failure(
            quote,
            opinion,
            diagnostic,
            citation=citation,
            cluster_id=cluster_id,
            exact_reason=exact_diagnostic.get("reason") if exact_diagnostic else None,
            opinion_count=len(opinion_records),
        )
    return {"status": "not_found", "opinion_type": None}


def _format_validated_cases_for_guard(validated_cases: dict) -> str:
    if not validated_cases:
        return "(No validated cases in session. You must still extract every full-form case citation from the draft (including reporter, WL, and LEXIS formats) and return unmatched citations with is_valid=false, is_applicable=false, reason='Not found in our database'.)"

    lines = []
    for cit_str, data in validated_cases.items():
        case_name = data.get("case_name", "")
        holding = data.get("llm_holding", "")
        court = data.get("court", "")
        line = f'- "{cit_str}"'
        if case_name:
            line += f"  |  {case_name}"
        if court:
            line += f"  |  Court: {court}"
        if holding:
            line += f"  |  Holding: {holding}"
        else:
            line += "  |  (holding not available)"
        lines.append(line)
    return "\n".join(lines)


def _format_validated_statutes_for_guard(filtered_citations: list[dict]) -> str:
    rows = []
    seen = set()
    for c in filtered_citations:
        category = c.get("type")
        if category == "case":
            continue
        citation = c.get("form") or c.get("displayName") or c.get("citationName")
        if not citation or citation in seen:
            continue
        seen.add(citation)
        rows.append(f'- "{citation}"  |  Type: {category or "statute"}')

    if not rows:
        return "(No validated statutes in session.)"
    return "\n".join(rows)


def _clean_citation_key(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip(" \t\n\r.,;:")


def _register_citation_link(link_map: dict[str, str], citation: Any, href: str | None) -> None:
    key = _clean_citation_key(citation)
    if key and href and key not in link_map:
        link_map[key] = href


def _build_citation_link_map(
    filtered_citations: list[dict],
    validated_cases: dict | None = None,
    case_viewer_prefix: str | None = None,
) -> dict[str, str]:
    """Build a best-effort citation text -> URL map from already validated citation data."""
    link_map: dict[str, str] = {}

    for citation in filtered_citations or []:
        try:
            href = build_caseviewer_href(citation, case_viewer_prefix=case_viewer_prefix)
        except SoftTimeLimitExceeded:
            raise
        except Exception:
            href = citation.get("href")
        if not href:
            continue

        display_name = citation.get("displayName")
        _register_citation_link(link_map, citation.get("form"), href)
        _register_citation_link(link_map, citation.get("citationName"), href)
        _register_citation_link(link_map, display_name, href)
        if isinstance(display_name, str) and "\t" in display_name:
            _register_citation_link(link_map, display_name.split("\t")[-1], href)

    for citation_text, data in (validated_cases or {}).items():
        href = data.get("href")
        if not href:
            continue
        _register_citation_link(link_map, citation_text, href)
        _register_citation_link(link_map, data.get("citation"), href)

    return link_map


def _find_citation_href(citation_link_map: dict[str, str], *citation_values: Any) -> str | None:
    if not citation_link_map:
        return None

    cleaned_items = [(key, href) for key, href in citation_link_map.items() if key and href]
    for value in citation_values:
        cleaned_value = _clean_citation_key(value)
        if not cleaned_value:
            continue
        if cleaned_value in citation_link_map:
            return citation_link_map[cleaned_value]

        # LLM rows often include pincites, e.g. "550 U.S. 544, 555".
        # Link those back to the validated base citation, "550 U.S. 544".
        for key, href in cleaned_items:
            if len(key) < 6:
                continue
            if cleaned_value.startswith(key) or key.startswith(cleaned_value):
                return href

    return None


def _attach_citation_links_to_results(results: list[dict], citation_link_map: dict[str, str]) -> list[dict]:
    for result in results:
        if result.get("href"):
            continue
        href = _find_citation_href(
            citation_link_map,
            result.get("citation"),
            result.get("citation_as_drafted"),
        )
        if href:
            result["href"] = href
    return results


def _guard_citations_match(left_values: Any, right_values: Any) -> bool:
    """Return whether any normalized left citation matches any normalized right citation."""
    left_items = left_values if isinstance(left_values, (list, tuple, set)) else [left_values]
    right_items = right_values if isinstance(right_values, (list, tuple, set)) else [right_values]
    left_cleaned = [_clean_citation_key(value).lower() for value in left_items]
    right_cleaned = [_clean_citation_key(value).lower() for value in right_items]

    def _base_prefix(shorter: str, longer: str) -> bool:
        if not longer.startswith(shorter):
            return False
        return len(longer) == len(shorter) or longer[len(shorter)] in " ,;(["

    for left in (value for value in left_cleaned if value):
        for right in (value for value in right_cleaned if value):
            if left == right:
                return True
            if len(left) >= 6 and len(right) >= 6 and (
                _base_prefix(left, right) or _base_prefix(right, left)
            ):
                return True
    return False


def _ensure_case_result_coverage(case_results: list[dict], lookup_diagnostics: list[dict]) -> list[dict]:
    """Surface parser-detected case citations omitted by the substantive model."""
    for diagnostic in lookup_diagnostics or []:
        if not isinstance(diagnostic, dict):
            continue
        citation = str(diagnostic.get("citation") or "").strip()
        # Empty diagnostics describe whole-request failures; diagnostics without
        # offsets can contain broad context instead of one source citation.
        if not citation or diagnostic.get("start_index") is None:
            continue
        if any(
            isinstance(result, dict)
            and _guard_citations_match(
                (citation, diagnostic.get("citation_as_drafted")),
                (result.get("citation"), result.get("citation_as_drafted")),
            )
            for result in case_results
        ):
            continue

        drafted_name = str(diagnostic.get("drafted_case_name") or "").strip()
        citation_as_drafted = str(diagnostic.get("citation_as_drafted") or "").strip() or citation
        if drafted_name and not citation_as_drafted.lower().startswith(drafted_name.lower()):
            citation_as_drafted = f"{drafted_name}, {citation}"
        cluster_id = diagnostic.get("clusterId") or diagnostic.get("cluster_id")
        case_results.append({
            "citation": citation,
            "citation_as_drafted": citation_as_drafted,
            "case_name": drafted_name,
            "location": "Detected by citation scanner",
            "clusterId": cluster_id,
            "cluster_id": cluster_id,
            "is_valid": True if cluster_id else None,
            "is_applicable": None,
            "reason": (
                "The citation scanner found this citation, but the substantive audit omitted it. "
                "Review it manually."
            ),
            "coverage_recovered": True,
        })
    return case_results


_DOCKET_ORDER_CITATION_RE = re.compile(
    r"\b(?P<case_name>(?:In re|Matter of)\s+[A-Z][A-Za-z0-9&’'().,\- ]{1,120}?),\s*"
    r"(?P<docket>(?:Case\s+)?No\.\s+[A-Za-z0-9][A-Za-z0-9:.\-]*"
    r"(?:\s*\([A-Za-z0-9]+\))?)"
    r"(?:\s*,)?(?P<entry>\s*\[(?:D\.I\.|Dkt\.|ECF)(?:\s+No\.)?\s*\d+\])?\s*"
    r"\((?P<court>[^()]{2,100}\b(?:19|20)\d{2})\)",
    flags=re.IGNORECASE,
)


def _normalize_docket_order_scan_text(draft_text: str) -> str:
    """Normalize PDF line wrapping while discarding common court filing headers."""
    text = str(draft_text or "")
    text = re.sub(r"(?mi)^.*\bDoc\s+\d+\s+Filed\b.*$", " ", text)
    text = re.sub(r"(?mi)^\s*Pg\s+\d+\s+of\s+\d+\s*$", " ", text)
    text = re.sub(r"(?<=\d)-\s+(?=\d)", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _extract_docket_order_citations(draft_text: str) -> list[dict]:
    """Extract named, docket-only judicial authorities, not ordinary record cites."""
    scan_text = _normalize_docket_order_scan_text(draft_text)
    rows: list[dict] = []
    seen: set[str] = set()
    for match in _DOCKET_ORDER_CITATION_RE.finditer(scan_text):
        citation = " ".join(
            part.strip(" ,")
            for part in (match.group("docket"), match.group("entry") or "")
            if part and part.strip(" ,")
        )
        key = "|".join([
            _clean_citation_key(match.group("case_name")).lower(),
            _clean_citation_key(citation).lower(),
        ])
        if not key or key in seen:
            continue
        seen.add(key)
        rows.append({
            "citation": citation,
            "citation_as_drafted": match.group(0).strip(),
            "case_name": match.group("case_name").strip(),
            "citation_kind": "docket_order",
            "location": "Named docket/order authority",
            "is_valid": None,
            "is_applicable": None,
            "reason": (
                "This named docket or order citation was detected, but it was not independently "
                "verified in the case-law database. Review the cited order manually."
            ),
        })
    return rows


def _ensure_docket_order_coverage(case_results: list[dict], draft_text: str) -> list[dict]:
    """Keep named docket/order authorities visible and conservatively unverified."""
    for result in case_results or []:
        if isinstance(result, dict) and result.get("citation_kind") == "docket_order":
            result["is_valid"] = None
            result["is_applicable"] = None
            result.setdefault(
                "reason",
                "This named docket or order citation requires manual source verification.",
            )

    for detected in _extract_docket_order_citations(draft_text):
        existing = next(
            (
                result for result in case_results
                if isinstance(result, dict)
                and _guard_citations_match(
                    (detected.get("citation"), detected.get("citation_as_drafted")),
                    (result.get("citation"), result.get("citation_as_drafted")),
                )
            ),
            None,
        )
        if existing is not None:
            existing["citation_kind"] = "docket_order"
            existing["is_valid"] = None
            existing["is_applicable"] = None
            if not str(existing.get("reason") or "").strip():
                existing["reason"] = detected["reason"]
            continue
        detected["coverage_recovered"] = True
        case_results.append(detected)
    return case_results


def _ensure_statute_result_coverage(
    statute_results: list[dict],
    filtered_citations: list[dict],
) -> list[dict]:
    """Surface parser-detected statute citations omitted by the substantive model."""
    for detected in filtered_citations or []:
        if not isinstance(detected, dict) or detected.get("type") == "case":
            continue
        citation = str(detected.get("form") or detected.get("displayName") or "").strip()
        if not citation:
            continue
        if any(
            isinstance(result, dict)
            and _guard_citations_match(
                citation,
                (result.get("citation"), result.get("citation_as_drafted")),
            )
            for result in statute_results
        ):
            continue
        statute_results.append({
            "citation": citation,
            "citation_as_drafted": citation,
            "location": "Detected by citation scanner",
            "is_valid": None,
            "is_applicable": None,
            "reason": (
                "The citation scanner found this statute, but the substantive audit omitted it. "
                "Review it manually."
            ),
            "coverage_recovered": True,
        })
    return statute_results


_DIRECT_QUOTE_RE = re.compile(r"“([^”]{4,})”|(?<!\w)\"([^\"\n]{4,})\"")


def _nearby_statute_quotes(result: dict, draft_text: str) -> list[str]:
    """Find direct quotations near a drafted statute citation as a safety net."""
    text = str(draft_text or "")
    if not text:
        return []

    quotes: list[str] = []
    seen: set[str] = set()
    needles = [result.get("citation_as_drafted"), result.get("citation")]
    for needle in (str(value).strip() for value in needles if value):
        for match in re.finditer(re.escape(needle), text, flags=re.IGNORECASE):
            start = max(0, match.start() - 300)
            end = min(len(text), match.end() + 300)
            for quote_match in _DIRECT_QUOTE_RE.finditer(text[start:end]):
                quote = next((group for group in quote_match.groups() if group is not None), "").strip()
                key = re.sub(r"\s+", " ", quote).lower()
                if key and key not in seen:
                    seen.add(key)
                    quotes.append(quote)
    return quotes


def _attach_statute_quote_review(statute_results: list[dict], draft_text: str = "") -> None:
    """Mark statutory quotations as unverified until source-text checking exists."""
    for result in statute_results or []:
        if not isinstance(result, dict):
            continue
        raw_quotes = result.get("quotes")
        raw_quotes = list(raw_quotes) if isinstance(raw_quotes, list) else []
        raw_quotes.extend(_nearby_statute_quotes(result, draft_text))
        checked = []
        seen: set[str] = set()
        for quote in raw_quotes:
            text = quote.get("text") if isinstance(quote, dict) else quote
            key = re.sub(r"\s+", " ", text).strip().lower() if isinstance(text, str) else ""
            if key and key not in seen:
                seen.add(key)
                checked.append({"text": text, "status": "not_checked", "opinion_type": None})
        if checked:
            result["quotes"] = checked
            result["quote_check"] = {"total": len(checked), "found": 0}
        else:
            result.pop("quotes", None)
            result.pop("quote_check", None)


def _attach_quote_verification(case_results: list[dict], validated_cases: dict) -> None:
    """For results carrying drafted quotes, verify each quote against the case's opinions.

    Opinions are fetched only for cases that actually quote something, deduped per
    cluster within this run. Each result gains:
      - result["quotes"]      -> [{"text", "status": found|not_found|not_checked, "opinion_type"}]
      - result["quote_check"] -> {"total", "found"}
    Best-effort: never raises into the caller (mirrors _attach_web_search_suggestions).
    """
    if not isinstance(validated_cases, dict):
        validated_cases = {}
    # citation -> cluster id, for resolving a result's source case.
    citation_to_cluster: dict[str, Any] = {}
    for citation, data in validated_cases.items():
        cid = _cluster_id(data) if isinstance(data, dict) else None
        if cid:
            citation_to_cluster[str(citation).strip()] = cid

    opinions_cache: dict[Any, list[dict[str, Any]]] = {}

    def _normalized_opinions_for(cluster_id: Any) -> list[dict[str, Any]]:
        key = _normalize_cluster_id(cluster_id) or cluster_id
        if key in opinions_cache:
            return opinions_cache[key]
        normalized: list[dict[str, Any]] = []
        try:
            cluster = get_cluster(cluster_id)
            if cluster:
                for opinion_index, op in enumerate(get_cluster_opinions(cluster)):
                    text = op.get("plain_text") or op.get("html_with_citations") or op.get("html") or ""
                    normalized.append({
                        "index": opinion_index,
                        "id": op.get("id"),
                        "url": op.get("url"),
                        "type": op.get("type") or "Opinion",
                        "normalized_text": _normalize_for_match(text),
                    })
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            logger.warning(
                "standalone_hg quote-check opinion_load_failed cluster_id=%r error=%s",
                cluster_id,
                exc,
            )
        opinions_cache[key] = normalized
        return normalized

    for result in case_results:
        if not isinstance(result, dict):
            continue
        quotes = result.get("quotes")
        quotes = [q for q in quotes if isinstance(q, str) and q.strip()] if isinstance(quotes, list) else []
        if not quotes:
            result.pop("quotes", None)
            continue

        citation = str(result.get("citation") or "").strip()
        cluster_id = result.get("clusterId") or result.get("cluster_id") or citation_to_cluster.get(citation)
        normalized_opinions = _normalized_opinions_for(cluster_id) if cluster_id else []

        checked: list[dict] = []
        found = 0
        for q in quotes:
            if not normalized_opinions:
                logger.warning(
                    "standalone_hg quote-check skipped reason=no_opinions citation=%r "
                    "cluster_id=%r quote_preview=%r",
                    citation,
                    cluster_id,
                    _quote_log_preview(q, _QUOTE_LOG_QUOTE_PREVIEW_CHARS),
                )
                checked.append({"text": q, "status": "not_checked", "opinion_type": None})
                continue
            verdict = _quote_in_opinions(q, normalized_opinions, citation=citation, cluster_id=cluster_id)
            if verdict["status"] == "found":
                found += 1
            checked.append({"text": q, "status": verdict["status"], "opinion_type": verdict.get("opinion_type")})
        result["quotes"] = checked
        result["quote_check"] = {"total": len(checked), "found": found}


def _linkify_citation(citation: Any, href: str | None) -> str:
    citation_text = escape(str(citation or ""))
    if not href:
        return citation_text
    safe_href = escape(str(href), quote=True)
    return f'<a href="{safe_href}" target="_blank" rel="noopener noreferrer">{citation_text}</a>'


_WEBSEARCH_CASE_NAME_STRIP_PATTERNS = [
    # Docket / slip-number prefixes before a citation.
    r"\b(?:No\.|Nos\.|Case\s+No\.|Civil\s+Action\s+No\.|Civ\.\s+No\.|Docket\s+No\.)(?=\s|,|$)",
    # Westlaw and LEXIS citations.
    r"\b\d{4}\s+WL\s+\d+\b",
    r"\b\d{4}\s+[A-Za-z. ]+\s+LEXIS\s+\d+\b",
    # Public-domain state citations, e.g. 2010 WI 78.
    r"\b\d{4}\s+[A-Z]{2,}\s+\d+\b",
    # Common reporter citations.
    r"\b\d{1,4}\s+(?:U\.S\.|S\. ?Ct\.|L\. ?Ed\. ?(?:2d)?|F\. ?(?:Supp\. ?\d*d?|2d|3d|4th)?|B\.R\.|"
    r"Cal\. ?(?:App\.)? ?\d*[A-Za-z]*|N\.?Y\.? ?\d*d?|Wis\. ?\d*d?|A\.? ?\d*d?|P\.? ?\d*d?|"
    r"S\.?W\.? ?\d*d?|N\.?E\.? ?\d*d?|So\.? ?\d*d?)\s+\d+\b",
]
_WEBSEARCH_CASE_NAME_STRIP_RE = re.compile(
    "|".join(f"(?:{pattern})" for pattern in _WEBSEARCH_CASE_NAME_STRIP_PATTERNS),
    re.IGNORECASE,
)
_WEBSEARCH_LEADING_SIGNAL_RE = re.compile(
    r"^\s*(?:see(?: also)?|cf\.?|accord|but see|e\.g\.,?|see,?\s+e\.g\.,?)\s+",
    re.IGNORECASE,
)


def _case_name_only_for_web_search(value: str | None) -> str:
    """Extract only the case-name portion from a drafted citation string.

    The drafted reporter citation is exactly the part we distrust in this fallback, so the web-search
    query must stop before the first citation-like marker.
    """
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return ""

    match = _WEBSEARCH_CASE_NAME_STRIP_RE.search(text)
    if match:
        text = text[:match.start()]

    text = _WEBSEARCH_LEADING_SIGNAL_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip(" \t\n\r,;([{")
    text = text.strip(" \t\n\r,;:")
    if not re.search(r"[A-Za-z]", text):
        return ""
    return text


def _web_search_case_name_candidates(row: dict) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for value in (row.get("citation_as_drafted"), row.get("case_name")):
        candidate = _case_name_only_for_web_search(value)
        key = re.sub(r"\s+", " ", candidate).strip().casefold()
        if candidate and key not in seen:
            candidates.append(candidate)
            seen.add(key)
    return candidates


def _resolve_citation_via_web_search(
    case_name_candidates: list[str],
    citation_as_drafted: str = "",
    drafted_reporter_citation: str = "",
) -> dict | None:
    """Web-search a single unvalidated citation and return a normalized resolution dict, or None.

    This is the seam tests monkeypatch. Returns a dict with keys resolved (bool), resolved_casename,
    resolved_reporter_citation, url, confidence_level — or None when nothing usable came back.
    """
    from backend.ai.base import web_search_completion

    candidates = [
        str(c).strip()
        for c in (case_name_candidates or [])
        if isinstance(c, str) and str(c).strip()
    ]
    if not candidates:
        return None

    prompt = LOCAL_CITATION_WEBSEARCH_PROMPT_PATH.read_text().format(
        case_name_candidates="\n".join(f"- {candidate}" for candidate in candidates),
        citation_as_drafted=citation_as_drafted or "",
        drafted_reporter_citation=drafted_reporter_citation or "",
    )
    output_text = web_search_completion(prompt)
    if not output_text:
        return None

    parsed = extract_from_json("resolved", output_text)
    # extract_from_json returns the value for the key on a hit, or [] on miss; the web-search prompt
    # returns a single object, so re-parse the whole payload when the keyed extraction isn't a dict.
    if isinstance(parsed, dict):
        obj = parsed
    else:
        try:
            obj = json.loads(output_text)
        except Exception:
            match = re.search(r"\{[\s\S]*\}", output_text)
            if not match:
                return None
            try:
                obj = json.loads(match.group(0))
            except Exception:
                return None
    if not isinstance(obj, dict):
        return None

    return {
        "resolved": bool(obj.get("resolved")),
        "resolved_casename": str(obj.get("resolved_casename") or "").strip(),
        "resolved_reporter_citation": str(obj.get("resolved_reporter_citation") or "").strip(),
        "url": str(obj.get("url") or "").strip(),
        "confidence_level": str(obj.get("confidence_level") or "").strip().lower(),
    }


def _attach_web_search_suggestions(case_results: list[dict], task=None) -> list[dict]:
    """For citations the cases LLM could not validate, web-search for a real-case match and attach a
    `did_you_mean` payload when a credible source ties the case name to a source-backed citation.

    Best-effort: any failure is swallowed so the guard still renders its normal tables.
    """
    from concurrent.futures import ThreadPoolExecutor

    try:
        invalid_rows = [
            r for r in (case_results or [])
            if (
                isinstance(r, dict)
                and r.get("citation_kind") != "docket_order"
                and not _guard_result_is_valid(r)
            )
        ]
        if not invalid_rows:
            return case_results

        if len(invalid_rows) > WEB_SEARCH_MAX_CITATIONS:
            print(
                f"[standalone_hg] web-search suggestions capped at {WEB_SEARCH_MAX_CITATIONS} of "
                f"{len(invalid_rows)} unvalidated citations; the rest are skipped this run."
            )
            invalid_rows = invalid_rows[:WEB_SEARCH_MAX_CITATIONS]

        _standalone_hg_progress(
            task,
            "Finalizing citation review...",
            progress=88,
            dev_message="Searching the web to resolve unverified citations...",
        )

        def _resolve(row: dict) -> tuple[dict, dict | None]:
            try:
                case_name_candidates = _web_search_case_name_candidates(row)
                if not case_name_candidates:
                    return row, None
                resolution = _resolve_citation_via_web_search(
                    case_name_candidates,
                    citation_as_drafted=row.get("citation_as_drafted") or "",
                    drafted_reporter_citation=row.get("citation") or "",
                )
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:  # pragma: no cover - network/model variability
                print(f"[standalone_hg] web-search failed for {row.get('citation')!r}: {exc}")
                resolution = None
            return row, resolution

        with ThreadPoolExecutor(max_workers=WEB_SEARCH_MAX_WORKERS) as executor:
            futures = [
                submit_with_context(executor, _resolve, row)
                for row in invalid_rows
            ]
            for future in futures:
                row, resolution = future.result()
                if not resolution or not resolution.get("resolved"):
                    continue
                confidence = str(resolution.get("confidence_level") or "").strip().lower()
                casename = resolution.get("resolved_casename")
                reporter = resolution.get("resolved_reporter_citation")
                url = resolution.get("url")
                # Surface only high-confidence resolutions that have a real link plus both a name and
                # a source-backed citation. Confidence gates the decision but is not shown to the user.
                if confidence == "high" and casename and reporter and url:
                    row["did_you_mean"] = {
                        "casename": casename,
                        "reporter_citation": reporter,
                        "url": url,
                    }
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        print(f"[standalone_hg] web-search suggestion pass skipped: {exc}")

    return case_results


def _load_local_hallucination_prompt() -> str:
    try:
        return LOCAL_CASES_HALLUCINATION_PROMPT_PATH.read_text()
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"Unable to load standalone HG case prompt at '{LOCAL_CASES_HALLUCINATION_PROMPT_PATH}': {exc}"
        ) from exc


def _enrich_validated_cases_for_standalone(validated_cases: dict) -> None:
    """Enrich validated cases with holding metadata, deduping by cluster ID."""
    cluster_snapshot_cache: dict[Any, dict[str, Any]] = {}
    for citation, data in validated_cases.items():
        cluster_id = _cluster_id(data)
        if not cluster_id:
            continue
        cluster_key = _normalize_cluster_id(cluster_id) or cluster_id

        # Deduplicate enrichment calls for citations mapped to the same cluster.
        if cluster_key in cluster_snapshot_cache:
            cached = dict(cluster_snapshot_cache[cluster_key])
            # Preserve the per-entry citation string when multiple citations map
            # to the same cluster.
            cached["citation"] = data.get("citation") or citation
            href = data.get("href")
            data.update(cached)
            if href:
                data["href"] = href
            continue

        try:
            cache_row = get_freelaw_cluster_cache(cluster_id)
            cached_snapshot = _snapshot_from_cache_row(cache_row) if cache_row and cache_row.llm_holding else None
            if cached_snapshot:
                cached_snapshot["citation"] = data.get("citation") or citation
                href = data.get("href")
                data.update(cached_snapshot)
                if href:
                    data["href"] = href
                cluster_snapshot_cache[cluster_key] = cached_snapshot
                record_freelaw_cluster_cache_hit(cache_row)
                continue

            cluster = get_cluster(cluster_id)
            if not cluster:
                continue

            court_citation = None
            docket_url = cluster.get("docket")
            if docket_url:
                docket_raw = get_docket(docket_url)
                if docket_raw and docket_raw.get("court"):
                    court_raw = get_court(docket_raw["court"])
                    if court_raw:
                        court_citation = court_raw.get("citation_string")

            sub_opinions = cluster.get("sub_opinions", [])
            opinion_url = sub_opinions[0] if sub_opinions else None

            llm_holding = data.get("llm_holding")
            if opinion_url and not llm_holding:
                opinion = get_opinion(opinion_url)
                if opinion:
                    llm_holding = extract_holding_from_opinion(opinion) or None

            abs_url = cluster.get("absolute_url")
            freelaw_url = f"https://www.courtlistener.com{abs_url}" if abs_url else data.get("freelaw_url")

            snapshot = build_freelaw_cluster_cache_snapshot(
                cluster_id=cluster_id,
                citation=data.get("citation") or citation,
                cluster={**cluster, "case_name": data.get("case_name") or cluster.get("case_name") or ""},
                opinion_url=opinion_url,
                docket_url=docket_url,
                llm_holding=llm_holding,
                freelaw_url=freelaw_url,
                court=court_citation,
                source="standalone_hg+enriched",
            )
            if not snapshot:
                continue

            data.update(snapshot)
            cluster_snapshot_cache[cluster_key] = snapshot
            upsert_freelaw_cluster_cache(snapshot)
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            print(f"[standalone_hg] Could not enrich citation '{citation}' (cluster {cluster_id}): {exc}")


def _extract_guard_results(llm_output: Any) -> list[dict]:
    results = extract_from_json("results", llm_output)
    return results if isinstance(results, list) else []


def standalone_hg_show_dev_progress_details() -> bool:
    try:
        from backend.constants import ENVIRONMENT
    except Exception:
        return False
    return str(ENVIRONMENT or "") == "dev-monica"


def _standalone_hg_progress(
    task,
    message: str,
    progress: int | None = None,
    *,
    dev_message: str | None = None,
    **extra,
) -> None:
    if task is None:
        return
    show_dev_details = standalone_hg_show_dev_progress_details()
    meta = {
        "message": message,
        "status": message,
        "show_dev_progress_details": show_dev_details,
    }
    if progress is not None:
        meta["progress"] = progress
    if show_dev_details:
        dev_details = {"message": dev_message or message}
        dev_details.update(extra)
        meta["dev_progress_details"] = dev_details
    task.update_state(state="PROGRESS", meta=meta)


def _standalone_hg_prompt_token_counts(prompts: list[str], token_counter) -> list[int]:
    return [token_counter(prompt) for prompt in prompts]


def _guard_validity_value(result: dict) -> Any:
    """Return the explicit validity value, defaulting missing legacy fields to false."""
    if not isinstance(result, dict):
        return False
    if "is_valid" in result:
        return result.get("is_valid")
    if "is_validated" in result:
        return result.get("is_validated")
    return False


def _guard_result_is_valid(result: dict) -> bool:
    """Accept both old `is_validated` and current `is_valid` guard schemas."""
    return bool(_guard_validity_value(result))


def _clean_guard_citation_key(value) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip(" \t\n\r.,;:")


def _find_guard_case_href(citation_link_map: dict, *citation_values) -> str | None:
    if not citation_link_map:
        return None
    items = [(key, href) for key, href in citation_link_map.items() if key and href]
    for value in citation_values:
        clean_value = _clean_guard_citation_key(value)
        if not clean_value:
            continue
        if clean_value in citation_link_map:
            return citation_link_map[clean_value]
        for key, href in items:
            if len(key) >= 6 and (clean_value.startswith(key) or key.startswith(clean_value)):
                return href
    return None


def _guard_attach_links(results: list, link_map: dict[str, str]) -> list:
    for result in results or []:
        if not isinstance(result, dict) or result.get("href"):
            continue
        href = _find_guard_case_href(
            link_map,
            result.get("citation"),
            result.get("citation_as_drafted"),
        )
        if href:
            result["href"] = href
    return results


def _guard_find_lookup_diagnostic(diagnostic_map: dict[str, dict], *citation_values) -> dict | None:
    if not diagnostic_map:
        return None
    items = [(k, v) for k, v in diagnostic_map.items() if k and v]
    for value in citation_values:
        cleaned = _clean_guard_citation_key(value)
        if not cleaned:
            continue
        if cleaned in diagnostic_map:
            return diagnostic_map[cleaned]
        for key, diagnostic in items:
            if len(key) < 6:
                continue
            if cleaned.startswith(key) or key.startswith(cleaned):
                return diagnostic
    return None


def _guard_name_mismatch_flag(result: dict):
    """Read the LLM's semantic name_mismatch flag as True, False, or None."""
    flag = result.get("name_mismatch")
    if isinstance(flag, bool):
        return flag
    if isinstance(flag, str):
        token = flag.strip().lower()
        if token in ("true", "yes", "1"):
            return True
        if token in ("false", "no", "0"):
            return False
    return None


def _guard_attach_lookup_diagnostics(results: list, lookup_diagnostics: list) -> list:
    """Attach deterministic Free Law lookup tags and reconcile semantic name mismatch."""
    diagnostic_map: dict[str, dict] = {}
    for diagnostic in lookup_diagnostics or []:
        if not isinstance(diagnostic, dict):
            continue
        for value in (diagnostic.get("citation"), diagnostic.get("citation_as_drafted")):
            key = _clean_guard_citation_key(value)
            if key and key not in diagnostic_map:
                diagnostic_map[key] = diagnostic

    for result in results or []:
        if not isinstance(result, dict):
            continue
        diagnostic = _guard_find_lookup_diagnostic(
            diagnostic_map,
            result.get("citation"),
            result.get("citation_as_drafted"),
        )
        if not diagnostic:
            continue
        result["lookup_tag"] = diagnostic.get("lookup_tag")
        result["possible_issue"] = diagnostic.get("possible_issue") or ""
        result["clusterId"] = diagnostic.get("clusterId")
        result["cluster_id"] = diagnostic.get("cluster_id")
        result["resolved_case_name"] = diagnostic.get("case_name") or ""
        result["resolved_citation"] = diagnostic.get("citation") or ""

        resolved = bool(diagnostic.get("clusterId") or diagnostic.get("cluster_id"))
        if resolved:
            flag = _guard_name_mismatch_flag(result)
            if flag is False and result["lookup_tag"] == LOOKUP_TAG_CASE_NAME_MISMATCH:
                result["lookup_tag"] = LOOKUP_TAG_VERIFIED_FL
                result["possible_issue"] = ""
            elif flag is True and result["lookup_tag"] != LOOKUP_TAG_CASE_NAME_MISMATCH:
                result["lookup_tag"] = LOOKUP_TAG_CASE_NAME_MISMATCH
                if not result["possible_issue"]:
                    result["possible_issue"] = (
                        "Drafted case name does not match the resolved case for this citation."
                    )
    return results


def _guard_issue_text(result: dict) -> str:
    lookup_tag = str(result.get("lookup_tag") or "").strip()
    possible_issue = str(result.get("possible_issue") or "").strip()
    reason = str(result.get("reason") or "").strip()

    note = f"{lookup_tag}: {possible_issue}" if (lookup_tag and possible_issue) else possible_issue
    segments = [s for s in [note] if s]
    is_generic_not_found = reason.strip().rstrip(".").lower() == "not found in our database"
    if reason and reason != possible_issue and reason not in note and not (note and is_generic_not_found):
        if not _guard_applicability_reason(result):
            segments.append(reason)
    return "  —  ".join(segments)


def _guard_applicability_reason(result: dict) -> str:
    if (
        isinstance(result, dict)
        and result.get("is_applicable") is False
        and _guard_result_is_valid(result)
    ):
        return str(result.get("reason") or "").strip()
    return ""


def _guard_applicable_cell_html(result: dict, icon: str) -> str:
    reason = _guard_applicability_reason(result)
    if not reason:
        return icon
    return (
        f'{icon}<div class="hg-applicability-reason">Does not support: {escape(reason)}</div>'
    )


def _guard_compact_location(location: object) -> str:
    """Turn verbose model locations into familiar legal pinpoint references."""
    text = re.sub(r"\s+", " ", str(location or "")).strip()
    if not text:
        return "—"
    scan_text = re.sub(r",\s*and\s+", ", ", text, flags=re.IGNORECASE)

    paragraph_refs = []
    for match in re.finditer(
        r"(?:\bparagraphs?\b|\bparas?\.?|¶{1,2})\s*"
        r"(\d+(?:\s*[-–]\s*\d+)?(?:\s*(?:,|and)\s*\d+(?:\s*[-–]\s*\d+)?)*)",
        scan_text,
        flags=re.IGNORECASE,
    ):
        for raw_ref in re.split(r"\s*(?:,|and)\s*", match.group(1)):
            ref = re.sub(r"\s*[-–]\s*", "–", raw_ref)
            if ref and ref not in paragraph_refs:
                paragraph_refs.append(ref)
    if paragraph_refs:
        marker = "¶" if len(paragraph_refs) == 1 else "¶¶"
        return f"{marker} {', '.join(paragraph_refs)}"

    page_refs = []
    for match in re.finditer(
        r"(?:\bpages?\b|\bpp?\.)\s*"
        r"(\d+(?:\s*[-–]\s*\d+)?(?:\s*(?:,|and)\s*\d+(?:\s*[-–]\s*\d+)?)*)",
        scan_text,
        flags=re.IGNORECASE,
    ):
        for raw_ref in re.split(r"\s*(?:,|and)\s*", match.group(1)):
            ref = re.sub(r"\s*[-–]\s*", "–", raw_ref)
            if ref and ref not in page_refs:
                page_refs.append(ref)
    if page_refs:
        marker = "p." if len(page_refs) == 1 and "–" not in page_refs[0] else "pp."
        return f"{marker} {', '.join(page_refs)}"

    section_match = re.fullmatch(r"(?:section|sec\.)\s+(.+)", text, flags=re.IGNORECASE)
    if section_match:
        return f"§ {section_match.group(1).strip()}"

    return text if len(text) <= 72 else text[:69].rstrip() + "…"


def _guard_status_html(status: str) -> str:
    if status == "Issue":
        modifier, label = "issue", "Issue found"
    elif status == "Needs Review":
        modifier, label = "review", "Needs review"
    else:
        modifier, label = "confirmed", "Confirmed"
    return f'<span class="hg-status hg-status--{modifier}">{label}</span>'


def _guard_cell_status_html(value: object, *, positive: str, negative: str) -> str:
    if value is True:
        return f'<span class="hg-cell-status hg-cell-status--positive">✓ {escape(positive)}</span>'
    if value is False:
        return f'<span class="hg-cell-status hg-cell-status--negative">× {escape(negative)}</span>'
    return '<span class="hg-cell-status hg-cell-status--neutral">— Not assessed</span>'


_GUARD_COLUMN_TOOLTIPS = {
    "Citation as drafted": "The citation text exactly as it appears in your document.",
    "Where cited": "Where this citation appears in the document.",
    "Citation reviewed": "The authority Hallucination Guard matched for review.",
    "Result": "Overall result for this citation: confirmed, issue found, or needs review.",
    "Authority found": "Whether the cited authority was located in our legal database.",
    "Provision found": "Whether the cited statute appears to be a real provision.",
    "Supports proposition": "Whether the authority supports the proposition in your document.",
    "Quotation review": "Whether quoted language was verified against the matched authority text.",
    "Possible match": (
        "A possible matching authority when the citation could not be confidently located."
    ),
    "Possible Issues": "Developer diagnostic details about lookup or citation matching.",
}


def _guard_tooltip_attrs(column: str) -> str:
    tooltip = _GUARD_COLUMN_TOOLTIPS.get(column)
    if not tooltip:
        return ""
    tooltip_attr = escape(tooltip, quote=True)
    return f' class="hg-tooltip-cell" data-tooltip="{tooltip_attr}"'


def _guard_th(column: str, style: str) -> str:
    return f'<th style="{style}"{_guard_tooltip_attrs(column)}>{escape(column)}</th>'


def _guard_td(column: str, style: str, content: str) -> str:
    return f'<td style="{style}">{content}</td>'


_GUARD_NEEDS_REVIEW_LOOKUP_TAGS = {
    LOOKUP_TAG_LACKS_OPINION_DATA,
    LOOKUP_TAG_AMBIGUOUS_CLUSTER,
    LOOKUP_TAG_NO_CITATION_PARSED,
    LOOKUP_TAG_API_ERROR_OR_PARTIAL,
}
_GUARD_ISSUE_LOOKUP_TAGS = {
    LOOKUP_TAG_CITE_MISMATCH,
    LOOKUP_TAG_CASE_NAME_MISMATCH,
}


def _guard_show_possible_issues() -> bool:
    try:
        from backend.constants import ENVIRONMENT
    except Exception:
        return False
    return "monica" in str(ENVIRONMENT or "").lower()


def _guard_possible_lookup_present(result: dict) -> bool:
    return bool(isinstance(result, dict) and result.get("did_you_mean"))


def _guard_quote_statuses(result: dict) -> list[str]:
    quotes = result.get("quotes") if isinstance(result, dict) else None
    statuses = [
        str(q.get("status") or "").strip()
        for q in quotes
        if isinstance(q, dict) and q.get("status")
    ] if isinstance(quotes, list) else []

    quote_check = result.get("quote_check") if isinstance(result, dict) else None
    if isinstance(quote_check, dict):
        try:
            total = int(quote_check.get("total") or 0)
        except (TypeError, ValueError):
            total = 0
        if total > 0 and not statuses:
            statuses.append("not_checked")
    return statuses


def _guard_case_status(result: dict) -> str:
    if not isinstance(result, dict):
        return "Needs Review"

    lookup_tag = str(result.get("lookup_tag") or "").strip()
    quote_statuses = _guard_quote_statuses(result)

    # Unavailable, ambiguous, incomplete, or alternate-lookup outcomes are review work,
    # not confirmed failures.
    if (
        result.get("citation_kind") == "docket_order"
        or result.get("is_applicable") is None
        or lookup_tag in _GUARD_NEEDS_REVIEW_LOOKUP_TAGS
        or any(status == "not_checked" for status in quote_statuses)
        or _guard_possible_lookup_present(result)
    ):
        return "Needs Review"

    if (
        result.get("is_applicable") is False
        or any(status == "not_found" for status in quote_statuses)
        or lookup_tag in _GUARD_ISSUE_LOOKUP_TAGS
        or not _guard_result_is_valid(result)
    ):
        return "Issue"

    if (
        _guard_result_is_valid(result)
        and result.get("is_applicable") is True
        and not str(result.get("possible_issue") or "").strip()
        and all(status == "found" for status in quote_statuses)
    ):
        return "Verified"

    return "Needs Review"


def _guard_statute_status(result: dict) -> str:
    if not isinstance(result, dict):
        return "Needs Review"

    valid_value = _guard_validity_value(result)
    quote_statuses = _guard_quote_statuses(result)
    if (
        valid_value is False
        or result.get("is_applicable") is False
        or any(status == "not_found" for status in quote_statuses)
    ):
        return "Issue"
    if (
        valid_value is not True
        or result.get("is_applicable") is None
        or result.get("coverage_recovered")
        or any(status == "not_checked" for status in quote_statuses)
    ):
        return "Needs Review"
    if result.get("is_applicable") is True and all(status == "found" for status in quote_statuses):
        return "Verified"
    return "Needs Review"


def _guard_results_to_html(guard_results: list, *, partial_coverage: bool = False) -> str:
    """Convert parsed case audit results to the shared Hallucination Guard table."""
    n = len(guard_results)
    statuses = [_guard_case_status(r) for r in guard_results]
    issues = statuses.count("Issue")
    needs_review = statuses.count("Needs Review")
    recovered = sum(1 for r in guard_results if isinstance(r, dict) and r.get("coverage_recovered"))

    show_dym = any(isinstance(r, dict) and r.get("did_you_mean") for r in guard_results)
    show_quotes = any(isinstance(r, dict) and r.get("quote_check") for r in guard_results)
    show_possible_issues = _guard_show_possible_issues()

    th = ""
    td = ""
    td_c = "text-align:center;"

    rows = []
    for r in guard_results:
        valid_value = _guard_validity_value(r)
        validated = _guard_result_is_valid(r)
        status = _guard_case_status(r)
        r["status"] = status
        applicable = r.get("is_applicable")
        citation = r.get("citation", "")
        lookup_tag = str(r.get("lookup_tag") or "").strip()
        display_name = str(r.get("resolved_case_name") or "").strip() or str(r.get("case_name") or "").strip()
        display_citation = str(r.get("resolved_citation") or "").strip() or str(citation or "").strip()
        name_html = escape(display_name)
        cite_html = escape(display_citation)
        href = r.get("href")

        full_link = bool(href) and lookup_tag in LOOKUP_TAGS_VALIDATED_CASES
        if full_link:
            href_attr = escape(str(href), quote=True)
            whole = f"{name_html} {cite_html}".strip() if name_html else cite_html
            case_citation_html = f'<a href="{href_attr}" target="_blank" rel="noopener noreferrer">{whole}</a>'
        elif href and lookup_tag == LOOKUP_TAG_CASE_NAME_MISMATCH:
            href_attr = escape(str(href), quote=True)
            linked_cite = f'<a href="{href_attr}" target="_blank" rel="noopener noreferrer">{cite_html}</a>'
            case_citation_html = f"{name_html} {linked_cite}".strip() if name_html else linked_cite
        else:
            case_citation_html = f"{name_html} {cite_html}".strip() if name_html else cite_html

        as_drafted = escape(str(r.get("citation_as_drafted") or citation))
        issue_text = _guard_issue_text(r)
        if r.get("citation_kind") == "docket_order":
            authority_html = '<span class="hg-cell-status hg-cell-status--neutral">— Manual check</span>'
        elif lookup_tag == LOOKUP_TAG_CASE_NAME_MISMATCH:
            authority_html = _guard_cell_status_html(False, positive="Found", negative="Different case")
        elif lookup_tag == LOOKUP_TAG_CITE_MISMATCH:
            authority_html = _guard_cell_status_html(False, positive="Found", negative="Different authority")
        else:
            authority_html = _guard_cell_status_html(valid_value, positive="Found", negative="Not found")
        support_html = _guard_cell_status_html(
            applicable if validated else None,
            positive="Supports",
            negative="Does not support",
        )
        applicable_html = _guard_applicable_cell_html(r, support_html)
        status_html = _guard_status_html(status)

        dym_cell = ""
        if show_dym:
            dym = r.get("did_you_mean") if isinstance(r, dict) else None
            if isinstance(dym, dict) and dym.get("url") and dym.get("casename") and dym.get("reporter_citation"):
                dym_href = escape(str(dym.get("url")), quote=True)
                dym_text = escape(f"{dym.get('casename')} {dym.get('reporter_citation')}".strip())
                dym_cell = _guard_td(
                    "Possible match",
                    td,
                    f'<a href="{dym_href}" target="_blank" rel="noopener noreferrer">{dym_text}</a>'
                )
            else:
                dym_cell = _guard_td("Possible match", td, "")

        quote_cell = ""
        if show_quotes:
            quotes = r.get("quotes") if isinstance(r, dict) else None
            if isinstance(quotes, list) and quotes:
                quote_lines = []
                for q in quotes:
                    if not isinstance(q, dict):
                        continue
                    q_text = str(q.get("text") or "")
                    if len(q_text) > 160:
                        q_text = q_text[:157].rstrip() + "…"
                    status = q.get("status")
                    op_type = str(q.get("opinion_type") or "").strip()
                    if status == "found":
                        where = f" in {escape(op_type)}" if op_type else ""
                        label = f'<span class="hg-cell-status hg-cell-status--positive">✓ Found{where}</span>'
                    elif status == "not_found":
                        label = '<span class="hg-cell-status hg-cell-status--negative">× Not found in opinion</span>'
                    else:
                        label = '<span class="hg-cell-status hg-cell-status--neutral">— Could not verify</span>'
                    quote_lines.append(
                        f'<div class="hg-quote-review">{label}'
                        f'<br><span class="hg-quote-text">“{escape(q_text)}”</span></div>'
                    )
                quote_cell = _guard_td("Quotation review", td, "".join(quote_lines))
            else:
                quote_cell = _guard_td("Quotation review", td, "")

        possible_issue_cell = (
            _guard_td("Possible Issues", td, f'<span class="hg-issue-detail">{escape(issue_text)}</span>')
            if show_possible_issues else ""
        )

        rows.append(
            f'<tr class="hg-review-row hg-review-row--{status.lower().replace(" ", "-")}">'
            f'{_guard_td("Citation as drafted", td, as_drafted)}'
            f'{_guard_td("Where cited", td, escape(_guard_compact_location(r.get("location"))))}'
            f'{_guard_td("Citation reviewed", td, case_citation_html)}'
            f'{_guard_td("Result", td_c, status_html)}'
            f'{_guard_td("Authority found", td_c, authority_html)}'
            f'{_guard_td("Supports proposition", td_c, applicable_html)}'
            f"{quote_cell}"
            f"{dym_cell}"
            f"{possible_issue_cell}"
            f"</tr>"
        )

    if n == 0:
        summary = "No case citations found in this document."
    else:
        confirmed = max(0, n - issues - needs_review)
        citation_word = "citation" if n == 1 else "citations"
        parts = [
            f"<strong>{n}</strong> {citation_word} checked",
            f'<strong class="hg-summary-count hg-summary-count--confirmed">{confirmed}</strong> confirmed',
        ]
        if issues:
            issue_word = "issue" if issues == 1 else "issues"
            parts.append(f'<strong class="hg-summary-count hg-summary-count--issue">{issues}</strong> {issue_word} found')
        if needs_review:
            parts.append(f'<strong class="hg-summary-count hg-summary-count--review">{needs_review}</strong> need review')
        summary = " &middot; ".join(parts)

    partial_note = (
        '<p class="hg-partial-review">Partial review — some citations could not be '
        "checked because the document was too large or the source service was temporarily unavailable.</p>"
        if partial_coverage else ""
    )
    coverage_note = (
        f'<p class="hg-partial-review">Coverage safeguard - {recovered} citation(s) were '
        "detected by the citation scanner and added for review.</p>"
        if recovered else ""
    )
    quote_header = _guard_th("Quotation review", th) if show_quotes else ""
    dym_header = _guard_th("Possible match", th) if show_dym else ""
    possible_issues_header = _guard_th("Possible Issues", th) if show_possible_issues else ""

    return (
        f'<div class="hg-review-block"><p class="hg-review-summary"><strong>Citation review</strong> &middot; {summary}</p>{partial_note}{coverage_note}'
        f'<div class="hg-review-table-shell"><table class="hg-review-table">'
        f"<thead><tr>"
        f'{_guard_th("Citation as drafted", th)}'
        f'{_guard_th("Where cited", th)}'
        f'{_guard_th("Citation reviewed", th)}'
        f'{_guard_th("Result", th + "text-align:center;")}'
        f'{_guard_th("Authority found", th + "text-align:center;")}'
        f'{_guard_th("Supports proposition", th + "text-align:center;")}'
        f"{quote_header}"
        f"{dym_header}"
        f"{possible_issues_header}"
        f"</tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        f"</table></div></div>"
    )


def _guard_statutes_to_html(statute_results: list) -> str:
    """Convert parsed statute results to the shared Hallucination Guard table."""
    n = len(statute_results)
    statuses = [_guard_statute_status(r) for r in statute_results]
    issues = statuses.count("Issue")
    needs_review = statuses.count("Needs Review")
    recovered = sum(1 for r in statute_results if isinstance(r, dict) and r.get("coverage_recovered"))
    show_quotes = any(isinstance(r, dict) and r.get("quote_check") for r in statute_results)
    show_possible_issues = _guard_show_possible_issues()

    th = ""
    td = ""
    td_c = "text-align:center;"

    rows = []
    for r in statute_results:
        valid_value = _guard_validity_value(r)
        valid = valid_value is True
        applicable = r.get("is_applicable")
        status = _guard_statute_status(r)
        citation = str(r.get("citation") or "")
        citation_html = escape(citation)
        if r.get("href"):
            href = escape(str(r.get("href")), quote=True)
            citation_html = f'<a href="{href}" target="_blank" rel="noopener noreferrer">{citation_html}</a>'
        as_drafted = escape(str(r.get("citation_as_drafted") or citation))
        issue_text = _guard_issue_text(r)
        provision_html = _guard_cell_status_html(valid_value, positive="Found", negative="Not found")
        if status == "Needs Review" and valid_value is not True:
            provision_html = _guard_status_html(status)
        support_html = _guard_cell_status_html(
            applicable if valid else None,
            positive="Supports",
            negative="Does not support",
        )
        applicable_html = _guard_applicable_cell_html(r, support_html)
        quote_cell = ""
        if show_quotes:
            quote_lines = []
            for quote in r.get("quotes") or []:
                if not isinstance(quote, dict):
                    continue
                quote_text = str(quote.get("text") or "")
                if len(quote_text) > 160:
                    quote_text = quote_text[:157].rstrip() + "…"
                quote_lines.append(
                    '<div class="hg-quote-review">'
                    '<span class="hg-cell-status hg-cell-status--neutral">— source-text check required</span>'
                    f'<br><span class="hg-quote-text">“{escape(quote_text)}”</span></div>'
                )
            quote_cell = _guard_td("Quotation review", td, "".join(quote_lines))
        possible_issue_cell = (
            _guard_td("Possible Issues", td, f'<span class="hg-issue-detail">{escape(issue_text)}</span>')
            if show_possible_issues else ""
        )
        rows.append(
            f'<tr class="hg-review-row hg-review-row--{status.lower().replace(" ", "-")}">'
            f'{_guard_td("Citation as drafted", td, as_drafted)}'
            f'{_guard_td("Citation reviewed", td, citation_html)}'
            f'{_guard_td("Where cited", td, escape(_guard_compact_location(r.get("location"))))}'
            f'{_guard_td("Provision found", td_c, provision_html)}'
            f'{_guard_td("Supports proposition", td_c, applicable_html)}'
            f"{quote_cell}"
            f"{possible_issue_cell}"
            f"</tr>"
        )

    if n == 0:
        summary = "No statute citations found in this document."
    else:
        confirmed = max(0, n - issues - needs_review)
        statute_word = "statute" if n == 1 else "statutes"
        parts = [
            f"<strong>{n}</strong> {statute_word} checked",
            f'<strong class="hg-summary-count hg-summary-count--confirmed">{confirmed}</strong> confirmed',
        ]
        if issues:
            issue_word = "issue" if issues == 1 else "issues"
            parts.append(f'<strong class="hg-summary-count hg-summary-count--issue">{issues}</strong> {issue_word} found')
        if needs_review:
            parts.append(f'<strong class="hg-summary-count hg-summary-count--review">{needs_review}</strong> need review')
        summary = " &middot; ".join(parts)

    possible_issues_header = _guard_th("Possible Issues", th) if show_possible_issues else ""
    quote_header = _guard_th("Quotation review", th) if show_quotes else ""
    coverage_note = (
        f'<p class="hg-partial-review">Coverage safeguard - {recovered} statute(s) were '
        "detected by the citation scanner and added for review.</p>"
        if recovered else ""
    )

    return (
        f'<div class="hg-review-block"><p class="hg-review-summary"><strong>Statute review</strong> &middot; {summary}</p>{coverage_note}'
        f'<div class="hg-review-table-shell"><table class="hg-review-table hg-review-table--statutes">'
        f"<thead><tr>"
        f'{_guard_th("Citation as drafted", th)}'
        f'{_guard_th("Citation reviewed", th)}'
        f'{_guard_th("Where cited", th)}'
        f'{_guard_th("Provision found", th + "text-align:center;")}'
        f'{_guard_th("Supports proposition", th + "text-align:center;")}'
        f"{quote_header}"
        f"{possible_issues_header}"
        f"</tr></thead>"
        f'<tbody>{"".join(rows)}</tbody>'
        f"</table></div></div>"
    )


def _format_draft_for_guard(draft: dict) -> str:
    """Render a structured Everything draft as plain text for the shared HG runner."""
    from backend.util.document_drafts import is_markdown_draft, draft_to_markdown
    if is_markdown_draft(draft):
        return draft_to_markdown(draft) or "(Empty draft)"
    sections = draft.get("sections", [])
    if not sections:
        return "(Empty draft)"
    parts = []
    title = draft.get("title", "")
    if title:
        parts.append(f"Document Title: {title}\n")
    for i, sec in enumerate(sections, 1):
        sec_name = sec.get("section_name", f"Section {i}")
        content = sec.get("section_content", sec.get("content", ""))
        clean = re.sub(r"<[^>]+>", " ", content)
        clean = re.sub(r"\s+", " ", clean).strip()
        parts.append(f"[Section {i}: {sec_name}]\n{clean}")
    return "\n\n".join(parts)


def merge_validated_cases(state: dict, new_cases: dict) -> None:
    """Merge validated citation records into state while preserving enriched fields."""
    vc = state.setdefault("validated_cases", {})
    for key, data in (new_cases or {}).items():
        existing = vc.get(key, {})
        merged = dict(data)
        for field in ("llm_holding", "llm_hist", "court", "freelaw_url"):
            if existing.get(field) is not None:
                merged[field] = existing[field]
        vc[key] = merged


def run_hallucination_guard_on_text(
    file_text: str,
    *,
    state: dict | None = None,
    task=None,
    persist_state=None,
    case_viewer_prefix: str | None = None,
    source_label: str = "uploaded document",
) -> dict:
    """Run the standalone HG backend logic on arbitrary document text.

    This is the shared source of truth used by the Everything adapter and the
    standalone-style uploaded-document task. It deliberately follows the
    standalone-hg workflow: lookup-safe citation scan, validated-case enrichment,
    separate case/statute prompts, semantic name mismatch reconciliation, quote
    verification, web suggestions, and shared HTML rendering.
    """
    from concurrent.futures import ThreadPoolExecutor

    from backend.ai.base import chat_completion_j, num_tokens_from_string

    state = state if isinstance(state, dict) else {}
    file_text = file_text or ""

    _standalone_hg_progress(
        task,
        "Starting citation review...",
        progress=15,
        dev_message=f"Looking for citations in the {source_label}...",
    )
    filtered_citations, lookup_diagnostics = verify_citations_with_lookup_tags_lookup_safe(file_text)

    new_cases = extract_validated_cases_from_citations(
        filtered_citations,
        allowed_tags=LOOKUP_TAGS_ELIGIBLE_FOR_LINKS,
    )
    merge_validated_cases(state, new_cases)
    validated_cases = state.setdefault("validated_cases", {})

    _standalone_hg_progress(
        task,
        "Locating cited authorities...",
        progress=45,
        dev_message="Checking citations against legal database...",
    )
    _enrich_validated_cases_for_standalone(validated_cases)
    if persist_state:
        persist_state(state)

    citation_link_map = _build_citation_link_map(
        filtered_citations,
        validated_cases,
        case_viewer_prefix=case_viewer_prefix,
    )

    _standalone_hg_progress(
        task,
        "Checking quotations and cited propositions...",
        progress=65,
        dev_message="Preparing legal context for analysis...",
    )
    validated_cases_text = _format_validated_cases_for_guard(validated_cases)
    try:
        cases_prompt = _load_local_hallucination_prompt().format(
            validated_cases_text=validated_cases_text,
            draft_text=file_text,
        )
        statutes_prompt = LOCAL_STATUTES_PROMPT_PATH.read_text().format(draft_text=file_text)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return {"error": True, "error_message": f"Unable to load standalone HG prompt files: {exc}"}

    prompt_token_counts = _standalone_hg_prompt_token_counts(
        [cases_prompt, statutes_prompt],
        num_tokens_from_string,
    )
    print(f"[standalone_hg] {HG_ANALYSIS_MODEL} prompt token counts: {prompt_token_counts}")

    token_summary = f"Case prompt: {prompt_token_counts[0]:,} tokens; statute prompt: {prompt_token_counts[1]:,} tokens."
    _standalone_hg_progress(
        task,
        "Checking quotations and cited propositions...",
        progress=80,
        dev_message=f"Analyzing within context... {token_summary}",
        token_counts=prompt_token_counts,
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        # ContextVars do not automatically cross ThreadPoolExecutor boundaries.
        # Give each provider call its own copy of the current analytics scope.
        cases_future = submit_with_context(
            executor,
            chat_completion_j,
            cases_prompt,
            HG_ANALYSIS_MODEL,
            reasoning_effort=HG_ANALYSIS_REASONING_EFFORT,
        )
        statutes_future = submit_with_context(
            executor,
            chat_completion_j,
            statutes_prompt,
            HG_ANALYSIS_MODEL,
            reasoning_effort=HG_ANALYSIS_REASONING_EFFORT,
        )
        cases_llm_output = cases_future.result()
        statutes_llm_output = statutes_future.result()

    if cases_llm_output is None and statutes_llm_output is None:
        return {
            "error": True,
            "error_message": (
                "Hallucination Guard could not analyze this document right now "
                "(the model did not respond). Please try again."
            ),
        }

    case_results = _extract_guard_results(cases_llm_output)
    statute_results = _extract_guard_results(statutes_llm_output)
    _ensure_case_result_coverage(case_results, lookup_diagnostics)
    _ensure_docket_order_coverage(case_results, file_text)
    _ensure_statute_result_coverage(statute_results, filtered_citations)
    _guard_attach_lookup_diagnostics(case_results, lookup_diagnostics)
    _attach_citation_links_to_results(case_results, citation_link_map)
    _attach_citation_links_to_results(statute_results, citation_link_map)

    _standalone_hg_progress(
        task,
        "Finalizing citation review...",
        progress=88,
        dev_message="Verifying quoted language against opinions...",
    )
    try:
        _attach_quote_verification(case_results, validated_cases)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        print(f"[standalone_hg] quote verification step failed: {exc}")
    _attach_statute_quote_review(statute_results, file_text)

    _attach_web_search_suggestions(case_results, task)

    case_results_html = _guard_results_to_html(case_results)
    statute_results_html = _guard_statutes_to_html(statute_results)
    html_response = case_results_html + statute_results_html

    return {
        "case_results": case_results,
        "case_results_html": case_results_html,
        "statute_results": statute_results,
        "statute_results_html": statute_results_html,
        "response": html_response,
        "filtered_citations": filtered_citations,
        "lookup_diagnostics": lookup_diagnostics,
        "prompt_token_counts": prompt_token_counts,
        "show_dev_progress_details": standalone_hg_show_dev_progress_details(),
    }


@shared_task(bind=True, ignore_result=False, soft_time_limit=3600, time_limit=3660)
def standalone_hg_upload_file_task(self, payload: dict) -> dict:
    """Parse and persist one standalone HG source document outside the request worker."""
    draft_public_id = (payload or {}).get("draftId")
    temp_path = (payload or {}).get("temp_path")
    original_filename = os.path.basename((payload or {}).get("filename") or "unknown")
    task_id = _current_task_id(self)
    doc_id = None

    try:
        if not draft_public_id:
            return {"ok": False, "reason": "missing_draft_id", "error": "Missing draft id"}
        if not original_filename or not standalone_hg_allowed_file(original_filename):
            return {
                "ok": False,
                "reason": "unsupported_file_type",
                "error": "Unsupported file type. Please upload a .pdf, .docx, .doc, or .txt file.",
            }
        if not temp_path or not os.path.exists(temp_path):
            return {
                "ok": False,
                "reason": "temp_missing",
                "error": "Temporary file missing before processing",
            }

        doc_id = ChatThread.resolve_id(draft_public_id)
        draft = ChatThread.query.get(doc_id)
        if draft is None or draft.docCategory != STANDALONE_HG_DRAFT_TYPE:
            return {"ok": False, "reason": "invalid_draft", "error": "Draft is not a Hallucination Guard run"}
        from backend.util.account_access import chat_account_id, require_enabled_account
        owner_id = chat_account_id(doc_id)
        require_enabled_account(owner_id)

        if UploadedFiles.query.filter_by(draft_id=doc_id).first() is not None:
            return {
                "ok": False,
                "reason": "file_exists",
                "error": "Only one document is allowed per Hallucination Guard run.",
            }

        file_size = os.path.getsize(temp_path)
        if file_size > STANDALONE_HG_MAX_FILE_SIZE:
            return {
                "ok": False,
                "reason": "file_too_large",
                "error": f"File too large. Maximum size is 15 MB per file (received {file_size / (1024*1024):.1f}MB)",
            }

        _standalone_hg_progress(
            self,
            "Uploading document...",
            progress=15,
            dev_message="Reading uploaded document...",
            filename=original_filename,
            file_size=file_size,
        )
        parsed_text, _, _ = get_complaint_text_from_file(temp_path)
        ocr_warning = get_last_ocr_warning()

        _standalone_hg_progress(
            self,
            "Preparing document and identifying citations...",
            progress=45,
            dev_message="Counting pages and preparing extracted text...",
            filename=original_filename,
            file_size=file_size,
        )
        if original_filename.lower().endswith(".pdf"):
            try:
                page_count = get_pagenum_from_pdf_fitz(temp_path)
            except Exception:
                words = len(parsed_text.split()) if parsed_text else 0
                page_count = max(1, round(words / 400)) if words else 0
        else:
            words = len(parsed_text.split()) if parsed_text else 0
            page_count = max(1, round(words / 400)) if words else 0

        _standalone_hg_progress(
            self,
            "Finishing document preparation...",
            progress=70,
            dev_message="Saving extracted text for Hallucination Guard...",
            filename=original_filename,
            file_size=file_size,
            page_count=page_count,
        )
        # Parsing can outlive the owner's access; do not publish its late result.
        require_enabled_account(owner_id)
        unique_id = f"{uuid.uuid4()}_{original_filename}"
        uploaded_file = UploadedFiles(
            unique_id=unique_id,
            original_filename=original_filename,
            file_size=file_size,
            page_count=page_count,
            content=parsed_text or "",
            draft_id=doc_id,
        )
        db.session.add(uploaded_file)
        db.session.flush()

        chunks = chunk_text(parsed_text or "")
        for c in chunks:
            db.session.add(DocumentChunk(
                uploaded_file_id=uploaded_file.id,
                draft_id=doc_id,
                chunk_index=c["chunk_index"],
                content=c["content"],
                token_count=c["token_count"],
                char_count=c["char_count"],
                source_filename=original_filename,
                source_relative_path=None,
                source_unique_id=unique_id,
            ))
        db.session.commit()

        mem = DBMemory(db=db, doc_id=doc_id)
        mem[f"inputs/{unique_id}"] = parsed_text or ""
        mem["inputs/uploaded_filename"] = original_filename
        clear_standalone_hg_result_state(mem)

        _standalone_hg_progress(
            self,
            "Finishing document preparation...",
            progress=100,
            dev_message="Document uploaded and parsed.",
            filename=original_filename,
            file_size=file_size,
            page_count=page_count,
            chunk_count=len(chunks),
        )
        return {
            "ok": True,
            "unique_id": unique_id,
            "filename": original_filename,
            "file_size": file_size,
            "page_count": page_count,
            "chunk_count": len(chunks),
            "uploaded_file": _standalone_hg_uploaded_file_payload(uploaded_file),
            "ocr_warning": ocr_warning,
            "ocr_warning_message": "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            "show_dev_progress_details": standalone_hg_show_dev_progress_details(),
        }
    except Exception as exc:
        db.session.rollback()
        return {
            "ok": False,
            "reason": "processing_error",
            "error": str(exc),
            "show_dev_progress_details": standalone_hg_show_dev_progress_details(),
        }
    finally:
        if doc_id is not None:
            try:
                clear_standalone_hg_active_task(
                    DBMemory(db=db, doc_id=doc_id),
                    STANDALONE_HG_ACTIVE_UPLOAD_TASK_KEY,
                    expected_task_id=task_id,
                )
            except Exception:
                pass
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except OSError:
                pass


@shared_task(bind=True, ignore_result=False, soft_time_limit=1200, time_limit=1260)
def standalone_hg_task(self, data: dict) -> dict:
    """Run the standalone HG audit, converting any unhandled failure into a friendly, logged error.

    The body (`_standalone_hg_impl`) does many DB writes and external calls (CourtListener, OpenAI);
    an unhandled exception there would otherwise surface to the UI as the opaque generic error. Here
    we log the full traceback (for diagnosis) and return a clean retryable message instead.
    """
    import traceback
    draft_public_id = (data or {}).get("draftId")
    task_id = _current_task_id(self)
    doc_id = None
    try:
        if draft_public_id:
            doc_id = ChatThread.resolve_id(draft_public_id)
        return _standalone_hg_impl(self, data)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        print(f"[standalone_hg] draft={draft_public_id} failed: {exc}\n{traceback.format_exc()}")
        return {
            "error": True,
            "error_message": (
                "Hallucination Guard ran into a problem analyzing this document. "
                "Please try again — if it keeps happening, contact support."
            ),
            "show_dev_progress_details": standalone_hg_show_dev_progress_details(),
        }
    finally:
        if doc_id is not None:
            try:
                clear_standalone_hg_active_task(
                    DBMemory(db=db, doc_id=doc_id),
                    STANDALONE_HG_ACTIVE_HG_TASK_KEY,
                    expected_task_id=task_id,
                )
            except Exception:
                pass


@guided_step_operation(DOC_TYPE, RUN_STEP, lambda self, data: data["draftId"])
def _standalone_hg_impl(self, data: dict) -> dict:
    # Local import keeps helper-unit tests lightweight in environments without
    # full runtime deps while preserving production behavior.
    from backend.everything import load_state_from_memory, save_state_to_memory

    draft_public_id = data["draftId"]
    doc_id = ChatThread.resolve_id(draft_public_id)

    uploaded_file = UploadedFiles.query.filter_by(draft_id=doc_id).order_by(UploadedFiles.upload_time.desc()).first()
    if uploaded_file is None or not uploaded_file.content:
        return {"error": True, "error_message": "Please upload a document before running Hallucination Guard."}

    mem = DBMemory(db=db, doc_id=doc_id)
    state = load_state_from_memory(mem)

    result = run_hallucination_guard_on_text(
        uploaded_file.content,
        state=state,
        task=self,
        persist_state=lambda updated_state: save_state_to_memory(updated_state, mem, auxiliary_fields=('validated_cases',)),
        source_label="uploaded document",
    )

    if result.get("error_message"):
        mem["standalone_hg/error_message"] = result["error_message"]
        return result

    mem["standalone_hg/filtered_citations"] = json.dumps(result.get("filtered_citations", []))
    mem["standalone_hg/lookup_diagnostics"] = json.dumps(result.get("lookup_diagnostics", []))
    mem["standalone_hg/case_results"] = json.dumps(result.get("case_results", []))
    mem["standalone_hg/case_results_html"] = result.get("case_results_html", "")
    mem["standalone_hg/statute_results"] = json.dumps(result.get("statute_results", []))
    mem["standalone_hg/statute_results_html"] = result.get("statute_results_html", "")
    mem["inputs/uploaded_filename"] = uploaded_file.original_filename
    mem["standalone_hg/prompt_token_counts"] = json.dumps(result.get("prompt_token_counts", []))

    result["uploaded_file"] = {
        "unique_id": uploaded_file.unique_id,
        "filename": uploaded_file.original_filename,
        "file_size": uploaded_file.file_size,
        "upload_time": uploaded_file.upload_time.isoformat() if uploaded_file.upload_time else None,
    }
    result["show_dev_progress_details"] = standalone_hg_show_dev_progress_details()
    return result


def route_standalone_hg(request):
    data = request.get_json(silent=True) or request.form
    draft_id = data.get("draftId")
    if not draft_id:
        return {"msg": "No draftId provided"}, 400

    doc_id = ChatThread.resolve_id(draft_id)
    uploaded_file = UploadedFiles.query.filter_by(draft_id=doc_id).first()
    if uploaded_file is None or not uploaded_file.content:
        return {"msg": "Please upload a document before running Hallucination Guard."}, 400

    mem = DBMemory(db=db, doc_id=doc_id)
    active_task_id = get_active_standalone_hg_task_id(doc_id, STANDALONE_HG_ACTIVE_HG_TASK_KEY)
    if active_task_id:
        return {
            "result_id": active_task_id,
            "already_running": True,
            "show_dev_progress_details": standalone_hg_show_dev_progress_details(),
        }, 200

    clear_standalone_hg_result_state(mem, reset_validated_cases=False)
    result = standalone_hg_task.delay({"draftId": draft_id})
    mem[STANDALONE_HG_ACTIVE_HG_TASK_KEY] = result.id
    return {"result_id": result.id, "show_dev_progress_details": standalone_hg_show_dev_progress_details()}, 200
