"""Offline safeguards for the private Waymo pilot; no provider calls."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.waymo import core, estimate, freeze, judge, pilot, prepare, run


def fixture_data() -> dict:
    return {
        "version": 2,
        "fac": {
            "lines": ["First Amended Complaint", "1.", "Allegation one", "2.", "Allegation two"],
            "causes": [{"number": n, "title": f"Cause {n}", "start_line": n,
                        "end_line": n + 1} for n in range(1, 8)],
            "paragraph_locations": {"1": 1, "2": 3},
        },
        "answer_input": {"default_choice": "deny"},
        "answer_reference": {"source_path": "/private/reference-answer.pdf"},
        "rfp_reference": {
            "requests": [{"number": n, "text": f"SECRET AEO request {n}"}
                         for n in range(1, 147)],
        },
        "fixed_factual_digest": {"summary": "Frozen factual summary"},
        "fixed_element_summaries": {"factual_summary": "Frozen factual summary",
                                    "cause_summaries": {str(n): f"Claim {n}\nDescription" for n in range(7)}},
    }


def test_source_documents_align_and_ocr_yields_all_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(prepare, "PRIVATE_DIR", tmp_path / "prepared")
    fixture = core.load_prepared(prepare.prepare())
    fac = fixture["fac"]
    rfp = fixture["rfp_reference"]
    assert len(fac["causes"]) == 7
    assert len(fac["paragraph_locations"]) == 158
    assert [row["number"] for row in rfp["requests"]] == list(range(1, 147))
    assert rfp["ocr_pages"] == [9, 10]
    assert fac["source_path"] == str(prepare.FAC)
    assert fixture["answer_reference"]["source_path"] == str(prepare.ANSWER)
    assert rfp["source_path"] == str(prepare.RFP)
    assert prepare.FAC.parent == prepare.ROOT / "tests/e2e/fixtures"
    assert run.FAC_FILENAME == prepare.FAC.name
    fixture["fixed_factual_digest"] = {"summary": "Frozen facts"}
    fixture["fixed_element_summaries"] = {"factual_summary": "Frozen facts"}
    candidate = core.candidate_view(fixture)
    assert "answer_reference" not in candidate
    assert "rfp_reference" not in candidate


def test_fac_only_candidate_allowlist_excludes_filed_reference():
    fixture = fixture_data()
    candidate = core.candidate_view(fixture)
    assert "rfp_reference" not in candidate
    assert "answer_choices" in candidate
    assert {row["choice"] for row in candidate["answer_choices"].values()} == {"deny"}
    assert "SECRET AEO" not in repr(candidate)
    assert "/private/reference-answer.pdf" not in repr(candidate)
    fixture["version"] = 1
    with pytest.raises(ValueError, match="v2 preparer"):
        core.candidate_view(fixture)
    fixture["version"] = 2
    fixture["fixed_factual_digest"] = None
    with pytest.raises(ValueError, match="Frozen factual digest"):
        core.candidate_view(fixture)


def test_rfp_target_is_exact_and_never_padded():
    rows = [{"Request": f"Request {n}"} for n in range(30)]
    core.validate_final_rfps(rows, candidate_count=40)
    with pytest.raises(ValueError, match="incomplete"):
        core.validate_final_rfps(rows, candidate_count=29)
    with pytest.raises(ValueError, match="exactly 30"):
        core.validate_final_rfps(rows[:29], candidate_count=40)
    with pytest.raises(ValueError, match="distinct"):
        core.validate_final_rfps(rows[:29] + [rows[0]], candidate_count=40)


def test_two_run_preflight_counts_candidates_and_combined_judgments():
    assumptions = estimate.estimate_pilot()["assumptions"]
    assert core.REPETITIONS == 2
    assert assumptions["product_runs"] == 24
    assert assumptions["comparison_judgments"] == 12


def test_local_cost_threshold_warns_without_stopping(capsys):
    index = {"runs": {"first": {"cost_usd": 4}}, "comparison_judgments": {}}
    pilot._warn_if_cost_high(index, 5)
    assert capsys.readouterr().out == ""
    pilot._warn_if_cost_high(index, 4)
    warning = capsys.readouterr().out
    assert "WARNING" in warning
    assert "benchmark will continue" in warning
    assert "not the billed API cost" in warning


def test_weighted_judge_score_requires_evidence_and_exact_rubric():
    assert core.RUBRICS["answer"] == {
        "paragraph_coverage_and_choice_fidelity": 25,
        "complaint_grounding": 30,
        "defense_quality": 35,
        "clarity": 10,
    }
    assert core.RUBRICS["element_checker"] == {
        "element_coverage": 30,
        "complaint_grounding": 25,
        "recommendations": 30,
        "authority_accuracy": 15,
    }
    criteria = [core.Criterion(name=name, score=4, evidence="FAC ¶1 and response ¶1")
                for name in core.RUBRICS["answer"]]
    result = core.JudgeResult(criteria=criteria, critical_errors=[], explanation="Grounded")
    assert core.weighted_score("answer", result) == 80
    defense_only = core.JudgeResult(
        criteria=[core.Criterion(name=name, score=5 if name == "defense_quality" else 0,
                                 evidence="FAC and draft")
                  for name in core.RUBRICS["answer"]],
        critical_errors=[], explanation="Defense-only check",
    )
    assert core.weighted_score("answer", defense_only) == 35
    with pytest.raises(ValueError, match="exactly once"):
        core.weighted_score("answer", core.JudgeResult(
            criteria=criteria[:-1], critical_errors=[], explanation="Incomplete"))


def test_rfp_comparison_receives_labeled_reference_but_no_candidate_model_identity():
    fixture = fixture_data()
    runs = [{"run_id": f"run-{n}", "product": "rfp", "model": candidate.model,
             "effort": candidate.effort, "repetition": 1,
             "output": {"final_requests": [{"Request": f"Candidate text {n}"}]},
             "error": None}
            for n, candidate in enumerate(core.CANDIDATES)]
    observed = {}

    def parse(**kwargs):
        observed.update(kwargs)
        candidates = json.loads(kwargs["input"][1]["content"])["candidates"]
        return SimpleNamespace(output_parsed=core.CohortResult(
            evaluations=[core.CohortEvaluation(
                blind_id=item["blind_id"], rank=index,
                criteria=[core.Criterion(name=name, score=4, evidence="FAC ¶1")
                          for name in core.RUBRICS["rfp"]],
                critical_errors=[], explanation="Case grounded")
                for index, item in enumerate(candidates, start=1)],
            comparison_summary="Compare FAC grounding"),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = judge.judge_comparison(fixture, runs, core.JUDGES[0], client=client)
    prompt = json.loads(observed["input"][1]["content"])
    assert "SECRET AEO request 146" in repr(prompt["reference"])
    assert "SECRET AEO" not in repr(prompt["candidates"])
    assert "gpt-5.5" not in repr(prompt)
    assert "fifth candidate" in prompt["task"]
    assert len(prompt["candidates"]) == 4
    assert observed["model"] == "gpt-6-astra"
    assert observed["reasoning"] == {"effort": "medium"}
    assert observed["text_format"] is core.CohortResult
    assert len(result["evaluations"]) == 4
    assert all(item["score"] == 80 for item in result["evaluations"])


def test_answer_comparison_includes_filed_answer_only_as_non_competing_reference(monkeypatch):
    fixture = fixture_data()
    monkeypatch.setattr(judge, "_read_pdf", lambda path: "PRIVATE FILED ANSWER KNOWLEDGE")
    assert "PRIVATE FILED ANSWER" not in repr(core.candidate_view(fixture))
    runs = [{"run_id": f"run-{n}", "product": "answer", "model": candidate.model,
             "effort": candidate.effort, "repetition": 1,
             "output": {"responses": f"Defendants deny Paragraph {n}."}}
            for n, candidate in enumerate(core.CANDIDATES, start=1)]
    observed = {}

    def parse(**kwargs):
        observed.update(kwargs)
        candidates = json.loads(kwargs["input"][1]["content"])["candidates"]
        return SimpleNamespace(output_parsed=core.CohortResult(
            evaluations=[core.CohortEvaluation(
                blind_id=item["blind_id"], rank=index,
                criteria=[core.Criterion(name=name, score=4, evidence="FAC paragraph and denial")
                          for name in core.RUBRICS["answer"]],
                critical_errors=[], explanation="Denial aligned")
                for index, item in enumerate(candidates, start=1)],
            comparison_summary="Compare denials"),
            usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = judge.judge_comparison(fixture, runs, core.JUDGES[0], client=client)
    prompt = json.loads(observed["input"][1]["content"])
    assert prompt["reference"]["filed_answer_reference_only"] == "PRIVATE FILED ANSWER KNOWLEDGE"
    assert "PRIVATE FILED ANSWER" not in repr(prompt["candidates"])
    assert "only for paragraphs where it itself gives a clear denial" in prompt["reference"]["filed_answer_comparison_scope"]
    assert "Ignore filed admissions, insufficient-knowledge responses" in prompt["reference"]["filed_answer_comparison_scope"]
    assert "Every candidate was instructed to deny" in prompt["reference"]["answer_scoring_rule"]
    assert "Do not score the merits of choosing deny" in prompt["reference"]["answer_scoring_rule"]
    assert "filed Answer only where it clearly denies" in prompt["scoring"]
    assert len(result["evaluations"]) == 4
    assert {item["run_id"] for item in result["evaluations"]} == {item["run_id"] for item in runs}


def test_element_checker_comparison_uses_fac_without_filed_document():
    fixture = fixture_data()
    runs = [{"run_id": f"run-{n}", "product": "element_checker", "model": candidate.model,
             "effort": candidate.effort, "repetition": 2,
             "output": {"analysis": f"Draft {n}"}}
            for n, candidate in enumerate(core.CANDIDATES)]
    prompts = {}

    def parse(**kwargs):
        prompts.update(kwargs)
        candidates = json.loads(kwargs["input"][1]["content"])["candidates"]
        return SimpleNamespace(output_parsed=core.CohortResult(
            evaluations=[core.CohortEvaluation(
                blind_id=item["blind_id"], rank=index,
                criteria=[core.Criterion(name=name, score=4, evidence="FAC ¶1")
                          for name in core.RUBRICS["element_checker"]],
                critical_errors=[], explanation="Grounded")
                for index, item in enumerate(candidates, start=1)],
            comparison_summary="Compare FAC grounding"),
            usage=SimpleNamespace(input_tokens=100, output_tokens=50))

    client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
    result = judge.judge_comparison(fixture, runs, core.JUDGES[0], client=client)
    assert len(result["evaluations"]) == 4
    assert {row["run_id"] for row in result["evaluations"]} == {row["run_id"] for row in runs}
    assert all(row["score"] == 80 for row in result["evaluations"])
    prompt = json.loads(prompts["input"][1]["content"])
    assert "SECRET AEO" not in repr(prompt)
    assert "filed_answer_reference_only" not in prompt["reference"]
    assert "filed_rfp_reference_only" not in prompt["reference"]
    assert "gpt-5.5" not in repr(prompt) and "gpt-6.1-sol" not in repr(prompt)
    assert prompts["text_format"] is core.CohortResult


def test_run_one_isolated_repetition_and_usage_without_reference(monkeypatch, tmp_path: Path):
    fixture = fixture_data()
    views = []

    def fake_product(view, candidate):
        views.append(copy.deepcopy(view))
        assert "SECRET AEO" not in repr(view)
        return {"responses": "Drafted output"}

    monkeypatch.setitem(run.PRODUCT_RUNNERS, "answer", fake_product)
    client = SimpleNamespace(responses=SimpleNamespace())
    first = run.run_one(fixture, "answer", core.CANDIDATES[0], 1,
                        client=client, output_dir=tmp_path)
    second = run.run_one(fixture, "answer", core.CANDIDATES[0], 2,
                         client=client, output_dir=tmp_path)
    assert first["run_id"] != second["run_id"]
    assert views[0] == views[1]
    assert len(list(tmp_path.glob("run-*.json"))) == 2
    assert len(list(tmp_path.glob("candidate_outputs/answer/gpt-5.5-medium/repeat-*.json"))) == 2
    assert len(list(tmp_path.glob("candidate_outputs/answer/gpt-5.5-medium/repeat-*.md"))) == 2
    assert all(row["output"]["responses"] == "Drafted output" for row in (first, second))
    assert run.run_one(fixture, "answer", core.CANDIDATES[0], 1,
                       client=client, output_dir=tmp_path)["run_id"] == first["run_id"]


def test_answer_adapter_uses_live_prompt_paths_with_candidate_route(monkeypatch):
    from backend import answer_drafter as answer
    from backend.ai.answer_schemas import (
        AnswerDefenseSuggestions, AnswerDraftedDefenses, AnswerParagraphResponses,
    )

    fixture = fixture_data()
    fixture["fac"].update({
        "cover": "Waymo LLC v. Uber Technologies, Inc.",
        "venue": "United States District Court, Northern District of California",
        "represented_defendants": ["Uber Technologies, Inc.", "Ottomotto LLC"],
    })
    calls = []

    def fake_parse(prompt, schema, *, model, reasoning_effort):
        calls.append((schema, model, reasoning_effort, prompt))
        if schema is AnswerParagraphResponses:
            return schema.model_validate({"Answers": [
                {"paragraph": str(n), "response": f"Defendants deny Paragraph {n}."}
                for n in (1, 2)
            ]})
        if schema is AnswerDefenseSuggestions:
            return schema.model_validate({"coa_id": "1", "coa_title": "Trade secrets",
                                          "affirmative_defenses": [{"name": "Failure to State a Claim",
                                                                    "description": "Insufficient allegations.",
                                                                    "supporting_facts": [], "citations": []}]})
        if schema is AnswerDraftedDefenses:
            return schema.model_validate({"defenses": [{"title": "Failure to State a Claim",
                                                         "paragraphs": "The complaint fails to state a claim."}]})
        raise AssertionError(schema)

    monkeypatch.setattr(answer, "chat_completion_parse", fake_parse)
    monkeypatch.setattr(answer, "num_tokens_from_string", lambda _: 1)
    monkeypatch.setattr(answer, "sleep", lambda _: None)
    output = run.run_answer(core.candidate_view(fixture), core.CANDIDATES[2])
    assert "Defendants deny Paragraph 1." in output["responses"]
    assert output["selected_defense_titles"] == ["Failure to State a Claim"]
    assert "The complaint fails to state a claim." in output["affirmative_defenses"]
    assert {schema for schema, *_ in calls} == {
        AnswerParagraphResponses, AnswerDefenseSuggestions, AnswerDraftedDefenses,
    }
    assert all((model, effort) == ("gpt-6.1-sol", "medium") for _, model, effort, _ in calls)


def test_rfp_adapter_runs_propose_consolidate_reduce_only(monkeypatch):
    from backend import rfp_drafter as rfp
    from backend.ai.discovery_request_schemas import RFPRequests

    fixture = fixture_data()
    fixture["fac"].update({
        "cover": "Waymo LLC v. Uber Technologies, Inc.",
        "venue": "United States District Court, Northern District of California",
        "represented_defendants": ["Uber Technologies, Inc.", "Ottomotto LLC"],
    })
    calls = []

    def fake_parse(prompt, schema, *, model, reasoning_effort):
        assert schema is RFPRequests
        calls.append((model, reasoning_effort, prompt))
        count = 40 if len(calls) in (1, 2) else 30
        return RFPRequests.model_validate({"Requests for Production of Documents": [
            {"Topic": f"Issue {n}", "Request": f"All documents concerning issue {n}",
             "Justification": "FAC allegation"} for n in range(1, count + 1)
        ]})

    monkeypatch.setattr(rfp, "chat_completion_parse", fake_parse)
    monkeypatch.setattr(rfp, "sleep", lambda _: None)
    output = run.run_rfp(core.candidate_view(fixture), core.CANDIDATES[3])
    assert output["proposal_and_consolidation_count"] == 40
    assert len(output["final_requests"]) == 30
    assert len(calls) == 3
    assert all((model, effort) == ("gpt-6.1-sol", "high") for model, effort, _ in calls)
    assert "target is 30" in calls[-1][2]


def test_element_adapter_varies_analysis_and_recommendation_only(monkeypatch):
    from backend import element_checker as element

    fixture = fixture_data()
    fixture["fac"].update({
        "venue": "United States District Court, Northern District of California",
        "represented_defendants": ["Uber Technologies, Inc.", "Ottomotto LLC"],
    })
    calls = []

    def fake_json(prompt, *, model, reasoning_effort):
        calls.append((model, reasoning_effort, prompt))
        if "Output the legal elements" in prompt:
            return '{"legal_elements":[{"element":"Duty","source":[]}]}'
        if "Evaluate each and every legal element" in prompt:
            return '{"element_analysis":[{"element":"Duty","element_id":1,"status":"Met","complaint_references":"Compl. ¶ 1","weakness":"","defense":"","citations":[]}]}'
        if "recommendations" in prompt:
            return '{"recommendations":["Duty appears sufficiently pleaded."]}'
        raise AssertionError("Unexpected Element Checker call")

    monkeypatch.setattr(element, "chat_completion_j", fake_json)
    monkeypatch.setattr(element, "sleep", lambda _: None)
    output = run.run_element_checker(core.candidate_view(fixture), core.CANDIDATES[0])
    assert len(output["analysis"]) == 7
    assert "Duty appears sufficiently pleaded." in output["recommendations"]
    assert len(calls) == 21
    assert all((model, effort) == ("gpt-5.5", "medium") for model, effort, _ in calls)


def test_cost_estimator_and_summary_keep_failed_runs():
    assert core.estimate_cost([{"model": "gpt-5.5", "input_tokens": 100_000,
                                "output_tokens": 100_000}]) == 3.5
    assert core.estimate_cost([{"model": "gpt-5.5", "input_tokens": 1_000_000,
                                "output_tokens": 1_000_000}]) == 55
    rows = [
        {"product": "answer", "model": "gpt-5.5", "effort": "medium", "score": 80, "cost_usd": 1},
        {"product": "answer", "model": "gpt-5.5", "effort": "medium", "score": 90, "cost_usd": 2},
        {"product": "answer", "model": "gpt-5.5", "effort": "medium", "score": None, "cost_usd": 0.5},
    ]
    assert core.summarize_runs(rows)[0] == {
        "product": "answer", "model": "gpt-5.5", "effort": "medium",
        "runs": 3, "successful": 2, "mean": 85, "range": [80, 90],
        "failed": 1, "cost_usd": 3.5,
    }


def test_pilot_saves_24_outputs_then_scores_two_comparison_rounds(monkeypatch, tmp_path):
    fixture = fixture_data()
    candidates = []
    comparisons = []

    def fake_run_one(fixture_arg, product, candidate, repetition, *, output_dir):
        candidates.append((product, candidate.model, candidate.effort, repetition))
        return {"run_id": f"run-{len(candidates)}", "product": product,
                "model": candidate.model, "effort": candidate.effort,
                "repetition": repetition, "duration_seconds": 1,
                "cost_usd": 0.01, "output": {"text": "Candidate"}, "error": None}

    def fake_comparison(fixture_arg, four_runs, judge_candidate):
        comparisons.append((four_runs[0]["product"], four_runs[0]["repetition"], judge_candidate.model))
        return {"judge_model": judge_candidate.model,
                "usage": {"cost_usd": 0.004},
                "evaluations": [{"run_id": item["run_id"], "judge_model": judge_candidate.model,
                                 "score": 75, "rank": 1, "result": {"critical_errors": []}}
                                for item in four_runs]}

    monkeypatch.setattr(pilot, "_execute_candidate", fake_run_one)
    monkeypatch.setattr(pilot, "judge_comparison", fake_comparison)
    pilot.generate_all(fixture, output_dir=tmp_path, warn_cost_usd=0.01)
    assert len(candidates) == 24 and not comparisons
    index = pilot.evaluate_all(fixture, output_dir=tmp_path, warn_cost_usd=0.01)
    result = pilot.report(index)
    assert len(candidates) == 24
    assert len(comparisons) == 12
    assert len(result["model_summary"]) == 12
    assert {row["comparison_score"] for row in result["runs"]} == {75}
    assert {row["mean_rank"] for row in result["runs"]} == {1}
    assert all(set(row["judge_scores"].values()) == {75}
               for row in result["runs"])
    assert result["rounds"] == ["comparison_repeat_1", "comparison_repeat_2"]
    assert all(row["comparison"]["scored_runs"] == 2 for row in result["model_summary"])
    pilot.generate_all(fixture, output_dir=tmp_path, warn_cost_usd=0.01)
    pilot.evaluate_all(fixture, output_dir=tmp_path, warn_cost_usd=0.01)
    assert len(candidates) == 24 and len(comparisons) == 12


def test_freeze_preprocessing_is_once_only_and_keeps_seven_causes(monkeypatch):
    from backend import answer_drafter as answer
    from backend import element_checker as element

    fixture = fixture_data()
    fixture["fac"]["source_path"] = str(prepare.FAC)
    fixture["fac"]["represented_defendants"] = ["Uber Technologies, Inc.", "Ottomotto LLC"]
    fixture["fac"]["venue"] = "Northern District of California"
    original_static_folder = element.STATIC_FOLDER
    monkeypatch.setattr(answer, "get_answer_factual_digest_from_llm",
                        lambda *a, **k: {"summary": "Frozen digest"})

    def fake_state0(filename, draft_id, side):
        assert filename == prepare.FAC.name
        fac_path = Path(element.STATIC_FOLDER) / filename
        assert fac_path == prepare.FAC
        with fac_path.open("rb") as fac_file:
            assert fac_file.read(4) == b"%PDF"
        memory = element.DBMemory(None, 1)
        memory["analysis/coa_sum"] = __import__("json").dumps({
            str(n): f"Claim {n}\nDescription" for n in range(7)
        })
        memory["drafted_sections/fa_sum"] = "Frozen element facts"
        memory["drafted_sections/fa_sum_final"] = "Short facts"
        return {"message": "Form processed successfully"}

    monkeypatch.setattr(element.state0, "run", fake_state0)
    result = freeze.freeze(fixture)
    assert element.STATIC_FOLDER == original_static_folder
    assert result["fixed_factual_digest"]["summary"] == "Frozen digest"
    assert result["fixed_element_summaries"]["factual_summary"] == "Frozen element facts"
    assert len(result["fixed_element_summaries"]["cause_summaries"]) == 7


def test_product_subprocess_budget_is_equal_across_candidate_models(monkeypatch, tmp_path):
    import subprocess

    observed = []

    def fake_subprocess(*args, **kwargs):
        observed.append(kwargs["timeout"])
        candidate_fixture = json.loads((tmp_path / "run-fixture-v2.json").read_text(encoding="utf-8"))
        assert "rfp_reference" not in candidate_fixture
        assert "answer_reference" not in candidate_fixture
        assert "SECRET AEO" not in repr(candidate_fixture)
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(pilot.subprocess, "run", fake_subprocess)
    results = [pilot._execute_candidate(
        fixture_data(), "rfp", candidate, 1, output_dir=tmp_path,
    ) for candidate in core.CANDIDATES]
    assert observed == [1890] * 4
    assert all(result["error"]["class"] == "BenchmarkHardTimeout" for result in results)
    assert all(result["cost_unknown"] for result in results)
