"""Typed keyword expansion contract and fallback tests."""

import pytest
from celery.exceptions import SoftTimeLimitExceeded

from backend.ai import keyword_expander


def test_keyword_expansion_uses_typed_parse_and_forwards_effort(monkeypatch):
    calls = []

    def parse(prompt, response_model, *, model, reasoning_effort, token_counter):
        calls.append((prompt, response_model, model, reasoning_effort, token_counter))
        return response_model(keywords=["  Contract  ", "BREACH", " ", "remedy"])

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", parse)
    counter = {"total": 2}

    assert keyword_expander.expand_search_keywords(
        "breach of contract",
        model="gpt-6.1-sol",
        reasoning_effort="low",
        token_counter=counter,
    ) == ["contract", "breach", "remedy"]
    assert len(calls) == 1
    prompt, response_model, model, effort, passed_counter = calls[0]
    assert "breach of contract" in prompt
    assert response_model is keyword_expander.KeywordExpansion
    assert model == "gpt-6.1-sol"
    assert effort == "low"
    assert passed_counter is counter


@pytest.mark.parametrize("parsed_keywords", [None, [], [" ", "\t"]])
def test_keyword_expansion_falls_back_for_unusable_parsed_output(
    monkeypatch, parsed_keywords
):
    def parse(_prompt, response_model, **_kwargs):
        if parsed_keywords is None:
            return None
        return response_model(keywords=parsed_keywords)

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", parse)

    assert keyword_expander.expand_search_keywords(
        "What are the damages for breach of contract?"
    ) == ["damages", "breach", "contract"]


def test_keyword_expansion_falls_back_on_provider_error(monkeypatch):
    def parse(*_args, **_kwargs):
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", parse)

    assert keyword_expander.expand_search_keywords("breach of contract") == [
        "breach", "contract"
    ]


def test_keyword_expansion_propagates_celery_soft_timeout(monkeypatch):
    def parse(*_args, **_kwargs):
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", parse)

    with pytest.raises(SoftTimeLimitExceeded):
        keyword_expander.expand_search_keywords("breach of contract")


def test_keyword_expansion_empty_query_skips_provider(monkeypatch):
    def parse(*_args, **_kwargs):
        raise AssertionError("provider should not be called")

    monkeypatch.setattr(keyword_expander.base, "chat_completion_parse", parse)

    assert keyword_expander.expand_search_keywords("  ") == []
