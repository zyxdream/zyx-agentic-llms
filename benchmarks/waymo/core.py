"""Guardrails, scoring, and accounting for the private Waymo model pilot."""

from __future__ import annotations

import json
import hashlib
from dataclasses import dataclass
from pathlib import Path
from statistics import mean

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class Candidate:
    model: str
    effort: str


CANDIDATES = (
    Candidate("gpt-5.5", "medium"),
    Candidate("gpt-6-sol", "medium"),
    Candidate("gpt-6.1-sol", "medium"),
    Candidate("gpt-6.1-sol", "high"),
)
JUDGES = (Candidate("gpt-6-astra", "medium"), Candidate("gpt-5.6-sol", "high"))
PRODUCTS = ("answer", "element_checker", "rfp")
REPETITIONS = 2
RFP_TARGET = 30

# Standard API text-token rates per million, verified against official model
# pages on 2026-10-13. Recheck before any paid run; long-context uplifts and
# cache discounts are deliberately not folded into this simple estimate.
STANDARD_RATES = {
    "gpt-5.5": (5.00, 30.00),
    "gpt-6-sol": (2.00, 10.00),
    "gpt-6.1-sol": (2.00, 10.00),
    "gpt-6-astra": (10.00, 50.00),
    "gpt-5.6-sol": (4.00, 20.00),
}


class Criterion(BaseModel):
    name: str
    score: int = Field(ge=0, le=5)
    evidence: str


class JudgeResult(BaseModel):
    criteria: list[Criterion]
    critical_errors: list[str]
    explanation: str


class CohortEvaluation(BaseModel):
    blind_id: str
    rank: int = Field(ge=1, le=4)
    criteria: list[Criterion]
    critical_errors: list[str]
    explanation: str


class CohortResult(BaseModel):
    evaluations: list[CohortEvaluation]
    comparison_summary: str


RUBRICS = {
    "answer": {
        "paragraph_coverage_and_choice_fidelity": 25,
        "complaint_grounding": 30,
        "defense_quality": 35,
        "clarity": 10,
    },
    "element_checker": {
        "element_coverage": 30,
        "complaint_grounding": 25,
        "recommendations": 30,
        "authority_accuracy": 15,
    },
    "rfp": {
        "complaint_inferable_coverage": 30,
        "specificity_and_proportionality": 30,
        "distinctness_and_priority": 20,
        "usability": 20,
    },
}

RUBRIC_GUIDANCE = {
    "answer": {
        "paragraph_coverage_and_choice_fidelity": "All 158 FAC paragraphs receive a denial. Check that each denial addresses the correctly numbered FAC paragraph and follows the fixed deny choice; do not score whether a different admission or response type would have been preferable. The filed Answer's lawyer-specific admissions and qualifications are not available to candidates.",
        "complaint_grounding": "Check that each denial aligns with the allegations in its corresponding FAC paragraph, without invented case facts, unwarranted admissions, or unsupported qualifications; no later-case knowledge is required.",
        "defense_quality": "Evaluate the model's own defense suggestion list and the completed drafting of every distinct suggested defense for applicability, support, and authority.",
        "clarity": "Coherent, usable, nonduplicative Answer language; do not reward verbosity or resemblance to filed prose.",
    },
    "element_checker": {
        "element_coverage": "Cover legal elements for each of all seven causes, including the federal and California claims, without conflating standards.",
        "complaint_grounding": "Status and weakness analysis cite allegations actually present in the FAC and distinguish allegations from proof.",
        "recommendations": "Specific, practical defense-side recommendations tied to alleged pleading gaps, not generic summaries.",
        "authority_accuracy": "Check legal authorities and their propositions; unsupported or fabricated citations are critical errors.",
    },
    "rfp": {
        "complaint_inferable_coverage": "Thirty final requests should cover important issues reasonably inferable from the FAC; do not require later discovery-specific themes.",
        "specificity_and_proportionality": "Requests are targeted, intelligible, and proportionate to FAC allegations, without blanket overbreadth.",
        "distinctness_and_priority": "The thirty requests are substantively distinct and ordered from more to less critical.",
        "usability": "Each request is draft-ready and seeks identifiable documents, not interrogatory answers or unsupported later-case details.",
    },
}


