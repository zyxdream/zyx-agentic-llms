# The Flask parts of response drafter.

import difflib
import logging
import os
import re
from functools import wraps
from backend.util.analytics import guided_step_operation, submit_with_context
from backend.ai.base import num_tokens_from_string
from backend.ai import base as ai_base
from backend.ai.base import chat_completion, chat_completion_j, chat_completion_parse, extract_headings
from backend.ai.answer_schemas import (
    AnswerComplaintStructure,
    AnswerSectionChunk,
    AnswerParagraphChunk,
    AnswerFactualDigestChunk,
    AnswerFactualDigestReduction,
    AnswerDefenseSuggestions,
    AnswerParagraphResponses,
    AnswerDraftedDefenses,
)
from backend.util.db import BaseMemory, DBMemory
from backend.constants import API_URL, STATIC_FOLDER, SOURCE_CODE_ROOT, PYTHON_EXECUTABLE
from backend.util.data_tools import (
    remove_empty_rows,
    selected_answers2text,
    get_complaint_text_from_file,
    get_complaint_text_with_line_metadata_from_file,
    log_complaint_text_metadata_preview,
    prepare_complaint_text_for_memory,
    process_is_federal_output,
    extract_from_json,
)
from backend.util.data_tools import handle_file_upload
from backend.util.guided_draft_reset import ANSWER_RESET_KEYS, clear_memory_keys
from backend.ai.complaint_parser import get_text_from_pdf_fitz
from backend.ai.complaint_parser import get_last_ocr_warning
from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from celery.result import AsyncResult
from backend.database import db
from backend.models import Drafts
from backend.pdf_creator import create_answer_pdf
from backend.word_creator import create_answer_word
import json
import subprocess
from time import sleep
from backend.util.free_law_tools import (
    find_citations,
    unpack_cluster,
    find_USC_citation,
    find_state_codes,
    filter_raw_citations,
    insert_tags,
    filter_on_casename,
    iter_citation_lookup_chunks,
)
import concurrent.futures


logger = logging.getLogger(__name__)


DOC_TYPE = "answer"
DOWNLOAD_STEP = 5
ANSWER_PARA_PRIMARY_LINES = 500
ANSWER_PARA_CONTEXT_LINES = 40
ANSWER_PARA_EXTRACT_WORKERS = 8
ANSWER_PARA_RETRY_PRIMARY_LINES = 250
ANSWER_PARA_WARNING_ISSUE_LIMIT = 8
ANSWER_PARA_SMALL_MISSING_GAP_LIMIT = 10
ANSWER_PARA_SUSPICIOUS_GAP_THRESHOLD = 10
ANSWER_STRUCTURE_PAGE_LIMIT = 20
ANSWER_STRUCTURE_FALLBACK_LINES_PER_PAGE = 60
ANSWER_SECTION_PRIMARY_LINES = 500
ANSWER_SECTION_CONTEXT_BEFORE_LINES = 50
ANSWER_SECTION_CONTEXT_AFTER_LINES = 75
ANSWER_SECTION_MAX_PROMPT_TOKENS = 60000
ANSWER_SECTION_EXTRACT_WORKERS = 8
ANSWER_SECTION_VALID_TYPES = (
    "introduction",
    "parties",
    "jurisdiction_venue",
    "factual_allegations",
    "class_allegations",
    "cause_of_action",
    "damages",
    "prayer",
    "jury_demand",
    "signature",
    "certificate",
    "exhibit",
    "other",
)
ANSWER_SECTION_MAJOR_TYPES = {
    "introduction",
    "parties",
    "jurisdiction_venue",
    "factual_allegations",
    "class_allegations",
    "cause_of_action",
    "damages",
    "prayer",
    "jury_demand",
    "signature",
    "certificate",
    "exhibit",
}
ANSWER_SECTION_COA_BOUNDARY_TYPES = {
    "cause_of_action",
    "prayer",
    "jury_demand",
    "signature",
    "certificate",
    "exhibit",
}
ANSWER_FACT_DIGEST_PRIMARY_LINES = 1000
ANSWER_FACT_DIGEST_CONTEXT_BEFORE_LINES = 100
ANSWER_FACT_DIGEST_CONTEXT_AFTER_LINES = 150
ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS = 60000
ANSWER_FACT_DIGEST_EXTRACT_WORKERS = 8
ANSWER_AD_SUGGEST_WORKERS = 8
ANSWER_AD_CITATION_FILTER_WORKERS = 20
ANSWER_HG_AD_CHUNK_CHARS = 50000
ANSWER_HG_AD_CHUNK_OVERLAP = 500
ANSWER_AD_NAME_ALIASES = {
    "limitations": "Statute of Limitations",
    "statute of limitations": "Statute of Limitations",
    "statute limitations": "Statute of Limitations",
    "failure to mitigate": "Failure to Mitigate Damages",
    "failure to mitigate damages": "Failure to Mitigate Damages",
    "estoppel": "Equitable Estoppel",
    "equitable estoppel": "Equitable Estoppel",
    "failure to state claim": "Failure to State a Claim",
    "failure to state a claim": "Failure to State a Claim",
    "reservation of rights": "Reservation of Right to Amend",
    "reservation of right to amend": "Reservation of Right to Amend",
}
ANSWER_RESPONSE_PARAS_PER_CALL = 100
ANSWER_RESPONSE_WORKERS = 8
ANSWER_RESPONSE_MAX_PROMPT_TOKENS = 60000
ANSWER_TASK_TIME_LIMIT_SECONDS = 1200
ANSWER_TASK_SOFT_TIME_LIMIT_SECONDS = 1170
ANSWER_EXTRACTION_MODEL = "gpt-6-luna"
ANSWER_EXTRACTION_EFFORT = "medium"
ANSWER_RESPONSE_MODEL = "gpt-6.1-sol"
ANSWER_RESPONSE_EFFORT = "high"
ANSWER_DEFENSE_MODEL = ai_base.DEFAULT_MODEL
ANSWER_DEFENSE_EFFORT = "high"
ANSWER_TEXT_MODEL = "gpt-6.1-sol"
ANSWER_TEXT_EFFORT = "high"


CIT_FILE = os.path.join(os.path.dirname(__file__), "citation_pairs.jsonl")
MISMATCH_FILE = os.path.join(os.path.dirname(__file__), "mismatch_citation_pairs.jsonl")


def _answer_soft_timeout_error(step_name: str) -> dict:
    return {
        "message": "error",
        "error_message": (
            f"Answer Drafter timed out while {step_name}. This can happen with very long "
            "complaints or temporary AI/API slowdowns. Please try again."
        ),
    }


