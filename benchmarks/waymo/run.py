"""Run isolated Waymo candidate product stages after the review gate is met.

This is deliberately not wired to a production route or Celery. It uses the
current production prompts/schemas/chunk helpers inside an in-memory draft,
and stores all outputs only under the ignored private artifact directory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import threading
import time
import uuid
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend.ai import base
from backend.util import analytics

from benchmarks.waymo.core import (
    CANDIDATES, PRODUCTS, REPETITIONS, Candidate, candidate_view,
    estimate_cost, validate_final_rfps,
)
from benchmarks.waymo.prepare import FAC, PRIVATE_DIR


class BenchmarkMemory(dict):
    user = SimpleNamespace(contact_info="Benchmark only")


class MeteredResponses:
    def __init__(self, real, records: list[dict], lock: threading.Lock):
        self.real = real
        self.records = records
        self.lock = lock

    def _call(self, operation: str, kwargs: dict):
        started = time.monotonic()
        record = {
            "operation": operation,
            "model": kwargs.get("model"),
            "effort": (kwargs.get("reasoning") or {}).get("effort"),
            "schema": getattr(kwargs.get("text_format"), "__name__", None),
            "input_sha256": hashlib.sha256(
                repr(kwargs.get("input", "")).encode("utf-8")
            ).hexdigest(),
        }
        try:
            response = getattr(self.real, operation)(**kwargs)
            usage = getattr(response, "usage", None)
            record.update({
                "response_id": getattr(response, "id", None),
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
                "status": getattr(response, "status", None),
                "success": True,
            })
            return response
        except Exception as exc:
            record.update({"success": False, "failure_class": type(exc).__name__})
            raise
        finally:
            record["latency_seconds"] = round(time.monotonic() - started, 3)
            with self.lock:
                self.records.append(record)

    def parse(self, **kwargs):
        return self._call("parse", kwargs)

    def create(self, **kwargs):
        return self._call("create", kwargs)


class MeteredClient:
    def __init__(self, real, records: list[dict]):
        self.real = real
        self.responses = MeteredResponses(real.responses, records, threading.Lock())

    def __getattr__(self, name):
        return getattr(self.real, name)


@contextmanager
def isolated_product(memory: BenchmarkMemory, candidate: Candidate):
    """Patch draft persistence and analytics; restore every patch after a run."""
    from backend import answer_drafter, element_checker, rfp_drafter

    with ExitStack() as stack:
        for module in (answer_drafter, element_checker, rfp_drafter):
            stack.enter_context(patch.object(module, "DBMemory", lambda db, doc_id: memory))
            stack.enter_context(patch.object(module.Drafts, "resolve_id", lambda public_id: 1))
            stack.enter_context(patch.object(module.Drafts, "pdf_filename", lambda public_id: "benchmark.pdf"))
        stack.enter_context(patch.object(analytics, "start_step_analytics", lambda *a, **k: None))
        stack.enter_context(patch.object(analytics, "finish_step_analytics", lambda *a, **k: None))
        stack.enter_context(patch.object(answer_drafter, "create_answer_pdf", lambda **k: "benchmark-only"))
        stack.enter_context(patch.object(element_checker, "create_analysis_pdf", lambda **k: "benchmark-only"))
        stack.enter_context(patch.object(answer_drafter, "ANSWER_RESPONSE_MODEL", candidate.model))
        stack.enter_context(patch.object(answer_drafter, "ANSWER_RESPONSE_EFFORT", candidate.effort))
        stack.enter_context(patch.object(answer_drafter, "ANSWER_DEFENSE_MODEL", candidate.model))
        stack.enter_context(patch.object(answer_drafter, "ANSWER_DEFENSE_EFFORT", candidate.effort))
        stack.enter_context(patch.object(element_checker, "ANALYSIS_MODEL", candidate.model))
        stack.enter_context(patch.object(element_checker, "ANALYSIS_EFFORT", candidate.effort))
        stack.enter_context(patch.object(rfp_drafter, "DRAFTING_MODEL", candidate.model))
        stack.enter_context(patch.object(rfp_drafter, "DRAFTING_REASONING_EFFORT", candidate.effort))
        yield


def _answer_memory(view: dict) -> BenchmarkMemory:
    from backend import answer_drafter as answer

    fac = view["fac"]
    causes = [
        {"title": row["title"], "type": "cause_of_action", "claim_number": row["number"],
         "start_line": row["start_line"], "end_line": row["end_line"],
         "against": fac["represented_defendants"], "by": ["Waymo LLC"]}
        for row in fac["causes"]
    ]
    structure = {
        "plaintiffs": ["Waymo LLC"], "defendants": fac["represented_defendants"],
        "court": fac["venue"], "venue": fac["venue"],
        "jurisdiction_type": "federal", "state": "california",
        "first_body_line_number": max(0, min(map(int, fac["paragraph_locations"].values())) - 1),
    }
    paragraph_locations = {int(number): int(line) for number, line in fac["paragraph_locations"].items()}
    first_body = max(0, min(paragraph_locations.values()) - 1)
    first_cause = fac["causes"][0]["start_line"]
    section_starts = []
    heading_start = first_body
    for index, line in enumerate(fac["lines"][heading_start:first_cause], start=heading_start):
        match = re.fullmatch(r"<B>([^<>]+)</B>", line.strip())
        if match and match.group(1).strip().isupper():
            section_starts.append((index, match.group(1).strip(), "factual_allegations"))
    section_starts.extend((row["start_line"], row["title"], "cause_of_action") for row in fac["causes"])
    section_starts.append((fac["causes"][-1]["end_line"], "PRAYER FOR RELIEF", "prayer"))
    section_starts.sort(key=lambda row: row[0])
    sections = [
        {"title": title, "type": kind, "start_line": start,
         "end_line": section_starts[index + 1][0] - 1 if index + 1 < len(section_starts) else len(fac["lines"]) - 1}
        for index, (start, title, kind) in enumerate(section_starts)
    ]
    outline = answer.build_answer_outline_from_locations(sections, paragraph_locations, first_body)
    return BenchmarkMemory({
        "complaint_text": json.dumps(fac["lines"]),
        "paragraphs": json.dumps(fac["paragraph_locations"]),
        "outline": json.dumps(outline),
        "complaint_structure": json.dumps(structure),
        "sections": json.dumps(sections),
        "causes_of_action": json.dumps(causes),
        "ads_suggestions": "{}",
        "inputs/plaintiff": "Waymo LLC",
        "inputs/defendant": ";".join(fac["represented_defendants"]),
        "inputs/venue": fac["venue"],
        "inputs/state": "california", "inputs/is_federal": "federal",
        "complaint_cover_text": fac["cover"],
    })


def run_answer(view: dict, candidate: Candidate) -> dict:
    from backend import answer_drafter as answer

    memory = _answer_memory(view)
    payload = {"draftId": "benchmark", "appState": 1,
               "defendant0": "Uber Technologies, Inc.", "defendant1": "Ottomotto LLC"}
    for number, row in view["answer_choices"].items():
        field = f"para{int(number):04d}"
        payload[field] = row["choice"]
        if row["choice"] == "custom":
            payload[f"customResponse-{field}"] = row["custom_instruction"]
    with isolated_product(memory, candidate), patch.object(
        answer, "get_answer_factual_digest_from_llm", return_value=view["fixed_factual_digest"]
    ):
        step1 = answer.state1.run(payload)
        if step1.get("message") == "error":
            raise RuntimeError(f"Answer Step 1 failed: {step1.get('error_message')}")
        suggestions = json.loads(memory.get("ads_suggestions", "{}"))
        titles = []
        seen_titles = set()
        for raw_title in suggestions:
            title = raw_title.strip()
            key = " ".join(title.casefold().split())
            if title and key not in seen_titles:
                seen_titles.add(key)
                titles.append(title)
        step2 = answer.state2.run({"draftId": "benchmark", **{
            f"ad{index}": title for index, title in enumerate(titles, start=1)
        }})
        if step2.get("message") == "error":
            raise RuntimeError(f"Answer Step 2 failed: {step2.get('error_message')}")
    return {
        "suggestions": suggestions, "selected_defense_titles": titles,
        "responses": memory.get("drafted_sections/responses", ""),
        "affirmative_defenses": memory.get("drafted_sections/affirmative_defenses", ""),
    }


def run_element_checker(view: dict, candidate: Candidate) -> dict:
    from backend import element_checker as element

    fac = view["fac"]
    summaries = view["fixed_element_summaries"]
    memory = BenchmarkMemory({
        "complaint_text": json.dumps(fac["lines"]),
        "inputs/side": "defendant", "inputs/venue": fac["venue"],
        "inputs/plaintiff": "Waymo LLC",
        "inputs/defendant": ";".join(fac["represented_defendants"]),
        "drafted_sections/fa_sum": summaries["factual_summary"],
        "drafted_sections/fa_sum_final": summaries["factual_summary"],
        "analysis/coa_sum": json.dumps(summaries["cause_summaries"]),
        "coa_loc": ",".join(str(row["start_line"]) for row in fac["causes"])
                   + "," + str(fac["causes"][-1]["end_line"]),
    })
    with isolated_product(memory, candidate), patch.object(
        element, "chat_completion", side_effect=lambda prompt, **kwargs: prompt
    ):
        step1 = element.state1.run({"draftId": "benchmark"})
        if step1.get("message") == "error":
            raise RuntimeError("Element Checker analysis failed")
        step2 = element.state2.run({"draftId": "benchmark"})
        if step2.get("message") == "error":
            raise RuntimeError("Element Checker recommendations failed")
    return {"analysis": json.loads(memory["analysis/element_analysis"]),
            "recommendations": memory.get("drafted_sections/recommendation", "")}


def run_rfp(view: dict, candidate: Candidate) -> dict:
    from backend import rfp_drafter as rfp

    fac = view["fac"]
    memory = BenchmarkMemory({
        "complaint_text": json.dumps(fac["lines"]),
        "complaint_cover_text": fac["cover"],
        "inputs/side": "defendant", "inputs/uploaded_filename": FAC_FILENAME,
    })
    with isolated_product(memory, candidate), patch.object(
        rfp, "extract_parties_venue", return_value=(["Waymo LLC"], fac["represented_defendants"], fac["venue"])
    ), patch.object(rfp, "chat_completion", return_value="FEDERAL"):
        generated = rfp.generate_rfp_requests_from_memory(memory, "benchmark")
        if generated.get("message") == "error":
            raise RuntimeError(f"RFP generation failed: {generated.get('error_message')}")
        consolidated = generated["requests"]
        if len(consolidated) < 30:
            raise ValueError(f"RFP incomplete: only {len(consolidated)} consolidated candidates")
        reduced = rfp.further_reduce_rfp_task.run({"draftId": "benchmark", "targetN": 30})
        if reduced.get("message") == "error":
            raise RuntimeError(f"RFP reduction failed: {reduced.get('error_message')}")
        validate_final_rfps(reduced["requests"], len(consolidated))
    return {"proposal_and_consolidation_count": len(consolidated),
            "final_requests": reduced["requests"]}


FAC_FILENAME = FAC.name
PRODUCT_RUNNERS = {
    "answer": run_answer, "element_checker": run_element_checker, "rfp": run_rfp,
}


def _review_markdown(result: dict) -> str:
    """Readable complete output for a lawyer; keep beside the private JSON."""
    lines = [f"# {result['product']} — {result['model']} / {result['effort']} — repeat {result['repetition']}", "",
             f"Run ID: `{result['run_id']}`", ""]
    if result["error"]:
        return "\n".join(lines + [f"Run failed: {result['error']['class']}", ""])
    output = result["output"]
    if result["product"] == "answer":
        lines += ["## Paragraph responses", "", str(output.get("responses", "")), "",
                  "## Suggested affirmative defenses", "",
                  json.dumps(output.get("suggestions", {}), ensure_ascii=False, indent=2), "",
                  "## Drafted affirmative defenses", "",
                  str(output.get("affirmative_defenses", "")), ""]
    elif result["product"] == "element_checker":
        lines += ["## Element and weakness analysis", "",
                  json.dumps(output.get("analysis", {}), ensure_ascii=False, indent=2), "",
                  "## Recommendations", "", str(output.get("recommendations", "")), ""]
    else:
        lines += [f"Final requests: {len(output.get('final_requests', []))}", ""]
        for number, item in enumerate(output.get("final_requests", []), start=1):
            lines += [f"## Request {number}: {item.get('Topic', '')}", "",
                      item.get("Request", ""), "", item.get("Justification", ""), ""]
    return "\n".join(lines)


def run_one(fixture: dict, product: str, candidate: Candidate, repetition: int,
            *, client=None, output_dir: Path = PRIVATE_DIR) -> dict:
    if product not in PRODUCTS or candidate not in CANDIDATES or repetition not in range(1, REPETITIONS + 1):
        raise ValueError("Unknown product, candidate, or repetition")
    view = candidate_view(fixture)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    review_dir = output_dir / "candidate_outputs" / product / f"{candidate.model}-{candidate.effort}"
    review_dir.mkdir(parents=True, exist_ok=True)
    review_dir.chmod(0o700)
    review_file = review_dir / f"repeat-{repetition}.json"
    review_markdown = review_dir / f"repeat-{repetition}.md"
    if review_file.exists():
        old_run_id = json.loads(review_file.read_text(encoding="utf-8"))["run_id"]
        old_run_path = output_dir / f"run-{old_run_id}.json"
        if old_run_path.exists():
            old_result = json.loads(old_run_path.read_text(encoding="utf-8"))
            if not review_markdown.exists():
                review_markdown.write_text(_review_markdown(old_result), encoding="utf-8")
                review_markdown.chmod(0o600)
            return old_result
        raise FileExistsError(f"Review output exists without a complete run: {review_file}")
    records: list[dict] = []
    run_id = str(uuid.uuid4())
    started = time.monotonic()
    real_client = client or base.get_openai_client()
    with patch.object(base, "client", MeteredClient(real_client, records)):
        try:
            output = PRODUCT_RUNNERS[product](view, candidate)
            error = None
        except Exception as exc:
            output = None
            error = {"class": type(exc).__name__, "message": str(exc)}
    result = {
        "run_id": run_id, "product": product, "model": candidate.model,
        "effort": candidate.effort, "repetition": repetition,
        "duration_seconds": round(time.monotonic() - started, 3),
        "provider_attempts": records, "cost_usd": estimate_cost(records),
        "output": output, "error": error,
    }
    result["review_file"] = str(review_file)
    result["review_markdown_file"] = str(review_markdown)
    review_file.write_text(json.dumps({
        "run_id": run_id, "product": product, "model": candidate.model,
        "effort": candidate.effort, "repetition": repetition,
        "output": output, "error": error,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    review_file.chmod(0o600)
    review_markdown.write_text(_review_markdown(result), encoding="utf-8")
    review_markdown.chmod(0o600)
    destination = output_dir / f"run-{run_id}.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    destination.chmod(0o600)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=PRIVATE_DIR / "prepared-v2.json")
    parser.add_argument("--product", choices=PRODUCTS, required=True)
    parser.add_argument("--candidate", type=int, choices=range(len(CANDIDATES)), required=True)
    parser.add_argument("--repetition", type=int, choices=range(1, REPETITIONS + 1), required=True)
    parser.add_argument("--execute-paid", action="store_true", help="Required to send provider requests")
    parser.add_argument("--result-pointer", type=Path, help="Private file receiving the completed run ID")
    args = parser.parse_args()
    # The subprocess receives only a FAC-derived fixture. Judge references
    # are intentionally absent and therefore cannot enter provider prompts.
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    candidate_view(fixture)  # Validate human approvals before opening a provider client.
    if not args.execute_paid:
        raise SystemExit("Review gate passed; add --execute-paid to run this one paid candidate")
    result = run_one(fixture, args.product, CANDIDATES[args.candidate], args.repetition)
    if args.result_pointer:
        if args.result_pointer.resolve().parent != PRIVATE_DIR.resolve():
            raise SystemExit("Result pointer must be in the private benchmark directory")
        args.result_pointer.write_text(json.dumps({"run_id": result["run_id"]}) + "\n", encoding="utf-8")
        args.result_pointer.chmod(0o600)
    print(json.dumps({key: result[key] for key in (
        "run_id", "product", "model", "effort", "repetition", "duration_seconds", "cost_usd", "error"
    )}, indent=2))


if __name__ == "__main__":
    main()
