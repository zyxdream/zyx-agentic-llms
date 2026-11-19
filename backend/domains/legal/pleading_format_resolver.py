"""Resolve pleading-paper layout spec for Workspace court export."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from typing import Any

from pydantic import BaseModel

from backend.ai.base import chat_completion_j, chat_completion_parse
from backend.domains.legal.pleading_format import (
    DEFAULT_CAED_PLEADING_SPEC,
    DEFAULT_EXPORT_SPEC,
    DEFAULT_STRUCTURAL_SPEC,
)

PROMPT_PATH = "backend/ai/prompts/legal/PLEADING_FORMAT_REQUIREMENTS.txt"

_MARGIN_MIN = 0.5
_MARGIN_MAX = 2.0
_FONT_SIZE_MIN = 10
_FONT_SIZE_MAX = 14
_LINE_SPACING_MIN = 12
_LINE_SPACING_MAX = 48
_INDENT_MIN = 0.0
_INDENT_MAX = 2.0

_CAPTION_STRING_FIELDS = (
    "court_line_1",
    "court_line_2",
    "plaintiff",
    "defendant",
    "case_number",
    "pleading_title",
    "attorney_contact",
)

_MEMO_TITLE_PATTERNS = (
    "analysis",
    "memo",
    "research",
    "summary",
    "outline",
    "briefing",
)

_FORMAL_FILING_TITLE_PATTERNS = (
    "complaint",
    "answer",
    "counterclaim",
    "crossclaim",
    "cross-claim",
    "motion",
    "opposition",
    "reply",
    "petition",
    "application",
    "brief",
    "memorandum of points and authorities",
    "notice of motion",
    "demurrer",
)

_CA_VERTICAL_LINE_MARKERS = (
    "california",
    "caed",
    "superior court",
    "eastern district of california",
    "central district of california",
    "northern district of california",
    "southern district of california",
)

logger = logging.getLogger(__name__)


class _MarginsResult(BaseModel):
    left: float | None
    right: float | None
    top: float | None
    bottom: float | None


class _BodyResult(BaseModel):
    first_line_indent_in: float | None
    line_spacing_pt: float | None
    line_spacing_rule: str | None


class _ContinuedBodyResult(BaseModel):
    first_line_indent_in: float | None
    line_spacing_pt: float | None


class _QuoteResult(BaseModel):
    left_indent_in: float | None
    right_indent_in: float | None
    line_spacing_pt: float | None
    alignment: str | None


class _TitleResult(BaseModel):
    bold: bool | None
    all_caps: bool | None
    centered: bool | None


class _IndentedTitleResult(BaseModel):
    bold: bool | None
    left_indent_in: float | None


class _TitleIndentResult(BaseModel):
    left_indent_in: float | None


class _TitleHangingResult(BaseModel):
    left_indent_in: float | None
    hanging_indent_in: float | None


class _LayoutResult(BaseModel):
    font_name: str | None
    font_size_pt: float | None
    page_margins_in: _MarginsResult | None
    body: _BodyResult | None
    body_continued: _ContinuedBodyResult | None
    block_quote: _QuoteResult | None
    title: _TitleResult | None
    title1: _IndentedTitleResult | None
    title2: _TitleIndentResult | None
    title3: _TitleHangingResult | None
    source: str | None


class _StructuralResult(BaseModel):
    require_caption_page: bool
    require_numbered_paragraphs: bool
    continuous_paragraph_numbering: bool
    use_vertical_lines: bool
    caption_ready: bool
    caption_source_section_indices: list[int]


class _CaptionResult(BaseModel):
    court_line_1: str
    court_line_2: str
    plaintiff: str
    defendant: str
    case_number: str
    pleading_title: str
    attorney_contact: str | None


class PleadingFormatResult(BaseModel):
    confidence: str
    jurisdiction: str
    court: str
    pleading_type: str
    document_class: str
    caption_reason: str
    sources: list[str]
    structural: _StructuralResult
    caption: _CaptionResult
    layout: _LayoutResult | None


def build_draft_excerpt(
    draft: dict,
    *,
    max_sections: int = 5,
    max_chars_per_section: int = 1200,
    max_total_chars: int = 5000,
) -> str:
    """Build a truncated excerpt from draft title and opening sections for classification.

    Each section is labeled with its 0-based index in the original `draft["sections"]` list so the
    LLM can report which leading sections are caption material (see caption_source_section_indices).
    """
    if not isinstance(draft, dict):
        draft = {}
    parts: list[str] = []
    title = (draft.get("title") or "").strip()
    if title:
        parts.append(f"DOCUMENT TITLE:\n{title}")

    sections = draft.get("sections", [])
    if not isinstance(sections, list):
        sections = []

    emitted = 0
    for index, sec in enumerate(sections):
        if emitted >= max_sections:
            break
        if not isinstance(sec, dict):
            continue
        name = (sec.get("section_name") or "").strip()
        content = sec.get("section_content") or ""
        if not isinstance(content, str):
            content = str(content)
        content = content.strip()
        if not name and not content:
            continue
        chunk = content[:max_chars_per_section]
        if len(content) > max_chars_per_section:
            chunk += "\n[... truncated ...]"
        section_id = str(sec.get("section_id") or "").strip()
        id_label = f" id={section_id}" if section_id else ""
        parts.append(f"---\n[index {index}]{id_label} SECTION: {name or '(unnamed)'}\n{chunk}")
        emitted += 1

    excerpt = "\n\n".join(parts)
    if len(excerpt) > max_total_chars:
        excerpt = excerpt[:max_total_chars] + "\n[... excerpt truncated ...]"
    return excerpt or "(empty draft)"


def extract_heuristic_caption(
    draft: dict,
    constraints: dict | None,
) -> dict:
    """Pre-fill caption fields from constraints and draft excerpt (no invention)."""
    hints: dict[str, str] = {}
    if constraints and isinstance(constraints.get("cases"), dict):
        for case_data in constraints["cases"].values():
            if not isinstance(case_data, dict):
                continue
            court = (case_data.get("court") or "").strip()
            if court and not hints.get("court_line_2"):
                if "united states district court" in court.lower():
                    hints["court_line_1"] = "UNITED STATES DISTRICT COURT"
                    district = court.lower().replace("united states district court", "").strip(" ,")
                    if district:
                        hints["court_line_2"] = district.upper()
                elif "superior court" in court.lower():
                    hints["court_line_1"] = "SUPERIOR COURT OF THE STATE OF CALIFORNIA"
                    hints["court_line_2"] = court.upper()
                elif "supreme court" in court.lower() and "new york" in court.lower():
                    hints["court_line_1"] = "SUPREME COURT OF THE STATE OF NEW YORK"
                    hints["court_line_2"] = court.upper()
                else:
                    hints["court_line_1"] = court.upper()
            parties = case_data.get("party_names") or []
            if isinstance(parties, list) and len(parties) >= 1 and not hints.get("plaintiff"):
                hints["plaintiff"] = str(parties[0]).strip()
            if isinstance(parties, list) and len(parties) >= 2 and not hints.get("defendant"):
                hints["defendant"] = str(parties[1]).strip()

    title = (draft.get("title") or "").strip() if isinstance(draft, dict) else ""
    if title:
        hints.setdefault("pleading_title", title)

    return hints


def _draft_title_text(draft: dict | None) -> str:
    if not isinstance(draft, dict):
        return ""
    return (draft.get("title") or "").strip().lower()


def _is_internal_work_product_title(title: str) -> bool:
    return any(pattern in title for pattern in _MEMO_TITLE_PATTERNS)


def _has_formal_filing_title(draft: dict | None) -> bool:
    title = _draft_title_text(draft)
    if not title or _is_internal_work_product_title(title):
        return False
    return any(pattern in title for pattern in _FORMAL_FILING_TITLE_PATTERNS)


def _court_context_text(hints: dict, constraints: dict | None) -> str:
    parts = [
        hints.get("court_line_1") or "",
        hints.get("court_line_2") or "",
    ]
    if constraints and isinstance(constraints.get("cases"), dict):
        for case_data in constraints["cases"].values():
            if not isinstance(case_data, dict):
                continue
            parts.append(case_data.get("court") or "")
            parts.append(case_data.get("jurisdiction") or "")
    return " ".join(parts).lower()


def _is_california_pleading_paper(court_text: str) -> bool:
    return any(marker in court_text for marker in _CA_VERTICAL_LINE_MARKERS)


def infer_export_policy_heuristic(
    draft: dict,
    constraints: dict | None,
) -> dict[str, bool]:
    """Infer caption/vertical-line flags when LLM output is unavailable."""
    title = _draft_title_text(draft)
    if _is_internal_work_product_title(title):
        return {
            "require_caption_page": False,
            "caption_ready": False,
            "use_vertical_lines": False,
        }

    hints = extract_heuristic_caption(draft, constraints)
    court_text = _court_context_text(hints, constraints)
    has_cases = bool(
        constraints
        and isinstance(constraints.get("cases"), dict)
        and constraints["cases"]
    )
    has_court = bool(hints.get("court_line_1"))

    if has_cases and has_court:
        return {
            "require_caption_page": True,
            "caption_ready": True,
            "use_vertical_lines": _is_california_pleading_paper(court_text),
        }

    if _has_formal_filing_title(draft):
        return {
            "require_caption_page": True,
            "caption_ready": True,
            "use_vertical_lines": _is_california_pleading_paper(court_text),
        }

    return {
        "require_caption_page": False,
        "caption_ready": False,
        "use_vertical_lines": False,
    }


def _finalize_export_policy(
    export: dict,
    draft: dict | None = None,
    constraints: dict | None = None,
) -> None:
    """Apply post-merge rules for caption page and template variant."""
    structural = export["structural"]
    meta = export.setdefault("_meta", {})
    confidence = str(meta.get("confidence", "low")).lower()
    formal_filing_title = _has_formal_filing_title(draft)

    if confidence == "low" and not formal_filing_title:
        structural["require_caption_page"] = False
        structural["use_vertical_lines"] = False

    if formal_filing_title:
        structural["require_caption_page"] = True
        structural["caption_ready"] = True
        caption = structural.setdefault("caption", {})
        if caption.get("pleading_title") in ("", "TITLE OF PLEADING", None):
            caption["pleading_title"] = (draft or {}).get("title", "").strip()
        if confidence == "low":
            meta["confidence"] = "medium"
        if not meta.get("caption_reason"):
            meta["caption_reason"] = "formal filing title"

    if structural.get("require_caption_page") and not structural.get("caption_ready"):
        structural["require_caption_page"] = False

    if not structural.get("require_caption_page"):
        structural["use_vertical_lines"] = False
        # Caption-source indices are only meaningful when a caption page is produced.
        structural["caption_source_section_indices"] = []

    if not meta.get("document_class"):
        if structural.get("require_caption_page"):
            meta["document_class"] = meta.get("pleading_type") or "formal_pleading"
        elif draft and isinstance(draft, dict):
            title = _draft_title_text(draft)
            if _is_internal_work_product_title(title):
                meta["document_class"] = "internal_analysis"
            else:
                meta["document_class"] = "unknown"
        else:
            meta["document_class"] = "unknown"


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, float(value)))


def _merge_margins(base: dict, raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for key in ("left", "right", "top", "bottom"):
        if key in raw and isinstance(raw[key], (int, float)):
            base[key] = _clamp(raw[key], _MARGIN_MIN, _MARGIN_MAX)


def _merge_body_like(base: dict, raw: Any, *, allow_rule: bool = False) -> None:
    if not isinstance(raw, dict):
        return
    if "first_line_indent_in" in raw and isinstance(raw["first_line_indent_in"], (int, float)):
        base["first_line_indent_in"] = _clamp(
            raw["first_line_indent_in"], _INDENT_MIN, _INDENT_MAX
        )
    if "line_spacing_pt" in raw and isinstance(raw["line_spacing_pt"], (int, float)):
        base["line_spacing_pt"] = int(
            _clamp(raw["line_spacing_pt"], _LINE_SPACING_MIN, _LINE_SPACING_MAX)
        )
    if allow_rule and raw.get("line_spacing_rule") in ("exact", "exactly", "single", "double"):
        rule = raw["line_spacing_rule"]
        base["line_spacing_rule"] = "exact" if rule == "exactly" else rule


def _merge_title_style(base: dict, raw: Any) -> None:
    if not isinstance(raw, dict):
        return
    for key in ("bold", "all_caps", "centered"):
        if key in raw and isinstance(raw[key], bool):
            base[key] = raw[key]
    if "left_indent_in" in raw and isinstance(raw["left_indent_in"], (int, float)):
        base["left_indent_in"] = _clamp(raw["left_indent_in"], _INDENT_MIN, _INDENT_MAX)
    if "hanging_indent_in" in raw and isinstance(raw["hanging_indent_in"], (int, float)):
        base["hanging_indent_in"] = _clamp(raw["hanging_indent_in"], _INDENT_MIN, _INDENT_MAX)


def _merge_layout(layout: dict, raw: dict) -> None:
    """Merge layout fields from LLM response (nested or flat top-level)."""
    src = raw.get("layout") if isinstance(raw.get("layout"), dict) else raw
    if not isinstance(src, dict):
        return
    if isinstance(src.get("font_name"), str) and src["font_name"].strip():
        layout["font_name"] = src["font_name"].strip()[:100]
    if isinstance(src.get("font_size_pt"), (int, float)):
        layout["font_size_pt"] = int(_clamp(src["font_size_pt"], _FONT_SIZE_MIN, _FONT_SIZE_MAX))
    _merge_margins(layout["page_margins_in"], src.get("page_margins_in"))
    _merge_body_like(layout["body"], src.get("body"), allow_rule=True)
    _merge_body_like(layout["body_continued"], src.get("body_continued"))
    bq = src.get("block_quote")
    if isinstance(bq, dict):
        _merge_body_like(layout["block_quote"], bq)
        if bq.get("left_indent_in") is not None:
            layout["block_quote"]["left_indent_in"] = _clamp(
                bq["left_indent_in"], _INDENT_MIN, _INDENT_MAX
            )
        if bq.get("right_indent_in") is not None:
            layout["block_quote"]["right_indent_in"] = _clamp(
                bq["right_indent_in"], _INDENT_MIN, _INDENT_MAX
            )
        if bq.get("alignment") in ("justify", "justified", "left", "right", "center"):
            align = bq["alignment"]
            layout["block_quote"]["alignment"] = "justify" if align == "justified" else align
    _merge_title_style(layout["title"], src.get("title"))
    _merge_title_style(layout["title1"], src.get("title1"))
    _merge_title_style(layout["title2"], src.get("title2"))
    _merge_title_style(layout["title3"], src.get("title3"))
    if isinstance(src.get("source"), str) and src["source"].strip():
        layout["source"] = src["source"].strip()[:500]


def _merge_caption(caption: dict, raw_caption: Any) -> None:
    if not isinstance(raw_caption, dict):
        return
    for key in _CAPTION_STRING_FIELDS:
        val = raw_caption.get(key)
        if isinstance(val, str) and val.strip():
            caption[key] = val.strip()[:500]


def _merge_caption_source_indices(structural: dict, raw: dict, *, num_sections: int | None = None) -> None:
    """Validate structural.caption_source_section_indices from the LLM response.

    Accepts a list, coerces to non-negative ints, drops out-of-range/invalid entries, dedupes and
    sorts. These are 0-based indices (matching build_draft_excerpt's [index N] labels) of leading
    sections that are purely caption material and should be dropped from the exported body.
    """
    raw_struct = raw.get("structural")
    if not isinstance(raw_struct, dict):
        return
    raw_indices = raw_struct.get("caption_source_section_indices")
    if not isinstance(raw_indices, list):
        return
    cleaned: set[int] = set()
    for value in raw_indices:
        try:
            idx = int(value)
        except (TypeError, ValueError):
            continue
        if idx < 0:
            continue
        if num_sections is not None and idx >= num_sections:
            continue
        cleaned.add(idx)
    structural["caption_source_section_indices"] = sorted(cleaned)


def _merge_structural_flags(structural: dict, raw: dict) -> None:
    raw_struct = raw.get("structural")
    if not isinstance(raw_struct, dict):
        return
    for key in (
        "require_caption_page",
        "require_numbered_paragraphs",
        "continuous_paragraph_numbering",
        "use_vertical_lines",
        "caption_ready",
    ):
        if key in raw_struct and isinstance(raw_struct[key], bool):
            structural[key] = raw_struct[key]


def merge_llm_format_response(
    raw: dict | None,
    *,
    heuristic_caption: dict | None = None,
    draft: dict | None = None,
    constraints: dict | None = None,
) -> dict:
    """Merge validated LLM JSON onto DEFAULT_EXPORT_SPEC."""
    export = copy.deepcopy(DEFAULT_EXPORT_SPEC)
    layout = export["layout"]
    structural = export["structural"]
    caption = structural["caption"]

    if heuristic_caption:
        _merge_caption(caption, heuristic_caption)

    if not isinstance(raw, dict):
        policy = infer_export_policy_heuristic(draft or {}, constraints)
        for key, value in policy.items():
            structural[key] = value
        confidence = "medium" if policy.get("require_caption_page") else "low"
        export["_meta"] = {
            "confidence": confidence,
            "jurisdiction": "",
            "court": "",
            "pleading_type": "",
            "document_class": "",
            "caption_reason": "heuristic fallback",
            "sources": [],
        }
        _finalize_export_policy(export, draft, constraints)
        return export

    confidence = str(raw.get("confidence", "low")).lower()
    export["_meta"] = {
        "confidence": confidence,
        "jurisdiction": str(raw.get("jurisdiction", "") or ""),
        "court": str(raw.get("court", "") or ""),
        "pleading_type": str(raw.get("pleading_type", "") or ""),
        "document_class": str(raw.get("document_class", "") or ""),
        "caption_reason": str(raw.get("caption_reason", "") or ""),
        "sources": raw.get("sources") if isinstance(raw.get("sources"), list) else [],
    }

    _merge_structural_flags(structural, raw)
    num_sections = len(draft.get("sections", [])) if isinstance(draft, dict) else None
    _merge_caption_source_indices(structural, raw, num_sections=num_sections)
    _merge_caption(caption, raw.get("caption"))

    if confidence == "low":
        _finalize_export_policy(export, draft, constraints)
        return export

    _merge_layout(layout, raw)
    _finalize_export_policy(export, draft, constraints)
    return export


def _parse_llm_json(result: str | None) -> dict | None:
    if not result:
        return None
    if isinstance(result, dict):
        return result
    text = result.strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                parsed = json.loads(match.group(0))
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
    return None


def _load_prompt_template() -> str:
    path = PROMPT_PATH
    if os.path.isfile(path):
        return open(path, encoding="utf-8").read()
    return "Determine pleading paper layout from the draft excerpt. Return JSON only."


def _build_resolver_prompt(
    excerpt: str,
    constraints: dict | None,
    conversation_brief: str | None,
) -> str:
    template = _load_prompt_template()
    constraints_str = json.dumps(constraints, indent=2) if constraints else "{}"
    brief_str = (conversation_brief or "").strip() or "(none)"
    return f"""{template}

DRAFT EXCERPT:
{excerpt}

SESSION CONSTRAINTS:
{constraints_str}

CONVERSATION BRIEF:
{brief_str}
"""


def fetch_raw_format_requirements_llm(
    draft: dict,
    *,
    constraints: dict | None = None,
    conversation_brief: str | None = None,
    model: str = "gpt-6-luna",
) -> tuple[str | None, dict | None]:
    """Call the LLM and return (raw_output_text, parsed_dict). For integration tests/debug."""
    excerpt = build_draft_excerpt(draft)
    prompt = _build_resolver_prompt(excerpt, constraints, conversation_brief)
    result = chat_completion_j(
        prompt,
        model=model,
        sys_prompt="You output JSON layout requirements for legal Word export only.",
    )
    raw_text = result if isinstance(result, str) else (json.dumps(result) if result else None)
    parsed = _parse_llm_json(result)
    return raw_text, parsed


def resolve_format_spec_for_export(
    draft: dict,
    *,
    constraints: dict | None = None,
    conversation_brief: str | None = None,
    model: str = "gpt-6-luna",
    reasoning_effort: str | None = None,
) -> dict:
    """Return full export spec (structural + layout) for Word export."""
    excerpt = build_draft_excerpt(draft)
    prompt = _build_resolver_prompt(excerpt, constraints, conversation_brief)
    hints = extract_heuristic_caption(draft, constraints)

    try:
        result = chat_completion_parse(
            prompt,
            PleadingFormatResult,
            model=model,
            reasoning_effort=reasoning_effort,
            sys_prompt="You output JSON layout requirements for legal Word export only.",
        )
        raw = result.model_dump(exclude_none=True) if result is not None else None
        if raw is None:
            logger.warning(
                "pleading_format_resolver: no parsed LLM result; using default export spec"
            )
            return merge_llm_format_response(
                None, heuristic_caption=hints, draft=draft, constraints=constraints
            )
        spec = merge_llm_format_response(
            raw, heuristic_caption=hints, draft=draft, constraints=constraints
        )
        meta = spec.get("_meta", {})
        logger.info(
            "pleading_format_resolver: confidence=%s jurisdiction=%s pleading_type=%s "
            "require_caption=%s use_vertical_lines=%s",
            meta.get("confidence"),
            meta.get("jurisdiction"),
            meta.get("pleading_type"),
            spec["structural"].get("require_caption_page"),
            spec["structural"].get("use_vertical_lines"),
        )
        return spec
    except Exception as e:
        logger.exception(
            "pleading_format_resolver: LLM error (%s); using default export spec",
            e,
        )
        return merge_llm_format_response(
            None, heuristic_caption=hints, draft=draft, constraints=constraints
        )
