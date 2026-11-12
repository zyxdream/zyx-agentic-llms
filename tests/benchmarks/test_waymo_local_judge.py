"""Offline safeguards for the separate local Qwen judge; no provider calls."""

from __future__ import annotations

import io
import json
import random
import urllib.error
import uuid
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.waymo import core, judge, local_judge


def fixture_data() -> dict:
    return {
        "version": 2,
        "fac": {
            "lines": ["First Amended Complaint", "1. Allegation"],
            "causes": [{"number": n, "title": f"Cause {n}"} for n in range(1, 8)],
            "paragraph_locations": {"1": 1},
        },
        "answer_input": {"default_choice": "deny"},
        "answer_reference": {"source_path": "/private/answer.pdf"},
        "rfp_reference": {
            "requests": [
                {"number": n, "text": f"SECRET AEO request {n}"}
                for n in range(1, 147)
            ],
        },
        "fixed_factual_digest": {"summary": "Fixed"},
        "fixed_element_summaries": {"cause_summaries": {"1": "Fixed"}},
    }


def source_data() -> dict:
    source = {"runs": {}, "comparison_judgments": {}}
    for product in core.PRODUCTS:
        for repeat in range(1, core.REPETITIONS + 1):
            rows = []
            for position, candidate in enumerate(core.CANDIDATES, start=1):
                run_id = f"{product}-{repeat}-{position}"
                key = f"{product}|{candidate.model}|{candidate.effort}|{repeat}"
                source["runs"][key] = {
                    "run_id": run_id,
                    "product": product,
                    "repetition": repeat,
                    "model": candidate.model,
                    "effort": candidate.effort,
                    "output": {"text": f"Candidate {position}"},
                    "duration_seconds": 1,
                    "error": None,
                    "review_file": f"/private/{run_id}.json",
                }
                rows.append({
                    "run_id": run_id,
                    "blind_id": str(uuid.UUID(int=position)),
                    "score": 75,
                    "rank": position,
                })
            source["comparison_judgments"][f"{product}|{repeat}|gpt-6-astra"] = {
                "evaluations": rows,
            }
    return source


def valid_response(payload: dict) -> dict:
    user_payload = json.loads(payload["messages"][1]["content"])
    product = user_payload["reference"]["product"]
    result = core.CohortResult(
        evaluations=[
            core.CohortEvaluation(
                blind_id=item["blind_id"],
                rank=position,
                criteria=[
                    core.Criterion(name=name, score=4, evidence="FAC allegation")
                    for name in core.RUBRICS[product]
                ],
                critical_errors=[],
                explanation="Compared with the FAC",
            )
            for position, item in enumerate(user_payload["candidates"], start=1)
        ],
        comparison_summary="Four-way comparison",
    )
    return {
        "done": True,
        "done_reason": "stop",
        "message": {"content": result.model_dump_json()},
        "prompt_eval_count": 37_500,
        "eval_count": 2_000,
    }


def loaded_context(product: str = "rfp") -> dict:
    return {"models": [{"name": local_judge.MODEL,
                        "context_length": local_judge.CONTEXT_BY_PRODUCT[product]}]}


def test_gguf_configuration_cannot_overwrite_earlier_mlx_results(tmp_path: Path):
    assert local_judge.MODEL == "qwen3.6:35b-a3b-q4_K_M"
    assert local_judge.INDEX_NAME == "qwen-gguf-index-v2.json"
    assert local_judge.REPORT_JSON == "qwen-gguf-report-v2.json"
    assert local_judge.REPORT_MARKDOWN == "qwen-gguf-report-v2.md"
    original = tmp_path / "qwen-index-v2.json"
    original.write_text('{"previous_model":"mlx"}', encoding="utf-8")

    index = local_judge._local_index(tmp_path, "fixture-fingerprint", "gguf-digest")
    assert index["model"] == local_judge.MODEL
    assert index["model_digest"] == "gguf-digest"
    local_judge._write_private(tmp_path / local_judge.INDEX_NAME, index)

    assert original.read_text(encoding="utf-8") == '{"previous_model":"mlx"}'
    assert (tmp_path / local_judge.INDEX_NAME).is_file()
    with pytest.raises(local_judge.LocalJudgeError, match="refusing to mix scores"):
        local_judge._local_index(tmp_path, "fixture-fingerprint", "changed-digest")


