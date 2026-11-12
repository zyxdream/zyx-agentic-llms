from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from openai import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    OpenAIError,
    RateLimitError,
)

from backend.ai import base as ai_base


_MISSING = object()


@pytest.mark.parametrize('typed', [False, True])
def test_context_preflight_escapes_without_provider_retry(monkeypatch, analytics_spies, sleeps, typed):
    from pydantic import BaseModel
    from backend.ai import token_budget
    class ParsedResult(BaseModel):
        value: str
    # Use the real request preflight with a deliberately tiny model window.
    # Rejection must precede client creation, provider retries and failure metrics.
    monkeypatch.setitem(token_budget.CONTEXT_LIMITS, 'gpt-6.1-sol', 100)
    client = Mock(side_effect=AssertionError('Oversized request reached provider client'))
    monkeypatch.setattr(ai_base, 'get_openai_client', client)
    with pytest.raises(token_budget.ContextBudgetExceeded):
        if typed:
            ai_base.chat_completion_parse('prompt', ParsedResult, model='gpt-6.1-sol', reasoning_effort='medium')
        else:
            ai_base.chat_completion('prompt', model='gpt-6.1-sol')
    client.assert_not_called()
    assert sleeps == []
    analytics_spies.guided_failure.assert_not_called()


@pytest.mark.parametrize('exception_name', ['WorkflowStopped', 'WorkPending'])
def test_workflow_cancellation_and_continuation_escape_provider_retries(monkeypatch, analytics_spies, sleeps, exception_name):
    from backend.util.case_assistant_workflow import WorkflowStopped
    from backend.util.attachment_parser import WorkPending
    error = {'WorkflowStopped': WorkflowStopped, 'WorkPending': WorkPending}[exception_name]
    create = Mock(side_effect=error('Stop this execution'))
    monkeypatch.setattr(ai_base, 'get_openai_client', lambda: SimpleNamespace(responses=SimpleNamespace(create=create)))
    with pytest.raises(error):
        ai_base.chat_completion('prompt')
    assert create.call_count == 1 and sleeps == []
    analytics_spies.guided_failure.assert_not_called()


def test_soft_timeout_escapes_typed_provider_retry_handler(
    monkeypatch, analytics_spies, sleeps
):
    parse = Mock(side_effect=SoftTimeLimitExceeded())
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(parse=parse)),
    )

    from pydantic import BaseModel

    class ParsedResult(BaseModel):
        value: str

    with pytest.raises(SoftTimeLimitExceeded):
        ai_base.chat_completion_parse(
            "prompt",
            ParsedResult,
            model="gpt-6.1-sol",
            reasoning_effort="medium",
        )

    assert parse.call_count == 1
    assert sleeps == []
    analytics_spies.guided_failure.assert_not_called()


def test_soft_timeout_escapes_free_text_provider(monkeypatch, analytics_spies, sleeps):
    create = Mock(side_effect=SoftTimeLimitExceeded())
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=create)),
    )

    with pytest.raises(SoftTimeLimitExceeded):
        ai_base.chat_completion("prompt")

    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.guided_failure.assert_not_called()


def _response(
    text: str | None = "answer",
    *,
    status: str | object = "completed",
    total_tokens: int = 11,
    response_id: str = "resp_test",
    error=None,
    incomplete_details=None,
    output=None,
):
    values = {
        "id": response_id,
        "model": "gpt-test-2026-09-01",
        "output_text": text,
        "usage": SimpleNamespace(total_tokens=total_tokens),
        "error": error,
        "incomplete_details": incomplete_details,
        "output": [] if output is None else output,
    }
    if status is not _MISSING:
        values["status"] = status
    return SimpleNamespace(**values)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(
    status_code: int,
    *,
    code: str = "server_error",
    message: str | None = None,
    headers: dict[str, str] | None = None,
):
    message = message or f"provider returned HTTP {status_code}"
    body = {
        "error": {
            "code": code,
            "message": message,
            "type": "invalid_request_error" if status_code < 500 else "server_error",
        }
    }
    response = httpx.Response(
        status_code,
        request=_request(),
        headers=headers,
        json=body,
    )
    exception_type = RateLimitError if status_code == 429 else APIStatusError
    return exception_type(message, response=response, body=body)


def _connection_error():
    return APIConnectionError(request=_request())


def _timeout_error():
    return APITimeoutError(_request())


