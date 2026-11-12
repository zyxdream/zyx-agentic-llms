from types import SimpleNamespace
from contextlib import nullcontext

import pytest

from backend import practice, research
from backend.ai import base as ai_base


@pytest.fixture(autouse=True)
def research_owner_boundary(monkeypatch):
    # These provider-contract tests mock the worker's account boundary.
    monkeypatch.setattr(research, 'account_execution', lambda _id: nullcontext())


@pytest.mark.parametrize(
    ("model", "provider_model", "effort"),
    [
        ("gpt-6.1-sol", "gpt-6.1-sol", "high"),
        ("gpt-6-sol", "gpt-6.1-sol", "high"),
        ("gpt-5.5", "gpt-5.5", None),
    ],
)
def test_research_uses_text_helper_with_selected_model(monkeypatch, model, provider_model, effort):
    captured = {}

    def fake_text(previous, prompt, **kwargs):
        captured.update(previous=previous, prompt=prompt, **kwargs)
        return "Research answer"

    monkeypatch.setattr(research, "multichat_completion_text", fake_text)
    monkeypatch.setattr(research, "filter_raw_citations", lambda _text: [])

    result = research.research_task.run({
        "model": model,
        "conversation": [{"input": "What is the rule?", "response": ""}],
    })

    assert result == {"response": "Research answer"}
    assert captured["previous"] == []
    assert "What is the rule?" in captured["prompt"]
    assert captured["model"] == provider_model
    assert captured["reasoning_effort"] == effort


def test_practice_chat_uses_text_helper_with_explicit_default_model(monkeypatch):
    captured = {}

    def fake_text(**kwargs):
        captured.update(kwargs)
        return "Mentor reply"

    monkeypatch.setattr(practice, "request", SimpleNamespace(json={"transcript": "My argument"}))
    monkeypatch.setattr(practice, "jsonify", lambda payload: payload)
    monkeypatch.setattr(practice, "multichat_completion_text", fake_text)

    result = practice.practice_chat.__wrapped__()

    assert result == {"response": "Mentor reply"}
    assert captured["prev_prompt_output"] == []
    assert "My argument" in captured["new_prompt"]
    assert captured["model"] == "gpt-6.1-sol"
    assert captured["reasoning_effort"] == "high"


@pytest.mark.parametrize("stage", ["reply", "coaching_summary"])
def test_voice_mentor_writing_sends_high_to_provider(monkeypatch, stage):
    calls = []
    summary = {
        "overall_assessment": "Clear argument.", "strengths": ["Structure"],
        "areas_to_improve": ["Authority"], "suggested_rewrite": "Revised argument.",
        "next_step": "Practice again.",
    }

    def create(**kwargs):
        import json
        calls.append(kwargs)
        return SimpleNamespace(
            id="voice-route", model=kwargs["model"], status="completed", output=[], usage=None,
            output_text="Mentor reply" if stage == "reply" else json.dumps(summary),
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(practice, "jsonify", lambda payload: payload)
    monkeypatch.setattr(practice, "request", SimpleNamespace(json={
        "transcript": "My argument",
        "turns": [{"user": "My argument", "mentor": "My feedback"}],
    }))

    if stage == "reply":
        assert practice.practice_chat.__wrapped__() == {"response": "Mentor reply"}
    else:
        assert practice.practice_summary.__wrapped__() == summary

    assert len(calls) == 1
    assert calls[0]["model"] == "gpt-6.1-sol"
    assert calls[0]["reasoning"] == {"effort": "high"}


@pytest.mark.parametrize("model,provider_model,effort", [
    ("gpt-6.1-sol", "gpt-6.1-sol", "high"),
    ("gpt-6-sol", "gpt-6.1-sol", "high"),
    ("gpt-5.5", "gpt-5.5", None),
])
def test_research_provider_high_sol_and_unchanged_legacy_model(monkeypatch, model, provider_model, effort):
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            id="research-route", model=kwargs["model"], status="completed", output=[], usage=None,
            output_text="Research answer",
        )

    monkeypatch.setattr(ai_base, "get_openai_client", lambda: SimpleNamespace(
        responses=SimpleNamespace(create=create),
    ))
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", lambda *_a, **_kw: None)
    monkeypatch.setattr(research, "filter_raw_citations", lambda _text: [])

    assert research.research_task.run({
        "model": model,
        "conversation": [{"input": "What is the rule?", "response": ""}],
    }) == {"response": "Research answer"}

    assert len(calls) == 1
    assert calls[0]["model"] == provider_model
    assert calls[0].get("reasoning") == ({"effort": effort} if effort else None)