def test_qwen_reuses_astra_substantive_prompt_and_blind_order(monkeypatch):
    # This compares prompt content and order, not tokenizer behavior.
    # Avoid downloading a tokenizer in this offline, mocked-provider test.
    monkeypatch.setattr(judge, "num_tokens_from_string", lambda _: 100)
    fixture, source = fixture_data(), source_data()
    expected_prompt, mapping = local_judge._astra_user_payload(fixture, source, "rfp", 1)
    runs = local_judge._runs_for(source, "rfp", 1)
    shuffled = list(runs)
    random.Random("rfp|1|gpt-6-astra").shuffle(shuffled)
    blind_by_run = {run["run_id"]: blind_id for blind_id, run in mapping.items()}
    ids = iter(uuid.UUID(blind_by_run[run["run_id"]]) for run in shuffled)
    monkeypatch.setattr(judge.uuid, "uuid4", lambda: next(ids))
    captured = {}

    def parse(**kwargs):
        captured.update(kwargs)
        payload = json.loads(kwargs["input"][1]["content"])
        return SimpleNamespace(
            output_parsed=core.CohortResult(
                evaluations=[
                    core.CohortEvaluation(
                        blind_id=item["blind_id"],
                        rank=position,
                        criteria=[
                            core.Criterion(name=name, score=4, evidence="FAC allegation")
                            for name in core.RUBRICS["rfp"]
                        ],
                        critical_errors=[],
                        explanation="Grounded",
                    )
                    for position, item in enumerate(payload["candidates"], start=1)
                ],
                comparison_summary="Compared candidates",
            ),
            usage=SimpleNamespace(input_tokens=100, output_tokens=50),
        )

    judge.judge_comparison(
        fixture, runs, core.JUDGES[0],
        client=SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )
    assert captured["input"][0]["content"] == local_judge.SYSTEM_PROMPT
    assert captured["input"][1]["content"] == expected_prompt
    assert "SECRET AEO request 146" in expected_prompt
    messages, _ = local_judge._messages(fixture, source, "rfp", 1)
    assert messages[1]["content"] == expected_prompt
    assert messages[0]["content"].startswith(local_judge.SYSTEM_PROMPT)
    assert "Return only one valid JSON" in messages[0]["content"]


def test_repeat_one_preflight_uses_full_prompt_without_scoring_or_second_round(capsys):
    calls = []

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context()
        calls.append((path, payload))
        return {"done": True, "prompt_eval_count": 37_500, "eval_count": 1}

    index = {"preflight": {}, "comparison_judgments": {}, "judge_failures": {}}
    local_judge.preflight(
        fixture_data(), source_data(), index, product="rfp", repeat=1, request=request,
    )
    assert set(index["preflight"]) == {"rfp|1"}
    assert index["comparison_judgments"] == {}
    assert len(calls) == 1
    assert calls[0][0] == "/api/chat"
    assert calls[0][1]["model"] == local_judge.MODEL
    assert calls[0][1]["options"] == {"num_ctx": 65_536, "num_predict": 1}
    assert "format" not in calls[0][1]
    assert "SECRET AEO" in calls[0][1]["messages"][1]["content"]
    assert "SECRET AEO" not in capsys.readouterr().out


def test_repeat_one_evaluation_does_not_score_second_round(capsys):
    calls = []

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context()
        calls.append(payload)
        return valid_response(payload)

    index = {
        "preflight": {"rfp|1": {"status": "fits_prompt_only",
                                 "allocated_context_tokens": 65_536,
                                 "runtime_options": {"num_ctx": 65_536, "num_predict": 1}}},
        "comparison_judgments": {},
        "judge_failures": {},
    }
    local_judge.evaluate(
        fixture_data(), source_data(), index, product="rfp", repeat=1, request=request,
    )
    assert set(index["comparison_judgments"]) == {"rfp|1"}
    assert len(calls) == 1
    assert "SECRET AEO" not in capsys.readouterr().out
    local_judge.evaluate(
        fixture_data(), source_data(), index, product="rfp", repeat=1, request=request,
    )
    assert len(calls) == 1