@pytest.fixture
def analytics_spies(monkeypatch):
    scoped_response = Mock(return_value=True)
    guided_response = Mock(return_value=True)
    guided_failure = Mock(return_value=True)
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", scoped_response)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", guided_response)
    monkeypatch.setattr(ai_base, "record_guided_llm_failure", guided_failure)
    return SimpleNamespace(
        scoped_response=scoped_response,
        guided_response=guided_response,
        guided_failure=guided_failure,
    )


@pytest.fixture
def provider(monkeypatch):
    installed = []

    def install(side_effect):
        if isinstance(side_effect, BaseException) or isinstance(side_effect, (list, tuple)):
            create = Mock(side_effect=side_effect)
        else:
            create = Mock(return_value=side_effect)
        client = SimpleNamespace(responses=SimpleNamespace(create=create))
        monkeypatch.setattr(ai_base, "get_openai_client", lambda: client)
        installed.append(create)
        return create

    return install


@pytest.fixture
def sleeps(monkeypatch):
    recorded = []
    monkeypatch.setattr(ai_base, "sleep", recorded.append)
    # Make exponential-backoff assertions deterministic regardless of which
    # stdlib random helper the implementation uses for its 0-0.25s jitter.
    monkeypatch.setattr(ai_base.random, "uniform", lambda _start, _end: 0.0)
    monkeypatch.setattr(ai_base.random, "random", lambda: 0.0)
    return recorded


