"""Run the scheduled real-LLM semantic evaluation.

The evaluation uses the replay complaint matrix as a stable reference set,
generates both export formats, renders PDF pages to PNG screenshots, records
the resolver's complete export metadata, and asks a real OpenAI model to judge
whether the generated content preserves the reference facts.

This module is intentionally a runner rather than a pytest test. The scheduled
workflow can therefore preserve its report and artifacts before returning a
non-zero exit status.
"""

from __future__ import annotations

import json
import os
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from backend.ai.base import get_openai_client
from backend.complaint_drafter import format_venue_caption
from backend.pdf_creator import create_complaint_pdf
from backend.util.pleading_format_resolver import resolve_format_spec_for_export
from backend.word_creator import create_complaint_word
from pypdf import PdfReader
from docx import Document

from tests.e2e.validators import validate_complaint_pdf, validate_complaint_word


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "complaints.json"
DEFAULT_ARTIFACT_DIR = Path("artifacts/live-semantic")
DEFAULT_MODEL = "gpt-5.5"
DEFAULT_MIN_SCORE = 0.80
CELERY_PROBE_TASK = "tests.e2e.celery_probe"


class SemanticEvaluationFailure(RuntimeError):
    """Raised after the report has been persisted with one or more failures."""


def _artifact_dir() -> Path:
    configured = os.getenv("E2E_ARTIFACT_DIR", "").strip()
    return Path(configured) if configured else DEFAULT_ARTIFACT_DIR


def _load_cases() -> list[dict[str, Any]]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def _case_draft(case: dict[str, Any]) -> dict[str, Any]:
    llm = case["llm"]
    causes = "\n\n".join(
        [
            *case.get("causes_of_action", []),
        ]
    )
    return {
        "title": "COMPLAINT",
        "sections": [
            {"section_name": "Introduction", "section_content": llm["introduction"]},
            {"section_name": "The Parties", "section_content": "\n\n".join(llm["parties"])},
            {"section_name": "Jurisdiction and Venue", "section_content": "\n\n".join([llm["jurisdiction"], llm["venue"]])},
            {"section_name": "Factual Allegations", "section_content": llm["factual_allegations"]},
            {"section_name": "First Cause of Action", "section_content": causes},
            {"section_name": "Request for Relief", "section_content": case["prayer_for_relief"]},
        ],
    }


def _case_constraints(case: dict[str, Any]) -> dict[str, Any]:
    case_name = f"{case['plaintiff']} v. {case['defendant']}"
    return {
        "cases": {
            case_name: {
                "jurisdiction": "US federal",
                "court": f"United States District Court, {case['venue']}",
                "case_type": "complaint",
            }
        }
    }


def _word_text(path: Path) -> str:
    document = Document(str(path))
    parts = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        parts.extend(
            cell.text
            for row in table.rows
            for cell in row.cells
        )
    return "\n".join(parts)


def _pdf_text(path: Path) -> str:
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def _render_pdf_screenshots(pdf_path: Path, output_dir: Path) -> list[str]:
    """Render every generated PDF page so layout failures are inspectable."""

    import fitz

    screenshots: list[str] = []
    document = fitz.open(str(pdf_path))
    try:
        for page_index, page in enumerate(document, start=1):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5), alpha=False)
            screenshot_path = output_dir / f"{pdf_path.stem}-page-{page_index}.png"
            pixmap.save(str(screenshot_path))
            screenshots.append(str(screenshot_path))
    finally:
        document.close()
    return screenshots