def test_ollama_error_exposes_status_not_private_body_or_prompt(monkeypatch, capsys):
    def fail(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url, 500, "private server message", {},
            io.BytesIO(b"SECRET AEO request and private prompt"),
        )

    monkeypatch.setattr(local_judge.urllib.request, "urlopen", fail)
    with pytest.raises(local_judge.LocalJudgeError, match="^Ollama HTTP 500$") as exc:
        local_judge._ollama_request("/api/chat", {"messages": ["SECRET AEO prompt"]})
    assert "SECRET AEO" not in str(exc.value)
    assert "private server message" not in capsys.readouterr().out


def test_preflight_rejects_possible_ollama_truncation():
    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context()
        return {"done": True, "prompt_eval_count": 32_770, "eval_count": 1}

    with pytest.raises(local_judge.LocalJudgeError, match="truncated"):
        local_judge._preflight_one(fixture_data(), source_data(), "rfp", 1, request)


def test_gguf_report_stays_separate_from_astra_and_prior_qwen(tmp_path: Path):
    source, fixture = source_data(), fixture_data()
    local = {
        "model_digest": "gguf-digest",
        "preflight": {},
        "comparison_judgments": {},
        "judge_failures": {},
    }

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context()
        return valid_response(payload)

    local["comparison_judgments"]["rfp|1"] = local_judge._judge_one(
        fixture, source, "rfp", 1, request,
    )
    local_judge._save_report(tmp_path, source, local)
    report = json.loads((tmp_path / local_judge.REPORT_JSON).read_text(encoding="utf-8"))
    assert report["comparison_judgments"] == 1
    assert {row["comparison_score"] for row in report["runs"]
            if row["product"] == "rfp" and row["repetition"] == 1} == {80}
    assert all(row["comparison_score"] is None for row in report["runs"]
               if row["product"] == "answer")
    assert report["judge_model"] == local_judge.MODEL
    assert not (tmp_path / "qwen-report-v2.json").exists()
    assert not (tmp_path / "report-v2.json").exists()
    assert "SECRET AEO request" not in (tmp_path / local_judge.REPORT_JSON).read_text()


@pytest.mark.parametrize("product,expected_options", [
    ("answer", {"num_ctx": 262_144}),
    ("rfp", {"num_ctx": 65_536}),
    ("element_checker", {"num_ctx": 262_144, "num_batch": 256}),
])
def test_provider_options_and_full_prompt_are_recorded_for_both_paths(product, expected_options,
                                                                    monkeypatch):
    monkeypatch.setattr(judge, "_read_pdf", lambda _: "Filed Answer reference")
    fixture, source = fixture_data(), source_data()
    expected_messages, _ = local_judge._messages(fixture, source, product, 2)
    calls = []

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context(product)
        calls.append(payload)
        return valid_response(payload)

    probe = local_judge._preflight_one(fixture, source, product, 2, request)
    judgment = local_judge._judge_one(fixture, source, product, 2, request)

    assert len(calls) == 2
    assert all(call["messages"] == expected_messages for call in calls)
    assert all(call["model"] == local_judge.MODEL and "format" not in call for call in calls)
    assert calls[0]["options"] == {**expected_options, "num_predict": 1}
    assert calls[1]["options"] == expected_options
    assert probe["runtime_options"] == calls[0]["options"]
    assert judgment["runtime_options"] == calls[1]["options"]


@pytest.mark.parametrize("old_options", [
    None,
    {"num_ctx": 262_144, "num_batch": 1024, "num_predict": 1},
])
def test_element_checker_requires_new_preflight_after_runtime_change(old_options):
    prior = {"status": "fits_prompt_only", "allocated_context_tokens": 262_144}
    if old_options is not None:
        prior["runtime_options"] = old_options
    index = {"preflight": {"element_checker|2": prior},
             "comparison_judgments": {}, "judge_failures": {}}
    fixture, source = fixture_data(), source_data()
    calls = []

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context("element_checker")
        calls.append(payload)
        return valid_response(payload)

    local_judge.evaluate(fixture, source, index, product="element_checker", repeat=2, request=request)
    assert calls == []
    assert index["comparison_judgments"] == {}
    assert "rerun preflight" in index["judge_failures"]["element_checker|2"]

    local_judge.preflight(fixture, source, index, product="element_checker", repeat=2, request=request)
    assert len(calls) == 1
    local_judge.preflight(fixture, source, index, product="element_checker", repeat=2, request=request)
    assert len(calls) == 1  # A preflight with the current options can be reused.

    local_judge.evaluate(fixture, source, index, product="element_checker", repeat=2, request=request)
    assert len(calls) == 2
    assert set(index["comparison_judgments"]) == {"element_checker|2"}
    assert index["judge_failures"] == {}


