"""Freeze once-only Answer/Element Checker preprocessing for the Waymo pilot."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from backend import answer_drafter as answer
from backend import element_checker as element
from backend.util import analytics

from benchmarks.waymo.core import load_prepared
from benchmarks.waymo.prepare import PRIVATE_DIR


class Memory(dict):
    user = SimpleNamespace(contact_info="Benchmark only")


def freeze(fixture: dict) -> dict:
    """Use existing extraction/summarization prompts, never candidate models."""
    fac = fixture["fac"]
    fac_path = Path(fac["source_path"])
    lines = fac["lines"]
    first_body = int(fac["paragraph_locations"]["1"])
    first_cause = fac["causes"][0]["start_line"]
    paragraph_locations = {int(key): int(value) for key, value in fac["paragraph_locations"].items()}
    sections = [{
        "type": "factual_allegations", "title": "Pre-cause allegations",
        "start_line": first_body, "end_line": first_cause - 1,
    }]
    sections.extend(
        {"type": "cause_of_action", "title": row["title"],
         "start_line": row["start_line"], "end_line": row["end_line"]}
        for row in fac["causes"]
    )
    digest = answer.get_answer_factual_digest_from_llm(
        lines, sections, para_2_ln=paragraph_locations,
        first_body_line_number=first_body,
    )
    if not digest.get("summary"):
        raise ValueError("Answer factual digest is blank")

    memory = Memory()
    boundaries = [row["start_line"] for row in fac["causes"]] + [fac["causes"][-1]["end_line"]]
    with patch.object(element, "DBMemory", lambda db, doc_id: memory), \
         patch.object(element.Drafts, "resolve_id", lambda _: 1), \
         patch.object(analytics, "start_step_analytics", lambda *a, **k: None), \
         patch.object(analytics, "finish_step_analytics", lambda *a, **k: None), \
         patch.object(element, "STATIC_FOLDER", str(fac_path.parent)), \
         patch.object(element, "get_coa_loc", lambda *a, **k: boundaries), \
         patch.object(element, "extract_parties_venue", return_value=(
             ["Waymo LLC"], fac["represented_defendants"], fac["venue"],
         )):
        # Celery's bound Task.run receives its self automatically.
        result = element.state0.run(fac_path.name, "benchmark", "defendant")
    if result.get("message") == "error":
        raise RuntimeError(f"Element upload analysis failed: {result.get('error_message')}")
    cause_summaries = json.loads(memory["analysis/coa_sum"])
    if len(cause_summaries) != 7 or not memory.get("drafted_sections/fa_sum"):
        raise ValueError("Element summaries did not cover all seven causes")
    fixture["fixed_factual_digest"] = digest
    fixture["fixed_element_summaries"] = {
        "factual_summary": memory["drafted_sections/fa_sum"],
        "short_factual_summary": memory.get("drafted_sections/fa_sum_final", ""),
        "cause_summaries": cause_summaries,
    }
    return fixture


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=PRIVATE_DIR / "prepared-v2.json")
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    if not args.execute_paid:
        raise SystemExit("Freezing summaries uses fixed provider calls; add --execute-paid to proceed")
    if not os.environ.get("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY was not loaded from the root .env")
    fixture = load_prepared(args.fixture)
    if fixture.get("version") != 2:
        raise SystemExit("Use the v2 FAC-only benchmark fixture")
    if fixture.get("fixed_factual_digest") or fixture.get("fixed_element_summaries"):
        raise SystemExit("Frozen summaries already exist; refusing another paid preparation run")
    fixture = freeze(fixture)
    args.fixture.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    args.fixture.chmod(0o600)
    print("Frozen Answer and Element Checker summaries for seven causes")


if __name__ == "__main__":
    main()
