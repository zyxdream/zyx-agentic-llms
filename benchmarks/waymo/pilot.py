"""Resumable Waymo generation and two blind comparison rounds.

The stages are separate: generate all 24 outputs first, then compare the four
models together with a labeled filed reference for each repetition.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from statistics import mean

from benchmarks.waymo.core import (
    CANDIDATES, JUDGES, PRODUCTS, REPETITIONS, candidate_view, load_prepared,
)
from benchmarks.waymo.estimate import estimate_pilot
from benchmarks.waymo.judge import judge_comparison
from benchmarks.waymo.prepare import PRIVATE_DIR


ROOT = Path(__file__).resolve().parents[2]
PRODUCT_HARD_BUDGETS = {"answer": 2400, "element_checker": 1260, "rfp": 1890}
INDEX_NAME = "index-v2.json"


def _protocol_fingerprint(fixture: dict) -> str:
    digest = hashlib.sha256(json.dumps(fixture, ensure_ascii=False, sort_keys=True).encode("utf-8"))
    for root in (
        ROOT / "backend/answer_drafter.py",
        ROOT / "backend/element_checker.py",
        ROOT / "backend/rfp_drafter.py",
        ROOT / "backend/ai/base.py",
        ROOT / "backend/ai/prompts/answer",
        ROOT / "backend/ai/prompts/element_checker",
        ROOT / "backend/ai/prompts/rfp",
        ROOT / "benchmarks/waymo/core.py",
        ROOT / "benchmarks/waymo/run.py",
        ROOT / "benchmarks/waymo/judge.py",
    ):
        paths = sorted(root.rglob("*")) if root.is_dir() else [root]
        for path in paths:
            if path.is_file():
                digest.update(str(path.relative_to(ROOT)).encode("utf-8"))
                digest.update(path.read_bytes())
    return digest.hexdigest()


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _key(product: str, model: str, effort: str, repetition: int) -> str:
    return f"{product}|{model}|{effort}|{repetition}"


def _expected_keys() -> set[str]:
    return {_key(product, candidate.model, candidate.effort, repetition)
            for product in PRODUCTS for candidate in CANDIDATES
            for repetition in range(1, REPETITIONS + 1)}


def _index(output_dir: Path, fixture: dict) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    output_dir.chmod(0o700)
    fingerprint = _protocol_fingerprint(fixture)
    path = output_dir / INDEX_NAME
    if path.exists():
        index = json.loads(path.read_text(encoding="utf-8"))
        if index.get("protocol_fingerprint") != fingerprint:
            raise ValueError("Fixture, prompts, or benchmark code changed; cannot mix results")
        return index
    index = {"protocol_fingerprint": fingerprint, "runs": {},
             "comparison_judgments": {}, "judge_failures": {}}
    _save(path, index)
    return index


def _persist_index(output_dir: Path, index: dict) -> None:
    _save(output_dir / INDEX_NAME, index)


def _known_cost(index: dict) -> float:
    return round(
        sum(run.get("cost_usd", 0) for run in index["runs"].values())
        + sum(result["usage"]["cost_usd"] for result in index["comparison_judgments"].values()),
        4,
    )


def _warn_if_cost_high(index: dict, warn_cost_usd: float) -> None:
    known_cost = _known_cost(index)
    if warn_cost_usd > 0 and known_cost >= warn_cost_usd:
        print(
            f"WARNING: locally estimated known usage ${known_cost:.2f} reached "
            f"the ${warn_cost_usd:.2f} warning threshold. "
            "The benchmark will continue; this is not the billed API cost.",
            flush=True,
        )


def _execute_candidate(fixture: dict, product: str, candidate, repetition: int,
                       *, output_dir: Path) -> dict:
    """Hard-stop one isolated run; Celery is not involved in this harness."""
    fixture_path = output_dir / "run-fixture-v2.json"
    # The candidate subprocess must not even load the filed Answer or AEO RFP.
    # Write a separate allowlisted fixture, not the full judge fixture.
    _save(fixture_path, {key: fixture[key] for key in (
        "version", "fac", "answer_input", "fixed_factual_digest", "fixed_element_summaries",
    )})
    pointer = output_dir / f"pointer-{uuid.uuid4()}.json"
    command = [
        sys.executable, "-m", "benchmarks.waymo.run",
        "--fixture", str(fixture_path), "--product", product,
        "--candidate", str(CANDIDATES.index(candidate)), "--repetition", str(repetition),
        "--result-pointer", str(pointer), "--execute-paid",
    ]

    def failed_run(kind: str, duration: int) -> dict:
        run_id = str(uuid.uuid4())
        review_dir = output_dir / "candidate_outputs" / product / f"{candidate.model}-{candidate.effort}"
        review_file = review_dir / f"repeat-{repetition}.json"
        review_markdown = review_dir / f"repeat-{repetition}.md"
        if not review_file.exists():
            _save(review_file, {"run_id": run_id, "product": product,
                                "model": candidate.model, "effort": candidate.effort,
                                "repetition": repetition, "output": None,
                                "error": {"class": kind}})
        if not review_markdown.exists():
            review_markdown.write_text(f"# {product} — {candidate.model}/{candidate.effort} — repeat {repetition}\n\nRun failed: {kind}\n", encoding="utf-8")
            review_markdown.chmod(0o600)
        return {
            "run_id": run_id, "product": product, "model": candidate.model,
            "effort": candidate.effort, "repetition": repetition,
            "duration_seconds": duration, "cost_usd": 0.0,
            "cost_unknown": True, "review_file": str(review_file),
            "review_markdown_file": str(review_markdown),
            "output": None, "error": {"class": kind},
        }

    try:
        completed = subprocess.run(
            command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=PRODUCT_HARD_BUDGETS[product], check=False,
        )
        if completed.returncode != 0 or not pointer.is_file():
            raise RuntimeError("Candidate subprocess failed before saving a complete run")
        run_id = json.loads(pointer.read_text(encoding="utf-8"))["run_id"]
        return json.loads((output_dir / f"run-{run_id}.json").read_text(encoding="utf-8"))
    except subprocess.TimeoutExpired:
        return failed_run("BenchmarkHardTimeout", PRODUCT_HARD_BUDGETS[product])
    except (OSError, RuntimeError, ValueError, KeyError) as exc:
        return failed_run(type(exc).__name__, 0)
    finally:
        pointer.unlink(missing_ok=True)


def generate_all(fixture: dict, *, output_dir: Path, warn_cost_usd: float) -> dict:
    candidate_view(fixture)
    index = _index(output_dir, fixture)
    _warn_if_cost_high(index, warn_cost_usd)
    for product in PRODUCTS:
        for candidate in CANDIDATES:
            for repetition in range(1, REPETITIONS + 1):
                key = _key(product, candidate.model, candidate.effort, repetition)
                if key in index["runs"]:
                    continue
                index["runs"][key] = _execute_candidate(
                    fixture, product, candidate, repetition, output_dir=output_dir,
                )
                _persist_index(output_dir, index)
                _warn_if_cost_high(index, warn_cost_usd)
    _write_report(output_dir, index)
    return index


def evaluate_all(fixture: dict, *, output_dir: Path, warn_cost_usd: float) -> dict:
    candidate_view(fixture)
    index = _index(output_dir, fixture)
    _warn_if_cost_high(index, warn_cost_usd)
    missing = _expected_keys() - set(index["runs"])
    if missing:
        raise ValueError(f"Generate all 24 outputs first; {len(missing)} runs are missing")
    for product in PRODUCTS:
        for repetition in range(1, REPETITIONS + 1):
            runs = [index["runs"][_key(product, c.model, c.effort, repetition)]
                    for c in CANDIDATES]
            if any(run.get("error") for run in runs):
                continue
            for judge in JUDGES:
                key = f"{product}|{repetition}|{judge.model}"
                if key in index["comparison_judgments"]:
                    continue
                try:
                    result = judge_comparison(fixture, runs, judge)
                except Exception as exc:
                    # Provider exceptions may echo the AEO RFP. Save only
                    # the class, never its body or request prompt.
                    index["judge_failures"][key] = type(exc).__name__
                else:
                    index["comparison_judgments"][key] = result
                    index["judge_failures"].pop(key, None)
                _persist_index(output_dir, index)
                _warn_if_cost_high(index, warn_cost_usd)
    _write_report(output_dir, index)
    return index


def _mean_or_none(values: list[float]) -> float | None:
    return round(mean(values), 2) if values else None


def report(index: dict) -> dict:
    rows = []
    for run in index["runs"].values():
        comparisons = []
        comparison_cost = 0.0
        for judge in JUDGES:
            key = f"{run['product']}|{run['repetition']}|{judge.model}"
            if key in index["comparison_judgments"]:
                group = index["comparison_judgments"][key]
                comparisons += [item for item in group["evaluations"] if item["run_id"] == run["run_id"]]
                comparison_cost += group["usage"]["cost_usd"] / 4
        comparison_score = _mean_or_none([rating["score"] for rating in comparisons]) if len(comparisons) == len(JUDGES) else None
        row = {
            "run_id": run["run_id"], "product": run["product"],
            "model": run["model"], "effort": run["effort"],
            "repetition": run["repetition"], "review_file": run.get("review_file"),
            "review_markdown_file": run.get("review_markdown_file"),
            "comparison_round": f"comparison_repeat_{run['repetition']}",
            "comparison_score": comparison_score,
            "judge_scores": {judge.model: next(
                (item["score"] for item in comparisons
                 if item["run_id"] == run["run_id"] and item.get("judge_model") == judge.model), None)
                for judge in JUDGES},
            "judge_ranks": {judge.model: next(
                (item["rank"] for item in comparisons
                 if item["run_id"] == run["run_id"] and item.get("judge_model") == judge.model), None)
                for judge in JUDGES},
            "mean_rank": _mean_or_none([item["rank"] for item in comparisons])
            if len(comparisons) == len(JUDGES) else None,
            "judge_evaluations": {item["judge_model"]: item["result"] for item in comparisons},
            "critical_errors": list(dict.fromkeys(
                error for rating in comparisons
                for error in rating["result"]["critical_errors"]
            )),
            "latency_seconds": run["duration_seconds"],
            "cost_usd": round(run.get("cost_usd", 0) + comparison_cost, 4),
            "cost_unknown": bool(run.get("cost_unknown")),
            "failure": run.get("error"),
        }
        rows.append(row)
    rows.sort(key=lambda row: (row["product"], row["repetition"], row["model"], row["effort"]))
    summaries = []
    for product in PRODUCTS:
        for candidate in CANDIDATES:
            selected = [row for row in rows if row["product"] == product
                        and (row["model"], row["effort"]) == (candidate.model, candidate.effort)]
            def aggregate(field: str) -> dict:
                values = [row[field] for row in selected if row[field] is not None]
                return {"mean": _mean_or_none(values),
                        "range": [min(values), max(values)] if values else None,
                        "scored_runs": len(values)}
            summaries.append({
                "product": product, "model": candidate.model, "effort": candidate.effort,
                "comparison": aggregate("comparison_score"),
                "rank": aggregate("mean_rank"),
                "failures": sum(bool(row["failure"]) for row in selected),
                "cost_usd": round(sum(row["cost_usd"] for row in selected), 4),
            })
    return {
        "protocol": "waymo-v2-two-run-comparison", "generated_runs": len(index["runs"]),
        "comparison_judgments": len(index["comparison_judgments"]),
        "rounds": ["comparison_repeat_1", "comparison_repeat_2"],
        "runs": rows, "model_summary": summaries,
        "judge_failures": index["judge_failures"],
        "total_known_cost_usd": round(_known_cost(index), 2),
        "cost_estimate_incomplete": bool(index["judge_failures"])
        or any(row["cost_unknown"] for row in rows)
        or len(index["comparison_judgments"]) < len(PRODUCTS) * REPETITIONS * len(JUDGES),
        "reference_note": "Each judge sees four anonymous candidates and a separately labeled filed Answer or RFP as context, not a fifth ranked candidate. Answer comparisons use only clear filed denials, not admissions or other lawyer-selected responses. Element Checker uses the FAC because no filed analysis exists.",
        "human_review": "All 24 candidate output files are preserved for separate lawyer review; no lawyer approval is a run gate.",
    }


def _render_markdown(data: dict) -> str:
    lines = ["# Waymo guided-drafter benchmark", "",
             f"Generated: {data['generated_runs']}/24 outputs; four-way comparison judgments: {data['comparison_judgments']}/12.",
             f"Known cost: ${data['total_known_cost_usd']:.2f}"
             + (" (incomplete: a hard-stopped run may have unreported usage)" if data["cost_estimate_incomplete"] else ""),
             "", data["reference_note"], "",
             "Scores are 0–100. The filed document is context only; only the four generated outputs receive scores and ranks.", ""]
    for product in PRODUCTS:
        lines += [f"## {product}", "", "### Per-run round scores", "",
                  "| Model | Effort | Repeat | Comparison score | Mean rank | Critical errors | Cost | Output |",
                  "|---|---|---:|---:|---:|---:|---:|---|"]
        for row in (item for item in data["runs"] if item["product"] == product):
            def fmt(value):
                return "—" if value is None else str(value)
            output = row["review_markdown_file"] or row["review_file"] or "—"
            lines.append(f"| {row['model']} | {row['effort']} | {row['repetition']} | "
                         f"{fmt(row['comparison_score'])} | {fmt(row['mean_rank'])} | "
                         f"{len(row['critical_errors'])} | "
                         f"${row['cost_usd']:.4f} | `{output}` |")
        lines += ["", "### Two-run model summary", "",
                  "| Model | Effort | Comparison mean [range] | Rank mean [range] | Failures |",
                  "|---|---|---|---|---:|"]
        for item in (entry for entry in data["model_summary"] if entry["product"] == product):
            def summary(section):
                return "—" if section["mean"] is None else f"{section['mean']} {section['range']}"
            lines.append(f"| {item['model']} | {item['effort']} | {summary(item['comparison'])} | "
                         f"{summary(item['rank'])} | "
                         f"{item['failures']} |")
        lines.append("")
    if data["judge_failures"]:
        lines += ["## Judge calls needing rerun", ""]
        lines += [f"- `{key}`: {kind}" for key, kind in sorted(data["judge_failures"].items())]
    lines += ["", "The report JSON retains both judges' individual scores, ranks, evidence, and critical errors for each of the two rounds. Review all candidate output files with a lawyer before any production model decision.", ""]
    return "\n".join(lines)


def _write_report(output_dir: Path, index: dict) -> dict:
    data = report(index)
    _save(output_dir / "report-v2.json", data)
    path = output_dir / "report-v2.md"
    path.write_text(_render_markdown(data), encoding="utf-8")
    path.chmod(0o600)
    return data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", choices=("generate", "evaluate", "report"), required=True)
    parser.add_argument("--fixture", type=Path, default=PRIVATE_DIR / "prepared-v2.json")
    parser.add_argument("--warn-cost-usd", "--max-cost-usd", dest="warn_cost_usd",
                        type=float, default=0,
                        help="Optional local estimated-cost warning; never stops the benchmark. "
                             "--max-cost-usd is a deprecated alias.")
    parser.add_argument("--execute-paid", action="store_true")
    args = parser.parse_args()
    fixture = load_prepared(args.fixture)
    estimate = estimate_pilot()
    print(json.dumps({"illustrative_estimate_usd": estimate["total_usd"],
                      "local_estimated_cost_warning_usd": args.warn_cost_usd}, indent=2), flush=True)
    if args.stage != "report":
        print(
            "Cost notice: the optional local warning uses returned token usage only. "
            "It never stops the benchmark and is not an API billing cap. "
            "Review billed cost in the OpenAI Platform if desired.",
            flush=True,
        )
    if "--max-cost-usd" in sys.argv:
        print("--max-cost-usd is deprecated and now warns only; it never stops the benchmark.", flush=True)
    try:
        candidate_view(fixture)
    except ValueError as exc:
        raise SystemExit(f"Benchmark preparation is incomplete: {exc}") from None
    if args.stage != "report":
        if not args.execute_paid:
            raise SystemExit("Paid stages require --execute-paid")
        if args.warn_cost_usd < 0:
            raise SystemExit("--warn-cost-usd must be nonnegative")
        if not os.environ.get("OPENAI_API_KEY"):
            raise SystemExit("OPENAI_API_KEY was not loaded from the root .env")
    if args.stage == "generate":
        index = generate_all(fixture, output_dir=PRIVATE_DIR, warn_cost_usd=args.warn_cost_usd)
    elif args.stage == "evaluate":
        index = evaluate_all(fixture, output_dir=PRIVATE_DIR, warn_cost_usd=args.warn_cost_usd)
    else:
        index = _index(PRIVATE_DIR, fixture)
    data = _write_report(PRIVATE_DIR, index)
    print(json.dumps({"generated_runs": data["generated_runs"],
                      "comparison_judgments": data["comparison_judgments"],
                      "known_cost_usd": data["total_known_cost_usd"],
                      "report": str(PRIVATE_DIR / "report-v2.md")}, indent=2))


if __name__ == "__main__":
    main()