def test_signature_removes_temperature_and_makes_options_keyword_only():
    signature = inspect.signature(ai_base.chat_completion)

    assert list(signature.parameters) == [
        "prompt",
        "model",
        "sys_prompt",
        "token_counter",
        "reasoning_effort",
    ]
    assert signature.parameters["prompt"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert signature.parameters["model"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert all(
        signature.parameters[name].kind is inspect.Parameter.KEYWORD_ONLY
        for name in ("sys_prompt", "token_counter", "reasoning_effort")
    )
    assert "temp" not in signature.parameters


def test_success_preserves_request_output_usage_and_analytics(
    provider, analytics_spies, sleeps
):
    response = _response("  exact answer  ", total_tokens=17)
    create = provider(response)
    token_counter = {"total": 3}

    result = ai_base.chat_completion(
        "user prompt",
        model="gpt-test",
        sys_prompt="system prompt",
        token_counter=token_counter,
    )

    assert result == "  exact answer  "
    create.assert_called_once_with(
        model="gpt-test",
        input=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user prompt"},
        ],
    )
    assert "temperature" not in create.call_args.kwargs
    assert token_counter == {"total": 20}
    analytics_spies.scoped_response.assert_called_once_with(response, model="gpt-test")
    analytics_spies.guided_response.assert_called_once_with(response, model="gpt-test")
    analytics_spies.guided_failure.assert_not_called()
    assert sleeps == []


def test_reasoning_is_omitted_by_default_and_forwarded_when_explicit(
    provider, analytics_spies, sleeps
):
    create = provider([_response("first"), _response("second")])

    assert ai_base.chat_completion("one", model="gpt-test") == "first"
    assert (
        ai_base.chat_completion(
            "two", model="gpt-test", reasoning_effort="medium"
        )
        == "second"
    )

    first_request, second_request = [call.kwargs for call in create.call_args_list]
    assert "reasoning" not in first_request
    assert second_request["reasoning"] == {"effort": "medium"}
    assert "temperature" not in first_request
    assert "temperature" not in second_request


def test_response_without_status_is_treated_as_completed(
    provider, analytics_spies, sleeps
):
    provider(_response("compatible", status=_MISSING))

    assert ai_base.chat_completion("prompt", model="gpt-test") == "compatible"
    assert sleeps == []


def test_empty_output_retries_then_returns_success(
    provider, analytics_spies, sleeps
):
    empty = _response("   ", total_tokens=5, response_id="resp_empty")
    success = _response("answer", total_tokens=7, response_id="resp_success")
    create = provider([empty, success])
    token_counter = {}

    assert (
        ai_base.chat_completion(
            "prompt", model="gpt-test", token_counter=token_counter
        )
        == "answer"
    )

    assert create.call_count == 2
    assert sleeps == [1.0]
    assert token_counter == {"total": 12}
    assert analytics_spies.scoped_response.call_count == 2
    assert analytics_spies.guided_response.call_count == 2
    analytics_spies.guided_failure.assert_not_called()


def test_empty_output_exhausts_four_total_attempts(
    provider, analytics_spies, sleeps
):
    responses = [
        _response("", total_tokens=index, response_id=f"resp_{index}")
        for index in range(1, 5)
    ]
    create = provider(responses)
    token_counter = {}

    assert (
        ai_base.chat_completion(
            "prompt", model="gpt-test", token_counter=token_counter
        )
        is None
    )

    assert create.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert token_counter == {"total": 10}
    assert analytics_spies.scoped_response.call_count == 4
    assert analytics_spies.guided_response.call_count == 4
    analytics_spies.guided_failure.assert_not_called()


def test_refusal_returns_none_without_exposing_or_retrying_partial_text(
    provider, analytics_spies, sleeps
):
    refusal = SimpleNamespace(type="refusal", refusal="I cannot help with that")
    message = SimpleNamespace(type="message", content=[refusal])
    response = _response("partial text must not escape", output=[message])
    create = provider(response)

    assert ai_base.chat_completion("prompt", model="gpt-test") is None
    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.scoped_response.assert_called_once_with(response, model="gpt-test")
    analytics_spies.guided_response.assert_called_once_with(response, model="gpt-test")
    analytics_spies.guided_failure.assert_not_called()


def test_incomplete_response_returns_none_without_exposing_partial_text(
    provider, analytics_spies, sleeps
):
    response = _response(
        "partial text must not escape",
        status="incomplete",
        incomplete_details=SimpleNamespace(reason="max_output_tokens"),
    )
    create = provider(response)

    assert ai_base.chat_completion("prompt", model="gpt-test") is None
    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.guided_response.assert_called_once_with(response, model="gpt-test")


@pytest.mark.parametrize("status", ["cancelled", "queued", "in_progress"])
def test_nonterminal_or_cancelled_response_returns_none_without_retry(
    status, provider, analytics_spies, sleeps
):
    create = provider(_response("partial", status=status))

    assert ai_base.chat_completion("prompt", model="gpt-test") is None
    assert create.call_count == 1
    assert sleeps == []


def test_nontransient_failed_response_returns_none_without_retry(
    provider, analytics_spies, sleeps
):
    response = _response(
        None,
        status="failed",
        error=SimpleNamespace(code="invalid_request_error", message="bad request"),
    )
    create = provider(response)

    assert ai_base.chat_completion("prompt", model="gpt-test") is None
    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.guided_response.assert_called_once_with(response, model="gpt-test")


@pytest.mark.parametrize(
    "transient_code",
    ["server_error", "server_is_overloaded", "rate_limit_exceeded", "slow_down"],
)
def test_transient_failed_response_retries_then_succeeds(
    transient_code, provider, analytics_spies, sleeps
):
    failed = _response(
        None,
        status="failed",
        total_tokens=3,
        error=SimpleNamespace(code=transient_code, message="try again"),
    )
    success = _response("answer", total_tokens=7)
    create = provider([failed, success])
    token_counter = {}

    assert (
        ai_base.chat_completion(
            "prompt", model="gpt-test", token_counter=token_counter
        )
        == "answer"
    )

    assert create.call_count == 2
    assert sleeps == [1.0]
    assert token_counter == {"total": 10}
    assert analytics_spies.scoped_response.call_count == 2
    assert analytics_spies.guided_response.call_count == 2
    analytics_spies.guided_failure.assert_not_called()


@pytest.mark.parametrize(
    ("error_factory", "expected_delay"),
    [
        pytest.param(_connection_error, 1.0, id="connection"),
        pytest.param(_timeout_error, 1.0, id="timeout"),
        pytest.param(lambda: _status_error(408), 1.0, id="http-408"),
        pytest.param(lambda: _status_error(409), 1.0, id="http-409"),
        pytest.param(
            lambda: _status_error(429, code="rate_limit_exceeded"), 1.0,
            id="http-429",
        ),
        pytest.param(lambda: _status_error(500), 1.0, id="http-500"),
        pytest.param(lambda: _status_error(503), 5.0, id="http-503"),
    ],
)
def test_retryable_exception_recovers(
    error_factory, expected_delay, provider, analytics_spies, sleeps
):
    success = _response("answer", total_tokens=9)
    create = provider([error_factory(), success])

    assert ai_base.chat_completion("prompt", model="gpt-test") == "answer"

    assert create.call_count == 2
    assert sleeps == [expected_delay]
    analytics_spies.guided_failure.assert_called_once_with(model="gpt-test")
    analytics_spies.scoped_response.assert_called_once_with(success, model="gpt-test")
    analytics_spies.guided_response.assert_called_once_with(success, model="gpt-test")


@pytest.mark.parametrize(
    "error_factory",
    [
        pytest.param(lambda: _status_error(400), id="http-400"),
        pytest.param(lambda: _status_error(401), id="authentication"),
        pytest.param(lambda: _status_error(403), id="permission"),
        pytest.param(lambda: _status_error(422), id="http-422"),
        pytest.param(
            lambda: _status_error(429, code="insufficient_quota"),
            id="quota-429",
        ),
        pytest.param(lambda: OpenAIError("generic OpenAI failure"), id="openai-error"),
        pytest.param(lambda: RuntimeError("unexpected failure"), id="unexpected"),
    ],
)
def test_nonretryable_exception_fails_immediately(
    error_factory, provider, analytics_spies, sleeps
):
    create = provider(error_factory())

    assert ai_base.chat_completion("prompt", model="gpt-test") is None

    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.guided_failure.assert_called_once_with(model="gpt-test")
    analytics_spies.scoped_response.assert_not_called()
    analytics_spies.guided_response.assert_not_called()


@pytest.mark.parametrize(
    ("status_code", "headers", "expected_delay", "expected_source"),
    [
        (429, {"retry-after-ms": "1500"}, 1.5, "retry-after-ms"),
        (429, {"Retry-After": "2"}, 2.0, "retry-after"),
        (503, {"retry-after-ms": "1500", "Retry-After": "20"}, 1.5, "retry-after-ms"),
        (503, {"Retry-After": "2"}, 2.0, "retry-after"),
    ],
)
def test_retry_after_headers_are_honored(
    status_code, headers, expected_delay, expected_source,
    provider, analytics_spies, sleeps, caplog,
):
    error = _status_error(
        status_code,
        code="rate_limit_exceeded" if status_code == 429 else "server_is_overloaded",
        message="Please try again in 30s. Sensitive provider text",
        headers=headers,
    )
    provider([error, _response("answer")])

    assert ai_base.chat_completion("prompt", model="gpt-test") == "answer"
    assert sleeps == [expected_delay]
    if status_code == 503:
        diagnostic = next(record.message for record in caplog.records if "http_503" in record.message)
        assert f"hint_source={expected_source}" in diagnostic
        assert f"parsed_hint_seconds={expected_delay}" in diagnostic
        assert f"delay={expected_delay}" in diagnostic
        assert "matched_wait=" not in diagnostic
        assert "Sensitive provider text" not in diagnostic


def test_retry_delay_falls_back_to_existing_error_message_parser(
    provider, analytics_spies, sleeps
):
    error = _status_error(
        429,
        code="rate_limit_exceeded",
        message="Please try again in 2.31s.",
    )
    provider([error, _response("answer")])

    assert ai_base.chat_completion("prompt", model="gpt-test") == "answer"
    assert sleeps == [3]


@pytest.mark.parametrize(
    ("message", "expected_delay"),
    [
        ("Please try again in 322ms.", 1),
        ("Please try again in 2.31s.", 3),
        ("No retry hint", None),
    ],
)
def test_public_retry_message_parser_keeps_numeric_contract(message, expected_delay):
    assert ai_base.extract_openai_retry_in_seconds(message) == expected_delay


def test_503_invalid_header_uses_message_hint_before_fallback(
    provider, analytics_spies, sleeps, caplog
):
    error = _status_error(
        503,
        code="server_is_overloaded",
        message="Please try again in 2.31s.",
        headers={"Retry-After": "invalid"},
    )
    provider([error, _response("answer")])

    assert ai_base.chat_completion("prompt", model="gpt-test") == "answer"
    assert sleeps == [3]
    diagnostic = next(record.message for record in caplog.records if "http_503" in record.message)
    assert "hint_source=message" in diagnostic
    assert "parsed_hint_seconds=3.0" in diagnostic
    assert "delay=3.0" in diagnostic
    assert "matched_wait='Please try again in 2.31s'" in diagnostic


@pytest.mark.parametrize("status_code", [429, 503])
def test_retry_after_over_sixty_seconds_stops_without_sleeping(
    status_code, provider, analytics_spies, sleeps
):
    error = _status_error(
        status_code,
        code="rate_limit_exceeded" if status_code == 429 else "server_is_overloaded",
        headers={"Retry-After": "61"},
    )
    create = provider(error)

    assert ai_base.chat_completion("prompt", model="gpt-test") is None

    assert create.call_count == 1
    assert sleeps == []
    analytics_spies.guided_failure.assert_called_once_with(model="gpt-test")


def test_exception_exhaustion_is_four_attempts_and_never_sleeps_after_last(
    provider, analytics_spies, sleeps
):
    create = provider([_connection_error() for _ in range(4)])

    assert ai_base.chat_completion("prompt", model="gpt-test") is None

    assert create.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert analytics_spies.guided_failure.call_count == 4
    analytics_spies.scoped_response.assert_not_called()
    analytics_spies.guided_response.assert_not_called()


def test_unhinted_503_exhausts_four_attempts_with_longer_backoff(
    provider, analytics_spies, sleeps
):
    create = provider([_status_error(503) for _ in range(4)])

    assert ai_base.chat_completion("prompt", model="gpt-test") is None

    assert create.call_count == 4
    assert sleeps == [5.0, 10.0, 20.0]
    assert analytics_spies.guided_failure.call_count == 4


def test_503_fallback_uses_up_to_one_second_of_jitter(monkeypatch):
    seen_ranges = []

    def jitter(start, end):
        seen_ranges.append((start, end))
        return 0.5

    monkeypatch.setattr(ai_base.random, "uniform", jitter)

    assert [ai_base._chat_completion_503_backoff_seconds(i) for i in (1, 2, 3)] == [
        5.5, 10.5, 20.5,
    ]
    assert seen_ranges == [(0.0, 1.0)] * 3


def test_503_diagnostic_logs_only_matched_wait_phrase(
    provider, analytics_spies, sleeps, caplog
):
    body = {"error": {
        "type": "service_unavailable_error",
        "code": "server_is_overloaded",
        "param": "sensitive-param",
        "message": (
            "Please try again in 322ms. Bearer private-token "
            "sk-private-key person@example.com"
        ),
        "prompt": "Confidential pleading text that must not be logged",
    }}
    response = httpx.Response(
        503,
        request=_request(),
        headers={
            "Retry-After": "invalid",
            "x-request-id": "req_private_503",
            "Authorization": "Bearer response-header-secret",
        },
        json=body,
    )
    provider([
        APIStatusError(body["error"]["message"], response=response, body=body),
        _response("answer"),
    ])

    with caplog.at_level("WARNING", logger=ai_base.__name__):
        assert ai_base.chat_completion("Sensitive user prompt", model="gpt-test") == "answer"

    diagnostic = next(record.message for record in caplog.records if "http_503" in record.message)
    assert "chat_completion http_503 model=gpt-test attempt=1 status_code=503" in diagnostic
    assert "hint_source=message" in diagnostic
    assert "parsed_hint_seconds=1.0" in diagnostic
    assert "delay=1.0" in diagnostic
    assert "matched_wait='Please try again in 322ms'" in diagnostic
    logged = "\n".join(
        record.message for record in caplog.records if record.name == ai_base.__name__
    )
    for secret in (
        "private-token", "sk-private-key", "person@example.com",
        "sensitive-param", "Confidential pleading text", "Sensitive user prompt",
        "req_private_503", "response-header-secret", "invalid",
    ):
        assert secret not in logged
    assert sleeps == [1.0]


def test_503_diagnostic_tolerates_missing_error_body(provider, analytics_spies, sleeps, caplog):
    response = httpx.Response(503, request=_request(), content=b"not JSON")
    error = APIStatusError("provider error", response=response, body=None)
    provider([error, _response("answer")])

    with caplog.at_level("WARNING", logger=ai_base.__name__):
        assert ai_base.chat_completion("prompt", model="gpt-test") == "answer"

    diagnostic = next(record.message for record in caplog.records if "http_503" in record.message)
    assert "hint_source=fallback" in diagnostic
    assert "parsed_hint_seconds=None" in diagnostic
    assert "delay=5.0" in diagnostic
    assert "matched_wait=" not in diagnostic
    assert "provider error" not in diagnostic
    assert sleeps == [5.0]