def _generate_case_artifacts(case: dict[str, Any], root: Path) -> dict[str, Any]:
    slug = case["slug"]
    case_dir = root / slug
    case_dir.mkdir(parents=True, exist_ok=True)
    draft = _case_draft(case)
    constraints = _case_constraints(case)
    llm = case["llm"]
    venue = format_venue_caption(case["venue"])

    pdf_path = case_dir / f"{slug}.pdf"
    word_path = case_dir / f"{slug}.docx"
    create_complaint_pdf(
        str(pdf_path),
        plaintiff=case["plaintiff"],
        defendant=case["defendant"],
        attorneys_contact_info=case["attorneys_contact_info"],
        venue=venue,
        introduction=llm["introduction"],
        parties="\n\n".join(llm["parties"]),
        jurisdiction_n_venue="\n\n".join([llm["jurisdiction"], llm["venue"]]),
        factual_allegations=llm["factual_allegations"],
        cause_of_action="\n\n".join(case["causes_of_action"]),
        damages="specify damages sought",
        prayer_for_relief=case["prayer_for_relief"],
    )
    create_complaint_word(
        str(word_path),
        plaintiff=case["plaintiff"],
        defendant=case["defendant"],
        attorneys_contact_info=case["attorneys_contact_info"],
        venue=venue,
        introduction=llm["introduction"],
        parties="\n\n".join(llm["parties"]),
        jurisdiction_n_venue="\n\n".join([llm["jurisdiction"], llm["venue"]]),
        factual_allegations=llm["factual_allegations"],
        cause_of_action="\n\n".join(case["causes_of_action"]),
        damages="specify damages sought",
        prayer_for_relief=case["prayer_for_relief"],
    )

    format_spec = resolve_format_spec_for_export(
        draft,
        constraints=constraints,
        conversation_brief=f"Federal complaint for {case['venue']}.",
        model=os.getenv("E2E_LIVE_EVAL_MODEL", DEFAULT_MODEL),
    )
    resolver_path = case_dir / "resolver-metadata.json"
    _write_json(resolver_path, format_spec)

    screenshots = _render_pdf_screenshots(pdf_path, case_dir)
    pdf_validation = validate_complaint_pdf(pdf_path, case)
    word_validation = validate_complaint_word(word_path, case)
    return {
        "slug": slug,
        "venue": case["venue"],
        "pdf": str(pdf_path),
        "word": str(word_path),
        "screenshots": screenshots,
        "resolver_metadata": str(resolver_path),
        "pdf_pages": pdf_validation.page_count,
        "word_paragraphs": word_validation.paragraph_count,
        "pdf_text": pdf_validation.text,
        "word_text": word_validation.text,
    }


def _judge_reference(case: dict[str, Any]) -> dict[str, Any]:
    llm = case["llm"]
    return {
        "document_type": "complaint",
        "venue": case["venue"],
        "plaintiff": case["plaintiff"],
        "defendant": case["defendant"],
        "attorneys_contact_info": case["attorneys_contact_info"],
        "introduction": llm["introduction"],
        "parties": llm["parties"],
        "jurisdiction": llm["jurisdiction"],
        "venue_allegation": llm["venue"],
        "factual_allegations": llm["factual_allegations"],
        "cause_of_action": case["causes_of_action"],
        "prayer_for_relief": case["prayer_for_relief"],
    }


def _judge_prompt(case: dict[str, Any], pdf_text: str, word_text: str) -> str:
    reference = _judge_reference(case)
    return f"""You are evaluating a generated legal complaint for a regression test.

Compare the reference facts with the extracted text from the generated PDF and Word document.
Pass only if the generated documents preserve the parties, venue, material factual allegations,
cause of action, and requested relief without introducing a material unsupported fact. Formatting
is checked separately by deterministic validators, so focus on semantic fidelity and completeness.

Return JSON only with this schema:
{{
  "passed": true,
  "score": 0.0,
  "missing_facts": [],
  "unsupported_claims": [],
  "reasoning": "brief explanation"
}}

REFERENCE:
{json.dumps(reference, indent=2)}

GENERATED PDF TEXT:
{pdf_text[:30000]}

GENERATED WORD TEXT:
{word_text[:30000]}
"""


def _extract_usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    return {
        name: getattr(usage, name)
        for name in ("input_tokens", "output_tokens", "total_tokens")
        if getattr(usage, name, None) is not None
    }


