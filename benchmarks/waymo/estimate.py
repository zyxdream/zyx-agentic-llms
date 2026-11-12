"""Preflight estimate for the 24-run Waymo pilot; not a spending guarantee."""

from __future__ import annotations

from benchmarks.waymo.core import CANDIDATES, JUDGES, PRODUCTS, REPETITIONS, estimate_cost


def estimate_pilot(multiplier: float = 1.0) -> dict:
    """Standard, uncached text pricing, with explicit token assumptions.

    Per product run: 300K input + 30K output across all its provider calls.
    Per four-way comparison judge, including the filed reference where one
    exists: 180K input + 12K output, 3 products x 2 repetitions x 2 judges.
    Output includes billed reasoning tokens. Preparation calls and retries are
    excluded; use a higher multiplier if their volume is uncertain.
    """
    def item(model: str, input_tokens: int, output_tokens: int) -> dict:
        return {"model": model, "input_tokens": round(input_tokens * multiplier),
                "output_tokens": round(output_tokens * multiplier)}

    # Price per assumed call/run rather than aggregating tokens; the >272K
    # surcharge applies to an individual provider request, not a whole pilot.
    product_runs = len(PRODUCTS) * len(CANDIDATES) * REPETITIONS
    comparisons = len(PRODUCTS) * REPETITIONS * len(JUDGES)
    candidates = [item(candidate.model, 150_000, 15_000)
                  for candidate in CANDIDATES
                  for _ in range(len(PRODUCTS) * REPETITIONS * 2)]
    comparison_judges = [item(judge.model, 180_000, 12_000)
                         for judge in JUDGES
                         for _ in range(len(PRODUCTS) * REPETITIONS)]
    components = {
        "candidates_usd": estimate_cost(candidates),
        "comparison_judges_usd": estimate_cost(comparison_judges),
    }
    return {
        "assumptions": {
            "product_runs": product_runs, "comparison_judgments": comparisons,
            "candidate_tokens_per_product_run": [round(300_000 * multiplier), round(30_000 * multiplier)],
            "comparison_judge_tokens_per_call": [round(180_000 * multiplier), round(12_000 * multiplier)],
            "excludes": "frozen preprocessing, retries, cache effects, and regional/priority surcharges",
        },
        **components,
        "total_usd": round(sum(components.values()), 2),
    }


if __name__ == "__main__":
    import json
    print(json.dumps({"base": estimate_pilot(), "double_tokens": estimate_pilot(2)}, indent=2))
