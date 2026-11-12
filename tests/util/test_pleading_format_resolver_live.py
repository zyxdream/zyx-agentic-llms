"""Live LLM integration tests for pleading format resolver.

Requires OPENAI_API_KEY and explicit opt-in. Prints raw JSON from the model for
three jurisdiction scenarios.

Run (from repo root, with conda env active):

    conda activate lexintel
    RUN_LIVE_LLM_TESTS=1 pytest tests/util/test_pleading_format_resolver_live.py -v -s

Or as a script:

    python tests/util/test_pleading_format_resolver_live.py
"""

from __future__ import annotations

import json
import os
import sys

import pytest

from backend.util.pleading_format_resolver import (
    fetch_raw_format_requirements_llm,
    merge_llm_format_response,
)

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY") or os.getenv("RUN_LIVE_LLM_TESTS") != "1",
    reason="Live LLM tests require OPENAI_API_KEY and RUN_LIVE_LLM_TESTS=1",
)

# Sample drafts designed to signal each jurisdiction clearly.
LIVE_SCENARIOS = [
    {
        "label": "1. US federal",
        "draft": {
            "title": "COMPLAINT FOR DAMAGES",
            "sections": [
                {
                    "section_name": "Jurisdiction and Venue",
                    "section_content": (
                        "**JURISDICTION AND VENUE**\n\n"
                        "1. This Court has subject-matter jurisdiction under 28 U.S.C. § 1332 "
                        "because complete diversity exists and the amount in controversy exceeds $75,000.\n\n"
                        "2. Venue is proper in this District under 28 U.S.C. § 1391(b)(2)."
                    ),
                },
                {
                    "section_name": "Parties",
                    "section_content": (
                        "3. Plaintiff John Doe is a citizen of California.\n\n"
                        "4. Defendant Acme Corp. is a Delaware corporation with its principal "
                        "place of business in Nevada."
                    ),
                },
            ],
        },
        "constraints": {
            "cases": {
                "Doe v. Acme Corp.": {
                    "jurisdiction": "US federal",
                    "court": "United States District Court, Eastern District of California",
                    "case_type": "complaint",
                }
            }
        },
        "brief": "Federal diversity complaint for breach of contract.",
    },
    {
        "label": "2. California state",
        "draft": {
            "title": "COMPLAINT FOR DAMAGES",
            "sections": [
                {
                    "section_name": "Caption",
                    "section_content": (
                        "SUPERIOR COURT OF THE STATE OF CALIFORNIA\n"
                        "FOR THE COUNTY OF LOS ANGELES\n\n"
                        "JOHN DOE, Plaintiff,\n"
                        "v.\n"
                        "ACME CORPORATION, Defendant."
                    ),
                },
                {
                    "section_name": "Jurisdiction and Venue",
                    "section_content": (
                        "**JURISDICTION AND VENUE**\n\n"
                        "1. This Court has jurisdiction under California Code of Civil Procedure "
                        "§ 410.10.\n\n"
                        "2. Venue is proper in Los Angeles County under CCP § 395."
                    ),
                },
            ],
        },
        "constraints": {
            "cases": {
                "Doe v. Acme Corp.": {
                    "jurisdiction": "California",
                    "court": "Superior Court of California, County of Los Angeles",
                    "case_type": "complaint",
                }
            }
        },
        "brief": "State court complaint filed in Los Angeles Superior Court.",
    },
    {
        "label": "3. New York City",
        "draft": {
            "title": "COMPLAINT",
            "sections": [
                {
                    "section_name": "Caption",
                    "section_content": (
                        "SUPREME COURT OF THE STATE OF NEW YORK\n"
                        "COUNTY OF NEW YORK\n\n"
                        "JOHN DOE, Plaintiff,\n"
                        "-against-\n"
                        "ACME CORPORATION, Defendant.\n\n"
                        "Index No. _______"
                    ),
                },
                {
                    "section_name": "Nature of Action",
                    "section_content": (
                        "**NATURE OF THE ACTION**\n\n"
                        "Plaintiff brings this action for breach of contract arising from "
                        "Defendant's failure to pay for services rendered in New York City."
                    ),
                },
            ],
        },
        "constraints": {
            "cases": {
                "Doe v. Acme Corp.": {
                    "jurisdiction": "New York",
                    "court": "Supreme Court of the State of New York, County of New York (Manhattan)",
                    "case_type": "complaint",
                }
            }
        },
        "brief": "New York Supreme Court (trial-level) complaint in Manhattan.",
    },
]


def _print_scenario_result(label: str, raw_text: str | None, parsed: dict | None) -> None:
    border = "=" * 72
    print(f"\n{border}")
    print(label)
    print(border)
    print("\n--- RAW LLM OUTPUT ---")
    if raw_text:
        try:
            print(json.dumps(json.loads(raw_text), indent=2))
        except json.JSONDecodeError:
            print(raw_text)
    else:
        print("(empty)")
    print("\n--- PARSED (pretty) ---")
    print(json.dumps(parsed, indent=2) if parsed else "(parse failed)")
    if parsed:
        merged = merge_llm_format_response(parsed)
        meta = merged.get("_meta", {})
        layout = merged.get("layout", {})
        caption = merged.get("structural", {}).get("caption", {})
        print("\n--- MERGED SPEC (_meta + layout + caption) ---")
        print(
            json.dumps(
                {
                    "_meta": meta,
                    "structural": {
                        "require_caption_page": merged.get("structural", {}).get(
                            "require_caption_page"
                        ),
                        "require_numbered_paragraphs": merged.get("structural", {}).get(
                            "require_numbered_paragraphs"
                        ),
                    },
                    "caption": caption,
                    "font_name": layout.get("font_name"),
                    "font_size_pt": layout.get("font_size_pt"),
                    "page_margins_in": layout.get("page_margins_in"),
                    "body": layout.get("body"),
                },
                indent=2,
            )
        )


@pytest.mark.parametrize("scenario", LIVE_SCENARIOS, ids=[s["label"] for s in LIVE_SCENARIOS])
def test_live_llm_format_requirements_by_jurisdiction(scenario):
    """Call real LLM; print raw layout JSON for federal / California / NYC drafts."""
    raw_text, parsed = fetch_raw_format_requirements_llm(
        scenario["draft"],
        constraints=scenario.get("constraints"),
        conversation_brief=scenario.get("brief"),
    )
    _print_scenario_result(scenario["label"], raw_text, parsed)
    assert raw_text, "Expected non-empty LLM response"
    assert parsed, "Expected parseable JSON from LLM"
    assert "confidence" in parsed
    assert "jurisdiction" in parsed


def main() -> int:
    if not os.getenv("OPENAI_API_KEY"):
        print("Set OPENAI_API_KEY to run live pleading format tests.", file=sys.stderr)
        return 1
    for scenario in LIVE_SCENARIOS:
        raw_text, parsed = fetch_raw_format_requirements_llm(
            scenario["draft"],
            constraints=scenario.get("constraints"),
            conversation_brief=scenario.get("brief"),
        )
        _print_scenario_result(scenario["label"], raw_text, parsed)
        if not parsed:
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