def _semantic_judge(case: dict[str, Any], artifacts: dict[str, Any]) -> dict[str, Any]:
    model = os.getenv("E2E_LIVE_EVAL_MODEL", DEFAULT_MODEL)
    response = get_openai_client().responses.create(
        model=model,
        input=[
            {
                "role": "system",
                "content": "You are a strict legal-document semantic regression evaluator.",
            },
            {"role": "user", "content": _judge_prompt(case, artifacts["pdf_text"], artifacts["word_text"])},
        ],
        text={"format": {"type": "json_object"}},
    )
    raw_text = response.output_text or ""
    raw_path = Path(artifacts["pdf"]).parent / "semantic-judge-response.json"
    _write_json(raw_path, {"model": model, "raw_output": raw_text})
    try:
        result = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SemanticEvaluationFailure(
            f"{case['slug']} semantic judge returned invalid JSON"
        ) from exc
    if not isinstance(result, dict):
        raise SemanticEvaluationFailure(f"{case['slug']} semantic judge returned a non-object")

    try:
        score = float(result.get("score", 0))
    except (TypeError, ValueError):
        score = 0.0
    threshold = float(os.getenv("E2E_LIVE_EVAL_MIN_SCORE", DEFAULT_MIN_SCORE))
    judge_passed = result.get("passed") is True
    passed = judge_passed and score >= threshold
    return {
        "model": model,
        "threshold": threshold,
        "passed": passed,
        "judge_passed": judge_passed,
        "score": score,
        "missing_facts": result.get("missing_facts", []),
        "unsupported_claims": result.get("unsupported_claims", []),
        "reasoning": result.get("reasoning", ""),
        "usage": _extract_usage(response),
        "raw_response": str(raw_path),
    }


def _semantic_failure_message(slug: str, semantic: dict[str, Any]) -> str:
    reasons: list[str] = []
    score = semantic.get("score", 0)
    threshold = semantic.get("threshold", DEFAULT_MIN_SCORE)

    if not semantic.get("judge_passed"):
        reasons.append("semantic judge returned passed=false")
    if score < threshold:
        reasons.append(f"score {score} was below threshold {threshold}")

    unsupported_claims = semantic.get("unsupported_claims") or []
    if unsupported_claims:
        reasons.append(f"unsupported claims: {' | '.join(map(str, unsupported_claims))}")
    missing_facts = semantic.get("missing_facts") or []
    if missing_facts:
        reasons.append(f"missing facts: {' | '.join(map(str, missing_facts))}")

    if not reasons:
        reasons.append("unspecified semantic validation failure")
    return f"{slug} semantic evaluation failed: {'; '.join(reasons)}"


def _wait_for_celery_probe(timeout: float = 60.0) -> dict[str, Any]:
    from backend.make_celery import celery_app

    task = celery_app.send_task(CELERY_PROBE_TASK)
    deadline = time.monotonic() + timeout
    while not task.ready():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Celery probe did not complete: {task.id}")
        time.sleep(1)
    if not task.successful():
        raise RuntimeError(f"Celery probe failed: {task.id} ({task.state})")
    return {"task_id": task.id, "state": task.state, "result": task.get(timeout=1)}


def run() -> int:
    artifact_dir = _artifact_dir()
    artifact_dir.mkdir(parents=True, exist_ok=True)
    report_path = artifact_dir / "semantic-evaluation.json"
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "running",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "model": os.getenv("E2E_LIVE_EVAL_MODEL", DEFAULT_MODEL),
        "cases": [],
    }
    _write_json(report_path, report)

    try:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY is required for live semantic evaluation")

        report["celery_probe"] = _wait_for_celery_probe()
        for case in _load_cases():
            case_report: dict[str, Any] = {"slug": case["slug"], "venue": case["venue"]}
            report["cases"].append(case_report)
            artifacts = _generate_case_artifacts(case, artifact_dir)
            case_report.update({key: value for key, value in artifacts.items() if not key.endswith("_text")})
            case_report["semantic"] = _semantic_judge(case, artifacts)
            if not case_report["semantic"]["passed"]:
                raise SemanticEvaluationFailure(
                    _semantic_failure_message(case["slug"], case_report["semantic"])
                )

        report["status"] = "passed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(report_path, report)
        return 0
    except Exception as exc:
        report["status"] = "failed"
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        report["error"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
        _write_json(report_path, report)
        print(f"Live semantic evaluation failed: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(run())