def load_prepared(path: Path) -> dict:
    fixture = json.loads(path.read_text(encoding="utf-8"))
    if len(fixture["fac"]["causes"]) != 7:
        raise ValueError("Expected seven FAC causes")
    if len(fixture["fac"].get("paragraph_locations", {})) != 158:
        raise ValueError("Expected FAC paragraphs 1–158")
    if [row["number"] for row in fixture["rfp_reference"]["requests"]] != list(range(1, 147)):
        raise ValueError("RFP reference must contain requests 1–146")
    for section in (fixture["fac"], fixture["answer_reference"], fixture["rfp_reference"]):
        if section.get("source_path") and section.get("source_sha256"):
            source = Path(section["source_path"])
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != section["source_sha256"]:
                raise ValueError(f"Source document changed or disappeared: {source}")
    return fixture


def require_ready(fixture: dict) -> None:
    """Only FAC-derived, fixed preprocessing is required before generation."""
    if fixture.get("version") != 2:
        raise ValueError("Run the v2 preparer; the old filed-Answer choice map is not safe for generation")
    if fixture.get("answer_input", {}).get("default_choice") != "deny":
        raise ValueError("Benchmark Answer choices must use the same FAC-only default deny")
    if not fixture["fac"].get("paragraph_locations"):
        raise ValueError("FAC paragraph locations are required")
    if not fixture.get("fixed_factual_digest") or not fixture.get("fixed_element_summaries"):
        raise ValueError("Frozen factual digest and element summaries are required")


def candidate_view(fixture: dict) -> dict:
    """Allowlist candidate data: filed Answer/RFP never cross this boundary."""
    require_ready(fixture)
    return {
        "fac": fixture["fac"],
        "answer_choices": {str(number): {"choice": "deny"}
                           for number in fixture["fac"]["paragraph_locations"]},
        "fixed_factual_digest": fixture["fixed_factual_digest"],
        "fixed_element_summaries": fixture["fixed_element_summaries"],
    }


def validate_final_rfps(requests: list[dict], candidate_count: int) -> None:
    if candidate_count < RFP_TARGET:
        raise ValueError(f"Only {candidate_count} candidates; run is incomplete, not padded")
    if len(requests) != RFP_TARGET:
        raise ValueError(f"Expected exactly {RFP_TARGET} final RFPs, got {len(requests)}")
    texts = [str(row.get("Request", "")).strip() for row in requests]
    if not all(texts) or len({" ".join(text.lower().split()) for text in texts}) != RFP_TARGET:
        raise ValueError("Final RFPs must be nonblank and textually distinct")


def weighted_score(product: str, result: JudgeResult) -> float:
    weights = RUBRICS[product]
    by_name = {row.name: row for row in result.criteria}
    if len(by_name) != len(result.criteria) or set(by_name) != set(weights):
        raise ValueError("Judge result must score each rubric criterion exactly once")
    if any(not by_name[name].evidence.strip() for name in weights):
        raise ValueError("Each criterion needs evidence")
    return round(sum(weights[name] * by_name[name].score / 5 for name in weights), 2)


def summarize_runs(rows: list[dict]) -> list[dict]:
    grouped: dict[tuple[str, str, str], list[dict]] = {}
    for row in rows:
        grouped.setdefault((row["product"], row["model"], row["effort"]), []).append(row)
    summary = []
    for (product, model, effort), group in sorted(grouped.items()):
        scores = [row["score"] for row in group if row.get("score") is not None]
        summary.append({
            "product": product, "model": model, "effort": effort,
            "runs": len(group), "successful": len(scores),
            "mean": round(mean(scores), 2) if scores else None,
            "range": [min(scores), max(scores)] if scores else None,
            "failed": len(group) - len(scores),
            "cost_usd": round(sum(row.get("cost_usd", 0) for row in group), 4),
        })
    return summary


def estimate_cost(usage: list[dict], *, long_context: bool = False) -> float:
    """A conservative uncached Standard-tier estimate from measured/assumed tokens."""
    total = 0.0
    for item in usage:
        input_rate, output_rate = STANDARD_RATES[item["model"]]
        if long_context or item.get("input_tokens", 0) > 272_000:
            input_rate *= 2
            output_rate *= 1.5
        total += (
            item.get("input_tokens", 0) * input_rate
            + item.get("output_tokens", 0) * output_rate
        ) / 1_000_000
    return round(total, 4)