@pytest.mark.parametrize("stage", ["preflight", "evaluate"])
def test_http_200_runtime_error_is_safe_unscored_and_not_retried(stage, monkeypatch, capsys):
    calls = []

    def urlopen(request, timeout=None):
        calls.append(request)
        return io.BytesIO(b'{"error":"Compute error SECRET AEO private prompt"}')

    monkeypatch.setattr(local_judge.urllib.request, "urlopen", urlopen)
    expected_options = {"num_ctx": 262_144, "num_batch": 256}
    index = {
        "model_digest": "gguf-digest",
        "preflight": {"element_checker|2": {
            "status": "fits_prompt_only", "allocated_context_tokens": 262_144,
            "runtime_options": {**expected_options, "num_predict": 1},
        }} if stage == "evaluate" else {},
        "comparison_judgments": {}, "judge_failures": {},
    }
    getattr(local_judge, stage)(
        fixture_data(), source_data(), index, product="element_checker", repeat=2,
        request=local_judge._ollama_request,
    )

    assert len(calls) == 1
    assert index["comparison_judgments"] == {}
    expected_error = "Ollama reported a runtime error; inspect the Ollama server log"
    if stage == "preflight":
        assert index["preflight"]["element_checker|2"]["failure_reason"] == expected_error
        assert index["preflight"]["element_checker|2"]["runtime_options"] == {
            **expected_options, "num_predict": 1,
        }
    else:
        assert index["judge_failures"]["element_checker|2"] == expected_error
        data = local_judge.report(source_data(), index)
        failed_rows = [row for row in data["runs"]
                       if row["product"] == "element_checker" and row["repetition"] == 2]
        assert all(row["comparison_score"] is None for row in failed_rows)
        assert all(row["judge_runtime_options"] == expected_options for row in failed_rows)
    assert "SECRET AEO" not in json.dumps(index)
    assert "SECRET AEO" not in capsys.readouterr().out


def test_runtime_change_preserves_four_completed_comparisons_and_report(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(judge, "_read_pdf", lambda _: "Filed Answer reference")
    fixture, source = fixture_data(), source_data()

    def old_request(path, payload=None):
        if path == "/api/ps":
            return loaded_context("answer")
        return valid_response(payload)

    completed = {
        f"{product}|{repeat}": local_judge._judge_one(fixture, source, product, repeat, old_request)
        for product in ("answer", "rfp") for repeat in (1, 2)
    }
    # Existing completed judgments predate runtime-option recording.
    for judgment in completed.values():
        judgment.pop("runtime_options")
    original = deepcopy(completed)
    index = {"model_digest": "gguf-digest", "preflight": {},
             "comparison_judgments": completed, "judge_failures": {}}
    calls = []

    def request(path, payload=None):
        if path == "/api/ps":
            return loaded_context("element_checker")
        calls.append(payload)
        return valid_response(payload)

    local_judge.preflight(fixture, source, index, product="all", repeat=2, request=request)
    local_judge.evaluate(fixture, source, index, product="all", repeat=2, request=request)
    local_judge._write_private(tmp_path / local_judge.INDEX_NAME, index)
    local_judge._save_report(tmp_path, source, index)

    assert len(calls) == 2  # Only the unscored Element Checker round is processed.
    assert all(index["comparison_judgments"][key] == value for key, value in original.items())
    assert len(json.loads((tmp_path / local_judge.INDEX_NAME).read_text())["comparison_judgments"]) == 5
    data = json.loads((tmp_path / local_judge.REPORT_JSON).read_text())
    assert data["comparison_judgments"] == 5
    legacy_rows = [row for row in data["runs"] if row["product"] in ("answer", "rfp")]
    assert all(row["comparison_score"] == 80 and row["judge_runtime_options"] is None
               for row in legacy_rows)
    current_rows = [row for row in data["runs"]
                    if row["product"] == "element_checker" and row["repetition"] == 2]
    assert all(row["judge_runtime_options"] == {"num_ctx": 262_144, "num_batch": 256}
               for row in current_rows)
    assert data["preflight"]["element_checker|2"]["runtime_options"] == {
        "num_ctx": 262_144, "num_batch": 256, "num_predict": 1,
    }
