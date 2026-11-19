import json
import re
from datetime import datetime, timezone

from pydantic import BaseModel

from backend.ai.base import chat_completion_parse


METADATA_SCHEMA_VERSION = 1
METADATA_STATUSES = {"pending", "ready", "failed", "missing"}
DEFAULT_DOCUMENT_KIND = "unknown"
MAX_METADATA_SUMMARY_SENTENCES = 3
FRONT_EXCERPT_CHARS = 8000
BACK_EXCERPT_CHARS = 4000


class DocumentMetadataResult(BaseModel):
    summary: str
    document_kind: str


def default_document_metadata(status: str = "missing") -> dict:
    if status not in METADATA_STATUSES:
        status = "missing"
    return {
        "schema_version": METADATA_SCHEMA_VERSION,
        "status": status,
        "summary": "",
        "document_kind": DEFAULT_DOCUMENT_KIND,
    }


def build_metadata_excerpt(parsed_text: str, front_chars: int = FRONT_EXCERPT_CHARS,
                           back_chars: int = BACK_EXCERPT_CHARS) -> str:
    text = (parsed_text or "").strip()
    if not text:
        return ""
    if len(text) <= front_chars + back_chars:
        return text
    return (
        text[:front_chars].strip()
        + "\n\n[... middle omitted for metadata extraction ...]\n\n"
        + text[-back_chars:].strip()
    )


def _parse_json_object(value) -> dict:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        return json.loads(value)
    except Exception:
        pass
    match = re.search(r"\{.*\}", value, re.S)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def _limit_summary_sentences(summary: str, max_sentences: int = MAX_METADATA_SUMMARY_SENTENCES) -> str:
    summary = " ".join(str(summary or "").split())
    if not summary:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", summary)
    kept = [part.strip() for part in parts if part.strip()]
    return " ".join(kept[:max_sentences]).strip()


def normalize_document_metadata(value, status: str | None = None) -> dict:
    parsed = _parse_json_object(value)
    if parsed.get('schema_version') == 2:
        # Prepared artifacts enforce token budgets upstream; retain their fields
        # and all sentences rather than applying legacy excerpt normalization.
        meta = dict(parsed)
        meta['status'] = status or parsed.get('status', 'missing')
        meta['summary'] = parsed.get('preview', parsed.get('summary', ''))
        return meta
    meta = default_document_metadata(status or parsed.get("status", "missing"))

    raw_status = parsed.get("status")
    if raw_status in METADATA_STATUSES:
        meta["status"] = raw_status
    if status in METADATA_STATUSES:
        meta["status"] = status

    summary = parsed.get("summary")
    if isinstance(summary, str):
        meta["summary"] = _limit_summary_sentences(summary)

    document_kind = parsed.get("document_kind")
    if isinstance(document_kind, str) and document_kind.strip():
        meta["document_kind"] = document_kind.strip()[:80]

    return meta


def serialize_metadata(metadata: dict) -> str:
    return json.dumps(normalize_document_metadata(metadata), ensure_ascii=False)


def parse_metadata(metadata_json: str | None) -> dict:
    if not metadata_json:
        return default_document_metadata("missing")
    return normalize_document_metadata(metadata_json)


def extract_document_metadata(
    parsed_text: str,
    filename: str,
    model: str = "gpt-6-luna",
    *,
    reasoning_effort: str | None = None,
) -> dict:
    excerpt = build_metadata_excerpt(parsed_text)
    if not excerpt:
        return default_document_metadata("missing")

    prompt = f"""
You are extracting lightweight metadata for a user-uploaded document.

Filename:
{filename}

Document excerpt:
{excerpt}

Return JSON only with exactly these keys:
{{
  "summary": "1-3 sentence neutral summary of the document. Do not exceed 3 sentences.",
  "document_kind": "short general type such as contract, email, pleading, report, spreadsheet, image, transcript, memo, letter, invoice, unknown"
}}

Rules:
- Keep the summary factual and general.
- Do not infer facts not supported by the excerpt.
- If the document type is unclear, use "unknown".
"""
    output = chat_completion_parse(
        prompt,
        DocumentMetadataResult,
        model=model,
        reasoning_effort=reasoning_effort,
        sys_prompt="You extract compact, factual metadata from documents.",
    )
    if output is None:
        raise ValueError("metadata extraction returned empty output")
    metadata = normalize_document_metadata({
        "status": "ready",
        "summary": output.summary,
        "document_kind": output.document_kind,
    }, status="ready")
    metadata["generated_at"] = datetime.now(timezone.utc).isoformat()
    return metadata
