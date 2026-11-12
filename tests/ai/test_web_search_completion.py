"""Provider-contract tests for the web-enabled Responses helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from backend.ai import base as ai_base


def _response(
    text: str = "result",
    *,
    output=None,
    sources=None,
):
    return SimpleNamespace(
        id="resp_web_test",
        model="gpt-6.1-sol",
        output_text=text,
        output=[] if output is None else output,
        sources=sources,
        usage=SimpleNamespace(total_tokens=12),
    )


def _request():
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(status_code, *, code="server_error", headers=None):
    body = {"error": {"code": code, "message": "provider error"}}
    response = httpx.Response(
        status_code,
        request=_request(),
        headers=headers,
        json=body,
    )
    return APIStatusError("provider error", response=response, body=body)


@pytest.fixture
def analytics_spies(monkeypatch):
    scoped = Mock()
    guided = Mock()
    failure = Mock()
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", scoped)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", guided)
    monkeypatch.setattr(ai_base, "record_guided_llm_failure", failure)
    return SimpleNamespace(scoped=scoped, guided=guided, failure=failure)


@pytest.fixture
def provider(monkeypatch):
    create = Mock()
    fake_client = SimpleNamespace(responses=SimpleNamespace(create=create))
    monkeypatch.setattr(ai_base, "get_openai_client", lambda: fake_client)
    # A direct use of the module's uninitialized global should fail this suite.
    monkeypatch.setattr(ai_base, "client", None)
    return create


@pytest.fixture
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr(ai_base, "sleep", delays.append)
    monkeypatch.setattr(ai_base.random, "uniform", lambda _start, _end: 0.0)
    return delays


def test_cold_start_initializes_lazy_client_and_preserves_single_user_request(
    monkeypatch, analytics_spies, sleeps
):
    create = Mock(return_value=_response("exact text"))
    openai_constructor = Mock(
        return_value=SimpleNamespace(responses=SimpleNamespace(create=create))
    )
    monkeypatch.setattr(ai_base, "client", None)
    monkeypatch.setattr(ai_base, "OpenAI", openai_constructor)

    result = ai_base.web_search_completion("find source")

    assert result == "exact text"
    openai_constructor.assert_called_once()
    assert openai_constructor.call_args.kwargs["max_retries"] == 0
    create.assert_called_once_with(
        model="gpt-6.1-sol",
        input=[{"role": "user", "content": "find source"}],
        tools=[{"type": "web_search"}],
        include=["web_search_call.action.sources"],
        reasoning={"effort": "medium"},
    )
    analytics_spies.scoped.assert_called_once_with(
        create.return_value, model="gpt-6.1-sol"
    )
    analytics_spies.guided.assert_called_once_with(
        create.return_value, model="gpt-6.1-sol"
    )
    analytics_spies.failure.assert_not_called()
    assert sleeps == []


def test_history_and_system_are_sent_in_original_order_without_quoting(
    provider, analytics_spies, sleeps
):
    provider.return_value = _response("answer")

    result = ai_base.web_search_completion(
        "current question",
        sys_prompt="research instructions",
        prev_prompt_output=[
            ("first question", "first answer"),
            ("second question", "second answer"),
        ],
    )

    assert result == "answer"
    assert provider.call_args.kwargs["input"] == [
        {"role": "system", "content": "research instructions"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
        {"role": "user", "content": "current question"},
    ]


@pytest.mark.parametrize(
    "model,reasoning_effort,expected_reasoning",
    [
        ("gpt-6.1-sol", None, {"effort": "medium"}),
        ("gpt-6.1-sol", "high", {"effort": "high"}),
        ("gpt-5.5", None, None),
    ],
)
def test_reasoning_defaults_overrides_and_other_models(
    provider, analytics_spies, sleeps, model, reasoning_effort, expected_reasoning
):
    provider.return_value = _response()

    ai_base.web_search_completion(
        "search", model=model, reasoning_effort=reasoning_effort
    )

    sent = provider.call_args.kwargs
    assert sent["model"] == model
    if expected_reasoning is None:
        assert "reasoning" not in sent
    else:
        assert sent["reasoning"] == expected_reasoning


def test_allowed_domains_are_capped_at_100(provider, analytics_spies, sleeps):
    provider.return_value = _response()
    domains = [f"example-{index}.com" for index in range(101)]

    ai_base.web_search_completion("search", allowed_domains=domains)

    assert provider.call_args.kwargs["tools"] == [
        {"type": "web_search", "filters": {"allowed_domains": domains[:100]}}
    ]
    assert provider.call_args.kwargs["include"] == [
        "web_search_call.action.sources"
    ]


def test_metadata_collects_nested_sources_and_annotations_from_objects_and_dicts(
    provider, analytics_spies, sleeps
):
    first = {"url": "https://first.example"}
    second = SimpleNamespace(url="https://second.example")
    third = {"url": "https://third.example"}
    annotation_one = SimpleNamespace(url="https://cited.example")
    annotation_two = {"url": "https://other-cited.example"}
    response = _response(
        "cited answer",
        sources=[{"url": "https://fallback.example"}],
        output=[
            SimpleNamespace(
                type="web_search_call",
                action={"sources": [first, second]},
            ),
            {"type": "web_search_call", "action": SimpleNamespace(sources=[third])},
            SimpleNamespace(
                type="message",
                content=[
                    SimpleNamespace(
                        type="output_text", annotations=[annotation_one]
                    )
                ],
            ),
            {
                "type": "message",
                "content": [
                    {"type": "output_text", "annotations": [annotation_two]}
                ],
            },
        ],
    )
    provider.return_value = response

    text, metadata = ai_base.web_search_completion("search", return_meta=True)

    assert text == "cited answer"
    assert metadata == {
        "sources": [first, second, third],
        "annotations": [annotation_one, annotation_two],
    }
    analytics_spies.scoped.assert_called_once_with(response, model="gpt-6.1-sol")


def test_top_level_sources_remain_fallback_and_tuple_shape_is_unchanged(
    provider, analytics_spies, sleeps
):
    sources = [{"url": "https://fallback.example"}]
    provider.return_value = _response("answer", sources=sources)

    result = ai_base.web_search_completion("search", return_meta=True)

    assert result == ("answer", {"sources": sources, "annotations": []})


def test_blank_response_returns_unchanged_without_retry(
    provider, analytics_spies, sleeps
):
    provider.return_value = _response("")

    assert ai_base.web_search_completion("search") == ""

    assert provider.call_count == 1
    analytics_spies.scoped.assert_called_once()
    analytics_spies.guided.assert_called_once()
    analytics_spies.failure.assert_not_called()
    assert sleeps == []


@pytest.mark.parametrize("status_code", [408, 409, 429, 500, 503])
def test_transient_status_retries_then_records_success(
    provider, analytics_spies, sleeps, status_code
):
    failure = _status_error(status_code, code="rate_limit_exceeded")
    response = _response("recovered")
    provider.side_effect = [failure, response]

    assert ai_base.web_search_completion("search") == "recovered"

    assert provider.call_count == 2
    assert sleeps == ([5.0] if status_code == 503 else [1.0])
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")
    analytics_spies.scoped.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.guided.assert_called_once_with(response, model="gpt-6.1-sol")


@pytest.mark.parametrize("error_type", [APIConnectionError, APITimeoutError])
def test_connection_or_timeout_retries_then_recovers(
    provider, analytics_spies, sleeps, error_type
):
    provider.side_effect = [
        error_type(request=_request()),
        _response("recovered"),
    ]

    assert ai_base.web_search_completion("search") == "recovered"

    assert provider.call_count == 2
    assert sleeps == [1.0]
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")
    analytics_spies.scoped.assert_called_once()
    analytics_spies.guided.assert_called_once()


@pytest.mark.parametrize("status_code,code", [(400, "invalid_request_error"), (429, "insufficient_quota")])
def test_non_retryable_status_fails_immediately_and_reraises(
    provider, analytics_spies, sleeps, status_code, code
):
    error = _status_error(status_code, code=code)
    provider.side_effect = error

    with pytest.raises(APIStatusError) as raised:
        ai_base.web_search_completion("search")

    assert raised.value is error
    assert provider.call_count == 1
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")
    analytics_spies.scoped.assert_not_called()
    analytics_spies.guided.assert_not_called()
    assert sleeps == []


def test_retry_after_ms_is_honored(provider, analytics_spies, sleeps):
    provider.side_effect = [
        _status_error(429, code="rate_limit_exceeded", headers={"retry-after-ms": "1500"}),
        _response("recovered"),
    ]

    assert ai_base.web_search_completion("search") == "recovered"
    assert sleeps == [1.5]
    assert provider.call_count == 2


@pytest.mark.parametrize(
    ("headers", "expected_delay"),
    [({"Retry-After": "2"}, 2.0), ({"Retry-After": "invalid"}, 5.0)],
)
def test_503_provider_hint_precedes_longer_fallback(
    provider, analytics_spies, sleeps, headers, expected_delay
):
    provider.side_effect = [
        _status_error(503, code="server_is_overloaded", headers=headers),
        _response("recovered"),
    ]

    assert ai_base.web_search_completion("search") == "recovered"
    assert sleeps == [expected_delay]
    assert provider.call_count == 2


@pytest.mark.parametrize("status_code", [429, 503])
def test_retry_after_over_60_seconds_reraises_without_sleep(
    status_code, provider, analytics_spies, sleeps
):
    error = _status_error(
        status_code,
        code="rate_limit_exceeded" if status_code == 429 else "server_is_overloaded",
        headers={"retry-after": "61"},
    )
    provider.side_effect = error

    with pytest.raises(APIStatusError) as raised:
        ai_base.web_search_completion("search")

    assert raised.value is error
    assert provider.call_count == 1
    analytics_spies.failure.assert_called_once()
    assert sleeps == []


def test_four_attempts_exhaustion_reraises_final_error(
    provider, analytics_spies, sleeps
):
    errors = [_status_error(503) for _ in range(4)]
    provider.side_effect = errors

    with pytest.raises(APIStatusError) as raised:
        ai_base.web_search_completion("search")

    assert raised.value is errors[-1]
    assert provider.call_count == 4
    assert sleeps == [5.0, 10.0, 20.0]
    assert analytics_spies.failure.call_count == 4
    analytics_spies.scoped.assert_not_called()
    analytics_spies.guided.assert_not_called()


def test_unexpected_provider_error_is_not_swallowed(
    provider, analytics_spies, sleeps
):
    error = ValueError("unexpected provider issue")
    provider.side_effect = error

    with pytest.raises(ValueError) as raised:
        ai_base.web_search_completion("search")

    assert raised.value is error
    assert provider.call_count == 1
    analytics_spies.failure.assert_called_once()
    assert sleeps == []
