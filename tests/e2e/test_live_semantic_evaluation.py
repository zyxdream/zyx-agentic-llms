import json

from tests.e2e.run_live_semantic_evaluation import (
    FIXTURE_PATH,
    _judge_reference,
    _semantic_failure_message,
    run,
)


def test_live_runner_writes_failure_report_when_api_key_is_missing(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("E2E_ARTIFACT_DIR", str(tmp_path))

    assert run() == 1

    report = json.loads((tmp_path / "semantic-evaluation.json").read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["error"]["type"] == "RuntimeError"
    assert "OPENAI_API_KEY" in report["error"]["message"]


def test_judge_reference_includes_every_generated_fixture_fact():
    case = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]

    reference = _judge_reference(case)

    assert reference["attorneys_contact_info"] == case["attorneys_contact_info"]
    assert reference["introduction"] == case["llm"]["introduction"]
    assert reference["parties"] == case["llm"]["parties"]
    assert reference["jurisdiction"] == case["llm"]["jurisdiction"]
    assert reference["venue_allegation"] == case["llm"]["venue"]


def test_semantic_failure_message_distinguishes_verdict_from_score():
    message = _semantic_failure_message(
        "northern-california",
        {
            "judge_passed": False,
            "score": 0.86,
            "threshold": 0.80,
            "unsupported_claims": ["Unsupported jurisdiction allegation"],
            "missing_facts": [],
        },
    )

    assert "semantic judge returned passed=false" in message
    assert "score 0.86 was below" not in message
    assert "Unsupported jurisdiction allegation" in message