def _answer_drafter_task(step_name: str, **task_options):
    options = {
        "ignore_result": False,
        "soft_time_limit": ANSWER_TASK_SOFT_TIME_LIMIT_SECONDS,
        "time_limit": ANSWER_TASK_TIME_LIMIT_SECONDS,
    }
    options.update(task_options)

    def decorator(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except SoftTimeLimitExceeded:
                logger.warning("Answer Drafter task timed out while %s.", step_name, exc_info=True)
                return _answer_soft_timeout_error(step_name)

        return shared_task(**options)(wrapped)

    return decorator


def draft_full_answer(mem: BaseMemory, fn: str) -> str:
    """Drafts an answer from available elements in memory."""

    # create formatted pdf
    gen_pdf_status = create_answer_pdf(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        responses=mem.get('drafted_sections/responses', ''),
                                        affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
                                        counter_claims=mem.get('drafted_sections/counter_claims', ''),
                                          )
    return gen_pdf_status


def draft_full_answer_word(mem: BaseMemory, fn: str) -> str:
    """Drafts an answer from available elements in memory."""
    gen_word_status = create_answer_word(fn=fn,
                                        plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        defendant=mem.get('inputs/defendant', "specify defendant"),
                                        attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        introduction=mem.get('drafted_sections/intro', ''),
                                        responses=mem.get('drafted_sections/responses', ''),
                                        affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
                                        counter_claims=mem.get('drafted_sections/counter_claims', ''),
                                          )
    return  gen_word_status


def expand_outline(outline_raw: str) -> list[str]:
    """Expand an outline form to a list of strings to be displayed.
    Input:
    III. FACTUAL ALLEGATIONS
    A. Digital Envoy and its Technology
    8-9
    B. Google Background
    18-19

    Output:  [('III. FACTUAL ALLEGATIONS', None), ('A. Digital Envoy and its Technology', None), ('Paragraph 8', 8), ('Paragraph 9', 9), ('Paragraph 10', 10), ('B. Google Background', None), ('Paragraph 11', 11), ('Paragraph 12', 12), ('Paragraph 13', 13), ('Paragraph 14', 14), ('Paragraph 15', 15), ('Paragraph 16', 16), ('Paragraph 17', 17), ('Paragraph 18', 18), ('Paragraph 19', 19)]
    """
    total_paragraphs = 0

    def expand_num_range(nrange_str: str) -> list[str]:
        """takes something like: 8-12 and returns four strings:  8 returns (paragraph 8, 8)"""
        ends = nrange_str.split('-')
        if len(ends) == 1:
            index = int(ends[0])
            return [(f"Paragraph {index}", index)]
        return [(f"Paragraph {i}", i) for i in range(int(ends[0]), int(ends[1]) + 1)]

    ret_l = []
    prev_end = 0
    for line in outline_raw.split('\n'):
        line = line.strip()  # remove whitespace
        if len(line) < 1:  # skip empty lines
            continue
        elif re.search(r'\A\d+-\d+\Z', line) or line.isdigit():
            print("numbering line: ", line)
            recongnized_paragraphs = expand_num_range(line)
            start = int(recongnized_paragraphs[0][1])
            end = int(recongnized_paragraphs[-1][1])
            print(f"curr recognized start = {start} end = {end}")

            gap = start - prev_end
            if gap > 1 and total_paragraphs > 0:
                total_paragraphs += gap - 1
                ret_l.extend([(f"Paragraph {i}", i) for i in range(prev_end + 1, start)])

            total_paragraphs += len(recongnized_paragraphs)
            ret_l.extend(recongnized_paragraphs)
            prev_end = end

        else:
            ret_l.append((line, None))

    return ret_l, total_paragraphs


def parse_explained_list(intext: str) -> list[(str, str)]:
    """Parses text Afirmative Defenses from LLM."""
    full_rows = remove_empty_rows(intext=intext.replace('**', ''))
    ret_l = []
    for row in full_rows:
        pr = row.split(':')
        if len(pr) > 1:
            ad = pr[0].strip().strip('**')
            ret_l.append((ad, pr[1]))
        else:
            ret_l.append((pr, pr))

    return ret_l


def remove_biz_type(instr: str) -> str:
    """If the plaintiff or defendant ends in ,LLC or .Inc, remove that."""
    for suffix in [", LLC", "Inc."]:
        if instr.endswith(suffix):
            return instr[:instr.rfind(suffix)].strip()

    return instr

def paragraph_content2str(chose_paras: list[int], para_2_ln: dict, text_by_lines:list[str]) -> str:
    """
    For chose paragraphs ids, extract their content from whole doc, combine and write as a string, prepare for LLM prompt
    """
    para_text_lines = ""
    para_2_ln_list = list(para_2_ln.items())
    max_line_num = len(text_by_lines) - 1
    for i, (para_num, line_num) in enumerate(para_2_ln_list):   # list should be in order
        if int(para_num) in chose_paras:
            if i == len(para_2_ln_list)-1:
                end_line_num = max_line_num
            else:
                end_line_num = para_2_ln_list[i+1][1]
            # Guard against out-of-bounds indices
            if line_num < 0:
                line_num = 0
            if end_line_num > max_line_num:
                end_line_num = max_line_num + 1  # +1 because slice is exclusive
            if line_num > max_line_num:
                print(f"Warning: line_num {line_num} out of bounds, skipping paragraph {para_num}")
                continue
            print(f"para num = {para_num}, line_num = {line_num}, end_line_num = {end_line_num}")
            para_text_lines += f"paragraph {para_num}:\n" + " ".join(text_by_lines[line_num: end_line_num]) + '\n'
            if i == 0:print("first para_text_lines: \n", para_text_lines)
    return para_text_lines


def _load_saved_answer_line_metadata(memory: BaseMemory) -> list[dict]:
    raw_metadata = memory.get('line_metadata', '')
    if not raw_metadata:
        return []

    try:
        line_metadata = json.loads(raw_metadata)
        if isinstance(line_metadata, list):
            return line_metadata
    except Exception as exc:
        logger.warning("Error loading line_metadata: %s", exc)

    return []


def _load_answer_json_value(memory: BaseMemory, key: str, default):
    raw_value = memory.get(key, '')
    if raw_value in ('', None):
        return default
    if isinstance(raw_value, (dict, list)):
        return raw_value

    try:
        return json.loads(raw_value)
    except Exception as exc:
        logger.warning("Error loading Answer memory JSON key %s: %s", key, exc)
        return default


def is_answer_general_denial_state(is_federal, state) -> bool:
    return (
        _clean_answer_structure_string(is_federal).lower() == "state"
        and any(
            state_name in _clean_answer_structure_string(state).lower()
            for state_name in ("california", "texas")
        )
    )


def ensure_answer_factual_digest_and_ads(
    memory: BaseMemory,
    text_by_lines: list[str] | None = None,
    complaint_structure: dict | None = None,
    sections: list[dict] | None = None,
    causes_of_action: list[dict] | None = None,
    para_2_ln: dict | None = None,
    first_body_line_number: int | None = None,
    reuse_existing: bool = False,
) -> dict:
    if reuse_existing:
        saved_ads = _load_answer_json_value(memory, 'ads_suggestions', None)
        if isinstance(saved_ads, dict):
            logger.info("ANSWER reusing saved ads_suggestions.")
            return saved_ads

    if text_by_lines is None:
        text_by_lines = _load_saved_answer_complaint_lines(memory)
    if complaint_structure is None:
        complaint_structure = _load_answer_json_value(memory, 'complaint_structure', {})
    if sections is None:
        sections = _load_answer_json_value(memory, 'sections', [])
    if causes_of_action is None:
        causes_of_action = _load_answer_json_value(memory, 'causes_of_action', [])
    if para_2_ln is None:
        para_2_ln = _load_answer_json_value(memory, 'paragraphs', {})
    if first_body_line_number is None:
        first_body_line_number = _to_nonnegative_int(
            (complaint_structure or {}).get("first_body_line_number")
        ) or 0

    factual_digest = get_answer_factual_digest_from_llm(
        text_by_lines,
        sections,
        para_2_ln=para_2_ln,
        first_body_line_number=first_body_line_number,
    )
    summary = factual_digest.get("summary", "")
    memory['summary'] = summary
    memory['complaint_factual_digest'] = json.dumps(factual_digest)

    ads_output_dict = get_answer_ad_suggestions_from_llm(
        text_by_lines,
        complaint_structure,
        factual_digest,
        causes_of_action,
    )
    memory['ads_suggestions'] = json.dumps(ads_output_dict)
    return ads_output_dict


def _clean_answer_structure_string(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _clean_answer_structure_list(value) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned = []
    for item in value:
        text = _clean_answer_structure_string(item)
        if text:
            cleaned.append(text)
    return cleaned


def _clean_answer_structure_parties(value) -> list[dict]:
    if not isinstance(value, list):
        return []

    parties = []
    for item in value:
        if not isinstance(item, dict):
            continue
        name = _clean_answer_structure_string(item.get("name"))
        role = _clean_answer_structure_string(item.get("role")).lower()
        if not name:
            continue
        parties.append({"name": name, "role": role})
    return parties


def _answer_structure_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    lowered = str(value).strip().lower()
    if lowered in {"true", "yes", "y", "1"}:
        return True
    if lowered in {"false", "no", "n", "0"}:
        return False
    return None


def _answer_structure_int_list(value) -> list[int]:
    if not isinstance(value, list):
        return []
    output = []
    for item in value:
        item_int = _to_positive_int(item)
        if item_int is not None:
            output.append(item_int)
    return output


def _normalize_answer_jurisdiction_type(value, court: str, venue: str) -> str:
    raw = _clean_answer_structure_string(value).lower()
    if "federal" in raw:
        return "federal"
    if "state" in raw:
        return "state"
    court_venue = " ".join([court, venue]).lower()
    if "united states district court" in court_venue:
        return "federal"
    return raw or "unknown"


def _answer_structure_metadata_for_line(line_metadata: list[dict], line_number: int) -> dict:
    if 0 <= line_number < len(line_metadata) and isinstance(line_metadata[line_number], dict):
        return line_metadata[line_number]
    return {}


def _build_answer_structure_source_rows(
    text_by_lines: list[str],
    line_metadata: list[dict],
    page_limit: int = ANSWER_STRUCTURE_PAGE_LIMIT,
) -> list[dict]:
    has_page_metadata = any(
        _to_positive_int(row.get("page_number")) is not None
        for row in line_metadata
        if isinstance(row, dict)
    )

    if has_page_metadata:
        candidate_line_numbers = [
            line_number
            for line_number in range(len(text_by_lines))
            if (
                _to_positive_int(
                    _answer_structure_metadata_for_line(line_metadata, line_number).get("page_number")
                ) is not None
                and _to_positive_int(
                    _answer_structure_metadata_for_line(line_metadata, line_number).get("page_number")
                ) <= page_limit
            )
        ]
    else:
        fallback_line_count = page_limit * ANSWER_STRUCTURE_FALLBACK_LINES_PER_PAGE
        candidate_line_numbers = list(range(min(len(text_by_lines), fallback_line_count)))

    source_rows = []
    for line_number in candidate_line_numbers:
        metadata = _answer_structure_metadata_for_line(line_metadata, line_number)
        source_rows.append({
            "line_number": line_number,
            "page_number": _to_positive_int(metadata.get("page_number")),
            "page_line_number": _to_nonnegative_int(metadata.get("page_line_number")),
            "bold": _answer_structure_bool(metadata.get("bold")),
            "ocr": _answer_structure_bool(metadata.get("ocr")),
            "text": text_by_lines[line_number],
        })
    return source_rows


def _format_answer_structure_source_rows(source_rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in source_rows)


def _build_answer_structure_prompt(
    text_by_lines: list[str],
    line_metadata: list[dict],
    page_limit: int = ANSWER_STRUCTURE_PAGE_LIMIT,
) -> tuple[str, list[dict]]:
    source_rows = _build_answer_structure_source_rows(
        text_by_lines,
        line_metadata,
        page_limit=page_limit,
    )
    prompt_metadata = {
        "total_lines": len(text_by_lines),
        "page_limit": page_limit,
        "source_line_count": len(source_rows),
        "line_number_base": 0,
    }
    source_text = _format_answer_structure_source_rows(source_rows)
    prompt = f"""
You are classifying the front structure of a legal complaint for an Answer drafting workflow.
You are given compact source rows from the beginning of the complaint. Each row is one JSON
object with the exact global line_number, optional PDF page metadata, and text.

Metadata:
{json.dumps(prompt_metadata, indent=2)}

Source rows:
===
{source_text}
===

MOST IMPORTANT:
Use only the line numbers and page numbers shown in the source rows.
Do not invent page numbers, line numbers, parties, jurisdiction, or body starts.
TOC/TOA entries are not body starts.
first_body_line_number must point to the first real pleading/body content, not caption,
table of contents, table of authorities, certificate, page header/footer, or index entry.
Return the full list of plaintiffs and the full list of defendants when many parties are
named in the source rows. Do not collapse party lists into "et al.", "and others", class
labels, or representative examples unless that is the only party text available in the
source rows.
If a value is uncertain, return null, an empty string, or an empty array instead of a placeholder.

Extract the caption and structural metadata in one pass. Return only valid JSON using this shape:
{{
  "has_caption": true,
  "caption_pages": [1, 2],
  "has_table_of_contents": true,
  "toc_pages": [3, 4],
  "has_table_of_authorities": true,
  "toa_pages": [5, 6],
  "first_body_line_number": 420,
  "first_body_page": 8,
  "first_body_page_line_number": 12,
  "first_body_reason": "Paragraph 1 begins after INTRODUCTION and is followed by paragraphs 2 and 3.",
  "first_body_confidence": "high",
  "court": "UNITED STATES DISTRICT COURT ...",
  "venue": "UNITED STATES DISTRICT COURT ...",
  "jurisdiction_type": "federal",
  "jurisdiction_reason": "The caption identifies a United States District Court.",
  "jurisdiction_confidence": "high",
  "state": "",
  "case_number": "1:23-cv-00001",
  "plaintiffs": ["..."],
  "defendants": ["..."],
  "parties": [{{"name": "...", "role": "plaintiff"}}],
  "parties_reason": "The caption party block identifies plaintiffs and defendants.",
  "parties_confidence": "high",
  "pleading_title": "Complaint",
  "jury_demand": true
}}
""".strip()
    return prompt, source_rows


def _parse_answer_structure_output(llm_output) -> dict:
    if isinstance(llm_output, dict):
        return llm_output
    if not llm_output:
        return {}

    output_text = str(llm_output).strip()
    if output_text.startswith("```"):
        output_text = re.sub(r"^```(?:json)?", "", output_text).strip()
        output_text = re.sub(r"```$", "", output_text).strip()

    try:
        parsed = json.loads(output_text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    match = re.search(r"\{[\s\S]*\}", output_text)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    return {}


def _normalize_answer_structure(
    raw_structure: dict,
    text_by_lines: list[str],
    source_rows: list[dict],
) -> dict:
    allowed_line_numbers = {row["line_number"] for row in source_rows}
    first_body_line_number = _to_nonnegative_int(raw_structure.get("first_body_line_number"))
    if first_body_line_number not in allowed_line_numbers or first_body_line_number >= len(text_by_lines):
        first_body_line_number = None

    first_body_page = _to_positive_int(raw_structure.get("first_body_page"))
    first_body_page_line_number = _to_nonnegative_int(raw_structure.get("first_body_page_line_number"))
    if first_body_line_number is not None:
        body_row = next((row for row in source_rows if row["line_number"] == first_body_line_number), None)
        if body_row:
            first_body_page = first_body_page or body_row.get("page_number")
            first_body_page_line_number = (
                first_body_page_line_number
                if first_body_page_line_number is not None
                else body_row.get("page_line_number")
            )

    parties = _clean_answer_structure_parties(raw_structure.get("parties"))
    plaintiffs = _clean_answer_structure_list(raw_structure.get("plaintiffs"))
    defendants = _clean_answer_structure_list(raw_structure.get("defendants"))
    if not plaintiffs:
        plaintiffs = [party["name"] for party in parties if "plaintiff" in party["role"]]
    if not defendants:
        defendants = [party["name"] for party in parties if "defendant" in party["role"]]

    court = _clean_answer_structure_string(raw_structure.get("court"))
    venue = _clean_answer_structure_string(raw_structure.get("venue")) or court
    jurisdiction_type = _normalize_answer_jurisdiction_type(
        raw_structure.get("jurisdiction_type"),
        court,
        venue,
    )

    return {
        "has_caption": _answer_structure_bool(raw_structure.get("has_caption")),
        "caption_pages": _answer_structure_int_list(raw_structure.get("caption_pages")),
        "has_table_of_contents": _answer_structure_bool(raw_structure.get("has_table_of_contents")),
        "toc_pages": _answer_structure_int_list(raw_structure.get("toc_pages")),
        "has_table_of_authorities": _answer_structure_bool(raw_structure.get("has_table_of_authorities")),
        "toa_pages": _answer_structure_int_list(raw_structure.get("toa_pages")),
        "first_body_line_number": first_body_line_number,
        "first_body_page": first_body_page,
        "first_body_page_line_number": first_body_page_line_number,
        "first_body_reason": _clean_answer_structure_string(raw_structure.get("first_body_reason")),
        "first_body_confidence": _clean_answer_structure_string(raw_structure.get("first_body_confidence")),
        "court": court,
        "venue": venue,
        "jurisdiction_type": jurisdiction_type,
        "jurisdiction_reason": _clean_answer_structure_string(raw_structure.get("jurisdiction_reason")),
        "jurisdiction_confidence": _clean_answer_structure_string(raw_structure.get("jurisdiction_confidence")),
        "state": _clean_answer_structure_string(raw_structure.get("state")).lower(),
        "case_number": _clean_answer_structure_string(raw_structure.get("case_number")),
        "plaintiffs": plaintiffs,
        "defendants": defendants,
        "parties": parties,
        "parties_reason": _clean_answer_structure_string(raw_structure.get("parties_reason")),
        "parties_confidence": _clean_answer_structure_string(raw_structure.get("parties_confidence")),
        "pleading_title": _clean_answer_structure_string(raw_structure.get("pleading_title")),
        "jury_demand": _answer_structure_bool(raw_structure.get("jury_demand")),
    }


def _answer_structure_setup_error(complaint_structure: dict) -> str:
    if not complaint_structure.get("plaintiffs") or not complaint_structure.get("defendants"):
        return "Something went wrong when extracting parties from the complaint, please try again."
    if complaint_structure.get("first_body_line_number") is None:
        return "Something went wrong when identifying where the complaint body starts, please try again."
    return ""


def get_answer_complaint_structure_from_llm(
    text_by_lines: list[str],
    line_metadata: list[dict],
) -> dict:
    prompt, source_rows = _build_answer_structure_prompt(text_by_lines, line_metadata)
    if not source_rows:
        raise ValueError("No source lines available for complaint structure extraction.")

    print("ANSWER_STRUCTURE prompt length: ", num_tokens_from_string(prompt))
    print("ANSWER_STRUCTURE first source rows: ", source_rows[:3])
    parsed_output = chat_completion_parse(
        prompt, AnswerComplaintStructure,
        model=ANSWER_EXTRACTION_MODEL, reasoning_effort=ANSWER_EXTRACTION_EFFORT,
    )
    llm_output = parsed_output.model_dump() if parsed_output is not None else None
    print("ANSWER_STRUCTURE llm_output: ", str(llm_output)[:2000])

    raw_structure = _parse_answer_structure_output(llm_output)
    return _normalize_answer_structure(raw_structure, text_by_lines, source_rows)


def _answer_section_metadata_for_line(line_metadata: list[dict], line_number: int) -> dict:
    return _answer_structure_metadata_for_line(line_metadata, line_number)


def _build_answer_section_source_rows(
    text_by_lines: list[str],
    line_metadata: list[dict],
    start_line: int,
    end_line: int,
) -> list[dict]:
    source_rows = []
    for line_number in range(start_line, end_line + 1):
        metadata = _answer_section_metadata_for_line(line_metadata, line_number)
        source_rows.append({
            "line_number": line_number,
            "page_number": _to_positive_int(metadata.get("page_number")),
            "page_line_number": _to_nonnegative_int(metadata.get("page_line_number")),
            "bold": _answer_structure_bool(metadata.get("bold")),
            "ocr": _answer_structure_bool(metadata.get("ocr")),
            "text": text_by_lines[line_number],
        })
    return source_rows


def _format_answer_section_source_rows(source_rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in source_rows)


def _build_answer_section_chunk(
    total_lines: int,
    primary_start: int,
    primary_end: int,
    context_before_lines: int,
    context_after_lines: int,
    chunk_id,
    chunk_count: int,
) -> dict:
    context_start = max(0, primary_start - context_before_lines)
    context_end = min(total_lines - 1, primary_end + context_after_lines)
    return {
        "chunk_id": chunk_id,
        "chunk_count": chunk_count,
        "total_lines": total_lines,
        "line_number_base": 0,
        "context_line_start": context_start,
        "context_line_end": context_end,
        "primary_line_start": primary_start,
        "primary_line_end": primary_end,
    }


def _build_answer_section_chunk_prompt(
    chunk: dict,
    text_by_lines: list[str],
    line_metadata: list[dict],
) -> str:
    metadata = {
        "chunk_id": chunk["chunk_id"],
        "chunk_count": chunk["chunk_count"],
        "total_lines": chunk["total_lines"],
        "line_number_base": 0,
        "context_line_start": chunk["context_line_start"],
        "context_line_end": chunk["context_line_end"],
        "primary_line_start": chunk["primary_line_start"],
        "primary_line_end": chunk["primary_line_end"],
        "valid_types": list(ANSWER_SECTION_VALID_TYPES),
    }
    source_rows = _build_answer_section_source_rows(
        text_by_lines,
        line_metadata,
        chunk["context_line_start"],
        chunk["context_line_end"],
    )
    source_text = _format_answer_section_source_rows(source_rows)
    example_line = chunk["primary_line_start"]
    example_next_line = min(example_line + 1, chunk["context_line_end"])
    return f"""
You are extracting structural section starts and cause-of-action starts from a legal complaint.
You are given compact source rows. Each row is one JSON object with exact global line_number,
optional PDF page metadata, and text.

Metadata:
{json.dumps(metadata, indent=2)}

Source rows:
===
{source_text}
===

Valid type values. Return exactly one of these values for every item:
{json.dumps(list(ANSWER_SECTION_VALID_TYPES))}

MOST IMPORTANT:
Return only items whose start_line is inside the primary range.
Context lines are only for understanding continuity.
Use only source line numbers.
Do not invent line numbers, titles, claims, parties, or defendants.
Do not return TOC/TOA entries, page headers, page footers, or index entries as sections.
A cause of action is also a section. Return it once with type="cause_of_action".

Additional requirements:
1. A section can be a major pleading part, a subsection, or a cause/count/claim heading.
2. Use level=1 for top-level sections and larger numbers for nested subsections when clear.
3. For cause_of_action items, fill claim_name, claim_number, against, and by when the source
   rows identify them. Use null or [] when uncertain.
4. For non-cause-of-action items, use is_cause_of_action=false, claim_name=null,
   claim_number=null, against=[], and by=[].
5. source_lines should list the exact source row line numbers used to form the title.
6. Return end_line: null. Final end lines are computed later by code from sorted starts and hierarchy.
7. Return valid JSON using exactly this shape:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "items": [
    {{
      "start_line": {example_line},
      "end_line": null,
      "title": "FIRST CAUSE OF ACTION - Breach of Contract",
      "type": "cause_of_action",
      "level": 1,
      "is_cause_of_action": true,
      "claim_name": "Breach of Contract",
      "claim_number": 1,
      "against": ["ABC Corp."],
      "by": ["Plaintiff ABC"],
      "source_lines": [{example_line}, {example_next_line}],
      "confidence": "high",
      "reason": "Heading line states FIRST CAUSE OF ACTION and next line states Breach of Contract."
    }}
  ]
}}
If no section or cause-of-action starts exist in the primary range, return:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "items": []
}}
""".strip()


def _build_answer_section_chunks(
    text_by_lines: list[str],
    line_metadata: list[dict],
    first_body_line_number: int,
    primary_lines: int | None = None,
    context_before_lines: int | None = None,
    context_after_lines: int | None = None,
    max_prompt_tokens: int | None = None,
) -> list[dict]:
    primary_lines = ANSWER_SECTION_PRIMARY_LINES if primary_lines is None else primary_lines
    context_before_lines = (
        ANSWER_SECTION_CONTEXT_BEFORE_LINES
        if context_before_lines is None
        else context_before_lines
    )
    context_after_lines = (
        ANSWER_SECTION_CONTEXT_AFTER_LINES
        if context_after_lines is None
        else context_after_lines
    )
    max_prompt_tokens = ANSWER_SECTION_MAX_PROMPT_TOKENS if max_prompt_tokens is None else max_prompt_tokens

    if primary_lines < 1:
        raise ValueError("primary_lines must be positive.")
    if context_before_lines < 0 or context_after_lines < 0:
        raise ValueError("context line counts cannot be negative.")

    total_lines = len(text_by_lines)
    primary_start = max(0, first_body_line_number or 0)
    if total_lines == 0 or primary_start >= total_lines:
        return []

    chunks = []
    while primary_start < total_lines:
        current_primary_lines = min(primary_lines, total_lines - primary_start)
        while True:
            primary_end = min(primary_start + current_primary_lines - 1, total_lines - 1)
            chunk = _build_answer_section_chunk(
                total_lines,
                primary_start,
                primary_end,
                context_before_lines,
                context_after_lines,
                chunk_id=len(chunks) + 1,
                chunk_count=0,
            )
            prompt = _build_answer_section_chunk_prompt(chunk, text_by_lines, line_metadata)
            prompt_tokens = num_tokens_from_string(prompt)
            chunk["prompt_tokens"] = prompt_tokens
            if prompt_tokens <= max_prompt_tokens or current_primary_lines == 1:
                if prompt_tokens > max_prompt_tokens:
                    logger.warning(
                        "ANSWER_SECTION chunk %s prompt exceeds token cap with one primary line: %s > %s",
                        chunk["chunk_id"],
                        prompt_tokens,
                        max_prompt_tokens,
                    )
                chunks.append(chunk)
                primary_start = primary_end + 1
                break
            current_primary_lines = max(1, current_primary_lines // 2)

    chunk_count = len(chunks)
    for index, chunk in enumerate(chunks):
        chunk["chunk_id"] = index + 1
        chunk["chunk_count"] = chunk_count
        chunk["chunk_index"] = index
    return chunks


def _split_answer_section_chunk(
    chunk: dict,
    text_by_lines: list[str],
    primary_lines: int,
) -> list[dict]:
    if primary_lines < 1:
        primary_lines = 1

    retry_chunks = []
    primary_start = int(chunk["primary_line_start"])
    primary_end = int(chunk["primary_line_end"])
    total_lines = len(text_by_lines)
    child_index = 1
    while primary_start <= primary_end:
        child_end = min(primary_start + primary_lines - 1, primary_end)
        retry_chunks.append(
            _build_answer_section_chunk(
                total_lines,
                primary_start,
                child_end,
                ANSWER_SECTION_CONTEXT_BEFORE_LINES,
                ANSWER_SECTION_CONTEXT_AFTER_LINES,
                chunk_id=f"{chunk['chunk_id']}.{child_index}",
                chunk_count=chunk["chunk_count"],
            )
        )
        primary_start = child_end + 1
        child_index += 1
    return retry_chunks


def _parse_answer_section_chunk_output(llm_output) -> tuple[list, bool]:
    if not llm_output:
        return [], True

    parsed = _parse_answer_structure_output(llm_output)
    if isinstance(parsed, dict) and "items" in parsed:
        items = parsed["items"]
        return items if isinstance(items, list) else [], not isinstance(items, list)

    items = extract_from_json("items", llm_output)
    if items:
        return items, False
    return [], True


def _answer_chunk_top_level_message_text(llm_output) -> str:
    parsed = _parse_answer_structure_output(llm_output)
    if isinstance(parsed, dict) and parsed:
        messages = []
        for key in ("note", "error", "message"):
            value = parsed.get(key)
            if isinstance(value, dict):
                for nested_key in ("note", "error", "message", "description"):
                    nested_value = _clean_answer_structure_string(value.get(nested_key))
                    if nested_value:
                        messages.append(nested_value)
            elif isinstance(value, list):
                messages.extend(
                    _clean_answer_structure_string(item)
                    for item in value
                    if _clean_answer_structure_string(item)
                )
            else:
                cleaned = _clean_answer_structure_string(value)
                if cleaned:
                    messages.append(cleaned)
        return "\n".join(messages)

    return str(llm_output or "")


def _llm_output_requests_smaller_answer_para_chunk(llm_output) -> bool:
    if not llm_output:
        return False

    output_text = re.sub(r"\s+", " ", _answer_chunk_top_level_message_text(llm_output).lower()).strip()
    if not output_text:
        return False

    size_limit_patterns = [
        r"\b(?:single-response\s+)?response size limit\b",
        r"\boutput (?:would|will|may|might)?\s*exceed",
        r"\bplease split\b",
        r"\bsplit(?:\s+\w+){0,6}\s+into smaller chunks\b",
        r"\btoken limit\b",
    ]
    return any(re.search(pattern, output_text) for pattern in size_limit_patterns)


def _answer_chunk_retry_decision(parse_failed: bool, llm_output) -> tuple[bool, list[str]]:
    if not parse_failed:
        return False, []

    retry_reasons = ["parse_failed"]
    if _llm_output_requests_smaller_answer_para_chunk(llm_output):
        retry_reasons.append("size_limit_request")
    return True, retry_reasons


def _normalize_answer_section_type(value) -> str:
    raw = _clean_answer_structure_string(value).lower()
    raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
    aliases = {
        "jurisdiction": "jurisdiction_venue",
        "venue": "jurisdiction_venue",
        "jurisdiction_and_venue": "jurisdiction_venue",
        "factual_background": "factual_allegations",
        "facts": "factual_allegations",
        "background": "factual_allegations",
        "class_action_allegations": "class_allegations",
        "claim": "cause_of_action",
        "claims": "cause_of_action",
        "claim_for_relief": "cause_of_action",
        "claims_for_relief": "cause_of_action",
        "count": "cause_of_action",
        "counts": "cause_of_action",
        "cause": "cause_of_action",
        "causes_of_action": "cause_of_action",
        "prayer_for_relief": "prayer",
        "relief": "prayer",
        "jury_trial_demand": "jury_demand",
        "jury_trial": "jury_demand",
        "exhibits": "exhibit",
    }
    normalized = aliases.get(raw, raw)
    if normalized in ANSWER_SECTION_VALID_TYPES:
        return normalized
    return "other"


def _answer_section_confidence_score(value) -> int:
    return {
        "high": 3,
        "medium": 2,
        "low": 1,
    }.get(_clean_answer_structure_string(value).lower(), 0)


def _answer_section_valid_source_lines(value, source_line_numbers: set[int], start_line: int) -> list[int]:
    output = []
    if isinstance(value, list):
        for item in value:
            line_number = _to_nonnegative_int(item)
            if line_number is not None and line_number in source_line_numbers:
                output.append(line_number)
    if start_line not in output:
        output.append(start_line)
    return sorted(set(output))


def _clean_answer_section_title(title) -> str:
    cleaned = _clean_answer_structure_string(title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _is_placeholder_answer_section_title(title: str) -> bool:
    cleaned = title.strip().lower()
    return cleaned in {"", "...", "n/a", "na", "none", "null", "unknown", "untitled"}


def _is_likely_toc_toa_or_page_artifact(line_text: str) -> bool:
    cleaned = line_text.replace("<B>", "").replace("</B>", "").strip()
    lowered = cleaned.lower()
    if not lowered:
        return True
    if lowered in {"table of contents", "table of authorities"}:
        return True
    if re.fullmatch(r"\d+", lowered):
        return True
    if re.fullmatch(r"page\s+\d+(?:\s+of\s+\d+)?", lowered):
        return True
    if re.search(r"\.{3,}\s*\d+\s*$", cleaned):
        return True
    if lowered.startswith("case ") and any(marker in lowered for marker in ["document", "filed", "pageid", "page id"]):
        return True
    return False


def _roman_to_int(value: str) -> int | None:
    roman_values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}
    lowered = value.lower()
    if not re.fullmatch(r"[ivxlc]+", lowered):
        return None
    total = 0
    previous = 0
    for char in reversed(lowered):
        current = roman_values[char]
        if current < previous:
            total -= current
        else:
            total += current
            previous = current
    return total if total > 0 else None


def _infer_answer_section_claim_number(title: str) -> int | None:
    lowered = title.lower()
    ordinal_words = {
        "first": 1,
        "second": 2,
        "third": 3,
        "fourth": 4,
        "fifth": 5,
        "sixth": 6,
        "seventh": 7,
        "eighth": 8,
        "ninth": 9,
        "tenth": 10,
        "eleventh": 11,
        "twelfth": 12,
        "thirteenth": 13,
        "fourteenth": 14,
        "fifteenth": 15,
        "sixteenth": 16,
        "seventeenth": 17,
        "eighteenth": 18,
        "nineteenth": 19,
        "twentieth": 20,
    }
    for word, number in ordinal_words.items():
        if re.search(rf"\b{word}\b\s+(?:cause of action|claim|count)", lowered):
            return number

    numeric_match = re.search(r"\b(?:cause of action|claim|count)\s+(?:no\.\s*)?(\d+)\b", lowered)
    if numeric_match:
        return int(numeric_match.group(1))

    count_match = re.search(r"\bcount\s+([ivxlc]+)\b", lowered)
    if count_match:
        return _roman_to_int(count_match.group(1))
    return None


def _strip_answer_section_claim_label(title: str) -> str:
    cleaned = re.sub(
        r"\b(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|eleventh|twelfth|"
        r"thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|eighteenth|nineteenth|twentieth)"
        r"\s+(?:cause of action|claim|count)\b",
        "",
        title,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"\b(?:cause of action|claim|count)\s+(?:no\.\s*)?\d+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\bcount\s+[ivxlc]+\b", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^[\s\-:;]+|[\s\-:;]+$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _infer_answer_section_claim_name(title: str) -> str | None:
    claim_name = _strip_answer_section_claim_label(title)
    return claim_name or None


def _filter_answer_section_items(
    chunk: dict,
    section_items: list,
    text_by_lines: list[str],
) -> list[dict]:
    rows = []
    rejected_count = 0
    source_line_numbers = set(range(chunk["context_line_start"], chunk["context_line_end"] + 1))
    for item in section_items:
        if not isinstance(item, dict):
            rejected_count += 1
            continue

        start_line = _to_nonnegative_int(item.get("start_line"))
        if start_line is None:
            rejected_count += 1
            continue
        if start_line < chunk["primary_line_start"] or start_line > chunk["primary_line_end"]:
            rejected_count += 1
            continue
        if start_line >= len(text_by_lines):
            rejected_count += 1
            continue

        title = _clean_answer_section_title(item.get("title"))
        if _is_placeholder_answer_section_title(title):
            rejected_count += 1
            continue
        line_text = text_by_lines[start_line]
        if _is_likely_toc_toa_or_page_artifact(line_text):
            rejected_count += 1
            continue

        section_type = _normalize_answer_section_type(item.get("type"))
        is_cause_of_action = section_type == "cause_of_action"
        claim_name = _clean_answer_section_title(item.get("claim_name")) or None
        claim_number = _to_positive_int(item.get("claim_number"))
        if is_cause_of_action:
            claim_name = claim_name or _infer_answer_section_claim_name(title)
            claim_number = claim_number or _infer_answer_section_claim_number(title)
        else:
            claim_name = None
            claim_number = None

        source_lines = _answer_section_valid_source_lines(
            item.get("source_lines"),
            source_line_numbers,
            start_line,
        )
        rows.append({
            "start_line": start_line,
            "end_line": None,
            "title": title,
            "type": section_type,
            "level": _to_positive_int(item.get("level")),
            "is_cause_of_action": is_cause_of_action,
            "claim_name": claim_name,
            "claim_number": claim_number,
            "against": _clean_answer_structure_list(item.get("against")) if is_cause_of_action else [],
            "by": _clean_answer_structure_list(item.get("by")) if is_cause_of_action else [],
            "source_lines": source_lines,
            "confidence": _clean_answer_structure_string(item.get("confidence")),
            "reason": _clean_answer_structure_string(item.get("reason")),
            "_chunk_id": chunk["chunk_id"],
            "_line_text": line_text,
        })

    if rejected_count:
        logger.warning("ANSWER_SECTION chunk %s rejected %s invalid rows.", chunk["chunk_id"], rejected_count)
    return rows


def _extract_answer_section_items_for_chunk(
    chunk: dict,
    text_by_lines: list[str],
    line_metadata: list[dict],
    allow_split: bool = True,
) -> dict:
    prompt = _build_answer_section_chunk_prompt(chunk, text_by_lines, line_metadata)
    prompt_tokens = num_tokens_from_string(prompt)
    logger.info(
        "ANSWER_SECTION chunk %s of %s primary %s-%s context %s-%s prompt length %s",
        chunk["chunk_id"],
        chunk["chunk_count"],
        chunk["primary_line_start"],
        chunk["primary_line_end"],
        chunk["context_line_start"],
        chunk["context_line_end"],
        prompt_tokens,
    )
    parsed_output = chat_completion_parse(
        prompt, AnswerSectionChunk,
        model=ANSWER_EXTRACTION_MODEL, reasoning_effort=ANSWER_EXTRACTION_EFFORT,
    )
    llm_output = parsed_output.model_dump() if parsed_output is not None else None
    logger.info("ANSWER_SECTION chunk %s output preview:\n%s", chunk["chunk_id"], str(llm_output)[:1000])

    section_items, parse_failed = _parse_answer_section_chunk_output(llm_output)
    should_split, retry_reasons = _answer_chunk_retry_decision(parse_failed, llm_output)
    primary_count = chunk["primary_line_end"] - chunk["primary_line_start"] + 1
    if should_split and allow_split and primary_count > 1:
        retry_size = max(1, primary_count // 2)
        logger.warning(
            "Retrying ANSWER_SECTION chunk %s in line-based halves of %s lines. reason=%s",
            chunk["chunk_id"],
            retry_size,
            ",".join(retry_reasons),
        )
        retry_rows = []
        for retry_chunk in _split_answer_section_chunk(chunk, text_by_lines, retry_size):
            retry_result = _extract_answer_section_items_for_chunk(
                retry_chunk,
                text_by_lines,
                line_metadata,
                allow_split=True,
            )
            retry_rows.extend(retry_result["items"])
        return {
            "chunk": chunk,
            "items": retry_rows,
            "parse_failed": parse_failed,
            "split_retry": True,
            "retry_reasons": retry_reasons,
        }

    rows = _filter_answer_section_items(chunk, section_items, text_by_lines)
    return {
        "chunk": chunk,
        "items": rows,
        "parse_failed": parse_failed,
        "split_retry": False,
        "retry_reasons": retry_reasons,
    }


def _run_answer_section_chunk_batch(
    chunks: list[dict],
    text_by_lines: list[str],
    line_metadata: list[dict],
) -> list[dict]:
    if not chunks:
        return []

    workers = min(ANSWER_SECTION_EXTRACT_WORKERS, len(chunks))
    logger.info("Running ANSWER_SECTION over %s chunks with max_workers=%s.", len(chunks), workers)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            submit_with_context(pool, _extract_answer_section_items_for_chunk, chunk, text_by_lines, line_metadata): chunk
            for chunk in chunks
        }
        for future in concurrent.futures.as_completed(future_map):
            chunk = future_map[future]
            try:
                results.append(future.result())
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("ANSWER_SECTION chunk %s failed with exception: %s", chunk["chunk_id"], exc)
                results.append({
                    "chunk": chunk,
                    "items": [],
                    "parse_failed": True,
                    "split_retry": False,
                })
    return results


def _normalized_answer_section_title(title: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", title.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _answer_section_titles_similar(first: str, second: str) -> bool:
    first_normalized = _normalized_answer_section_title(first)
    second_normalized = _normalized_answer_section_title(second)
    if not first_normalized or not second_normalized:
        return False
    if first_normalized == second_normalized:
        return True
    return difflib.SequenceMatcher(None, first_normalized, second_normalized).ratio() >= 0.82


def _answer_section_title_source_score(item: dict, text_by_lines: list[str]) -> int:
    title = _normalized_answer_section_title(item.get("title", ""))
    source_text = " ".join(
        text_by_lines[line_number]
        for line_number in item.get("source_lines", [])
        if 0 <= line_number < len(text_by_lines)
    )
    source_text = _normalized_answer_section_title(source_text)
    if not title or not source_text:
        return 0
    if title in source_text or source_text in title:
        return 2
    title_tokens = set(title.split())
    source_tokens = set(source_text.split())
    if not title_tokens:
        return 0
    overlap = len(title_tokens & source_tokens) / len(title_tokens)
    return 1 if overlap >= 0.5 else 0


def _answer_section_richness_score(item: dict) -> int:
    score = 0
    if item.get("claim_name"):
        score += 1
    if item.get("claim_number") is not None:
        score += 1
    score += min(len(item.get("against", [])), 2)
    score += min(len(item.get("by", [])), 2)
    score += min(len(item.get("source_lines", [])), 3)
    return score


def _answer_section_items_are_duplicates(first: dict, second: dict) -> bool:
    line_distance = abs(first["start_line"] - second["start_line"])
    if line_distance == 0:
        return True
    if line_distance > 10:
        return False
    compatible_types = (
        first["type"] == second["type"]
        or "other" in {first["type"], second["type"]}
    )
    return compatible_types and _answer_section_titles_similar(first["title"], second["title"])


def _choose_duplicate_answer_section_item(candidates: list[dict], text_by_lines: list[str]) -> dict:
    def score(item: dict) -> tuple:
        return (
            _answer_section_title_source_score(item, text_by_lines),
            _answer_section_confidence_score(item.get("confidence")),
            _answer_section_richness_score(item),
            len(item.get("title", "")),
            -item["start_line"],
        )

    return max(candidates, key=score)


def _dedupe_answer_section_items(items: list[dict], text_by_lines: list[str]) -> list[dict]:
    groups = []
    for item in sorted(items, key=lambda row: (row["start_line"], row["title"])):
        for group in groups:
            if any(_answer_section_items_are_duplicates(item, existing) for existing in group):
                group.append(item)
                break
        else:
            groups.append([item])

    deduped = []
    for group in groups:
        chosen = _choose_duplicate_answer_section_item(group, text_by_lines)
        if len(group) > 1:
            logger.info(
                "ANSWER_SECTION duplicate candidates %s kept %s",
                [(item["title"], item["start_line"]) for item in group],
                (chosen["title"], chosen["start_line"]),
            )
        deduped.append(chosen)
    return sorted(deduped, key=lambda row: row["start_line"])


def _answer_section_title_is_label_only(title: str) -> bool:
    cleaned = _normalized_answer_section_title(title)
    if re.fullmatch(r"[ivxlc]+", cleaned):
        return True
    if re.fullmatch(r"[a-z]", cleaned):
        return True
    if re.fullmatch(r"(?:count|claim)\s+(?:[ivxlc]+|\d+)", cleaned):
        return True
    if re.fullmatch(
        r"(?:first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|"
        r"eleventh|twelfth|thirteenth|fourteenth|fifteenth|sixteenth|seventeenth|"
        r"eighteenth|nineteenth|twentieth|\d+(?:st|nd|rd|th)?)\s+"
        r"(?:cause of action|claim|count)",
        cleaned,
    ):
        return True
    return False


def _answer_section_title_is_major_boundary_text(title: str) -> bool:
    cleaned = _normalized_answer_section_title(title)
    boundary_phrases = [
        "introduction",
        "parties",
        "jurisdiction",
        "venue",
        "factual allegations",
        "class allegations",
        "damages",
        "prayer",
        "prayer for relief",
        "jury demand",
        "jury trial demand",
        "signature",
        "certificate",
        "exhibit",
    ]
    return any(phrase in cleaned for phrase in boundary_phrases)


def _answer_section_is_major_non_coa_boundary(item: dict) -> bool:
    return item["type"] in {
        "introduction",
        "parties",
        "jurisdiction_venue",
        "factual_allegations",
        "class_allegations",
        "damages",
        "prayer",
        "jury_demand",
        "signature",
        "certificate",
        "exhibit",
    } or _answer_section_title_is_major_boundary_text(item["title"])


def _answer_section_title_looks_like_coa_start(title: str) -> bool:
    cleaned = _normalized_answer_section_title(title)
    if _infer_answer_section_claim_number(title):
        return True
    return any(
        phrase in cleaned
        for phrase in ["cause of action", "claim for relief", "claims for relief", "count"]
    )


def _should_merge_answer_section_continuation(previous: dict, current: dict) -> bool:
    if current["start_line"] - previous["start_line"] > 3:
        return False
    if _answer_section_title_is_label_only(previous["title"]) and _answer_section_title_is_label_only(current["title"]):
        return False
    if previous["is_cause_of_action"] or current["is_cause_of_action"]:
        if _answer_section_is_major_non_coa_boundary(current):
            return False
        if _answer_section_is_major_non_coa_boundary(previous):
            return False
        if current["is_cause_of_action"] and previous["is_cause_of_action"]:
            previous_claim_number = previous.get("claim_number")
            current_claim_number = current.get("claim_number")
            if previous_claim_number and current_claim_number and previous_claim_number != current_claim_number:
                return False
        if current["is_cause_of_action"] and not previous["is_cause_of_action"]:
            if _answer_section_title_looks_like_coa_start(current["title"]):
                return False
        if current["is_cause_of_action"] and _answer_section_title_is_label_only(current["title"]):
            return False
        if (
            _answer_section_title_starts_with_upper_letter_label(current["title"])
            or _answer_section_title_starts_with_upper_roman_label(current["title"])
            or _answer_section_title_starts_with_nested_label(current["title"])
        ):
            return False
        return (
            _answer_section_title_is_label_only(previous["title"])
            or current["title"].lower().startswith(("against ", "as to ", "on behalf of "))
            or current["type"] in {"cause_of_action", "other"}
        )
    if previous["type"] != current["type"]:
        return False
    if _answer_section_is_major_non_coa_boundary(previous) and _answer_section_is_major_non_coa_boundary(current):
        return False
    return _answer_section_title_is_label_only(previous["title"])


def _merge_unique_answer_section_strings(first: list[str], second: list[str]) -> list[str]:
    output = []
    seen = set()
    for value in first + second:
        cleaned = _clean_answer_structure_string(value)
        compare_key = cleaned.lower()
        if cleaned and compare_key not in seen:
            seen.add(compare_key)
            output.append(cleaned)
    return output


def _merge_answer_section_titles(first: str, second: str) -> str:
    first_normalized = _normalized_answer_section_title(first)
    second_normalized = _normalized_answer_section_title(second)
    if first_normalized == second_normalized:
        return first
    if first_normalized and second_normalized in first_normalized:
        return first
    if second_normalized and first_normalized in second_normalized:
        return second
    return " - ".join([title for title in [first, second] if title])


def _merge_answer_section_items(previous: dict, current: dict) -> dict:
    merged = dict(previous)
    merged["title"] = _merge_answer_section_titles(previous["title"], current["title"])
    merged["source_lines"] = sorted(set(previous.get("source_lines", []) + current.get("source_lines", [])))
    merged["is_cause_of_action"] = previous["is_cause_of_action"] or current["is_cause_of_action"]
    merged["type"] = "cause_of_action" if merged["is_cause_of_action"] else previous["type"]
    merged["level"] = previous.get("level") or current.get("level")
    merged["claim_name"] = previous.get("claim_name") or current.get("claim_name")
    merged["claim_number"] = previous.get("claim_number") or current.get("claim_number")
    if merged["is_cause_of_action"]:
        merged["claim_name"] = merged["claim_name"] or _infer_answer_section_claim_name(merged["title"])
        merged["claim_number"] = merged["claim_number"] or _infer_answer_section_claim_number(merged["title"])
    else:
        merged["claim_name"] = None
        merged["claim_number"] = None
    merged["against"] = _merge_unique_answer_section_strings(previous.get("against", []), current.get("against", []))
    merged["by"] = _merge_unique_answer_section_strings(previous.get("by", []), current.get("by", []))
    merged["confidence"] = (
        previous.get("confidence")
        if _answer_section_confidence_score(previous.get("confidence")) >= _answer_section_confidence_score(current.get("confidence"))
        else current.get("confidence")
    )
    reasons = [previous.get("reason", ""), current.get("reason", "")]
    merged["reason"] = " ".join(reason for reason in reasons if reason).strip()
    return merged


def _merge_answer_section_continuations(items: list[dict]) -> list[dict]:
    merged_items = []
    for item in sorted(items, key=lambda row: row["start_line"]):
        if merged_items and _should_merge_answer_section_continuation(merged_items[-1], item):
            merged_items[-1] = _merge_answer_section_items(merged_items[-1], item)
        else:
            merged_items.append(item)
    return merged_items


def _answer_section_title_starts_with_upper_letter_label(title: str) -> bool:
    return re.match(r"^[A-Z]\.\s+\S", title.strip()) is not None


def _answer_section_title_starts_with_upper_roman_label(title: str) -> bool:
    stripped = title.strip()
    match = re.match(r"^([IVXLCDM]+)\.\s+\S", stripped)
    if not match:
        return False
    label = match.group(1)
    return len(label) > 1 or label in {"I", "V", "X"}


def _answer_section_title_starts_with_nested_label(title: str) -> bool:
    stripped = title.strip()
    return (
        re.match(r"^[a-z]\.\s+\S", stripped) is not None
        or re.match(r"^[ivxlcdm]+\.\s+\S", stripped) is not None
        or re.match(r"^\d+\.\s+\S", stripped) is not None
    )


def _infer_answer_section_level(item: dict) -> int:
    existing_level = _to_positive_int(item.get("level"))
    if existing_level is not None:
        return existing_level

    title = _clean_answer_section_title(item.get("title"))
    section_type = item.get("type")
    if section_type == "cause_of_action":
        return 1
    if _answer_section_title_starts_with_upper_roman_label(title):
        return 1
    if _answer_section_title_starts_with_upper_letter_label(title):
        return 2
    if _answer_section_title_starts_with_nested_label(title):
        return 3
    if section_type in ANSWER_SECTION_MAJOR_TYPES or _answer_section_title_is_major_boundary_text(title):
        return 1
    return 1


def _assign_answer_section_levels(items: list[dict]) -> list[dict]:
    for item in items:
        item["level"] = _infer_answer_section_level(item)
    return items


def _is_answer_section_coa_boundary(current: dict, candidate: dict) -> bool:
    if not current.get("is_cause_of_action"):
        return False
    return candidate.get("is_cause_of_action") or candidate.get("type") in ANSWER_SECTION_COA_BOUNDARY_TYPES


def _compute_answer_section_end_lines(items: list[dict], text_by_lines: list[str]) -> None:
    last_line = max(0, len(text_by_lines) - 1)
    for index, item in enumerate(items):
        current_level = _infer_answer_section_level(item)
        end_line = last_line
        for candidate in items[index + 1:]:
            candidate_level = _infer_answer_section_level(candidate)
            if _is_answer_section_coa_boundary(item, candidate) or candidate_level <= current_level:
                end_line = candidate["start_line"] - 1
                break
        item["end_line"] = max(item["start_line"], min(end_line, last_line))


def _strip_answer_section_internal_fields(item: dict) -> dict:
    return {
        "start_line": item["start_line"],
        "end_line": item["end_line"],
        "title": item["title"],
        "type": item["type"],
        "level": item.get("level"),
        "is_cause_of_action": item["is_cause_of_action"],
        "claim_name": item.get("claim_name"),
        "claim_number": item.get("claim_number"),
        "against": item.get("against", []),
        "by": item.get("by", []),
        "source_lines": item.get("source_lines", []),
        "confidence": item.get("confidence", ""),
        "reason": item.get("reason", ""),
    }


def _reconcile_answer_section_items(items: list[dict], text_by_lines: list[str]) -> list[dict]:
    if not items:
        return []

    deduped = _dedupe_answer_section_items(items, text_by_lines)
    merged = _merge_answer_section_continuations(deduped)
    merged = sorted(merged, key=lambda row: row["start_line"])
    _assign_answer_section_levels(merged)
    _compute_answer_section_end_lines(merged, text_by_lines)
    return [_strip_answer_section_internal_fields(item) for item in merged]


def _answer_section_headers_from_sections(sections: list[dict]) -> dict:
    return {
        section["title"]: section["start_line"]
        for section in sections
        if section.get("title") and section.get("start_line") is not None
    }


def _normalize_answer_outline_paragraphs(para_2_ln: dict, first_body_line_number: int) -> list[dict]:
    paragraphs = []
    first_body_line_number = max(0, first_body_line_number or 0)
    for paragraph_number, line_number in para_2_ln.items():
        normalized_paragraph_number = _to_positive_int(paragraph_number)
        normalized_line_number = _to_nonnegative_int(line_number)
        if normalized_paragraph_number is None or normalized_line_number is None:
            continue
        if normalized_line_number < first_body_line_number:
            continue
        paragraphs.append({
            "paragraph_number": normalized_paragraph_number,
            "line_number": normalized_line_number,
        })
    return sorted(paragraphs, key=lambda row: (row["line_number"], row["paragraph_number"]))


def _answer_section_contains_paragraph(section: dict, paragraphs: list[dict]) -> bool:
    start_line = _to_nonnegative_int(section.get("start_line"))
    if start_line is None:
        return False
    end_line = _to_nonnegative_int(section.get("end_line"))
    if end_line is None or end_line < start_line:
        end_line = start_line
    return any(
        start_line <= paragraph["line_number"] <= end_line
        for paragraph in paragraphs
    )


def _normalize_answer_outline_sections(
    sections: list[dict],
    paragraphs: list[dict],
    first_body_line_number: int,
) -> list[dict]:
    outline_sections = []
    first_body_line_number = max(0, first_body_line_number or 0)
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            continue
        title = _clean_answer_section_title(section.get("title"))
        start_line = _to_nonnegative_int(section.get("start_line"))
        if not title or start_line is None or start_line < first_body_line_number:
            continue
        if not _answer_section_contains_paragraph(section, paragraphs):
            continue
        outline_sections.append({
            "title": title,
            "line_number": start_line,
            "level": _infer_answer_section_level(section),
            "index": index,
        })
    return outline_sections


def build_answer_outline_from_locations(
    sections: list[dict],
    para_2_ln: dict,
    first_body_line_number: int,
) -> list:
    paragraphs = _normalize_answer_outline_paragraphs(para_2_ln, first_body_line_number)
    outline_sections = _normalize_answer_outline_sections(sections, paragraphs, first_body_line_number)

    events = []
    for section in outline_sections:
        events.append((
            section["line_number"],
            0,
            section["level"],
            section["index"],
            [section["title"], None],
        ))
    for index, paragraph in enumerate(paragraphs):
        paragraph_number = paragraph["paragraph_number"]
        events.append((
            paragraph["line_number"],
            1,
            0,
            index,
            [f"Paragraph {paragraph_number}", paragraph_number],
        ))

    return [event[-1] for event in sorted(events, key=lambda event: event[:4])]


def _answer_causes_of_action_from_sections(sections: list[dict]) -> list[dict]:
    causes_of_action = []
    for section in sections:
        if not section.get("is_cause_of_action"):
            continue
        coa = dict(section)
        coa["summary"] = ""
        causes_of_action.append(coa)
    return causes_of_action


def get_answer_sections_and_coas_from_llm(
    text_by_lines: list[str],
    line_metadata: list[dict],
    first_body_line_number: int,
) -> dict:
    chunks = _build_answer_section_chunks(
        text_by_lines,
        line_metadata,
        first_body_line_number,
    )
    if not chunks:
        return {"sections": [], "causes_of_action": []}

    logger.info(
        f"ANSWER_SECTION chunked setup: total_lines={len(text_by_lines)}, "
        f"first_body_line_number={max(0, first_body_line_number or 0)}, "
        f"primary_lines={ANSWER_SECTION_PRIMARY_LINES}, "
        f"context_before={ANSWER_SECTION_CONTEXT_BEFORE_LINES}, "
        f"context_after={ANSWER_SECTION_CONTEXT_AFTER_LINES}, "
        f"max_prompt_tokens={ANSWER_SECTION_MAX_PROMPT_TOKENS}, "
        f"chunks={len(chunks)}, workers={ANSWER_SECTION_EXTRACT_WORKERS}"
    )
    chunk_results = _run_answer_section_chunk_batch(chunks, text_by_lines, line_metadata)
    all_items = []
    for result in chunk_results:
        all_items.extend(result["items"])

    final_sections = _reconcile_answer_section_items(all_items, text_by_lines)
    final_causes_of_action = _answer_causes_of_action_from_sections(final_sections)
    if not final_sections:
        logger.warning("ANSWER_SECTION did not identify any sections.")
    return {
        "sections": final_sections,
        "causes_of_action": final_causes_of_action,
    }


def _empty_answer_factual_digest(source_ranges: list[dict] | None = None) -> dict:
    return {
        "summary": "",
        "chunk_summaries": [],
        "key_allegations": [],
        "defense_relevant_facts": [],
        "source_ranges": source_ranges or [],
    }


def _answer_fact_digest_boundary_line(
    sections: list[dict],
    text_by_lines: list[str],
    first_body_line_number: int,
) -> int:
    first_body_line_number = max(0, first_body_line_number or 0)
    boundary_types = {
        "cause_of_action",
        "prayer",
        "jury_demand",
        "signature",
        "certificate",
        "exhibit",
    }
    boundary_lines = []
    for section in sections or []:
        if not isinstance(section, dict) or section.get("type") not in boundary_types:
            continue
        start_line = _to_nonnegative_int(section.get("start_line"))
        if start_line is not None and start_line >= first_body_line_number:
            boundary_lines.append(start_line)
    return min(boundary_lines) if boundary_lines else len(text_by_lines)


def _merge_answer_fact_digest_ranges(ranges: list[dict]) -> list[dict]:
    merged = []
    for source_range in sorted(ranges, key=lambda row: (row["start_line"], row["end_line"])):
        if not merged or source_range["start_line"] > merged[-1]["end_line"] + 1:
            merged.append(dict(source_range))
            continue
        merged[-1]["end_line"] = max(merged[-1]["end_line"], source_range["end_line"])
        sources = merged[-1].setdefault("sources", [])
        for source in source_range.get("sources", []):
            if source not in sources:
                sources.append(source)
    return merged


def _answer_fact_digest_source_ranges(
    sections: list[dict],
    text_by_lines: list[str],
    first_body_line_number: int,
) -> list[dict]:
    first_body_line_number = max(0, first_body_line_number or 0)
    if not text_by_lines or first_body_line_number >= len(text_by_lines):
        return []

    first_boundary_line = _answer_fact_digest_boundary_line(
        sections,
        text_by_lines,
        first_body_line_number,
    )
    max_source_end = min(len(text_by_lines), first_boundary_line) - 1
    if max_source_end < first_body_line_number:
        return []

    factual_ranges = []
    for section in sections or []:
        if not isinstance(section, dict) or section.get("type") != "factual_allegations":
            continue
        start_line = _to_nonnegative_int(section.get("start_line"))
        if start_line is None or start_line < first_body_line_number or start_line > max_source_end:
            continue
        end_line = _to_nonnegative_int(section.get("end_line"))
        if end_line is None or end_line < start_line:
            end_line = start_line
        end_line = min(end_line, max_source_end)
        if end_line >= start_line:
            factual_ranges.append({
                "start_line": start_line,
                "end_line": end_line,
                "sources": [section.get("title") or "factual_allegations"],
            })

    if factual_ranges:
        return _merge_answer_fact_digest_ranges(factual_ranges)

    return [{
        "start_line": first_body_line_number,
        "end_line": max_source_end,
        "sources": ["fallback_main_body_before_first_coa"],
    }]


def _answer_fact_digest_paragraph_line_map(para_2_ln: dict | None, total_lines: int) -> dict[int, int]:
    if not para_2_ln:
        return {}

    anchors = []
    for paragraph_number, line_number in para_2_ln.items():
        normalized_paragraph_number = _to_positive_int(paragraph_number)
        normalized_line_number = _to_nonnegative_int(line_number)
        if (
            normalized_paragraph_number is not None
            and normalized_line_number is not None
            and normalized_line_number < total_lines
        ):
            anchors.append((normalized_line_number, normalized_paragraph_number))
    anchors = sorted(set(anchors))

    paragraph_by_line = {}
    for index, (line_number, paragraph_number) in enumerate(anchors):
        next_line = anchors[index + 1][0] if index + 1 < len(anchors) else total_lines
        for current_line in range(line_number, max(line_number, next_line)):
            if current_line < total_lines:
                paragraph_by_line[current_line] = paragraph_number
    return paragraph_by_line


def _build_answer_fact_digest_source_rows(
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
    start_line: int,
    end_line: int,
) -> list[dict]:
    source_rows = []
    for line_number in range(start_line, end_line + 1):
        source_rows.append({
            "line_number": line_number,
            "paragraph_number": paragraph_by_line.get(line_number),
            "text": text_by_lines[line_number],
        })
    return source_rows


def _format_answer_fact_digest_source_rows(source_rows: list[dict]) -> str:
    return "\n".join(json.dumps(row) for row in source_rows)


def _build_answer_fact_digest_chunk(
    total_lines: int,
    primary_start: int,
    primary_end: int,
    context_before_lines: int,
    context_after_lines: int,
    chunk_id,
    chunk_count: int,
    source_range_index: int | None = None,
) -> dict:
    context_start = max(0, primary_start - context_before_lines)
    context_end = min(total_lines - 1, primary_end + context_after_lines)
    return {
        "chunk_id": chunk_id,
        "chunk_count": chunk_count,
        "total_lines": total_lines,
        "line_number_base": 0,
        "context_line_start": context_start,
        "context_line_end": context_end,
        "primary_line_start": primary_start,
        "primary_line_end": primary_end,
        "source_range_index": source_range_index,
    }


def _build_answer_fact_digest_chunk_prompt(
    chunk: dict,
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
) -> str:
    metadata = {
        "chunk_id": chunk["chunk_id"],
        "chunk_count": chunk["chunk_count"],
        "total_lines": chunk["total_lines"],
        "line_number_base": 0,
        "context_line_start": chunk["context_line_start"],
        "context_line_end": chunk["context_line_end"],
        "primary_line_start": chunk["primary_line_start"],
        "primary_line_end": chunk["primary_line_end"],
    }
    source_rows = _build_answer_fact_digest_source_rows(
        text_by_lines,
        paragraph_by_line,
        chunk["context_line_start"],
        chunk["context_line_end"],
    )
    source_text = _format_answer_fact_digest_source_rows(source_rows)
    example_line = chunk["primary_line_start"]
    example_paragraph = paragraph_by_line.get(example_line)
    example_paragraph_numbers_json = json.dumps(
        [example_paragraph] if example_paragraph is not None else []
    )
    return f"""
You are compressing factual allegations from a legal complaint for an Answer drafting workflow.
The purpose is to help draft responses to complaint allegations and later identify possible
affirmative defenses. You are not drafting defenses now.

Metadata:
{json.dumps(metadata, indent=2)}

Source rows:
===
{source_text}
===

MOST IMPORTANT:
Summarize and extract key allegations only from primary factual lines.
Context lines are only for continuity.
Use source line numbers and paragraph numbers only.
Do not invent facts, paragraph numbers, dates, parties, locations, claims, or defenses.

Focus on preserving facts useful for answering the complaint and later spotting defenses:
1. Alleged conduct by each defendant.
2. Dates, time periods, deadlines, notice, delay, discovery, termination, payment,
   performance, breach, injury, damages, and alleged loss.
3. Who did what, to whom, when, where, and under what agreement, duty, statute, or policy.
4. Facts relevant to limitations, waiver, estoppel, consent, authorization, failure to
   mitigate, comparative fault, causation, standing, damages, jurisdiction, arbitration,
   contractual defenses, statutory prerequisites, or administrative exhaustion.
5. Facts that may support denial, lack of knowledge, partial admission, or a narrower answer.

Do not argue legal defenses yet. Do not turn allegations into conclusions. Do not over-compress
away dates, actors, documents, amounts, conditions, or sequence.

Return valid JSON using exactly this shape:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "summary": "Compact factual summary of this primary range.",
  "key_allegations": [
    {{
      "text": "Defendants allegedly coordinated price increases after late-2012 meetings.",
      "paragraph_numbers": {example_paragraph_numbers_json},
      "line_numbers": [{example_line}],
      "tags": ["price increase", "coordination", "late 2012"]
    }}
  ],
  "defense_relevant_facts": [
    {{
      "text": "Plaintiff alleges it discovered the issue in March 2020 but did not sue until August 2025.",
      "possible_relevance": ["statute_of_limitations", "laches", "notice"],
      "paragraph_numbers": {example_paragraph_numbers_json},
      "line_numbers": [{example_line}]
    }}
  ]
}}
If the primary range contains no meaningful factual allegations, return:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "summary": "",
  "key_allegations": [],
  "defense_relevant_facts": []
}}
""".strip()


def _build_answer_fact_digest_chunks(
    text_by_lines: list[str],
    source_ranges: list[dict],
    paragraph_by_line: dict[int, int] | None = None,
    primary_lines: int | None = None,
    context_before_lines: int | None = None,
    context_after_lines: int | None = None,
    max_prompt_tokens: int | None = None,
) -> list[dict]:
    primary_lines = ANSWER_FACT_DIGEST_PRIMARY_LINES if primary_lines is None else primary_lines
    context_before_lines = (
        ANSWER_FACT_DIGEST_CONTEXT_BEFORE_LINES
        if context_before_lines is None
        else context_before_lines
    )
    context_after_lines = (
        ANSWER_FACT_DIGEST_CONTEXT_AFTER_LINES
        if context_after_lines is None
        else context_after_lines
    )
    max_prompt_tokens = (
        ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS
        if max_prompt_tokens is None
        else max_prompt_tokens
    )
    paragraph_by_line = paragraph_by_line or {}

    if primary_lines < 1:
        raise ValueError("primary_lines must be positive.")
    if context_before_lines < 0 or context_after_lines < 0:
        raise ValueError("context line counts cannot be negative.")

    total_lines = len(text_by_lines)
    chunks = []
    for source_range_index, source_range in enumerate(source_ranges or []):
        primary_start = _to_nonnegative_int(source_range.get("start_line"))
        source_end = _to_nonnegative_int(source_range.get("end_line"))
        if primary_start is None or source_end is None or primary_start >= total_lines:
            continue
        source_end = min(source_end, total_lines - 1)
        if source_end < primary_start:
            continue

        while primary_start <= source_end:
            current_primary_lines = min(primary_lines, source_end - primary_start + 1)
            while True:
                primary_end = min(primary_start + current_primary_lines - 1, source_end)
                chunk = _build_answer_fact_digest_chunk(
                    total_lines,
                    primary_start,
                    primary_end,
                    context_before_lines,
                    context_after_lines,
                    chunk_id=len(chunks) + 1,
                    chunk_count=0,
                    source_range_index=source_range_index,
                )
                prompt = _build_answer_fact_digest_chunk_prompt(
                    chunk,
                    text_by_lines,
                    paragraph_by_line,
                )
                prompt_tokens = num_tokens_from_string(prompt)
                chunk["prompt_tokens"] = prompt_tokens
                if prompt_tokens <= max_prompt_tokens or current_primary_lines == 1:
                    if prompt_tokens > max_prompt_tokens:
                        logger.warning(
                            "ANSWER_FACT_DIGEST chunk %s prompt exceeds token cap with one primary line: %s > %s",
                            chunk["chunk_id"],
                            prompt_tokens,
                            max_prompt_tokens,
                        )
                    chunks.append(chunk)
                    primary_start = primary_end + 1
                    break
                current_primary_lines = max(1, current_primary_lines // 2)

    chunk_count = len(chunks)
    for index, chunk in enumerate(chunks):
        chunk["chunk_id"] = index + 1
        chunk["chunk_count"] = chunk_count
        chunk["chunk_index"] = index
    return chunks


def _split_answer_fact_digest_chunk(
    chunk: dict,
    text_by_lines: list[str],
    primary_lines: int,
) -> list[dict]:
    if primary_lines < 1:
        primary_lines = 1

    retry_chunks = []
    primary_start = int(chunk["primary_line_start"])
    primary_end = int(chunk["primary_line_end"])
    total_lines = len(text_by_lines)
    child_index = 1
    while primary_start <= primary_end:
        child_end = min(primary_start + primary_lines - 1, primary_end)
        retry_chunks.append(
            _build_answer_fact_digest_chunk(
                total_lines,
                primary_start,
                child_end,
                ANSWER_FACT_DIGEST_CONTEXT_BEFORE_LINES,
                ANSWER_FACT_DIGEST_CONTEXT_AFTER_LINES,
                chunk_id=f"{chunk['chunk_id']}.{child_index}",
                chunk_count=chunk["chunk_count"],
                source_range_index=chunk.get("source_range_index"),
            )
        )
        primary_start = child_end + 1
        child_index += 1
    return retry_chunks


def _parse_answer_fact_digest_chunk_output(llm_output) -> tuple[dict, bool]:
    empty_output = {
        "summary": "",
        "key_allegations": [],
        "defense_relevant_facts": [],
    }
    if not llm_output:
        return empty_output, True

    parsed = _parse_answer_structure_output(llm_output)
    if isinstance(parsed, dict) and any(
        key in parsed
        for key in ("summary", "key_allegations", "defense_relevant_facts")
    ):
        return {
            "summary": _clean_answer_structure_string(parsed.get("summary")),
            "key_allegations": (
                parsed.get("key_allegations")
                if isinstance(parsed.get("key_allegations"), list)
                else []
            ),
            "defense_relevant_facts": (
                parsed.get("defense_relevant_facts")
                if isinstance(parsed.get("defense_relevant_facts"), list)
                else []
            ),
        }, False

    return empty_output, True


def _answer_fact_digest_int_list(value, positive: bool = False) -> list[int]:
    raw_values = value if isinstance(value, list) else [value]
    output = []
    for item in raw_values:
        normalized = _to_positive_int(item) if positive else _to_nonnegative_int(item)
        if normalized is not None:
            output.append(normalized)
    return sorted(set(output))


def _normalize_answer_fact_digest_record(
    item: dict,
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
    primary_start: int | None = None,
    primary_end: int | None = None,
    allowed_line_numbers: set[int] | None = None,
    record_kind: str = "key_allegation",
) -> dict | None:
    if not isinstance(item, dict):
        return None

    text = _clean_answer_structure_string(item.get("text"))
    if not text:
        return None

    line_numbers = [
        line_number
        for line_number in _answer_fact_digest_int_list(item.get("line_numbers"))
        if 0 <= line_number < len(text_by_lines)
    ]
    if allowed_line_numbers is not None:
        line_numbers = [line_number for line_number in line_numbers if line_number in allowed_line_numbers]
    if not line_numbers:
        return None
    if primary_start is not None and primary_end is not None:
        has_primary_line = any(primary_start <= line_number <= primary_end for line_number in line_numbers)
        if not has_primary_line:
            return None

    paragraph_numbers = _answer_fact_digest_int_list(item.get("paragraph_numbers"), positive=True)
    for line_number in line_numbers:
        paragraph_number = paragraph_by_line.get(line_number)
        if paragraph_number is not None:
            paragraph_numbers.append(paragraph_number)
    paragraph_numbers = sorted(set(paragraph_numbers))

    normalized = {
        "text": text,
        "paragraph_numbers": paragraph_numbers,
        "line_numbers": sorted(set(line_numbers)),
    }
    if record_kind == "defense_relevant_fact":
        normalized["possible_relevance"] = _clean_answer_structure_list(item.get("possible_relevance"))
    else:
        normalized["tags"] = _clean_answer_structure_list(item.get("tags"))
    return normalized


def _filter_answer_fact_digest_records(
    items: list,
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
    primary_start: int | None = None,
    primary_end: int | None = None,
    allowed_line_numbers: set[int] | None = None,
    record_kind: str = "key_allegation",
) -> list[dict]:
    rows = []
    rejected_count = 0
    for item in items or []:
        normalized = _normalize_answer_fact_digest_record(
            item,
            text_by_lines,
            paragraph_by_line,
            primary_start=primary_start,
            primary_end=primary_end,
            allowed_line_numbers=allowed_line_numbers,
            record_kind=record_kind,
        )
        if normalized is None:
            rejected_count += 1
            continue
        rows.append(normalized)
    if rejected_count:
        logger.warning("ANSWER_FACT_DIGEST rejected %s invalid %s rows.", rejected_count, record_kind)
    return rows


def _extract_answer_fact_digest_for_chunk(
    chunk: dict,
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
    allow_split: bool = True,
) -> dict:
    prompt = _build_answer_fact_digest_chunk_prompt(chunk, text_by_lines, paragraph_by_line)
    prompt_tokens = num_tokens_from_string(prompt)
    logger.info(
        "ANSWER_FACT_DIGEST chunk %s of %s primary %s-%s context %s-%s prompt length %s",
        chunk["chunk_id"],
        chunk["chunk_count"],
        chunk["primary_line_start"],
        chunk["primary_line_end"],
        chunk["context_line_start"],
        chunk["context_line_end"],
        prompt_tokens,
    )
    parsed_output = chat_completion_parse(
        prompt, AnswerFactualDigestChunk,
        model=ANSWER_EXTRACTION_MODEL, reasoning_effort=ANSWER_EXTRACTION_EFFORT,
    )
    llm_output = parsed_output.model_dump() if parsed_output is not None else None
    logger.info("ANSWER_FACT_DIGEST chunk %s output preview:\n%s", chunk["chunk_id"], str(llm_output)[:1000])

    parsed_output, parse_failed = _parse_answer_fact_digest_chunk_output(llm_output)
    should_split, retry_reasons = _answer_chunk_retry_decision(parse_failed, llm_output)
    primary_count = chunk["primary_line_end"] - chunk["primary_line_start"] + 1
    if should_split and allow_split and primary_count > 1:
        retry_size = max(1, primary_count // 2)
        logger.warning(
            "Retrying ANSWER_FACT_DIGEST chunk %s in line-based halves of %s lines. reason=%s",
            chunk["chunk_id"],
            retry_size,
            ",".join(retry_reasons),
        )
        retry_results = []
        for retry_chunk in _split_answer_fact_digest_chunk(chunk, text_by_lines, retry_size):
            retry_results.append(
                _extract_answer_fact_digest_for_chunk(
                    retry_chunk,
                    text_by_lines,
                    paragraph_by_line,
                    allow_split=True,
                )
            )
        return {
            "chunk": chunk,
            "summary": "\n".join(
                retry_result["summary"]
                for retry_result in retry_results
                if retry_result.get("summary")
            ),
            "key_allegations": [
                allegation
                for retry_result in retry_results
                for allegation in retry_result["key_allegations"]
            ],
            "defense_relevant_facts": [
                fact
                for retry_result in retry_results
                for fact in retry_result["defense_relevant_facts"]
            ],
            "parse_failed": parse_failed,
            "split_retry": True,
            "retry_reasons": retry_reasons,
        }

    key_allegations = _filter_answer_fact_digest_records(
        parsed_output["key_allegations"],
        text_by_lines,
        paragraph_by_line,
        primary_start=chunk["primary_line_start"],
        primary_end=chunk["primary_line_end"],
        record_kind="key_allegation",
    )
    defense_relevant_facts = _filter_answer_fact_digest_records(
        parsed_output["defense_relevant_facts"],
        text_by_lines,
        paragraph_by_line,
        primary_start=chunk["primary_line_start"],
        primary_end=chunk["primary_line_end"],
        record_kind="defense_relevant_fact",
    )
    return {
        "chunk": chunk,
        "summary": parsed_output["summary"],
        "key_allegations": key_allegations,
        "defense_relevant_facts": defense_relevant_facts,
        "parse_failed": parse_failed,
        "split_retry": False,
        "retry_reasons": retry_reasons,
    }


def _run_answer_fact_digest_chunk_batch(
    chunks: list[dict],
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
) -> list[dict]:
    if not chunks:
        return []

    workers = min(ANSWER_FACT_DIGEST_EXTRACT_WORKERS, len(chunks))
    logger.info("Running ANSWER_FACT_DIGEST over %s chunks with max_workers=%s.", len(chunks), workers)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            submit_with_context(pool, _extract_answer_fact_digest_for_chunk, chunk, text_by_lines, paragraph_by_line): chunk
            for chunk in chunks
        }
        for future in concurrent.futures.as_completed(future_map):
            chunk = future_map[future]
            try:
                results.append(future.result())
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("ANSWER_FACT_DIGEST chunk %s failed with exception: %s", chunk["chunk_id"], exc)
                results.append({
                    "chunk": chunk,
                    "summary": "",
                    "key_allegations": [],
                    "defense_relevant_facts": [],
                    "parse_failed": True,
                    "split_retry": False,
                })
    return results


def _normalized_answer_fact_digest_text(text: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", " ", text.lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _answer_fact_digest_texts_similar(first: str, second: str) -> bool:
    first_text = _normalized_answer_fact_digest_text(first)
    second_text = _normalized_answer_fact_digest_text(second)
    if not first_text or not second_text:
        return False
    if first_text in second_text or second_text in first_text:
        return True
    return difflib.SequenceMatcher(None, first_text, second_text).ratio() >= 0.82


def _answer_fact_digest_records_are_duplicates(first: dict, second: dict) -> bool:
    first_lines = set(first.get("line_numbers", []))
    second_lines = set(second.get("line_numbers", []))
    if first_lines & second_lines and _answer_fact_digest_texts_similar(first.get("text", ""), second.get("text", "")):
        return True

    first_paragraphs = set(first.get("paragraph_numbers", []))
    second_paragraphs = set(second.get("paragraph_numbers", []))
    if (
        first_paragraphs
        and first_paragraphs & second_paragraphs
        and _answer_fact_digest_texts_similar(first.get("text", ""), second.get("text", ""))
    ):
        return True

    if first_lines and second_lines and abs(min(first_lines) - min(second_lines)) <= 3:
        return _answer_fact_digest_texts_similar(first.get("text", ""), second.get("text", ""))
    return False


def _choose_answer_fact_digest_record(candidates: list[dict], list_field: str) -> dict:
    def score(item: dict) -> tuple:
        return (
            min(len(item.get("line_numbers", [])), 4),
            min(len(item.get("paragraph_numbers", [])), 4),
            min(len(item.get(list_field, [])), 4),
            len(item.get("text", "")),
            -min(item.get("line_numbers", [0])),
        )

    return max(candidates, key=score)


def _dedupe_answer_fact_digest_records(records: list[dict], list_field: str) -> list[dict]:
    groups = []
    for record in sorted(records, key=lambda row: (row.get("line_numbers", [0])[0], row.get("text", ""))):
        for group in groups:
            if any(_answer_fact_digest_records_are_duplicates(record, existing) for existing in group):
                group.append(record)
                break
        else:
            groups.append([record])

    deduped = []
    for group in groups:
        chosen = dict(_choose_answer_fact_digest_record(group, list_field))
        chosen["line_numbers"] = sorted(set(
            line_number
            for item in group
            for line_number in item.get("line_numbers", [])
        ))
        chosen["paragraph_numbers"] = sorted(set(
            paragraph_number
            for item in group
            for paragraph_number in item.get("paragraph_numbers", [])
        ))
        chosen[list_field] = _merge_unique_answer_section_strings(
            [],
            [
                value
                for item in group
                for value in item.get(list_field, [])
            ],
        )
        deduped.append(chosen)
    return sorted(deduped, key=lambda row: (row.get("line_numbers", [0])[0], row.get("text", "")))


def _build_answer_fact_digest_reducer_prompt(
    chunk_summaries: list[dict],
    key_allegations: list[dict],
    defense_relevant_facts: list[dict],
) -> str:
    reducer_input = {
        "chunk_summaries": chunk_summaries,
        "key_allegations": key_allegations,
        "defense_relevant_facts": defense_relevant_facts,
    }
    return f"""
You are consolidating factual digest output for an Answer drafting workflow.
Use only the facts, line_numbers, and paragraph_numbers in the JSON below.
Do not add new facts, new line numbers, new paragraph numbers, new parties, or legal conclusions.

Input JSON:
{json.dumps(reducer_input, ensure_ascii=False, indent=2)}

Return valid JSON using exactly this shape:
{{
  "overall_summary": "Concise consolidated factual summary useful for drafting answer responses and later spotting affirmative defenses.",
  "key_allegations": [
    {{
      "text": "Source-supported allegation.",
      "paragraph_numbers": [1],
      "line_numbers": [10],
      "tags": ["tag"]
    }}
  ],
  "defense_relevant_facts": [
    {{
      "text": "Source-supported fact that may matter later for affirmative-defense analysis.",
      "possible_relevance": ["statute_of_limitations"],
      "paragraph_numbers": [1],
      "line_numbers": [10]
    }}
  ]
}}
""".strip()


def _reduce_answer_fact_digest_with_llm(
    chunk_summaries: list[dict],
    key_allegations: list[dict],
    defense_relevant_facts: list[dict],
    text_by_lines: list[str],
    paragraph_by_line: dict[int, int],
) -> dict:
    fallback_summary = "\n".join(summary["summary"] for summary in chunk_summaries if summary.get("summary"))
    if not chunk_summaries and not key_allegations and not defense_relevant_facts:
        return {
            "summary": "",
            "key_allegations": [],
            "defense_relevant_facts": [],
        }

    prompt = _build_answer_fact_digest_reducer_prompt(
        chunk_summaries,
        key_allegations,
        defense_relevant_facts,
    )
    prompt_tokens = num_tokens_from_string(prompt)
    if prompt_tokens > ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS:
        logger.warning(
            "ANSWER_FACT_DIGEST reducer prompt exceeds token cap; using deterministic combined digest. %s > %s",
            prompt_tokens,
            ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS,
        )
        return {
            "summary": fallback_summary,
            "key_allegations": key_allegations,
            "defense_relevant_facts": defense_relevant_facts,
        }

    logger.info("ANSWER_FACT_DIGEST reducer prompt length %s", prompt_tokens)
    try:
        parsed_output = chat_completion_parse(
            prompt, AnswerFactualDigestReduction,
            model=ANSWER_EXTRACTION_MODEL, reasoning_effort=ANSWER_EXTRACTION_EFFORT,
        )
        llm_output = parsed_output.model_dump() if parsed_output is not None else None
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning("ANSWER_FACT_DIGEST reducer failed with exception: %s", exc)
        return {
            "summary": fallback_summary,
            "key_allegations": key_allegations,
            "defense_relevant_facts": defense_relevant_facts,
        }
    logger.info("ANSWER_FACT_DIGEST reducer output preview:\n%s", str(llm_output)[:1000])
    parsed = _parse_answer_structure_output(llm_output)
    if not isinstance(parsed, dict) or not parsed:
        logger.warning("ANSWER_FACT_DIGEST reducer returned unparseable output; using deterministic combined digest.")
        return {
            "summary": fallback_summary,
            "key_allegations": key_allegations,
            "defense_relevant_facts": defense_relevant_facts,
        }

    allowed_line_numbers = set()
    for record in key_allegations + defense_relevant_facts:
        allowed_line_numbers.update(record.get("line_numbers", []))

    reduced_key_allegations = _filter_answer_fact_digest_records(
        parsed.get("key_allegations", []),
        text_by_lines,
        paragraph_by_line,
        allowed_line_numbers=allowed_line_numbers,
        record_kind="key_allegation",
    )
    reduced_defense_facts = _filter_answer_fact_digest_records(
        parsed.get("defense_relevant_facts", []),
        text_by_lines,
        paragraph_by_line,
        allowed_line_numbers=allowed_line_numbers,
        record_kind="defense_relevant_fact",
    )
    return {
        "summary": (
            _clean_answer_structure_string(parsed.get("overall_summary"))
            or _clean_answer_structure_string(parsed.get("summary"))
            or fallback_summary
        ),
        "key_allegations": reduced_key_allegations or key_allegations,
        "defense_relevant_facts": reduced_defense_facts or defense_relevant_facts,
    }


def get_answer_factual_digest_from_llm(
    text_by_lines: list[str],
    sections: list[dict],
    para_2_ln: dict | None = None,
    first_body_line_number: int = 0,
) -> dict:
    source_ranges = _answer_fact_digest_source_ranges(
        sections,
        text_by_lines,
        first_body_line_number,
    )
    if not source_ranges:
        logger.warning("ANSWER_FACT_DIGEST did not find factual source ranges.")
        return _empty_answer_factual_digest(source_ranges)

    paragraph_by_line = _answer_fact_digest_paragraph_line_map(para_2_ln, len(text_by_lines))
    chunks = _build_answer_fact_digest_chunks(
        text_by_lines,
        source_ranges,
        paragraph_by_line,
    )
    if not chunks:
        logger.warning("ANSWER_FACT_DIGEST did not build any chunks.")
        return _empty_answer_factual_digest(source_ranges)

    logger.info(
        f"ANSWER_FACT_DIGEST chunked setup: total_lines={len(text_by_lines)}, "
        f"source_ranges={source_ranges}, "
        f"primary_lines={ANSWER_FACT_DIGEST_PRIMARY_LINES}, "
        f"context_before={ANSWER_FACT_DIGEST_CONTEXT_BEFORE_LINES}, "
        f"context_after={ANSWER_FACT_DIGEST_CONTEXT_AFTER_LINES}, "
        f"max_prompt_tokens={ANSWER_FACT_DIGEST_MAX_PROMPT_TOKENS}, "
        f"chunks={len(chunks)}, workers={ANSWER_FACT_DIGEST_EXTRACT_WORKERS}"
    )
    chunk_results = _run_answer_fact_digest_chunk_batch(chunks, text_by_lines, paragraph_by_line)
    chunk_summaries = []
    key_allegations = []
    defense_relevant_facts = []
    for result in sorted(chunk_results, key=lambda row: row["chunk"]["primary_line_start"]):
        chunk = result["chunk"]
        if result.get("summary"):
            chunk_summaries.append({
                "chunk_id": chunk["chunk_id"],
                "primary_line_start": chunk["primary_line_start"],
                "primary_line_end": chunk["primary_line_end"],
                "summary": result["summary"],
            })
        key_allegations.extend(result.get("key_allegations", []))
        defense_relevant_facts.extend(result.get("defense_relevant_facts", []))

    key_allegations = _dedupe_answer_fact_digest_records(key_allegations, "tags")
    defense_relevant_facts = _dedupe_answer_fact_digest_records(defense_relevant_facts, "possible_relevance")
    reduced_digest = _reduce_answer_fact_digest_with_llm(
        chunk_summaries,
        key_allegations,
        defense_relevant_facts,
        text_by_lines,
        paragraph_by_line,
    )
    final_digest = {
        "summary": reduced_digest["summary"],
        "chunk_summaries": chunk_summaries,
        "key_allegations": reduced_digest["key_allegations"],
        "defense_relevant_facts": reduced_digest["defense_relevant_facts"],
        "source_ranges": source_ranges,
    }
    logger.info("ANSWER factual_digest: %s", json.dumps(final_digest, ensure_ascii=False, indent=2))
    return final_digest


def _answer_ad_structure_context(complaint_structure: dict) -> dict:
    complaint_structure = complaint_structure or {}
    return {
        "plaintiffs": _clean_answer_structure_list(complaint_structure.get("plaintiffs")),
        "defendants": _clean_answer_structure_list(complaint_structure.get("defendants")),
        "parties": complaint_structure.get("parties") if isinstance(complaint_structure.get("parties"), list) else [],
        "court": _clean_answer_structure_string(complaint_structure.get("court")),
        "venue": _clean_answer_structure_string(complaint_structure.get("venue")),
        "jurisdiction_type": _clean_answer_structure_string(complaint_structure.get("jurisdiction_type")),
        "state": _clean_answer_structure_string(complaint_structure.get("state")),
        "case_number": _clean_answer_structure_string(complaint_structure.get("case_number")),
        "pleading_title": _clean_answer_structure_string(complaint_structure.get("pleading_title")),
    }


def _answer_ad_coa_id(coa: dict, index: int, used_ids: set[str] | None = None) -> str:
    used_ids = used_ids if used_ids is not None else set()
    claim_number = _to_positive_int(coa.get("claim_number"))
    if claim_number is None:
        claim_number = _infer_answer_section_claim_number(coa.get("title", ""))
    candidate = str(claim_number or index + 1)
    if candidate not in used_ids:
        used_ids.add(candidate)
        return candidate

    fallback = str(index + 1)
    if fallback not in used_ids:
        used_ids.add(fallback)
        return fallback

    suffix = 2
    while f"{candidate}.{suffix}" in used_ids:
        suffix += 1
    unique_candidate = f"{candidate}.{suffix}"
    used_ids.add(unique_candidate)
    return unique_candidate


def _answer_ad_coa_text_block(text_by_lines: list[str], coa: dict) -> str:
    if not text_by_lines or not isinstance(coa, dict):
        return ""

    start_line = _to_nonnegative_int(coa.get("start_line"))
    end_line = _to_nonnegative_int(coa.get("end_line"))
    if start_line is None:
        return ""
    if end_line is None or end_line < start_line:
        end_line = start_line
    start_line = min(start_line, len(text_by_lines) - 1)
    end_line = min(end_line, len(text_by_lines) - 1)
    return "\n".join(
        f"line {line_number}: {text_by_lines[line_number]}"
        for line_number in range(start_line, end_line + 1)
    )


def _build_answer_ad_suggestion_prompt(
    text_by_lines: list[str],
    complaint_structure: dict,
    factual_digest: dict,
    coa: dict,
    coa_id: str,
) -> str:
    coa_metadata = {
        "coa_id": coa_id,
        "title": _clean_answer_structure_string(coa.get("title")),
        "claim_number": _to_positive_int(coa.get("claim_number")),
        "claim_name": _clean_answer_structure_string(coa.get("claim_name")),
        "against": _clean_answer_structure_list(coa.get("against")),
        "by": _clean_answer_structure_list(coa.get("by")),
        "start_line": _to_nonnegative_int(coa.get("start_line")),
        "end_line": _to_nonnegative_int(coa.get("end_line")),
    }
    context = {
        "complaint_structure": _answer_ad_structure_context(complaint_structure),
        "factual_digest": factual_digest or {},
        "cause_of_action": coa_metadata,
    }
    coa_text = _answer_ad_coa_text_block(text_by_lines, coa)
    complaint_summary = (
        "Case-wide complaint structure, factual digest, and current cause of action:\n"
        f"{json.dumps(context, ensure_ascii=False, indent=2)}\n\n"
        "Verbatim text of the current cause of action:\n"
        f"{coa_text}"
    )
    with open("backend/ai/prompts/answer/SUGGEST_AD") as prompt_file:
        prompt_template = prompt_file.read()
    return prompt_template.format(
        complaint_summary=complaint_summary,
        coa_id_json=json.dumps(coa_id),
        coa_title_json=json.dumps(coa_metadata["title"], ensure_ascii=False),
    )


def _parse_answer_ad_suggestion_output(llm_output) -> tuple[list, bool]:
    if not llm_output:
        return [], True

    parsed = _parse_answer_structure_output(llm_output)
    if isinstance(parsed, dict) and "affirmative_defenses" in parsed:
        items = parsed["affirmative_defenses"]
        return items if isinstance(items, list) else [], not isinstance(items, list)

    items = extract_from_json("affirmative_defenses", llm_output)
    if items:
        return items, False
    items = extract_from_json("Affirmative Defenses", llm_output)
    if items:
        return items, False
    return [], True


def _normalized_answer_ad_name_key(name: str) -> str:
    cleaned = _clean_answer_structure_string(name).lower()
    cleaned = re.sub(r"\baffirmative\s+defen[sc]e\s+(?:of\s+)?", "", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def _normalize_answer_ad_name(name) -> str:
    raw_name = _clean_answer_structure_string(name)
    if not raw_name:
        return ""
    name_key = _normalized_answer_ad_name_key(raw_name)
    if name_key in ANSWER_AD_NAME_ALIASES:
        return ANSWER_AD_NAME_ALIASES[name_key]
    if raw_name.islower() or raw_name.isupper():
        return raw_name.title()
    return re.sub(r"\s+", " ", raw_name).strip()


def _normalize_answer_ad_citation(raw_citation) -> dict | None:
    if isinstance(raw_citation, str):
        citation = _clean_answer_structure_string(raw_citation)
        if citation:
            return {"case_name": "", "citation": citation, "parenthetical": ""}
        return None
    if not isinstance(raw_citation, dict):
        return None

    citation = (
        _clean_answer_structure_string(raw_citation.get("citation"))
        or _clean_answer_structure_string(raw_citation.get("cite"))
        or _clean_answer_structure_string(raw_citation.get("displayName"))
    )
    if not citation:
        return None
    return {
        "case_name": _clean_answer_structure_string(raw_citation.get("case_name")),
        "citation": citation,
        "parenthetical": _clean_answer_structure_string(raw_citation.get("parenthetical")),
    }


def _normalize_answer_ad_item(item) -> dict | None:
    if not isinstance(item, dict):
        return None

    name = _normalize_answer_ad_name(item.get("name"))
    description = _clean_answer_structure_string(item.get("description"))
    if not name or not description:
        return None

    citations = []
    seen_citations = set()
    for raw_citation in item.get("citations") or []:
        citation = _normalize_answer_ad_citation(raw_citation)
        if not citation:
            continue
        citation_key = (
            citation.get("case_name", "").lower(),
            citation.get("citation", "").lower(),
            citation.get("parenthetical", "").lower(),
        )
        if citation_key in seen_citations:
            continue
        seen_citations.add(citation_key)
        citations.append(citation)

    return {
        "name": name,
        "description": description,
        "supporting_facts": _clean_answer_structure_list(item.get("supporting_facts")),
        "citations": citations,
    }


def _answer_ad_description_score(description: str) -> tuple:
    paragraph_refs = len(re.findall(r"(?:¶|paragraph|para\.)\s*\d+", description, flags=re.I))
    line_refs = len(re.findall(r"\bline\s+\d+", description, flags=re.I))
    date_refs = len(re.findall(r"\b(?:19|20)\d{2}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b", description))
    return (paragraph_refs + line_refs + date_refs, len(description))


def _merge_answer_ad_entry(existing: dict, incoming: dict) -> dict:
    existing_description = existing.get("description", "")
    incoming_description = incoming.get("description", "")
    if _answer_fact_digest_texts_similar(existing_description, incoming_description):
        if _answer_ad_description_score(incoming_description) > _answer_ad_description_score(existing_description):
            existing["description"] = incoming_description
    else:
        existing["description"] = "\n\n".join(
            description
            for description in (existing_description, incoming_description)
            if description
        )

    existing["supporting_facts"] = _merge_unique_answer_section_strings(
        existing.get("supporting_facts", []),
        incoming.get("supporting_facts", []),
    )

    seen_citations = {
        (
            citation.get("case_name", "").lower(),
            citation.get("citation", "").lower(),
            citation.get("parenthetical", "").lower(),
        )
        for citation in existing.get("citations", [])
    }
    for citation in incoming.get("citations", []):
        citation_key = (
            citation.get("case_name", "").lower(),
            citation.get("citation", "").lower(),
            citation.get("parenthetical", "").lower(),
        )
        if citation_key not in seen_citations:
            existing.setdefault("citations", []).append(citation)
            seen_citations.add(citation_key)
    return existing


def _extract_answer_ad_suggestions_for_coa(
    text_by_lines: list[str],
    complaint_structure: dict,
    factual_digest: dict,
    coa: dict,
    coa_id: str,
) -> dict:
    prompt = _build_answer_ad_suggestion_prompt(
        text_by_lines,
        complaint_structure,
        factual_digest,
        coa,
        coa_id,
    )
    logger.info(
        "ANSWER_AD COA %s prompt length %s title=%s",
        coa_id,
        num_tokens_from_string(prompt),
        _clean_answer_structure_string(coa.get("title")),
    )
    parsed_output = chat_completion_parse(
        prompt, AnswerDefenseSuggestions,
        model=ANSWER_DEFENSE_MODEL, reasoning_effort=ANSWER_DEFENSE_EFFORT,
    )
    llm_output = parsed_output.model_dump() if parsed_output is not None else None
    logger.info("ANSWER_AD COA %s output preview:\n%s", coa_id, str(llm_output)[:1000])
    raw_items, parse_failed = _parse_answer_ad_suggestion_output(llm_output)
    items = []
    rejected_count = 0
    for item in raw_items:
        normalized = _normalize_answer_ad_item(item)
        if normalized is None:
            rejected_count += 1
            continue
        items.append(normalized)
    if rejected_count:
        logger.warning("ANSWER_AD COA %s rejected %s invalid rows.", coa_id, rejected_count)
    return {
        "coa_id": coa_id,
        "coa": coa,
        "items": items,
        "parse_failed": parse_failed,
    }


def _run_answer_ad_suggestion_batch(
    text_by_lines: list[str],
    complaint_structure: dict,
    factual_digest: dict,
    causes_of_action: list[dict],
) -> list[dict]:
    used_ids = set()
    coa_jobs = [
        (_answer_ad_coa_id(coa, index, used_ids), coa)
        for index, coa in enumerate(causes_of_action or [])
        if isinstance(coa, dict)
    ]
    if not coa_jobs:
        return []

    workers = min(ANSWER_AD_SUGGEST_WORKERS, len(coa_jobs))
    logger.info("Running ANSWER_AD suggestions over %s COAs with max_workers=%s.", len(coa_jobs), workers)
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            submit_with_context(
                pool,
                _extract_answer_ad_suggestions_for_coa,
                text_by_lines,
                complaint_structure,
                factual_digest,
                coa,
                coa_id,
            ): (coa_id, coa)
            for coa_id, coa in coa_jobs
        }
        for future in concurrent.futures.as_completed(future_map):
            coa_id, coa = future_map[future]
            try:
                results.append(future.result())
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("ANSWER_AD COA %s failed with exception: %s", coa_id, exc)
                results.append({
                    "coa_id": coa_id,
                    "coa": coa,
                    "items": [],
                    "parse_failed": True,
                })
    return results


def _consolidate_answer_ad_suggestions(coa_results: list[dict]) -> dict:
    ads_output_dict = {}
    for result in sorted(coa_results, key=lambda row: _to_positive_int(row.get("coa_id")) or 999999):
        coa = result.get("coa") or {}
        coa_id = str(result.get("coa_id"))
        coa_title = _clean_answer_structure_string(coa.get("title")) or result.get("coa_title") or f"Cause of Action {coa_id}"
        for item in result.get("items", []):
            ad_name = item["name"]
            entry = {
                "cause_of_action": coa_title,
                "description": item["description"],
                "supporting_facts": item.get("supporting_facts", []),
                "citations": item.get("citations", []),
                "filtered_citation_info": [],
                "filtered_citation": [],
                "filtered_citation_html": [],
            }
            existing = ads_output_dict.setdefault(ad_name, {}).get(coa_id)
            if existing:
                ads_output_dict[ad_name][coa_id] = _merge_answer_ad_entry(existing, entry)
            else:
                ads_output_dict[ad_name][coa_id] = entry
    return ads_output_dict


def _append_filtered_answer_ad_citation(entry: dict, raw_cit: dict, filtered_citation_info: list[dict]) -> None:
    entry.setdefault("filtered_citation_info", [])
    entry.setdefault("filtered_citation", [])
    entry.setdefault("filtered_citation_html", [])
    if not filtered_citation_info:
        return

    try:
        on_case_filtered_citation_info = filter_on_casename(raw_cit, filtered_citation_info)
    except Exception as exc:
        logger.warning("ANSWER_AD citation case-name filtering failed for %s: %s", raw_cit, exc)
        on_case_filtered_citation_info = filtered_citation_info
    if not on_case_filtered_citation_info:
        return

    on_case_filtered_citation_info = sorted(
        on_case_filtered_citation_info,
        key=lambda citation_info: citation_info.get("start_index", 0),
    )
    entry["filtered_citation_info"].extend(on_case_filtered_citation_info)

    annotated_result = raw_cit.get("citation", "")
    offset = 0
    try:
        for citation_info in on_case_filtered_citation_info:
            annotated_result, next_offset = insert_tags(annotated_result, citation_info, offset)
            offset += next_offset
    except Exception as exc:
        logger.warning("ANSWER_AD citation HTML tagging failed for %s: %s", raw_cit, exc)
        annotated_result = raw_cit.get("citation", "")

    llm_case_name = _clean_answer_structure_string(raw_cit.get("case_name"))
    if llm_case_name:
        areft_tag_regex = r'<a\s+href="[^"]*"\s+target="_blank"\s+rel="noopener noreferrer">'
        to_replace = re.search(areft_tag_regex, annotated_result)
        if to_replace:
            tag_plus_casename = to_replace.group(0) + f" {llm_case_name}, "
            annotated_result = annotated_result.replace(to_replace.group(0), tag_plus_casename, 1)

    parenthetical = _clean_answer_structure_string(raw_cit.get("parenthetical"))
    annotated_full_str = " ".join([annotated_result, parenthetical]).strip()
    plain_checked = " ".join([
        _clean_answer_structure_string(raw_cit.get("case_name")),
        _clean_answer_structure_string(raw_cit.get("citation")),
        parenthetical,
    ]).strip()
    if annotated_full_str:
        entry["filtered_citation_html"].append(annotated_full_str)
    if plain_checked:
        entry["filtered_citation"].append(plain_checked)


def _filter_answer_ad_citations(ads_output_dict: dict) -> dict:
    tasks = []
    for ad_name, coa_entries in ads_output_dict.items():
        for coa_id, entry in coa_entries.items():
            entry.setdefault("filtered_citation_info", [])
            entry.setdefault("filtered_citation", [])
            entry.setdefault("filtered_citation_html", [])
            for raw_cit in entry.get("citations", []):
                if isinstance(raw_cit, dict) and _clean_answer_structure_string(raw_cit.get("citation")):
                    tasks.append({
                        "ad_name": ad_name,
                        "coa_id": coa_id,
                        "entry": entry,
                        "raw_cit": raw_cit,
                    })

    if not tasks:
        return ads_output_dict

    workers = min(ANSWER_AD_CITATION_FILTER_WORKERS, len(tasks))
    logger.info("Running ANSWER_AD citation filtering over %s raw citations with max_workers=%s.", len(tasks), workers)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            pool.submit(filter_raw_citations, task["raw_cit"]["citation"]): task
            for task in tasks
        }
        for future in concurrent.futures.as_completed(future_map):
            task = future_map[future]
            try:
                filtered_citation_info = future.result()
                _append_filtered_answer_ad_citation(
                    task["entry"],
                    task["raw_cit"],
                    filtered_citation_info,
                )
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning(
                    "ANSWER_AD citation lookup failed for defense=%s coa=%s citation=%s: %s",
                    task["ad_name"],
                    task["coa_id"],
                    task["raw_cit"].get("citation"),
                    exc,
                )
    return ads_output_dict


def get_answer_ad_suggestions_from_llm(
    text_by_lines: list[str],
    complaint_structure: dict,
    factual_digest: dict,
    causes_of_action: list[dict],
) -> dict:
    if not causes_of_action:
        logger.warning("ANSWER_AD found no causes of action; saved empty ads_suggestions.")
        return {}

    coa_results = _run_answer_ad_suggestion_batch(
        text_by_lines,
        complaint_structure,
        factual_digest,
        causes_of_action,
    )
    ads_output_dict = _consolidate_answer_ad_suggestions(coa_results)
    if not ads_output_dict:
        logger.warning("ANSWER_AD generated no affirmative defense suggestions.")
        return {}

    _filter_answer_ad_citations(ads_output_dict)
    logger.info("ANSWER ads_suggestions: %s", json.dumps(ads_output_dict, ensure_ascii=False, indent=2))
    return ads_output_dict


def get_headers_from_llm(text_by_lines: list[str], first_body_line_number: int = 0) -> dict:
    """
    Extract all section headings as long as they're all in upper-case.
    Returns:
    hd_2_ln : list[("HEADING A", line_num)]   if llm can detect 5+ headings, otherwise []
    """
    # print("=== text by lines ===\n", text_by_lines)
    first_body_line_number = max(0, first_body_line_number or 0)
    hd, ln_numbers = extract_headings(text_by_lines)
    filtered_headings = [
        (h, l)
        for h, l in zip(hd, ln_numbers)
        if l >= first_body_line_number
    ]
    hd = [h for h, _l in filtered_headings]
    ln_numbers = [l for _h, l in filtered_headings]
    print(hd)
    print(ln_numbers)
    hd_lines = "\n".join(str(h) + "\t(" + str(l) + ")" for h, l in list(zip(hd, ln_numbers)))
    if not hd_lines:
        print("No candidate headings after first body line.")
        return {}
    command_prompt = open('backend/ai/prompts/answer/EXTRACT_HEADINGS').read()
    filled_in_prompt = command_prompt.format(hd_lines=hd_lines)
    print("== Extract headers filled_in_prompt ===\n", num_tokens_from_string(filled_in_prompt), filled_in_prompt)
    llm_output = chat_completion_j(filled_in_prompt)
    print("== Extract headers llm_output ===\n", llm_output)

    line_num_pattern = r"\(([0-9]+)\)"
    heading_cutoffs = [max(0, first_body_line_number - 1)]
    hd_2_ln = {}
    try:
        headings_str = json.loads(llm_output)["headings"]
        print("lm output headings_str: \n", headings_str)
        for head_line in headings_str.split(';'):
            matches = list(re.finditer(line_num_pattern, head_line))
            # If matches were found, print the last one
            if matches:
                matched_num = int(matches[-1].group(1))
                print("Last matched line number:", matched_num)
                heading = head_line[:head_line.rfind('(')].strip()
                if matched_num >= first_body_line_number and matched_num > heading_cutoffs[-1]:  # new added line number has to be greater than the current last
                    heading_cutoffs.append(matched_num)
                    print("New section headers added: ", heading)
                    hd_2_ln[heading] = matched_num
    except Exception as e:
        print("Error decoding heading line numbers: ", e)
    print("heading cutoffs: ", heading_cutoffs)
    return hd_2_ln if len(hd_2_ln) > 4 else {}


def _build_answer_para_chunk(
    total_lines: int,
    primary_start: int,
    primary_end: int,
    context_lines: int,
    chunk_id,
    chunk_count: int,
) -> dict:
    context_start = max(0, primary_start - context_lines)
    context_end = min(total_lines - 1, primary_end + context_lines)
    return {
        "chunk_id": chunk_id,
        "chunk_count": chunk_count,
        "total_lines": total_lines,
        "context_line_start": context_start,
        "context_line_end": context_end,
        "primary_line_start": primary_start,
        "primary_line_end": primary_end,
    }


def _build_answer_para_chunks(
    text_by_lines: list[str],
    primary_lines: int | None = None,
    context_lines: int | None = None,
    primary_start_line: int = 0,
) -> list[dict]:
    primary_lines = ANSWER_PARA_PRIMARY_LINES if primary_lines is None else primary_lines
    context_lines = ANSWER_PARA_CONTEXT_LINES if context_lines is None else context_lines
    if primary_lines < 1:
        raise ValueError("primary_lines must be positive.")
    if context_lines < 0:
        raise ValueError("context_lines cannot be negative.")

    total_lines = len(text_by_lines)
    if total_lines == 0:
        return []
    primary_start_line = max(0, primary_start_line or 0)
    if primary_start_line >= total_lines:
        return []

    chunks = []
    primary_start = primary_start_line
    while primary_start < total_lines:
        primary_end = min(primary_start + primary_lines - 1, total_lines - 1)
        chunks.append((primary_start, primary_end))
        primary_start = primary_end + 1

    chunk_count = len(chunks)
    return [
        {
            **_build_answer_para_chunk(
                total_lines,
                primary_start,
                primary_end,
                context_lines,
                chunk_id=index + 1,
                chunk_count=chunk_count,
            ),
            "chunk_index": index,
        }
        for index, (primary_start, primary_end) in enumerate(chunks)
    ]


def _split_answer_para_chunk(
    chunk: dict,
    text_by_lines: list[str],
    primary_lines: int,
) -> list[dict]:
    if primary_lines < 1:
        primary_lines = 1

    retry_chunks = []
    primary_start = int(chunk["primary_line_start"])
    primary_end = int(chunk["primary_line_end"])
    total_lines = len(text_by_lines)
    child_index = 1
    while primary_start <= primary_end:
        child_end = min(primary_start + primary_lines - 1, primary_end)
        retry_chunks.append(
            _build_answer_para_chunk(
                total_lines,
                primary_start,
                child_end,
                ANSWER_PARA_CONTEXT_LINES,
                chunk_id=f"{chunk['chunk_id']}.{child_index}",
                chunk_count=chunk["chunk_count"],
            )
        )
        primary_start = child_end + 1
        child_index += 1
    return retry_chunks


def _format_answer_para_source_lines(text_by_lines: list[str], start_line: int, end_line: int) -> str:
    return "\n".join(
        f"line {line_num}: {text_by_lines[line_num]}"
        for line_num in range(start_line, end_line + 1)
    )


def _build_answer_para_chunk_prompt(chunk: dict, text_by_lines: list[str]) -> str:
    metadata = {
        "chunk_id": chunk["chunk_id"],
        "chunk_count": chunk["chunk_count"],
        "total_lines": chunk["total_lines"],
        "line_number_base": 0,
        "context_line_start": chunk["context_line_start"],
        "context_line_end": chunk["context_line_end"],
        "primary_line_start": chunk["primary_line_start"],
        "primary_line_end": chunk["primary_line_end"],
    }
    source_lines = _format_answer_para_source_lines(
        text_by_lines,
        chunk["context_line_start"],
        chunk["context_line_end"],
    )
    example_line = chunk["primary_line_start"]
    return f"""
You are provided with a globally numbered slice of raw text extracted from a legal complaint.
Your task is to identify source lines that are the start of true numbered paragraphs in the
main body of the complaint.

Metadata:
{json.dumps(metadata, indent=2)}

Source content:
===
{source_lines}
===

MOST IMPORTANT:
Return only paragraph starts located in the primary range.
Context lines are only for understanding continuity.
Do not return paragraph starts from context-only lines.
Use the source lines as truth. Do not invent paragraph numbers or line numbers.

Additional requirements:
1. Use the global line number shown before each source line.
2. Paragraph numbers usually appear at or near the beginning of the source line.
3. Do not count table-of-contents entries, table-of-authorities entries, section headings,
   page numbers, citations, footers, or outline/list items unless they are true complaint
   body paragraphs.
4. Paragraph numbers should generally increase as global line numbers increase, but OCR
   may create gaps. Do not fill gaps with made-up locations.
5. If the primary range contains no true paragraph starts, return an empty paragraphs array.
6. Do not return placeholder objects with blank line_number or paragraph_number.
7. Return valid JSON using these exact keys:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "paragraphs": [
    {{"line_number": {example_line}, "paragraph_number": 1}}
  ]
}}
If no paragraph starts exist in the primary range, return:
{{
  "chunk_id": "{chunk['chunk_id']}",
  "paragraphs": []
}}
""".strip()


def _parse_answer_para_chunk_output(llm_output) -> tuple[list, bool]:
    if not llm_output:
        return [], True

    if isinstance(llm_output, dict):
        paragraphs = llm_output.get("paragraphs")
        return paragraphs if isinstance(paragraphs, list) else [], not isinstance(paragraphs, list)

    if isinstance(llm_output, str):
        try:
            parsed = json.loads(llm_output.strip())
            if isinstance(parsed, dict) and "paragraphs" in parsed:
                paragraphs = parsed["paragraphs"]
                return paragraphs if isinstance(paragraphs, list) else [], not isinstance(paragraphs, list)
        except Exception:
            pass

    paragraphs = extract_from_json("paragraphs", llm_output)
    if paragraphs:
        return paragraphs, False
    return [], True


def _to_positive_int(value):
    try:
        cleaned = str(value).strip().replace(",", "")
        if re.fullmatch(r"\d+", cleaned):
            intval = int(cleaned)
            if intval > 0:
                return intval
    except Exception:
        pass
    return None


def _to_nonnegative_int(value):
    try:
        cleaned = str(value).strip().replace(",", "")
        if re.fullmatch(r"\d+", cleaned):
            return int(cleaned)
    except Exception:
        pass
    return None


def _answer_para_item_value(item: dict, snake_key: str, spaced_key: str):
    if snake_key in item:
        return item[snake_key]
    return item.get(spaced_key)


def _line_text_supports_paragraph_number(line_text: str, paragraph_number: int) -> bool:
    cleaned = line_text.replace("<B>", "").replace("</B>", "").strip()
    prefix = cleaned[:80]
    pattern = rf"(^|\s)(?:¶\s*)?\(?{re.escape(str(paragraph_number))}\)?(?:[\.\):]|\s)"
    return re.search(pattern, prefix) is not None


def _filter_answer_para_rows(chunk: dict, paragraph_items: list, text_by_lines: list[str]) -> list[dict]:
    rows = []
    rejected_count = 0
    for item in paragraph_items:
        if not isinstance(item, dict):
            rejected_count += 1
            continue
        line_num = _to_nonnegative_int(_answer_para_item_value(item, "line_number", "line number"))
        para_num = _to_positive_int(_answer_para_item_value(item, "paragraph_number", "paragraph number"))
        if line_num is None or para_num is None:
            rejected_count += 1
            continue
        if line_num < chunk["primary_line_start"] or line_num > chunk["primary_line_end"]:
            rejected_count += 1
            continue
        if line_num >= len(text_by_lines):
            rejected_count += 1
            continue
        rows.append(
            {
                "paragraph_number": para_num,
                "line_number": line_num,
                "chunk_id": chunk["chunk_id"],
                "line_text": text_by_lines[line_num],
            }
        )

    if rejected_count:
        print(f"EXTRACT_PARA chunk {chunk['chunk_id']} rejected {rejected_count} out-of-range or invalid rows.")
    return rows


def _extract_answer_para_rows_for_chunk(chunk: dict, text_by_lines: list[str], allow_split: bool = True) -> dict:
    prompt = _build_answer_para_chunk_prompt(chunk, text_by_lines)
    print(
        "EXTRACT_PARA chunk",
        chunk["chunk_id"],
        "of",
        chunk["chunk_count"],
        "primary",
        f"{chunk['primary_line_start']}-{chunk['primary_line_end']}",
        "context",
        f"{chunk['context_line_start']}-{chunk['context_line_end']}",
        "prompt length",
        num_tokens_from_string(prompt),
    )
    parsed_output = chat_completion_parse(
        prompt, AnswerParagraphChunk,
        model=ANSWER_EXTRACTION_MODEL, reasoning_effort=ANSWER_EXTRACTION_EFFORT,
    )
    llm_output = parsed_output.model_dump() if parsed_output is not None else None
    print(f"EXTRACT_PARA chunk {chunk['chunk_id']} output preview:\n{str(llm_output)[:1000]}")

    paragraph_items, parse_failed = _parse_answer_para_chunk_output(llm_output)
    should_split, retry_reasons = _answer_chunk_retry_decision(parse_failed, llm_output)
    primary_count = chunk["primary_line_end"] - chunk["primary_line_start"] + 1
    if should_split and allow_split and primary_count > 1:
        retry_size = max(1, primary_count // 2)
        print(
            f"Retrying EXTRACT_PARA chunk {chunk['chunk_id']} in line-based halves of "
            f"{retry_size} lines. reason={','.join(retry_reasons)}"
        )
        retry_rows = []
        for retry_chunk in _split_answer_para_chunk(chunk, text_by_lines, retry_size):
            retry_result = _extract_answer_para_rows_for_chunk(retry_chunk, text_by_lines, allow_split=True)
            retry_rows.extend(retry_result["rows"])
        return {
            "chunk": chunk,
            "rows": retry_rows,
            "parse_failed": parse_failed,
            "split_retry": True,
            "retry_reasons": retry_reasons,
        }

    rows = _filter_answer_para_rows(chunk, paragraph_items, text_by_lines)
    return {
        "chunk": chunk,
        "rows": rows,
        "parse_failed": parse_failed,
        "split_retry": False,
        "retry_reasons": retry_reasons,
    }


def _run_answer_para_chunk_batch(chunks: list[dict], text_by_lines: list[str]) -> list[dict]:
    if not chunks:
        return []

    workers = min(ANSWER_PARA_EXTRACT_WORKERS, len(chunks))
    print(f"Running EXTRACT_PARA over {len(chunks)} chunks with max_workers={workers}.")
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            submit_with_context(pool, _extract_answer_para_rows_for_chunk, chunk, text_by_lines): chunk
            for chunk in chunks
        }
        for future in concurrent.futures.as_completed(future_map):
            chunk = future_map[future]
            try:
                results.append(future.result())
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                print(f"EXTRACT_PARA chunk {chunk['chunk_id']} failed with exception: {str(exc)}")
                results.append({
                    "chunk": chunk,
                    "rows": [],
                    "parse_failed": True,
                    "split_retry": False,
                })
    return results


def _previous_nonempty_chunk_result(results_by_index: list[dict], index: int):
    for prev_index in range(index - 1, -1, -1):
        result = results_by_index[prev_index]
        if result and result["rows"]:
            return result
    return None


def _next_nonempty_chunk_result(results_by_index: list[dict], index: int):
    for next_index in range(index + 1, len(results_by_index)):
        result = results_by_index[next_index]
        if result and result["rows"]:
            return result
    return None


def _should_retry_empty_answer_para_chunk(results_by_index: list[dict], index: int) -> bool:
    result = results_by_index[index]
    if not result or result["rows"]:
        return False

    previous_result = _previous_nonempty_chunk_result(results_by_index, index)
    next_result = _next_nonempty_chunk_result(results_by_index, index)
    if not previous_result or not next_result:
        return False

    previous_max = max(row["paragraph_number"] for row in previous_result["rows"])
    next_min = min(row["paragraph_number"] for row in next_result["rows"])
    return next_min <= previous_max or next_min - previous_max > 1


def _retry_empty_answer_para_chunks(
    chunks: list[dict],
    results_by_index: list[dict],
    text_by_lines: list[str],
) -> list[dict]:
    retry_chunks = []
    for index, chunk in enumerate(chunks):
        if _should_retry_empty_answer_para_chunk(results_by_index, index):
            print(f"Retrying empty EXTRACT_PARA chunk {chunk['chunk_id']} with {ANSWER_PARA_RETRY_PRIMARY_LINES}-line primary ranges.")
            retry_chunks.extend(
                _split_answer_para_chunk(chunk, text_by_lines, ANSWER_PARA_RETRY_PRIMARY_LINES)
            )
    return _run_answer_para_chunk_batch(retry_chunks, text_by_lines)


def _answer_para_order_violations(row: dict, all_rows: list[dict]) -> int:
    violations = 0
    for other in all_rows:
        if other is row or other["paragraph_number"] == row["paragraph_number"]:
            continue
        if other["line_number"] < row["line_number"] and other["paragraph_number"] > row["paragraph_number"]:
            violations += 1
        elif other["line_number"] > row["line_number"] and other["paragraph_number"] < row["paragraph_number"]:
            violations += 1
    return violations


def _answer_para_candidate_window_score(row: dict, all_rows: list[dict]) -> tuple[bool, int, bool, int]:
    paragraph_number = row["paragraph_number"]
    line_number = row["line_number"]
    has_exact_previous = False
    has_exact_next = False
    has_lower_anchor = False
    has_higher_anchor = False
    nearest_next_distance = 10**9

    for other in all_rows:
        if other is row or other["paragraph_number"] == paragraph_number:
            continue
        other_para = other["paragraph_number"]
        other_line = other["line_number"]
        if other_para == paragraph_number - 1 and other_line < line_number:
            has_exact_previous = True
        elif other_para == paragraph_number + 1 and other_line > line_number:
            has_exact_next = True
        if other_para < paragraph_number and other_line < line_number:
            has_lower_anchor = True
        elif other_para > paragraph_number and other_line > line_number:
            has_higher_anchor = True
            nearest_next_distance = min(nearest_next_distance, other_line - line_number)

    exact_neighbor_count = int(has_exact_previous) + int(has_exact_next)
    monotonic_anchor_count = int(has_lower_anchor) + int(has_higher_anchor)
    has_previous_anchor = has_exact_previous or has_lower_anchor
    line_tiebreak = -line_number if has_previous_anchor else -nearest_next_distance
    return exact_neighbor_count == 2, exact_neighbor_count + monotonic_anchor_count, has_previous_anchor, line_tiebreak


def _choose_duplicate_answer_para_row(candidates: list[dict], all_rows: list[dict]) -> dict:
    def score(row: dict) -> tuple:
        in_exact_window, window_score, has_previous_anchor, line_tiebreak = _answer_para_candidate_window_score(row, all_rows)
        return (
            _line_text_supports_paragraph_number(row["line_text"], row["paragraph_number"]),
            in_exact_window,
            window_score,
            has_previous_anchor,
            -_answer_para_order_violations(row, all_rows),
            line_tiebreak,
        )

    return max(candidates, key=score)


def _empty_answer_para_warnings() -> dict:
    return {
        "has_warnings": False,
        "summary": "",
        "issues": [],
        "stats": {
            "duplicate_count": 0,
            "decreasing_count": 0,
            "suspicious_gap_count": 0,
            "missing_count": 0,
        },
    }


def _answer_para_labels(paragraph_numbers: list[int]) -> str:
    labels = [f"Paragraph {number}" for number in paragraph_numbers]
    if len(labels) <= 1:
        return labels[0] if labels else "these paragraphs"
    if len(labels) == 2:
        return " and ".join(labels)
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _append_answer_para_warning_issue(issues: list[dict], issue: dict) -> None:
    if len(issues) < ANSWER_PARA_WARNING_ISSUE_LIMIT:
        issues.append(issue)


def _answer_para_rows_by_line(para_2_ln: dict) -> list[dict]:
    rows = []
    for paragraph_number, line_number in para_2_ln.items():
        normalized_paragraph_number = _to_positive_int(paragraph_number)
        normalized_line_number = _to_nonnegative_int(line_number)
        if normalized_paragraph_number is None or normalized_line_number is None:
            continue
        rows.append({
            "paragraph_number": normalized_paragraph_number,
            "line_number": normalized_line_number,
        })
    return sorted(rows, key=lambda row: (row["line_number"], row["paragraph_number"]))


def _build_answer_para_warning_payload(
    para_2_ln: dict,
    duplicate_issues: list[dict] | None = None,
) -> tuple[dict, list[tuple], list[tuple], list[tuple]]:
    warnings = _empty_answer_para_warnings()
    duplicate_issues = duplicate_issues or []
    rows_by_line = _answer_para_rows_by_line(para_2_ln)
    if not rows_by_line:
        return warnings, [], [], []

    issues = []
    decreasing = []
    suspicious_gaps = []
    small_missing_ranges = []

    for index in range(1, len(rows_by_line)):
        prev_row = rows_by_line[index - 1]
        curr_row = rows_by_line[index]
        prev_para = prev_row["paragraph_number"]
        curr_para = curr_row["paragraph_number"]
        prev_line = prev_row["line_number"]
        curr_line = curr_row["line_number"]
        para_gap = curr_para - prev_para

        if curr_para <= prev_para:
            decreasing.append((prev_para, prev_line, curr_para, curr_line))
            paragraph_numbers = [prev_para, curr_para]
            line_numbers = [prev_line, curr_line]
            _append_answer_para_warning_issue(issues, {
                "type": "decreasing_order",
                "message": f"Check numbering around {_answer_para_labels(paragraph_numbers)}.",
                "paragraph_numbers": paragraph_numbers,
                "line_numbers": line_numbers,
            })
        elif para_gap > ANSWER_PARA_SUSPICIOUS_GAP_THRESHOLD:
            suspicious_gaps.append((prev_para, curr_para, prev_line, curr_line))
            paragraph_numbers = [prev_para, curr_para]
            line_numbers = [prev_line, curr_line]
            if index + 1 < len(rows_by_line):
                next_row = rows_by_line[index + 1]
                if next_row["paragraph_number"] < curr_para:
                    paragraph_numbers.append(next_row["paragraph_number"])
                    line_numbers.append(next_row["line_number"])
            _append_answer_para_warning_issue(issues, {
                "type": "suspicious_gap",
                "message": f"Check numbering around {_answer_para_labels(paragraph_numbers)}.",
                "paragraph_numbers": paragraph_numbers,
                "line_numbers": line_numbers,
            })
        elif 1 < para_gap <= ANSWER_PARA_SMALL_MISSING_GAP_LIMIT:
            missing_start = prev_para + 1
            missing_end = curr_para - 1
            small_missing_ranges.append((missing_start, missing_end, prev_para, curr_para, prev_line, curr_line))
            missing_label = (
                f"Paragraph {missing_start}"
                if missing_start == missing_end
                else f"Paragraphs {missing_start}-{missing_end}"
            )
            _append_answer_para_warning_issue(issues, {
                "type": "missing_range",
                "message": (
                    f"{missing_label} may need review between "
                    f"{_answer_para_labels([prev_para])} and {_answer_para_labels([curr_para])}."
                ),
                "paragraph_numbers": [prev_para, curr_para],
                "line_numbers": [prev_line, curr_line],
            })

    missing_count = sum(
        missing_end - missing_start + 1
        for missing_start, missing_end, *_rest in small_missing_ranges
    )
    stats = {
        "duplicate_count": len(duplicate_issues),
        "decreasing_count": len(decreasing),
        "suspicious_gap_count": len(suspicious_gaps),
        "missing_count": missing_count,
    }
    warnings["stats"] = stats
    warnings["issues"] = issues
    warnings["has_warnings"] = bool(issues)
    if warnings["has_warnings"]:
        warnings["summary"] = "Some paragraph numbers may need review before drafting responses."

    return warnings, decreasing, suspicious_gaps, small_missing_ranges


def _consolidate_answer_para_rows(
    rows: list[dict],
    text_by_lines: list[str],
    duplicate_issues: list[dict] | None = None,
) -> dict:
    unique_rows = {}
    for row in rows:
        unique_rows.setdefault((row["paragraph_number"], row["line_number"]), row)

    rows = sorted(unique_rows.values(), key=lambda row: (row["line_number"], row["paragraph_number"]))
    by_paragraph = {}
    for row in rows:
        by_paragraph.setdefault(row["paragraph_number"], []).append(row)

    chosen_rows = []
    for paragraph_number, candidates in by_paragraph.items():
        if len(candidates) == 1:
            chosen_rows.append(candidates[0])
            continue
        chosen = _choose_duplicate_answer_para_row(candidates, rows)
        print(
            "Duplicate paragraph number",
            paragraph_number,
            "from lines",
            [candidate["line_number"] for candidate in candidates],
            "kept line",
            chosen["line_number"],
        )
        if duplicate_issues is not None:
            duplicate_issues.append({
                "paragraph_number": paragraph_number,
                "candidate_line_numbers": [candidate["line_number"] for candidate in candidates],
                "kept_line_number": chosen["line_number"],
            })
        chosen_rows.append(chosen)

    chosen_rows = sorted(chosen_rows, key=lambda row: (row["line_number"], row["paragraph_number"]))
    return {row["paragraph_number"]: row["line_number"] for row in chosen_rows}


def _validate_answer_para_line_map(para_2_ln: dict, duplicate_issues: list[dict] | None = None) -> dict:
    if not para_2_ln:
        print("Warning: EXTRACT_PARA returned no source-backed paragraph line locations.")
        return _empty_answer_para_warnings()

    warnings, decreasing, suspicious_gaps, small_missing_ranges = _build_answer_para_warning_payload(
        para_2_ln,
        duplicate_issues,
    )

    rows_by_line = _answer_para_rows_by_line(para_2_ln)
    paragraph_numbers = [row["paragraph_number"] for row in rows_by_line]
    print(
        "EXTRACT_PARA validation:",
        f"recognized={len(para_2_ln)}",
        f"range={min(paragraph_numbers)}-{max(paragraph_numbers)}",
        f"missing={warnings['stats']['missing_count']}",
        f"decreasing={len(decreasing)}",
        f"suspicious_gaps={len(suspicious_gaps)}",
    )
    if small_missing_ranges:
        print("EXTRACT_PARA small missing range sample:", small_missing_ranges[:20])
    if decreasing:
        print("EXTRACT_PARA decreasing order sample:", decreasing[:20])
    if suspicious_gaps:
        print("EXTRACT_PARA suspicious gap sample:", suspicious_gaps[:20])
    if warnings["has_warnings"]:
        logger.info("EXTRACT_PARA paragraph_warnings: %s", json.dumps(warnings, ensure_ascii=False, indent=2))
    return warnings


def get_paralines_from_llm(
    text_by_lines: list[str],
    primary_start_line: int = 0,
    return_warnings: bool = False,
) -> dict | tuple[dict, dict]:
    """
    Make chunked LLM calls over globally numbered source lines.
    Returns:
    para_2_ln: dict
    - key (int): para_number
    - value (int): line_number (within whole doc)
    """
    chunks = _build_answer_para_chunks(text_by_lines, primary_start_line=primary_start_line)
    if not chunks:
        raise ValueError("EXTRACT_PARA cannot run on an empty complaint.")

    print(
        f"EXTRACT_PARA chunked setup: total_lines={len(text_by_lines)}, "
        f"primary_start_line={max(0, primary_start_line or 0)}, "
        f"primary_lines={ANSWER_PARA_PRIMARY_LINES}, context_lines={ANSWER_PARA_CONTEXT_LINES}, "
        f"chunks={len(chunks)}, workers={ANSWER_PARA_EXTRACT_WORKERS}"
    )
    initial_results = _run_answer_para_chunk_batch(chunks, text_by_lines)
    results_by_index = [None] * len(chunks)
    for result in initial_results:
        chunk_index = result["chunk"].get("chunk_index")
        if chunk_index is not None:
            results_by_index[chunk_index] = result

    for index, chunk in enumerate(chunks):
        if results_by_index[index] is None:
            results_by_index[index] = {
                "chunk": chunk,
                "rows": [],
                "parse_failed": True,
                "split_retry": False,
            }

    retry_results = _retry_empty_answer_para_chunks(chunks, results_by_index, text_by_lines)
    all_rows = []
    for result in results_by_index:
        all_rows.extend(result["rows"])
    for result in retry_results:
        all_rows.extend(result["rows"])

    duplicate_issues = []
    para_2_ln = _consolidate_answer_para_rows(all_rows, text_by_lines, duplicate_issues=duplicate_issues)
    paragraph_warnings = _validate_answer_para_line_map(para_2_ln, duplicate_issues=duplicate_issues)
    if not para_2_ln:
        raise ValueError("EXTRACT_PARA chunked LLM output did not identify any paragraphs.")

    if return_warnings:
        return para_2_ln, paragraph_warnings
    return para_2_ln


def parse_paragraphs(text_by_lines: list[str], hd_2_ln: dict) -> dict:
    """
    Pattern and rule based paragraphs extraction, only to save time and save
    Returns:
    para_2_ln: dict
    - key (int): para_number
    - value (int): line_number (within whole doc)
    """
    para_2_ln = {}

    curr_para = 0
    para_pattern = r'^(\d{1,4})[^a-zA-Z0-9]+[A-Z]'
    first_header_loc = min(hd_2_ln.values())

    for ln_num in range(len(text_by_lines) - 1):
        check2lines = ' '.join([text_by_lines[ln_num], text_by_lines[ln_num + 1]])
        # print("check2lines: >", check2lines, "<")
        if ln_num > first_header_loc:  # not for sure to keep this, but start looking after first section heading
            match1 = re.match(para_pattern, check2lines)  # match since the start
            if match1:
                matched_num = int(match1.group(1))
                # print(f"match1: >{check2lines}<")
                if matched_num > curr_para and matched_num - curr_para <= 3:  # account for 2 consecutive mis-decoded numbers
                    curr_para = matched_num
                    print("valid number, curr para = ", curr_para)
                    para_2_ln[curr_para] = ln_num
                else:
                    print("not valid number: ", matched_num)

    if len(para_2_ln) < len(text_by_lines) / 20:  # hard-coded rule: very long paragraph are ~20 lines
        print(f"too few paragraphs found: # para = {len(para_2_ln)}, # text_by_lines = {len(text_by_lines)}")
        para_2_ln = get_paralines_from_llm(text_by_lines)

    return para_2_ln


def is_consecutive_or_expand(parag_2_ln:dict) -> dict:

    key_lst = list(parag_2_ln.keys())
    key_lst = [0] + key_lst
    parag_2_ln[0] = 0
    print("check consecutive: before:\n", key_lst)
    for i in range(1, len(key_lst)):  # add zero at the start, so first element should be 1
        diff = key_lst[i] - key_lst[i-1]
        diff_break = 1/diff
        prev_key = key_lst[i-1]
        line_diff = parag_2_ln[key_lst[i]] - parag_2_ln[key_lst[i-1]]  
        while diff > 1:
            new_key = prev_key + 1
            parag_2_ln[new_key] = parag_2_ln[prev_key] + int(diff_break * line_diff)   # a fake (evenly break) in line numbers
            print("New key added: ", new_key, "; new value: ", parag_2_ln[new_key])
            prev_key = new_key
            diff -= 1

    del parag_2_ln[0]
    print("check consecutive: after:\n", parag_2_ln.keys())
    return parag_2_ln


def longest_inc_sequence(nums: list[int]) -> list[int]:
    if not nums:
        return []
    n = len(nums)
    dp = [1] * n   # dp[i] represents the length of the LIS ending at index i
    prev = [-1] * n  # To reconstruct the sequence, store the previous index
    # Calculate LIS length for each element
    for i in range(1, n):
        for j in range(i):
            if nums[i] > nums[j] and dp[i] < dp[j] + 1:
                dp[i] = dp[j] + 1
                prev[i] = j

    # Find the index of the maximum LIS length
    max_index = dp.index(max(dp))
    # Reconstruct the sequence
    lis = []
    while max_index != -1:
        lis.append(nums[max_index])
        max_index = prev[max_index]

    return lis[::-1]  # Reverse to get the correct order


def launch_llm_call_process_j_id(prompt:str, id:str) -> subprocess.Popen:
    # launch subprocess llm_call.py
    os.environ['PYTHONPATH'] = SOURCE_CODE_ROOT + os.pathsep + os.environ.get('PYTHONPATH', '')

    process = subprocess.Popen([PYTHON_EXECUTABLE,
                             'backend/ai/llm_call_j_id.py',
                             prompt, id],
                             stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE,
                            text=True)
    return process


 # remove orphan headers with no paragraph after it
def remove_orphan_headers_at_end(merged_list:list[tuple]):
    while isinstance(merged_list[-1][0], str):   # the last item is ('header', ln_num)
        merged_list.pop()
    return merged_list


LINES_PER_CHUNK_DOCX = 60
LINES_PER_CHUNK_PDF = 40
ANSWER_ACTIVE_PARSE_TASK_KEY = 'answer/active_parse_task_id'
ANSWER_ACTIVE_CONTINUE_TASK_KEY = 'answer/active_continue_task_id'
ANSWER_GENERATED_RESET_KEYS = (
    'ads_suggestions',
    'outline',
    'analysis/outline',
    'paragraph_warnings',
    'analysis/general_denial',
    'drafted_sections/responses',
    'drafted_sections/affirmative_defenses',
    'drafted_sections/counter_claims',
    'drafted_sections/intro',
    'inputs/generated_pdf_filename',
    'inputs/chosen_defendants',
    'inputs/paragraph_selections',
    'inputs/ad',
    'inputs/plaintiff',
    'inputs/defendant',
    'inputs/venue',
    'inputs/is_federal',
    'inputs/state',
    'complaint_structure',
    'sections',
    'causes_of_action',
    'summary',
    'complaint_factual_digest',
    'section_headers',
    'paragraphs',
    'hallucination_check',
    'answer_hg/case_results',
    'answer_hg/statute_results',
    'answer_hg/lookup_diagnostics',
    'answer_hg/filtered_citations',
    'answer_hg/results_html',
    'answer_hg/chunk_metadata',
    ANSWER_ACTIVE_CONTINUE_TASK_KEY,
)


def _delete_memory_key(mem: BaseMemory, key: str) -> None:
    try:
        del mem[key]
    except Exception:
        pass


def clear_answer_active_task(mem: BaseMemory, key: str, expected_task_id: str | None = None) -> None:
    current_task_id = mem.get(key, '') or ''
    if expected_task_id and current_task_id and current_task_id != expected_task_id:
        return
    _delete_memory_key(mem, key)


def _current_task_id(task) -> str | None:
    return getattr(getattr(task, 'request', None), 'id', None)


def _answer_progress(task, message: str, progress: int, extra_meta: dict | None = None) -> None:
    if task is None:
        return
    meta = {'message': message, 'progress': progress}
    if extra_meta:
        meta.update(extra_meta)
    try:
        task.update_state(state='PROGRESS', meta=meta)
    except Exception as exc:
        print(f"Warning: failed to push Answer progress state: {exc}")


def get_active_answer_task_id(doc_id: int, key: str) -> str | None:
    mem = DBMemory(db=db, doc_id=doc_id)
    task_id = (mem.get(key, '') or '').strip()
    if not task_id:
        return None

    try:
        result = AsyncResult(task_id)
        if result.ready():
            clear_answer_active_task(mem, key)
            return None
    except Exception:
        return task_id

    return task_id


def save_parsed_answer_complaint(
    memory: BaseMemory,
    filename: str,
    complaint_text: str,
    case_number: str,
    line_metadata: list[dict] | None = None,
) -> list[str]:
    memory['inputs/case_number'] = case_number

    cover_text, text_by_lines, normalized_metadata = prepare_complaint_text_for_memory(
        filename,
        complaint_text,
        line_metadata,
        lines_per_chunk_pdf=LINES_PER_CHUNK_PDF,
        lines_per_chunk_docx=LINES_PER_CHUNK_DOCX,
    )
    print("total # of lines in complaint_text: ", len(text_by_lines))
    log_complaint_text_metadata_preview(filename, text_by_lines, normalized_metadata)
    memory['complaint_cover_text'] = cover_text
    memory['complaint_text'] = json.dumps(text_by_lines)
    memory['line_metadata'] = json.dumps(normalized_metadata)

    memory['inputs/uploaded_filename'] = filename
    return text_by_lines


def _load_saved_answer_complaint_lines(memory: BaseMemory) -> list[str]:
    raw_text = memory.get('complaint_text', '')
    if not raw_text:
        return []

    try:
        text_by_lines = json.loads(raw_text)
        if isinstance(text_by_lines, list):
            return text_by_lines
    except Exception:
        pass

    return str(raw_text).splitlines()


@_answer_drafter_task("parsing the complaint document", bind=True)
def answer_parse_complaint_task(self, filename: str, draftId: str) -> dict:
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        from backend.util.account_access import chat_account_id, require_enabled_account
        require_enabled_account(chat_account_id(doc_id))
        _answer_progress(self, 'Parsing complaint document...', 15)
        complaint_text, case_number, _is_image, line_metadata = get_complaint_text_with_line_metadata_from_file(os.path.join(STATIC_FOLDER, filename))
        ocr_warning = get_last_ocr_warning()
        if ocr_warning:
            _answer_progress(self, 'Some scanned pages could not be read. Your document is still processed.', 60, {
                'status': 'ocr_warning',
                'ocr_warning': True,
                'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
            })
        _answer_progress(self, 'Saving parsed complaint text...', 80)
        require_enabled_account(chat_account_id(doc_id))
        save_parsed_answer_complaint(memory, filename, complaint_text, case_number, line_metadata)
        _answer_progress(self, 'Complaint parsed successfully.', 100)
        return {
            'message': 'Complaint parsed successfully',
            'uploaded_filename': filename,
            'has_parsed_complaint': True,
            'ocr_warning': bool(ocr_warning),
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue." if ocr_warning else None,
            'file_name': f'{API_URL}static/{filename}',
        }
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error parsing complaint: {str(exc)}',
        }
    finally:
        clear_answer_active_task(memory, ANSWER_ACTIVE_PARSE_TASK_KEY, expected_task_id=task_id)


@guided_step_operation(DOC_TYPE, 0, lambda memory, draftId, task=None: draftId)
def generate_answer_analysis_from_memory(memory: BaseMemory, draftId: str, task=None) -> dict:
    doc_id = Drafts.resolve_id(draftId)

    text_by_lines = _load_saved_answer_complaint_lines(memory)
    complaint_cover_text = memory.get('complaint_cover_text', '')
    line_metadata = _load_saved_answer_line_metadata(memory)
    filename = memory.get('inputs/uploaded_filename', '')
    if not text_by_lines:
        return {"message": "error", "error_message": "Please upload and parse a complaint before continuing."}

    _answer_progress(task, 'Classifying complaint structure...', 10)
    complaint_structure = get_answer_complaint_structure_from_llm(text_by_lines, line_metadata)
    memory['complaint_structure'] = json.dumps(complaint_structure)
    setup_error = _answer_structure_setup_error(complaint_structure)
    if setup_error:
        return {"message": "error", "error_message": setup_error}

    plaintiff_l = complaint_structure.get("plaintiffs", [])
    defendant_l = complaint_structure.get("defendants", [])
    venue = complaint_structure.get("venue", "") or complaint_structure.get("court", "")
    is_federal = complaint_structure.get("jurisdiction_type") or "unknown"
    state = complaint_structure.get("state") or ""
    first_body_line_number = complaint_structure.get("first_body_line_number") or 0
    body_text_by_lines = text_by_lines[first_body_line_number:]
    if not body_text_by_lines:
        return {"message": "error", "error_message": "Something went wrong when identifying where the complaint body starts, please try again."}

    print("complaint_structure: ", complaint_structure)
    print("plaintiff: ", plaintiff_l)
    print("# plaintiff = ", len(plaintiff_l))
    print("defendant: ", defendant_l)
    print("# defendant = ", len(defendant_l))
    print("venue: ", venue)
    print("is_federal: ", is_federal)
    print("state: ", state)
    print("first_body_line_number: ", first_body_line_number)

    memory['inputs/plaintiff'] = ";".join(plaintiff_l)
    print("memory plaintiff saved as: ", memory['inputs/plaintiff'])
    memory['inputs/venue'] = venue
    memory['inputs/defendant'] = ";".join(defendant_l)
    memory['inputs/is_federal'] = is_federal
    memory['inputs/state'] = state
    if complaint_structure.get("case_number"):
        memory['inputs/case_number'] = complaint_structure["case_number"]

    _answer_progress(task, 'Extracting complaint sections and claims...', 18)
    section_result = get_answer_sections_and_coas_from_llm(
        text_by_lines,
        line_metadata,
        first_body_line_number,
    )
    final_sections = section_result.get("sections", [])
    final_causes_of_action = section_result.get("causes_of_action", [])
    hd_2_ln = _answer_section_headers_from_sections(final_sections)
    logger.info("ANSWER final_sections: %s", json.dumps(final_sections, ensure_ascii=False, indent=2))
    logger.info("ANSWER final_causes_of_action: %s", json.dumps(final_causes_of_action, ensure_ascii=False, indent=2))
    memory['sections'] = json.dumps(final_sections)
    memory['causes_of_action'] = json.dumps(final_causes_of_action)
    memory['section_headers'] = json.dumps(hd_2_ln)

    print("is_federal: ", is_federal)


    _answer_progress(task, 'Preparing answer outline...', 50)
    paragraph_warnings = _empty_answer_para_warnings()
    para_2_ln = {}

    if is_answer_general_denial_state(is_federal, state):
        outline = None
        general_denial_prompt = open('backend/ai/prompts/answer/GENERAL_DENIAL').read().format(venue=venue, num_def=len(defendant_l), num_pla=len(plaintiff_l))
        general_denial = chat_completion(
            general_denial_prompt,
            model=ANSWER_TEXT_MODEL,
            reasoning_effort=ANSWER_TEXT_EFFORT,
        )
        if not general_denial or not general_denial.strip():
            return {"message": "error", "error_message": "Could not generate the general denial. Please try again."}
        print("LLM  general_denial: ", general_denial)

        _answer_progress(task, 'Summarizing factual allegations...', 78)
        _answer_progress(task, 'Generating affirmative defense suggestions...', 90)
        ads_output_dict = ensure_answer_factual_digest_and_ads(
            memory,
            text_by_lines=text_by_lines,
            complaint_structure=complaint_structure,
            sections=final_sections,
            causes_of_action=final_causes_of_action,
            para_2_ln=para_2_ln,
            first_body_line_number=first_body_line_number,
        )
    else:  # federal cases need paragraphs, state cases need paragraphs if state is not california or texas
        general_denial = None
         # === this part extract section headers  k: header, v: line_num, empty dict is less than 5 headers found
        print("hd_2_ln:\n", hd_2_ln)
        memory['section_headers'] = json.dumps(hd_2_ln)

        # === this part extract paragraphs
        # here check how many headings returned, image-based pdf automatically use llm
        # if is_image or len(hd_2_ln) < 5:
        #     if is_image:
        #         print("image-based file: call llm for paragraphs.")
        #     if len(hd_2_ln) < 5:
        #         print("Too few headings returned by ai")
        para_2_ln, paragraph_warnings = get_paralines_from_llm(
            text_by_lines,
            primary_start_line=first_body_line_number,
            return_warnings=True,
        )
        # else:
        #     para_2_ln = parse_paragraphs(text_by_lines, hd_2_ln)

        print("para_2_ln:\n", para_2_ln)
        print("recognized paragraph count = ", len(para_2_ln))

        memory['paragraphs'] = json.dumps(para_2_ln)

        outline = build_answer_outline_from_locations(final_sections, para_2_ln, first_body_line_number)
        logger.info("ANSWER outline from sections and paragraphs: %s items", len(outline))
        memory['outline'] = json.dumps(outline)  # only federal cases ahve outline
        print("first page text: ", complaint_cover_text)

        ads_output_dict = {}
        memory['ads_suggestions'] = json.dumps(ads_output_dict)
        logger.info("ANSWER setup deferred factual digest and AD suggestions until state1.")

    memory['paragraph_warnings'] = json.dumps(paragraph_warnings)
    memory['analysis/general_denial'] = general_denial if general_denial else ""
    memory['analysis/outline'] = json.dumps(outline) if outline else "[]"

    print("state: ", memory['inputs/state'])
    print("complaint cover page text:", memory['complaint_cover_text'])
    print("venue: ", memory['inputs/venue'])
    print("is_federal: ", memory['inputs/is_federal'])

    _answer_progress(task, 'Answer setup ready.', 100)

    return {"message": "Form processed successfully",
            "defendant": defendant_l,
            "ads_suggestions": ads_output_dict,  # suggested ADs
            "outline": outline,
            "paragraph_warnings": paragraph_warnings,
            "general_denial": general_denial,
            'file_name': f'{API_URL}static/{filename}'}


@_answer_drafter_task("parsing and preparing the Answer setup", bind=True)
def state0(self, filename: str, draftId: str) -> dict:
    """Legacy combined step-0 task: parse complaint and run Answer setup."""
    doc_id = Drafts.resolve_id(draftId)
    memory = DBMemory(db=db, doc_id=doc_id)

    from backend.util.account_access import chat_account_id, require_enabled_account
    require_enabled_account(chat_account_id(doc_id))
    complaint_text, case_number, _is_image, line_metadata = get_complaint_text_with_line_metadata_from_file(os.path.join(STATIC_FOLDER, filename))
    ocr_warning = get_last_ocr_warning()
    if ocr_warning:
        _answer_progress(self, "Some scanned pages could not be read. Your document is still processed.", 15, {
            'status': 'ocr_warning',
            'ocr_warning': True,
            'ocr_warning_message': "Some scanned pages could not be read. Your document is still processed. Press OK to continue.",
        })
    require_enabled_account(chat_account_id(doc_id))
    save_parsed_answer_complaint(memory, filename, complaint_text, case_number, line_metadata)
    return generate_answer_analysis_from_memory(memory, draftId, task=self)


@_answer_drafter_task("preparing the Answer setup", bind=True)
def answer_continue_task(self, data: dict) -> dict:
    draftId = data['draftId']
    doc_id = Drafts.resolve_id(draftId)
    task_id = _current_task_id(self)
    memory = DBMemory(db=db, doc_id=doc_id)

    try:
        return generate_answer_analysis_from_memory(memory, draftId, task=self)
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        return {
            'message': 'error',
            'error_message': f'Error preparing Answer draft: {str(exc)}',
        }
    finally:
        clear_answer_active_task(memory, ANSWER_ACTIVE_CONTINUE_TASK_KEY, expected_task_id=task_id)


def find_statues(in_text: str) -> list:
    US_statues = []
    citations = find_USC_citation(in_text)
    if citations is not None:
        for citation in citations:
            US_statues.append(
                {'prefix': 'https://www.law.cornell.edu/uscode/text', 
                 'id': None, 
                 'displayName': f"{citation[0]} U.S.C. § {citation[1]}", 
                 'citationName': f"{citation[0]}/{citation[1]}",
                 'type':'us'
                 })
        return US_statues
    else:
        return []


def add_citations(in_list: list) -> list:
    """adds citations to the afirmative defenses"""
    ret_l = []
    for ad in in_list:
        ret_item = {'name': ad[0], 'descr': ad[1]}
        # look up citations
        full_citations = find_citations(ad[1])
        ad_citation_set = set()
        unique_full_citations = []
        for cit in full_citations:
            if cit["citation"] not in ad_citation_set:
                unique_full_citations.append(cit)
                ad_citation_set.add(cit["citation"])

        ret_item['citations'] = [unpack_cluster(cit) for cit in unique_full_citations]

        # add statues to citations
        statues = find_statues(ad[1])
        ret_item['citations'].extend(statues)

        state_codes = find_state_codes(ad[1])
        ret_item['citations'].extend(state_codes)

        ret_l.append(ret_item)
    return ret_l

def handle_state0(request):
    """State 0 is to simply upload a file, and generate an outline."""
    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        print(error_message)
        return {'message': error_message}, 401

    doc_id = Drafts.resolve_id(request.form['draftId'])
    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, ANSWER_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    # Call state0 as a Celery task to process the file
    result = state0.delay(filename=filename, draftId=request.form['draftId'])
    return {"result_id": result.id}, 200  # Return the result ID of the task


def handle_upload_complaint(request):
    """Upload and parse an Answer complaint without running setup analysis."""
    doc_id = Drafts.resolve_id(request.form['draftId'])
    active_parse_task_id = get_active_answer_task_id(doc_id, ANSWER_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Complaint parsing is already running.', 'result_id': active_parse_task_id}, 409

    active_continue_task_id = get_active_answer_task_id(doc_id, ANSWER_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'message': 'Answer setup is already running.', 'result_id': active_continue_task_id}, 409

    filename, error_message = handle_file_upload(request, STATIC_FOLDER)
    if error_message:
        print(error_message)
        return {'message': error_message}, 401

    memory = DBMemory(db=db, doc_id=doc_id)
    clear_memory_keys(memory, ANSWER_RESET_KEYS)
    memory['inputs/uploaded_filename'] = filename

    result = answer_parse_complaint_task.delay(
        filename=filename,
        draftId=request.form['draftId'],
    )
    memory[ANSWER_ACTIVE_PARSE_TASK_KEY] = result.id
    return {'result_id': result.id, 'uploaded_filename': filename}, 200


def handle_continue(request):
    """Run Answer setup analysis from previously parsed complaint text."""
    data = request.get_json(silent=True) or request.form
    draft_id = data.get('draftId')
    doc_id = Drafts.resolve_id(draft_id)
    memory = DBMemory(db=db, doc_id=doc_id)

    active_parse_task_id = get_active_answer_task_id(doc_id, ANSWER_ACTIVE_PARSE_TASK_KEY)
    if active_parse_task_id:
        return {'message': 'Complaint parsing is still running.', 'result_id': active_parse_task_id}, 400

    if not memory.get('complaint_text', '') or not memory.get('complaint_cover_text', ''):
        return {'message': 'Please upload and parse a complaint before continuing.'}, 400

    active_continue_task_id = get_active_answer_task_id(doc_id, ANSWER_ACTIVE_CONTINUE_TASK_KEY)
    if active_continue_task_id:
        return {'result_id': active_continue_task_id, 'already_running': True}, 200

    clear_memory_keys(memory, ANSWER_GENERATED_RESET_KEYS)
    result = answer_continue_task.delay({'draftId': draft_id})
    memory[ANSWER_ACTIVE_CONTINUE_TASK_KEY] = result.id
    return {'result_id': result.id}, 200


handle_answer_upload_complaint = handle_upload_complaint
handle_answer_continue = handle_continue


def paragraph_dict2str(ind: dict) -> str:
    """Writes the dict as a string to be sent to the LLM."""
    # handle custom responses
    dict_edits = []
    for key, v in ind.items():
        if key.startswith('customResponse-'):
            ka = key.split('-')
            dict_edits.append((ka[1], f"custom: {v}"))

    for edit in dict_edits:
        ind[edit[0]] = edit[1]

    # print(f'ind: {ind}')

    srted = sorted(ind.items())
    retstr = ""
    for kpr in srted:
        if kpr[0].startswith('para'):
            intval = int(kpr[0][4:])
            retstr += f'Paragraph{intval}: {kpr[1]}\n'

    return retstr


def _answer_response_paragraph_number_from_key(key: str) -> int | None:
    key = str(key)
    if not re.fullmatch(r"para\d+", key):
        return None
    try:
        return int(key[4:])
    except Exception:
        return None


def _answer_outline_paragraph_entries(outline: list | None) -> list[tuple[int, int]]:
    entries = []
    for outline_index, item in enumerate(outline or []):
        try:
            label, paragraph_number = item
        except Exception:
            continue
        if paragraph_number is None:
            continue
        try:
            paragraph_number_int = int(paragraph_number)
        except Exception:
            match = re.search(r"\bParagraph\s+(\d+)\b", str(label or ""), flags=re.IGNORECASE)
            if not match:
                continue
            paragraph_number_int = int(match.group(1))
        entries.append((outline_index, paragraph_number_int))
    return entries


def _answer_response_selection_row(
    para_num: int,
    choice: str,
    custom_text: str = "",
    outline_index: int | None = None,
) -> dict:
    choice = str(choice or "deny").strip() or "deny"
    custom_text = str(custom_text or "").strip()
    row = {
        "field": f"para{para_num:04d}",
        "choice": choice,
        "custom_text": custom_text,
        "prompt_value": f"custom: {custom_text}" if choice == "custom" else choice,
    }
    if outline_index is not None:
        row["outline_index"] = outline_index
    return row


def _answer_response_json_paragraph_selections(data: dict, outline: list | None = None) -> dict[int, dict]:
    outline_entries = _answer_outline_paragraph_entries(outline)
    outline_index_by_paragraph = {paragraph_number: outline_index for outline_index, paragraph_number in outline_entries}
    default_choice = str(data.get("default_choice", "deny") or "deny").strip() or "deny"
    selections = {
        paragraph_number: _answer_response_selection_row(
            paragraph_number,
            default_choice,
            outline_index=outline_index,
        )
        for outline_index, paragraph_number in outline_entries
    }

    allowed_choices = {"deny", "admit", "notEnoughInfo", "custom"}
    for submitted in data.get("paragraph_selections", []) or []:
        if not isinstance(submitted, dict):
            logger.warning("ANSWER_RESPONSE ignored non-object paragraph selection: %s", submitted)
            continue
        try:
            para_num = int(submitted.get("paragraph_number"))
        except Exception:
            logger.warning("ANSWER_RESPONSE ignored paragraph selection with invalid paragraph_number: %s", submitted)
            continue
        if outline_index_by_paragraph and para_num not in outline_index_by_paragraph:
            logger.warning("ANSWER_RESPONSE ignored paragraph selection outside saved outline: %s", submitted)
            continue

        choice = str(submitted.get("choice", default_choice) or default_choice).strip() or default_choice
        if choice not in allowed_choices:
            logger.warning("ANSWER_RESPONSE ignored paragraph selection with invalid choice: %s", submitted)
            continue

        expected_outline_index = outline_index_by_paragraph.get(para_num)
        submitted_outline_index = submitted.get("outline_index")
        if expected_outline_index is not None and submitted_outline_index is not None:
            try:
                if int(submitted_outline_index) != expected_outline_index:
                    logger.warning(
                        "ANSWER_RESPONSE paragraph %s submitted outline_index=%s but saved outline_index=%s",
                        para_num,
                        submitted_outline_index,
                        expected_outline_index,
                    )
            except Exception:
                logger.warning("ANSWER_RESPONSE paragraph %s submitted invalid outline_index=%s", para_num, submitted_outline_index)

        selections[para_num] = _answer_response_selection_row(
            para_num,
            choice,
            custom_text=submitted.get("custom_response", ""),
            outline_index=expected_outline_index,
        )

    return dict(sorted(selections.items(), key=lambda item: item[1].get("outline_index", item[0])))


def _answer_response_paragraph_selections(data: dict, outline: list | None = None) -> dict[int, dict]:
    if isinstance(data, dict) and isinstance(data.get("paragraph_selections"), list):
        return _answer_response_json_paragraph_selections(data, outline)

    selections = {}
    for key, value in data.items():
        para_num = _answer_response_paragraph_number_from_key(key)
        if para_num is None:
            continue
        choice = str(value).strip()
        custom_text = str(data.get(f"customResponse-{key}", "")).strip()
        selections[para_num] = _answer_response_selection_row(para_num, choice, custom_text)
    return dict(sorted(selections.items()))


def _answer_response_selection_text(selections: dict[int, dict], paragraph_numbers: list[int] | None = None) -> str:
    if paragraph_numbers is None:
        paragraph_numbers = sorted(selections, key=lambda para_num: selections[para_num].get("outline_index", para_num))
    return "".join(
        f"Paragraph{para_num}: {selections[para_num]['prompt_value']}\n"
        for para_num in paragraph_numbers
        if para_num in selections
    )


def _build_answer_response_prompt(
    paragraph_numbers: list[int],
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
) -> str:
    return prompt_template.format(
        paragraphs=paragraph_content2str(paragraph_numbers, para_2_ln, text_by_lines),
        para_user_responses=_answer_response_selection_text(selections, paragraph_numbers),
        expected_paragraph_numbers=", ".join(str(n) for n in paragraph_numbers),
        expected_answer_count=len(paragraph_numbers),
    )


def _build_answer_response_chunks_for_paragraphs(
    paragraph_numbers: list[int],
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
    max_prompt_tokens: int,
) -> list[dict]:
    if not paragraph_numbers:
        return []

    prompt = _build_answer_response_prompt(
        paragraph_numbers,
        selections,
        para_2_ln,
        text_by_lines,
        prompt_template,
    )
    prompt_tokens = num_tokens_from_string(prompt)
    if prompt_tokens <= max_prompt_tokens or len(paragraph_numbers) == 1:
        if prompt_tokens > max_prompt_tokens:
            logger.warning(
                "ANSWER_RESPONSE prompt exceeds token cap with one paragraph: paragraph=%s tokens=%s cap=%s",
                paragraph_numbers[0],
                prompt_tokens,
                max_prompt_tokens,
            )
        return [{
            "paragraph_numbers": paragraph_numbers,
            "prompt": prompt,
            "prompt_tokens": prompt_tokens,
        }]

    midpoint = max(1, len(paragraph_numbers) // 2)
    return (
        _build_answer_response_chunks_for_paragraphs(
            paragraph_numbers[:midpoint],
            selections,
            para_2_ln,
            text_by_lines,
            prompt_template,
            max_prompt_tokens,
        )
        + _build_answer_response_chunks_for_paragraphs(
            paragraph_numbers[midpoint:],
            selections,
            para_2_ln,
            text_by_lines,
            prompt_template,
            max_prompt_tokens,
        )
    )


def _build_answer_response_chunks(
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
    paras_per_call: int | None = None,
    max_prompt_tokens: int | None = None,
) -> list[dict]:
    paras_per_call = ANSWER_RESPONSE_PARAS_PER_CALL if paras_per_call is None else paras_per_call
    max_prompt_tokens = ANSWER_RESPONSE_MAX_PROMPT_TOKENS if max_prompt_tokens is None else max_prompt_tokens
    if paras_per_call < 1:
        raise ValueError("paras_per_call must be positive.")

    chunks = []
    paragraph_numbers = sorted(selections, key=lambda para_num: selections[para_num].get("outline_index", para_num))
    for index in range(0, len(paragraph_numbers), paras_per_call):
        group = paragraph_numbers[index:index + paras_per_call]
        chunks.extend(
            _build_answer_response_chunks_for_paragraphs(
                group,
                selections,
                para_2_ln,
                text_by_lines,
                prompt_template,
                max_prompt_tokens,
            )
        )

    chunk_count = len(chunks)
    for index, chunk in enumerate(chunks):
        chunk["chunk_id"] = index + 1
        chunk["chunk_count"] = chunk_count
    return chunks


def _parse_answer_response_rows(llm_output, expected_paragraph_numbers: set[int]) -> tuple[dict[int, str], bool]:
    try:
        answers_l = extract_from_json("Answers", llm_output)
    except Exception as exc:
        logger.warning("DRAFT_RESPONSES output parse failed: %s", exc)
        return {}, True

    if not answers_l:
        return {}, True

    rows = {}
    rejected_count = 0
    for each in answers_l:
        try:
            para_num = int(str(each.get("paragraph", "")).strip())
            para_resp = str(each.get("response", "")).strip()
        except Exception:
            rejected_count += 1
            continue
        if para_num not in expected_paragraph_numbers or not para_resp:
            rejected_count += 1
            continue
        rows[para_num] = para_resp
    if rejected_count:
        logger.warning("DRAFT_RESPONSES rejected %s invalid or unexpected answer rows.", rejected_count)
    return rows, not bool(rows)


def _split_answer_response_retry_chunks(
    chunk: dict,
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
) -> list[dict]:
    paragraph_numbers = chunk["paragraph_numbers"]
    if len(paragraph_numbers) <= 1:
        return []
    retry_size = max(1, len(paragraph_numbers) // 2)
    retry_chunks = []
    for index in range(0, len(paragraph_numbers), retry_size):
        retry_paragraph_numbers = paragraph_numbers[index:index + retry_size]
        prompt = _build_answer_response_prompt(
            retry_paragraph_numbers,
            selections,
            para_2_ln,
            text_by_lines,
            prompt_template,
        )
        retry_chunks.append({
            "chunk_id": f"{chunk['chunk_id']}.{len(retry_chunks) + 1}",
            "chunk_count": chunk["chunk_count"],
            "paragraph_numbers": retry_paragraph_numbers,
            "prompt": prompt,
            "prompt_tokens": num_tokens_from_string(prompt),
        })
    return retry_chunks


def _extract_answer_response_chunk(
    chunk: dict,
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
    allow_retry: bool = True,
) -> dict:
    logger.info(
        "DRAFT_RESPONSES chunk %s of %s paragraphs=%s-%s count=%s prompt_tokens=%s",
        chunk["chunk_id"],
        chunk["chunk_count"],
        chunk["paragraph_numbers"][0],
        chunk["paragraph_numbers"][-1],
        len(chunk["paragraph_numbers"]),
        chunk.get("prompt_tokens"),
    )
    try:
        parsed_output = chat_completion_parse(
            chunk["prompt"], AnswerParagraphResponses,
            model=ANSWER_RESPONSE_MODEL, reasoning_effort=ANSWER_RESPONSE_EFFORT,
        )
        llm_output = parsed_output.model_dump() if parsed_output is not None else None
        rows, parse_failed = _parse_answer_response_rows(llm_output, set(chunk["paragraph_numbers"]))
    except SoftTimeLimitExceeded:
        raise
    except Exception as exc:
        logger.warning("DRAFT_RESPONSES chunk %s failed: %s", chunk["chunk_id"], exc)
        rows = {}
        parse_failed = True

    missing_paragraph_numbers = sorted(set(chunk["paragraph_numbers"]) - set(rows))
    if allow_retry and (parse_failed or missing_paragraph_numbers) and len(chunk["paragraph_numbers"]) > 1:
        logger.warning(
            "Retrying DRAFT_RESPONSES chunk %s in half-size paragraph chunks. parse_failed=%s missing_count=%s",
            chunk["chunk_id"],
            parse_failed,
            len(missing_paragraph_numbers),
        )
        retry_rows = {}
        retry_failed = False
        for retry_chunk in _split_answer_response_retry_chunks(
            chunk,
            selections,
            para_2_ln,
            text_by_lines,
            prompt_template,
        ):
            retry_result = _extract_answer_response_chunk(
                retry_chunk,
                selections,
                para_2_ln,
                text_by_lines,
                prompt_template,
                allow_retry=False,
            )
            retry_rows.update(retry_result["rows"])
            retry_failed = retry_failed or retry_result["had_error"]
        retry_missing = sorted(set(chunk["paragraph_numbers"]) - set(retry_rows))
        return {
            "chunk": chunk,
            "rows": retry_rows,
            "missing": retry_missing,
            "had_error": retry_failed or bool(retry_missing),
            "retried": True,
        }

    return {
        "chunk": chunk,
        "rows": rows,
        "missing": missing_paragraph_numbers,
        "had_error": parse_failed or bool(missing_paragraph_numbers),
        "retried": False,
    }


def _run_answer_response_chunks(
    chunks: list[dict],
    selections: dict[int, dict],
    para_2_ln: dict,
    text_by_lines: list[str],
    prompt_template: str,
) -> tuple[dict[int, str], bool]:
    if not chunks:
        return {}, True

    workers = min(ANSWER_RESPONSE_WORKERS, len(chunks))
    logger.info("Running DRAFT_RESPONSES over %s chunks with max_workers=%s.", len(chunks), workers)
    all_para_responses = {}
    had_error = False
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        future_map = {
            submit_with_context(
                pool,
                _extract_answer_response_chunk,
                chunk,
                selections,
                para_2_ln,
                text_by_lines,
                prompt_template,
            ): chunk
            for chunk in chunks
        }
        sleep(1)
        print(f"-- DRAFT_RESPONSES LLM -- actually created {len(pool._threads)} threads")
        for fut in concurrent.futures.as_completed(future_map):
            chunk = future_map[fut]
            try:
                result = fut.result()
            except SoftTimeLimitExceeded:
                raise
            except Exception as exc:
                logger.warning("DRAFT_RESPONSES chunk %s raised after retry handling: %s", chunk["chunk_id"], exc)
                had_error = True
                continue
            all_para_responses.update(result["rows"])
            had_error = had_error or result["had_error"]

    expected_paragraph_numbers = {
        para_num
        for chunk in chunks
        for para_num in chunk["paragraph_numbers"]
    }
    missing_paragraph_numbers = sorted(expected_paragraph_numbers - set(all_para_responses))
    if missing_paragraph_numbers:
        logger.warning("DRAFT_RESPONSES missing paragraph responses after retries: %s", missing_paragraph_numbers[:50])
        had_error = True
    return all_para_responses, had_error


def _answer_selected_defendants_from_data(data: dict) -> list[str]:
    defendants = data.get("defendants") if isinstance(data, dict) else None
    if isinstance(defendants, list):
        return [str(defendant).strip() for defendant in defendants if str(defendant).strip()]

    return [
        str(value).strip()
        for key, value in data.items()
        if str(key).startswith("defendant") and str(value).strip()
    ]


def _answer_state1_prerequisite_error(mem: BaseMemory, data: dict) -> dict | None:
    """Reject stale Step 1 submissions before they can modify another draft."""
    complaint_text = mem.get('complaint_text', '')
    if not complaint_text:
        return {
            "message": "error",
            "error_message": "This Answer draft has not been prepared. Return to Upload Complaint and click Continue before drafting responses.",
        }

    if data.get('generalDenial'):
        prepared_general_denial = str(mem.get('analysis/general_denial', '') or '').strip()
        if prepared_general_denial:
            return None
        return {
            "message": "error",
            "error_message": "This Answer draft is missing its prepared general denial. Return to Upload Complaint and click Continue before drafting responses.",
        }

    paragraphs = _load_answer_json_value(mem, 'paragraphs', {})
    outline = _load_answer_json_value(mem, 'outline', [])
    if not isinstance(paragraphs, dict) or not paragraphs or not isinstance(outline, list) or not outline:
        return {
            "message": "error",
            "error_message": "This Answer draft is missing its prepared paragraph outline. Return to Upload Complaint and click Continue before drafting responses.",
        }

    return None


@_answer_drafter_task("drafting Answer responses")
@guided_step_operation(DOC_TYPE, 1, lambda data: data['draftId'])
def state1(data: dict) -> dict:
    doc_id=Drafts.resolve_id(data['draftId'])
    print("doc id: ", doc_id)
    mem = DBMemory(db=db, doc_id=doc_id)

    prerequisite_error = _answer_state1_prerequisite_error(mem, data)
    if prerequisite_error:
        return prerequisite_error

    # for k, v in data.items():
    #     print("datakey = ", k, "; datavalue = ", v)
    # Save selected defendant into database, overwrite the original
    chosen_defendants = _answer_selected_defendants_from_data(data)
    if not chosen_defendants:
        # If no defendants selected, use the original list as fallback
        print("Warning: No defendants selected, using original defendant list")
        chosen_defendants_str = mem.get('inputs/defendant', "specify defendant")
    else:
        chosen_defendants_str = ";".join(chosen_defendants)
        # Overwrite inputs/defendant with chosen defendants so all subsequent operations use the selected ones
        mem['inputs/defendant'] = chosen_defendants_str
    # Always save chosen_defendants so state3 can reliably read it
    mem['inputs/chosen_defendants'] = chosen_defendants_str
    defendants_l = chosen_defendants_str
    n_defendants = len(chosen_defendants) if chosen_defendants else len(defendants_l.split(';')) if defendants_l else 0
    state = mem.get('inputs/state', '')
    is_federal = mem.get('inputs/is_federal', '')
    ads_output_dict = _load_answer_json_value(mem, 'ads_suggestions', {})

    # see if a general denial has been submitted.  
    if "generalDenial" in data and data['generalDenial']:
        if n_defendants > 1 and len(chosen_defendants) == 1:
            mem['drafted_sections/responses'] = data['generalDenial'].replace("Defendants", "Defendant").replace("defendants", "defendant")
        else:
            mem['drafted_sections/responses'] = data['generalDenial']

        if is_answer_general_denial_state(is_federal, state):
            ads_output_dict = ensure_answer_factual_digest_and_ads(
                mem,
                para_2_ln={},
                reuse_existing=True,
            )
    else:
        # draft answers to every paragraph, add to responses section
        # the paragraph answers come as k/v pairs like: 'para11': 'deny'
        # format paragraph dict to text string, and save
        # custom paragraphs come over as: customResponse-para0008
        outline = json.loads(mem.get('outline', '[]'))
        normalized_paragraph_selections = _answer_response_paragraph_selections(data, outline=outline)
        paragraph_selections = _answer_response_selection_text(normalized_paragraph_selections)
        mem['inputs/paragraph_selections'] = paragraph_selections

        # retrieve what we generated at state0
        para_2_ln = json.loads(mem["paragraphs"])
        text_by_lines = json.loads(mem['complaint_text'])
        print("Retrieved text_by_lines: ", len(text_by_lines))

        para_to_text = {}
        para_2_ln_list = list(para_2_ln.items())
        max_line_num = len(text_by_lines) - 1
        for i, (pnum, ln) in enumerate(para_2_ln_list):
            end_ln = para_2_ln_list[i+1][1] if i < len(para_2_ln_list) - 1 else max_line_num + 1
            ln = max(0, ln)
            end_ln = min(end_ln, max_line_num + 1)
            if ln <= max_line_num:
                para_to_text[int(pnum)] = " ".join(text_by_lines[ln:end_ln]).strip()

        comand_prompt = open('backend/ai/prompts/answer/DRAFT_RESPONSES').read()
        chunks = _build_answer_response_chunks(
            normalized_paragraph_selections,
            para_2_ln,
            text_by_lines,
            comand_prompt,
        )
        logger.info(
            "DRAFT_RESPONSES chunked setup: paragraphs=%s chunks=%s paras_per_call=%s workers=%s max_prompt_tokens=%s",
            len(normalized_paragraph_selections),
            len(chunks),
            ANSWER_RESPONSE_PARAS_PER_CALL,
            ANSWER_RESPONSE_WORKERS,
            ANSWER_RESPONSE_MAX_PROMPT_TOKENS,
        )
        all_para_responses, had_empty_extract = _run_answer_response_chunks(
            chunks,
            normalized_paragraph_selections,
            para_2_ln,
            text_by_lines,
            comand_prompt,
        )

        print("== all_para_responses keys ==\n", list(all_para_responses.keys()))

         
        # all_para_responses = {}   # for testing error message
        if len(all_para_responses) == 0 or had_empty_extract:
            return {"message": "error", "error_message": "Something went wrong when generating responses, please try again."}

        # We need to use the outline, (paragraphs, headers intervened in order) to form "ANSWER TO SECTION HEADER A" in proper location
        all_responses_str = ""

        headers = []
        for (para_hd_str, para_num) in outline:
            if para_num:  # this is a paragraph num, look for response
                value = all_para_responses.get(para_num, None)
                if value:
                    if 'delaware' in state.lower():
                        original = para_to_text.get(para_num, "")
                        all_responses_str += f"{original}\nResponse:\n{value}\n"
                    else:
                        all_responses_str += value + '\n'
            else:  # null means it is a section header
                all_responses_str += f"\nANSWER TO {para_hd_str.upper()}\n"    # upper() makes sure this line writes as a header
                headers.append(para_hd_str)
        prayer_titles_to_check = ["prayer for relief", "relief requested", "demand for relief", "request for relief"]
        if headers and not any(title in headers[-1].lower() for title in prayer_titles_to_check):
            # if the last section header is not "prayer for relief" or "request for relief", etc. add a general denial for that section
            all_responses_str += '''ANSWER TO PRAYER FOR RELIEF\nDefendant denies that Plaintiff is entitled to any relief sought in the Complaint. On that basis, Defendant denies each and every allegation set forth in Plaintiff's Prayer for Relief.\n'''

        mem['drafted_sections/responses'] = all_responses_str
        ads_output_dict = ensure_answer_factual_digest_and_ads(
            mem,
            text_by_lines=text_by_lines,
            para_2_ln=para_2_ln,
        )

    # finally, write the full PDF and return
    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    # draft_full_answer(mem, f"backend/static/{filename}")
    gen_pdf_status = create_answer_pdf(fn=f"backend/static/{filename}",
                                    plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                    defendant=mem.get('inputs/defendant', "specify defendant"),
                                    attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                    venue=mem.get('inputs/venue', "Venue not yet specified"),
                                    introduction='',
                                    responses=mem.get('drafted_sections/responses', ''),
                                    affirmative_defenses='',
                                    counter_claims='',
                                    )
    print("state: ", state)
    print("complaint cover page text:", mem.get('complaint_cover_text', ''))
    print("venue: ", mem.get('inputs/venue', ""))
    return {"message": "Form processed successfully",
            "ads_suggestions": ads_output_dict,
            'file_name': f'{API_URL}static/{filename}'}


def _answer_request_data(request) -> dict:
    json_data = request.get_json(silent=True) if getattr(request, "is_json", False) else None
    if isinstance(json_data, dict):
        return json_data
    return request.form


def _answer_task_data(data) -> dict:
    if isinstance(data, dict):
        return data
    return dict(data)


def handle_state1(request, data=None):
    result = state1.delay(_answer_task_data(data if data is not None else _answer_request_data(request)))
    return {"result_id": result.id}, 200


@_answer_drafter_task("drafting affirmative defenses")
@guided_step_operation(DOC_TYPE, 2, lambda data: data['draftId'])
def state2(data: dict) -> dict:
    """Takes: 
    -complaint summary
    -chosen ADs
    and writes:
    drafted_sections/affirmative_defenses"""
    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    # get ADs
    chosen_values = selected_answers2text('ad', data)
    # Older clients submit "New Affirmative Defense N" placeholders without a real title.
    selected_ads = [
        title for raw_title in chosen_values.splitlines()
        if (title := raw_title.strip())
        and not re.fullmatch(r"New Affirmative Defense \d+", title, flags=re.IGNORECASE)
    ]
    chosen_values = "\n".join(selected_ads)
    mem['inputs/ad'] = chosen_values
    print("ADs:", chosen_values)

    # load generated/suggested ADs for reasons
    ads_output_dict = json.loads(mem["ads_suggestions"])
    with open("backend/ai/prompts/answer/ORDINAL_NUM", "r") as file:
        ordinal = json.load(file)
    print("ordinal 1 is ", ordinal[str(1)])

    draft_ad_prompt = open("backend/ai/prompts/answer/DRAFT_AD").read()
    # construct selected ad and description and citations string for llm, batched 5 ads in a group
    ad_des_cit_str = ""
    total_ads = len(selected_ads)
    for each_ad in selected_ads:
        print("each_ad in chosen_values: ", each_ad)
        if each_ad in ads_output_dict.keys():
            ad_des_cit_str += f"\nLawyer's selected defense: {each_ad}\nJustification and Reasoning:\n"
            for coa_id, ad_data in ads_output_dict[each_ad].items():  # min of coa_id should be 1
                ad_des_cit_str += f"Count {coa_id}: {ad_data.get('description', 'No description')}\n"
                # if 'filtered_citation' in ad_data and ad_data['filtered_citation']:
                #     ad_des_cit_str += "Supporting Citations:\n"
                #     for citation in ad_data['filtered_citation']:
                #         ad_des_cit_str += f"- {citation}\n"
        else:
            ad_des_cit_str += (
                f"\nLawyer's selected defense: {each_ad}\n"
                "Justification and Reasoning:\n"
                "No case-specific justification was supplied for this lawyer-added defense. "
                "Do not invent supporting case facts.\n"
            )
        
    total_num_ads_str = f"Total {total_ads} Affirmative Defenses selected by lawyer, So only draft {total_ads} Affirmative Defenses in your output, that is only {total_ads} items in your defenses list."
    ad_des_cit_str += "\n" + total_num_ads_str

    filled_in_prompt = draft_ad_prompt.format(ads = ad_des_cit_str, total_num_ads_str = total_num_ads_str)
    print("==  DRAFT_AD  filled_in_prompt==\n", num_tokens_from_string(filled_in_prompt), filled_in_prompt)  # musk v. openai  37 ads, 16682 tokens
    if not selected_ads:
        mem['drafted_sections/affirmative_defenses'] = ''
    else:
        parsed_output = chat_completion_parse(
            filled_in_prompt, AnswerDraftedDefenses,
            model=ANSWER_DEFENSE_MODEL, reasoning_effort=ANSWER_DEFENSE_EFFORT,
        )
        if parsed_output is None or not parsed_output.defenses:
            return {"message": "error", "error_message": "Could not draft the selected affirmative defenses. Please try again."}
        defenses_list = [defense.model_dump() for defense in parsed_output.defenses]
        print("len of llm drafted ads: ", len(defenses_list))

        drafted_ad_str = '''AFFIRMATIVE DEFENSES
    Defendant asserts the following affirmative defenses to the Complaint. Defendant reserves the right to assert additional affirmative defenses or to amend this Answer in accordance with the Rules of Civil Procedure.    
    '''
        for i, each_ad in enumerate(defenses_list):
            # Safely handle ordinal lookup beyond available entries
            ord_label = ordinal.get(str(i + 1), f"{i + 1}th")
            drafted_ad_str += f"\n{ord_label.upper()} AFFIRMATIVE DEFENSE\n"
            drafted_ad_str += f"({each_ad.get('title', 'Unknown')})\n"
            drafted_ad_str += each_ad.get('paragraphs', '') + '\n'

        # Save whole AD section as string
        mem['drafted_sections/affirmative_defenses'] = drafted_ad_str

    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    # draft_full_answer(mem, f"backend/static/{filename}")
    gen_pdf_status = create_answer_pdf(fn=f"backend/static/{filename}",
                                    plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                    defendant=mem.get('inputs/defendant', "specify defendant"),
                                    attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                    venue=mem.get('inputs/venue', "Venue not yet specified"),
                                    introduction='',
                                    responses=mem.get('drafted_sections/responses', ''),
                                    affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
                                    counter_claims='',
                                    )
    print("state: ", mem['inputs/state'])
    print("complaint cover page text:", mem.get('complaint_cover_text', ''))
    print("venue: ", mem.get('inputs/venue', ""))
    return {"message": "Form processed successfully",
            'file_name': f'{API_URL}static/{filename}'}


def handle_state2(request):
    # Logic for handling state 2
    result = state2.delay(request.form)
    return {"result_id": result.id}, 200


@_answer_drafter_task("drafting counterclaims and introduction")
@guided_step_operation(DOC_TYPE, 3, lambda data: data['draftId'])
def state3(data: dict) -> dict:
    """Takes: 
    -complaint summary
    -additional facts (ccFacts)
    and writes:

    drafted_sections/affirmative_defenses
    
    An intro is also drafted here.
    """
    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)

    default_claim = '''
WHEREFORE, Defendant prays as follows:
1. That Plaintiff's Complaint be dismissed in its entirety, with prejudice;
2. That Plaintiff take nothing by his Complaint;
3. For an award of Defendant's reasonable attorneys' fees and costs;
4. For an award of Defendant's costs of suit incurred herein; and,
5. For such other and further relief as the Court deems just and proper.
'''

    # see if the user wants a counter claim, if not ignore this part
    wants_cc = 'wants_counterclaim' in data and data['wants_counterclaim'] == 'on'
    if wants_cc:
        cc_facts = data.get('ccFacts', 'None given')

        comand_prompt = open('backend/ai/prompts/answer/DRAFT_CC').read()
        filled_in_prompt = comand_prompt + cc_facts + f"\n\ncomplaint summary: \n\n{mem['summary']}"
        counter_claims = chat_completion(
            filled_in_prompt,
            model=ANSWER_TEXT_MODEL,
            reasoning_effort=ANSWER_TEXT_EFFORT,
        )
        if not counter_claims or not counter_claims.strip():
            return {"message": "error", "error_message": "Could not draft the counterclaim. Please try again."}
        mem['drafted_sections/counter_claims'] = counter_claims.replace('**', '')
    else:
        mem['drafted_sections/counter_claims'] = default_claim

    # write intro
     # add a short standard intro at the front of intro main body
    court = _clean_answer_structure_string(mem.get('inputs/is_federal', '')).lower()
    chosen_defendants = mem.get('inputs/chosen_defendants', '') or mem.get('inputs/defendant', '')
    n_defendants = len(chosen_defendants.split(';')) if chosen_defendants else 1
    plaintiff_names = mem.get('inputs/plaintiff', "Plaintiff")
    n_plaintiffs = len(plaintiff_names.split(';')) if plaintiff_names else 1
    start_intro = ""

    if court == "federal":
        if n_defendants == 1 and n_plaintiffs == 1:
            start_intro = f'''Pursuant to Rule 8(b) of the Federal Rules of Civil Procedure, Defendant {chosen_defendants.upper()} ("Defendant") answers the Complaint of Plaintiff {plaintiff_names.upper()} ("Plaintiff").'''
        elif n_defendants == 1 and n_plaintiffs > 1:
            start_intro = f'''Pursuant to Rule 8(b) of the Federal Rules of Civil Procedure, Defendant {chosen_defendants.upper()} ("Defendant") answers the Complaint of Plaintiffs {plaintiff_names.upper()} ("Plaintiffs").'''
        elif n_defendants > 1 and n_plaintiffs == 1:
            start_intro = f'''Pursuant to Rule 8(b) of the Federal Rules of Civil Procedure, Defendants {chosen_defendants.upper()} (“Defendants”) answer the Complaint of Plaintiff {plaintiff_names.upper()} (“Plaintiff”).'''
        elif n_defendants > 1 and n_plaintiffs > 1:
            start_intro = f'''Pursuant to Rule 8(b) of the Federal Rules of Civil Procedure, Defendants {chosen_defendants.upper()} (“Defendants”) answer the Complaint of Plaintiffs {plaintiff_names.upper()} (“Plaintiffs”).'''

        start_intro += f''' If an averment is not specifically admitted, it is hereby denied.
        '''

    elif court == "state":
        if n_defendants == 1 and n_plaintiffs == 1:
            start_intro = f'''Defendant, {chosen_defendants.upper()} ("Defendant"), hereby answers the Complaint (“Complaint”) filed by Plaintiff, {plaintiff_names.upper()} ("Plaintiff"), as follows:
            '''
        elif n_defendants == 1 and n_plaintiffs > 1:
            start_intro = f'''Defendant, {chosen_defendants.upper()} ("Defendant"), hereby answers the Complaint (“Complaint”) filed by Plaintiffs, {plaintiff_names.upper()} ("Plaintiffs"), as follows:
            '''
        elif n_defendants > 1 and n_plaintiffs == 1:
            start_intro = f'''Defendants, {chosen_defendants.upper()} ("Defendants"), hereby answer the Complaint (“Complaint”) filed by Plaintiff, {plaintiff_names.upper()} ("Plaintiff"), as follows:
            '''
        elif n_defendants > 1 and n_plaintiffs > 1:
            start_intro = f'''Defendants, {chosen_defendants.upper()} ("Defendants"), hereby answer the Complaint (“Complaint”) filed by Plaintiffs, {plaintiff_names.upper()} ("Plaintiffs"), as follows:
            '''
    else:
        logger.warning("ANSWER state3 could not identify jurisdiction type for intro: %s", mem.get('inputs/is_federal', ''))
        start_intro = f'''Defendant {chosen_defendants.upper()} answers the Complaint filed by Plaintiff {plaintiff_names.upper()}.
        '''

    # comand_prompt = open('backend/ai/prompts/answer/DRAFT_INTRO').read()
    # filled_in_prompt = comand_prompt + mem['summary']+ \
    #     f"\n\nAffirmative Defenses: \n{mem['inputs/ad']}"
   
    # if wants_cc:
    #     filled_in_prompt = filled_in_prompt + f"\n\nCounter Claims: \n{counter_claims}"
    
    # filled_in_prompt += '''\nOutput in the following JSON format: {"Introduction":"..."}
    # '''
    # llm_output =  chat_completion_j(filled_in_prompt)
    # intro_llm = ""
    # try:
    #     intro_llm = json.loads(llm_output)["Introduction"]
    # except:
    #     print('error decoding LLM output, trying 2nd method ')

    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    # draft_full_answer(mem, f"backend/static/{filename}")
    gen_pdf_status = create_answer_pdf(fn=f"backend/static/{filename}",
                                    plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                    defendant=mem.get('inputs/defendant', "specify defendant"),
                                    attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                    venue=mem.get('inputs/venue', "Venue not yet specified"),
                                    introduction='',
                                    responses=mem.get('drafted_sections/responses', ''),
                                    affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
                                    counter_claims=mem.get('drafted_sections/counter_claims', ''),
                                    )

    return {"message": "Form processed successfully",
            "intro": start_intro,
            'file_name': f'{API_URL}static/{filename}'}


def handle_state3(request):
    # Logic for handling state 3
    result = state3.delay(request.form)
    return {"result_id": result.id}, 200


def _empty_answer_hg_result() -> dict:
    return {
        "halls": [],
        "case_results": [],
        "statute_results": [],
        "lookup_diagnostics": [],
        "filtered_citations": [],
        "results_html": "",
        "chunk_metadata": [],
    }


def _save_answer_hg_result(memory: BaseMemory, result: dict) -> None:
    memory['hallucination_check'] = json.dumps(result.get("halls", []))
    memory['answer_hg/case_results'] = json.dumps(result.get("case_results", []))
    memory['answer_hg/statute_results'] = json.dumps(result.get("statute_results", []))
    memory['answer_hg/lookup_diagnostics'] = json.dumps(result.get("lookup_diagnostics", []))
    memory['answer_hg/filtered_citations'] = json.dumps(result.get("filtered_citations", []))
    memory['answer_hg/results_html'] = result.get("results_html", "")
    memory['answer_hg/chunk_metadata'] = json.dumps(result.get("chunk_metadata", []))


def _build_answer_hg_ad_chunks(ad_text: str) -> list[dict]:
    chunks = []
    for index, (chunk_text, start_offset) in enumerate(
        iter_citation_lookup_chunks(
            ad_text or "",
            max_chars=ANSWER_HG_AD_CHUNK_CHARS,
            overlap_chars=ANSWER_HG_AD_CHUNK_OVERLAP,
        ),
        start=1,
    ):
        chunks.append({
            "chunk_index": index,
            "start_offset": start_offset,
            "end_offset": start_offset + len(chunk_text),
            "content": chunk_text,
            "char_count": len(chunk_text),
        })
    return chunks


def _answer_hg_key_text(value) -> str:
    return re.sub(r"\s+", " ", _clean_answer_structure_string(value)).strip().lower()


def _answer_hg_record_key(record: dict, record_type: str) -> tuple:
    citation = _answer_hg_key_text(
        record.get("citation")
        or record.get("resolved_citation")
        or record.get("displayName")
        or record.get("form")
    )
    as_drafted = _answer_hg_key_text(record.get("citation_as_drafted") or citation)
    if citation or as_drafted:
        return (record_type, citation, as_drafted)
    return (record_type, _answer_hg_key_text(json.dumps(record, sort_keys=True)))


def _answer_hg_case_status(record: dict) -> str:
    try:
        from backend import hallucination_guard as _hg
        return _hg._guard_case_status(record)
    except Exception:
        if not isinstance(record, dict):
            return "Needs Review"
        if record.get("is_applicable") is False or record.get("is_valid") is False:
            return "Issue"
        if record.get("is_applicable") is None:
            return "Needs Review"
        return "Verified"


def _answer_hg_result_score(record: dict, record_type: str) -> tuple:
    status = _answer_hg_case_status(record) if record_type == "case" else ""
    if record_type == "statute":
        if record.get("is_valid") is False or record.get("is_applicable") is False:
            status = "Issue"
        elif record.get("is_applicable") is None:
            status = "Needs Review"
        else:
            status = "Verified"
    status_score = {"Issue": 3, "Needs Review": 2, "Verified": 1}.get(status, 0)
    reason_text = _clean_answer_structure_string(record.get("reason"))
    possible_issue = _clean_answer_structure_string(record.get("possible_issue"))
    resolved = bool(record.get("resolved_citation") or record.get("resolved_case_name") or record.get("clusterId") or record.get("cluster_id"))
    return (
        status_score,
        bool(possible_issue),
        bool(reason_text),
        bool(record.get("href")),
        resolved,
        len(reason_text) + len(possible_issue),
        len(json.dumps(record, default=str)),
    )


def _dedupe_answer_hg_records(records: list, record_type: str) -> list[dict]:
    deduped = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = _answer_hg_record_key(record, record_type)
        if key not in deduped or _answer_hg_result_score(record, record_type) > _answer_hg_result_score(deduped[key], record_type):
            deduped[key] = record
    return list(deduped.values())


def _dedupe_answer_hg_dict_records(records: list) -> list[dict]:
    deduped = {}
    for record in records or []:
        if not isinstance(record, dict):
            continue
        key = _answer_hg_record_key(record, "dict")
        if key not in deduped or len(json.dumps(record, default=str)) > len(json.dumps(deduped[key], default=str)):
            deduped[key] = record
    return list(deduped.values())


def _answer_hg_issue_text(record: dict) -> str:
    try:
        from backend import hallucination_guard as _hg
        issue_text = _hg._guard_issue_text(record)
    except Exception:
        issue_text = ""
    return (
        issue_text
        or _clean_answer_structure_string(record.get("reason"))
        or _clean_answer_structure_string(record.get("possible_issue"))
        or "This citation needs manual review."
    )


def _answer_hg_location_text(record: dict) -> str:
    try:
        from backend import hallucination_guard as _hg
        return _hg._guard_compact_location(record.get("location"))
    except Exception:
        return _clean_answer_structure_string(record.get("location")) or "unknown location"


def _answer_hg_case_hall(record: dict) -> list | None:
    status = _answer_hg_case_status(record)
    if status == "Verified":
        return None
    label = "incorrect" if status == "Issue" else "possibly incorrect"
    citation = (
        _clean_answer_structure_string(record.get("citation_as_drafted"))
        or _clean_answer_structure_string(record.get("citation"))
        or "Unidentified case citation"
    )
    message = f"The case citation '{citation}' at {_answer_hg_location_text(record)} may need review. {_answer_hg_issue_text(record)}"
    hall = [label, message]
    if record.get("href"):
        hall.append(record["href"])
    return hall


def _answer_hg_statute_hall(record: dict) -> list | None:
    if record.get("is_valid") is True and record.get("is_applicable") is True:
        return None
    label = "incorrect" if record.get("is_valid") is False or record.get("is_applicable") is False else "possibly incorrect"
    citation = (
        _clean_answer_structure_string(record.get("citation_as_drafted"))
        or _clean_answer_structure_string(record.get("citation"))
        or "Unidentified statute citation"
    )
    message = f"The statute citation '{citation}' at {_answer_hg_location_text(record)} may need review. {_answer_hg_issue_text(record)}"
    hall = [label, message]
    if record.get("href"):
        hall.append(record["href"])
    return hall


def _answer_hg_failure_hall(failed_chunks: list[dict], chunk_count: int) -> list | None:
    if not failed_chunks:
        return None
    if len(failed_chunks) == chunk_count:
        message = "Hallucination Guard could not review the drafted affirmative defenses. Please rerun the check or verify citations manually."
    else:
        message = (
            f"Hallucination Guard completed with partial coverage: "
            f"{len(failed_chunks)} of {chunk_count} affirmative-defense chunks could not be reviewed."
        )
    return ["possibly incorrect", message]


def _answer_hg_results_html(case_results: list, statute_results: list, partial_coverage: bool) -> str:
    try:
        from backend import hallucination_guard as _hg
        return (
            _hg._guard_results_to_html(case_results, partial_coverage=partial_coverage)
            + _hg._guard_statutes_to_html(statute_results)
        )
    except Exception as exc:
        logger.warning("ANSWER_HG could not render rich results HTML: %s", exc)
        return ""


def _merge_answer_hg_chunk_results(chunk_results: list[dict], failed_chunks: list[dict], chunk_count: int) -> dict:
    case_results = _dedupe_answer_hg_records(
        [record for result in chunk_results for record in result.get("case_results", [])],
        "case",
    )
    statute_results = _dedupe_answer_hg_records(
        [record for result in chunk_results for record in result.get("statute_results", [])],
        "statute",
    )
    lookup_diagnostics = _dedupe_answer_hg_dict_records(
        [record for result in chunk_results for record in result.get("lookup_diagnostics", [])]
    )
    filtered_citations = _dedupe_answer_hg_dict_records(
        [record for result in chunk_results for record in result.get("filtered_citations", [])]
    )

    halls = []
    for record in case_results:
        hall = _answer_hg_case_hall(record)
        if hall:
            halls.append(hall)
    for record in statute_results:
        hall = _answer_hg_statute_hall(record)
        if hall:
            halls.append(hall)
    failure_hall = _answer_hg_failure_hall(failed_chunks, chunk_count)
    if failure_hall:
        halls.append(failure_hall)

    chunk_metadata = []
    for result in chunk_results:
        chunk_metadata.append(result.get("chunk_metadata", {}))
    chunk_metadata.extend(failed_chunks)

    return {
        "halls": halls,
        "case_results": case_results,
        "statute_results": statute_results,
        "lookup_diagnostics": lookup_diagnostics,
        "filtered_citations": filtered_citations,
        "results_html": _answer_hg_results_html(case_results, statute_results, bool(failed_chunks)),
        "chunk_metadata": chunk_metadata,
    }


def run_answer_hallucination_guard_on_ad_text(memory: BaseMemory) -> dict:
    ad_text = memory.get("drafted_sections/affirmative_defenses", "") or ""
    if not ad_text.strip():
        logger.info("ANSWER_HG skipped because drafted affirmative defenses are empty.")
        result = _empty_answer_hg_result()
        _save_answer_hg_result(memory, result)
        return result

    from backend import hallucination_guard as _hg

    chunks = _build_answer_hg_ad_chunks(ad_text)
    if not chunks:
        result = _empty_answer_hg_result()
        _save_answer_hg_result(memory, result)
        return result

    logger.info(
        "ANSWER_HG reviewing drafted affirmative defenses: chars=%s chunks=%s chunk_chars=%s overlap=%s",
        len(ad_text),
        len(chunks),
        ANSWER_HG_AD_CHUNK_CHARS,
        ANSWER_HG_AD_CHUNK_OVERLAP,
    )
    hg_state = {}
    chunk_results = []
    failed_chunks = []
    for chunk in chunks:
        metadata = {
            "chunk_index": chunk["chunk_index"],
            "chunk_count": len(chunks),
            "start_offset": chunk["start_offset"],
            "end_offset": chunk["end_offset"],
            "char_count": chunk["char_count"],
        }
        try:
            result = _hg.run_hallucination_guard_on_text(
                chunk["content"],
                state=hg_state,
                source_label="drafted affirmative defenses",
            )
            if result.get("error") or result.get("error_message"):
                metadata["status"] = "error"
                metadata["error_message"] = str(result.get("error_message") or result.get("error") or "Unknown HG error")
                failed_chunks.append(metadata)
                logger.warning("ANSWER_HG chunk %s failed: %s", chunk["chunk_index"], metadata["error_message"])
                continue

            metadata["status"] = "success"
            metadata["prompt_token_counts"] = result.get("prompt_token_counts", [])
            chunk_results.append({
                "case_results": result.get("case_results", []),
                "statute_results": result.get("statute_results", []),
                "lookup_diagnostics": result.get("lookup_diagnostics", []),
                "filtered_citations": result.get("filtered_citations", []),
                "chunk_metadata": metadata,
            })
        except SoftTimeLimitExceeded:
            raise
        except Exception as exc:
            metadata["status"] = "error"
            metadata["error_message"] = str(exc)
            failed_chunks.append(metadata)
            logger.warning("ANSWER_HG chunk %s raised: %s", chunk["chunk_index"], exc)

    merged_result = _merge_answer_hg_chunk_results(chunk_results, failed_chunks, len(chunks))
    _save_answer_hg_result(memory, merged_result)
    logger.info(
        "ANSWER_HG completed: halls=%s case_results=%s statute_results=%s failed_chunks=%s",
        len(merged_result.get("halls", [])),
        len(merged_result.get("case_results", [])),
        len(merged_result.get("statute_results", [])),
        len(failed_chunks),
    )
    return merged_result


@_answer_drafter_task("finalizing the Answer and checking citations")
@guided_step_operation(DOC_TYPE, 4, lambda data: data['draftId'])
def state4(data: dict) -> dict:
    # see if the intro is going to be ignored.
    if 'wants_intro' in data and data['wants_intro'] == 'on':
        intro_text = data['introBody']
    else:
        intro_text = ""

    doc_id=Drafts.resolve_id(data['draftId'])
    mem = DBMemory(db=db, doc_id=doc_id)
    mem['drafted_sections/intro'] = intro_text    

    filename = Drafts.pdf_filename(data['draftId'])
    mem['inputs/generated_pdf_filename'] = filename
    draft_full_answer(mem, f"backend/static/{filename}")
                                        #     plaintiff=mem.get('inputs/plaintiff', "specify plaintiff"),
                                        # defendant=mem.get('inputs/defendant', "specify defendant"),
                                        # attorneys_contact_info=mem.user.contact_info or "No Attorneys_contact_info",
                                        # venue=mem.get('inputs/venue', "Venue not yet specified"),
                                        # introduction=mem.get('drafted_sections/intro', ''),
                                        # responses=mem.get('drafted_sections/responses', ''),
                                        # affirmative_defenses=mem.get('drafted_sections/affirmative_defenses', ''),
    hg_result = run_answer_hallucination_guard_on_ad_text(mem)
    halls = hg_result.get("halls", [])

    return {"message": "Form processed successfully",
            'file_name': f'{API_URL}static/{filename}',
            "halls": halls
            }

def handle_state4(request):
    # Logic for handling state 4
    result = state4.delay(request.form)
    return {"result_id": result.id}, 200


def route_answer_logic(request):
    """Decides which step should be used and passes request to those steps."""

    # switch on appState in request
    data = _answer_request_data(request)
    print("Answer logic received data:", data)
    appState = int(data['appState'])

    if appState == 0:
        return handle_state0(request)
    elif appState == 1:
        return handle_state1(request, data)
    elif appState == 2:
        return handle_state2(request)
    elif appState == 3:
        return handle_state3(request)
    elif appState == 4:
        return handle_state4(request)



if __name__ == "__main__":
    parag_2_ln = {1:3,2:6,5:13,6:15}
    parag_2_ln = is_consecutive_or_expand(parag_2_ln)
