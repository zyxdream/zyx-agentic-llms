"""Provider-contract tests for the staged Responses API helpers."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from celery.exceptions import SoftTimeLimitExceeded
from openai import APIConnectionError, APIStatusError, OpenAI
from pydantic import BaseModel

from backend.ai import base as ai_base


class ExtractedValue(BaseModel):
    value: str


def _response(
    *,
    text: str | None = "answer",
    parsed: BaseModel | None = None,
    status: str = "completed",
    total_tokens: int = 11,
    output=None,
    error=None,
    incomplete_details=None,
):
    return SimpleNamespace(
        id="resp_test",
        model="gpt-6.1-sol",
        status=status,
        output_text=text,
        output_parsed=parsed,
        output=[] if output is None else output,
        usage=SimpleNamespace(total_tokens=total_tokens),
        error=error,
        incomplete_details=incomplete_details,
    )


def _request():
    return httpx.Request("POST", "https://api.openai.com/v1/responses")


def _status_error(status_code: int, *, headers: dict[str, str] | None = None):
    response = httpx.Response(
        status_code,
        request=_request(),
        headers=headers,
        json={"error": {"code": "invalid_request_error"}},
    )
    return APIStatusError("provider error", response=response, body=response.json())


@pytest.fixture
def analytics_spies(monkeypatch):
    scoped = Mock(return_value=True)
    guided = Mock(return_value=True)
    failure = Mock(return_value=True)
    monkeypatch.setattr(ai_base, "record_scoped_llm_response", scoped)
    monkeypatch.setattr(ai_base, "record_guided_llm_response", guided)
    monkeypatch.setattr(ai_base, "record_guided_llm_failure", failure)
    return SimpleNamespace(scoped=scoped, guided=guided, failure=failure)


@pytest.fixture
def provider(monkeypatch):
    create = Mock()
    parse = Mock()
    monkeypatch.setattr(
        ai_base,
        "get_openai_client",
        lambda: SimpleNamespace(responses=SimpleNamespace(create=create, parse=parse)),
    )
    return SimpleNamespace(create=create, parse=parse)


@pytest.fixture
def sleeps(monkeypatch):
    delays = []
    monkeypatch.setattr(ai_base, "sleep", delays.append)
    monkeypatch.setattr(ai_base.random, "uniform", lambda _start, _end: 0.0)
    return delays


def _call_helper(name, *, reasoning_effort="medium", token_counter=None):
    common = {
        "model": "gpt-6.1-sol",
        "reasoning_effort": reasoning_effort,
        "sys_prompt": "system prompt",
        "token_counter": token_counter,
    }
    if name == "chat_completion_parse":
        return ai_base.chat_completion_parse(
            "new prompt", ExtractedValue, **common
        )
    if name == "multichat_completion_parse":
        return ai_base.multichat_completion_parse(
            [("old prompt", "old answer")],
            "new prompt",
            ExtractedValue,
            **common,
        )
    return ai_base.multichat_completion_text(
        [("old prompt", "old answer")], "new prompt", **common
    )


def test_execution_disables_network_timeout_without_changing_default_client(monkeypatch):
    from backend.database import db
    from backend.util import case_assistant_workflow as service

    timeouts = []

    def respond(request):
        timeouts.append(request.extensions['timeout'])
        return httpx.Response(200, json={
            'id': 'resp_test', 'object': 'response', 'status': 'completed',
            'model': 'gpt-6.1-sol', 'output': [],
        })

    # The real SDK and a mock transport check the actual outgoing HTTP options.
    # Loopback keeps this fully offline, including under tests.offline_guard.
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        default_client = OpenAI(api_key='test-only', base_url='http://127.0.0.1/v1',
                                http_client=transport, timeout=42, max_retries=0)
        monkeypatch.setattr(ai_base, 'client', default_client)
        validate = Mock()
        commit = Mock()
        monkeypatch.setattr(service, 'validate_execution', validate)
        monkeypatch.setattr(db, 'session', SimpleNamespace(commit=commit))

        ai_base.get_openai_client().responses.create(model='gpt-6.1-sol', input='before')
        with service.execution('wf_test'):
            scoped = ai_base.get_openai_client()
            assert scoped.timeout is None and scoped.max_retries == 0
            scoped.responses.create(model='gpt-6.1-sol', input='during')
        ai_base.get_openai_client().responses.create(model='gpt-6.1-sol', input='after')

        assert ai_base.get_openai_client() is default_client
        assert default_client.timeout == 42
        assert timeouts == [dict.fromkeys(('connect', 'read', 'write', 'pool'), value)
                            for value in (42, None, 42)]
        validate.assert_called_once_with()
        commit.assert_called_once_with()


def test_cancelled_execution_cannot_obtain_provider_client(monkeypatch):
    from backend.database import db
    from backend.util import case_assistant_workflow as service

    cached = Mock()
    commit = Mock()
    monkeypatch.setattr(ai_base, 'client', cached)
    monkeypatch.setattr(db, 'session', SimpleNamespace(commit=commit))
    monkeypatch.setattr(service, 'validate_execution',
                        Mock(side_effect=service.WorkflowStopped('Request cancelled')))

    with pytest.raises(service.WorkflowStopped, match='cancelled'):
        with service.execution('wf_cancelled'):
            ai_base.get_openai_client()

    cached.with_options.assert_not_called()
    commit.assert_not_called()
    assert service.current_execution() is None
    assert ai_base.get_openai_client() is cached


@pytest.mark.parametrize('name', ['chat_completion_parse', 'multichat_completion_parse',
                                'multichat_completion_text'])
def test_celery_soft_timeout_escapes_response_helpers_without_retry(
    name, provider, analytics_spies, sleeps
):
    provider.parse.side_effect = SoftTimeLimitExceeded()
    provider.create.side_effect = SoftTimeLimitExceeded()

    with pytest.raises(SoftTimeLimitExceeded):
        _call_helper(name)

    assert provider.parse.call_count + provider.create.call_count == 1
    assert sleeps == []
    analytics_spies.failure.assert_not_called()


def test_chat_parse_sends_schema_and_returns_typed_result_with_analytics(
    provider, analytics_spies, sleeps
):
    parsed = ExtractedValue(value="exact value")
    response = _response(parsed=parsed, total_tokens=17)
    provider.parse.return_value = response
    counter = {"total": 3}

    result = ai_base.chat_completion_parse(
        "user prompt",
        ExtractedValue,
        model="gpt-6.1-sol",
        reasoning_effort=None,
        sys_prompt="system prompt",
        token_counter=counter,
    )

    assert result is parsed
    provider.parse.assert_called_once_with(
        model="gpt-6.1-sol",
        input=[
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user prompt"},
        ],
        text_format=ExtractedValue,
        reasoning={"effort": "medium"},
    )
    provider.create.assert_not_called()
    assert counter == {"total": 20}
    analytics_spies.scoped.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.guided.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.failure.assert_not_called()
    assert sleeps == []


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_explicit_reasoning_effort_overrides_sol_default(
    name, provider, analytics_spies, sleeps
):
    response = _response(parsed=ExtractedValue(value="yes"))
    provider.parse.return_value = response
    provider.create.return_value = response

    _call_helper(name, reasoning_effort="high")

    request = provider.create.call_args or provider.parse.call_args
    assert request.kwargs["model"] == "gpt-6.1-sol"
    assert request.kwargs["reasoning"] == {"effort": "high"}


def test_non_sol_model_does_not_gain_a_reasoning_override(
    provider, analytics_spies, sleeps
):
    provider.parse.return_value = _response(parsed=ExtractedValue(value="yes"))

    ai_base.chat_completion_parse(
        "prompt", ExtractedValue, model="gpt-5.5", reasoning_effort=None
    )

    assert provider.parse.call_args.kwargs["model"] == "gpt-5.5"
    assert "reasoning" not in provider.parse.call_args.kwargs


def test_multichat_text_preserves_message_order_and_free_text_contract(
    provider, analytics_spies, sleeps
):
    response = _response(text="  exact answer  ", total_tokens=13)
    provider.create.return_value = response
    counter = {}

    result = ai_base.multichat_completion_text(
        [("first", "one"), ("second", "two")],
        "third",
        model="gpt-6.1-sol",
        reasoning_effort=None,
        sys_prompt="instructions",
        context_message="case context",
        token_counter=counter,
    )

    assert result == "  exact answer  "
    provider.create.assert_called_once_with(
        model="gpt-6.1-sol",
        input=[
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": "case context"},
            {
                "role": "assistant",
                "content": "Understood. I will use this session context.",
            },
            {"role": "user", "content": ' "first"'},
            {"role": "assistant", "content": ' "one"'},
            {"role": "user", "content": ' "second"'},
            {"role": "assistant", "content": ' "two"'},
            {"role": "user", "content": ' "third"'},
        ],
        reasoning={"effort": "medium"},
    )
    provider.parse.assert_not_called()
    assert "text" not in provider.create.call_args.kwargs
    assert counter == {"total": 13}
    analytics_spies.scoped.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.guided.assert_called_once_with(response, model="gpt-6.1-sol")
    assert sleeps == []


def test_multichat_parse_preserves_message_order_and_schema(
    provider, analytics_spies, sleeps
):
    parsed = ExtractedValue(value="updated")
    provider.parse.return_value = _response(parsed=parsed)

    result = ai_base.multichat_completion_parse(
        [("first", "one")],
        "second",
        ExtractedValue,
        model="gpt-6.1-sol",
        reasoning_effort=None,
        sys_prompt="instructions",
    )

    assert result is parsed
    provider.parse.assert_called_once_with(
        model="gpt-6.1-sol",
        input=[
            {"role": "system", "content": "instructions"},
            {"role": "user", "content": ' "first"'},
            {"role": "assistant", "content": ' "one"'},
            {"role": "user", "content": ' "second"'},
        ],
        text_format=ExtractedValue,
        reasoning={"effort": "medium"},
    )
    provider.create.assert_not_called()


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_empty_completed_responses_stop_after_four_attempts(
    name, provider, analytics_spies, sleeps
):
    responses = [_response(text=" ", total_tokens=n) for n in range(1, 5)]
    provider.create.side_effect = responses
    provider.parse.side_effect = responses
    counter = {}

    assert _call_helper(name, token_counter=counter) is None

    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert counter == {"total": 10}
    assert analytics_spies.scoped.call_count == 4
    assert analytics_spies.guided.call_count == 4
    analytics_spies.failure.assert_not_called()


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_empty_completed_response_retries_then_succeeds_and_counts_both(
    name, provider, analytics_spies, sleeps
):
    empty = _response(text="", total_tokens=5)
    success = _response(
        text="answer", parsed=ExtractedValue(value="parsed"), total_tokens=7
    )
    provider.create.side_effect = [empty, success]
    provider.parse.side_effect = [empty, success]
    counter = {}

    result = _call_helper(name, token_counter=counter)

    assert result == ("answer" if name.endswith("text") else success.output_parsed)
    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 2
    assert sleeps == [1.0]
    assert counter == {"total": 12}
    assert analytics_spies.scoped.call_count == 2
    assert analytics_spies.guided.call_count == 2


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_nonretryable_400_fails_once_without_sleep(
    name, provider, analytics_spies, sleeps
):
    provider.create.side_effect = _status_error(400)
    provider.parse.side_effect = _status_error(400)

    assert _call_helper(name) is None

    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 1
    assert sleeps == []
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")
    analytics_spies.scoped.assert_not_called()
    analytics_spies.guided.assert_not_called()


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
@pytest.mark.parametrize(
    ("error_factory", "expected_delay"),
    [
        pytest.param(lambda: APIConnectionError(request=_request()), 1.0, id="connection"),
        pytest.param(lambda: _status_error(503), 5.0, id="http-503"),
    ],
)
def test_transient_error_recovers_with_attempt_analytics(
    name, error_factory, expected_delay, provider, analytics_spies, sleeps
):
    success = _response(text="answer", parsed=ExtractedValue(value="parsed"))
    provider.create.side_effect = [error_factory(), success]
    provider.parse.side_effect = [error_factory(), success]

    result = _call_helper(name)

    assert result == ("answer" if name.endswith("text") else success.output_parsed)
    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 2
    assert sleeps == [expected_delay]
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")
    analytics_spies.scoped.assert_called_once_with(success, model="gpt-6.1-sol")
    analytics_spies.guided.assert_called_once_with(success, model="gpt-6.1-sol")


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_retry_after_over_sixty_seconds_stops_without_sleeping(
    name, provider, analytics_spies, sleeps
):
    provider.create.side_effect = _status_error(503, headers={"Retry-After": "61"})
    provider.parse.side_effect = _status_error(503, headers={"Retry-After": "61"})

    assert _call_helper(name) is None

    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 1
    assert sleeps == []
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
@pytest.mark.parametrize(
    ("headers", "expected_delay"),
    [
        ({"Retry-After": "2"}, 2.0),
        ({"retry-after-ms": "1500"}, 1.5),
        ({"Retry-After": "not-a-delay"}, 5.0),
    ],
)
def test_503_provider_hint_precedes_longer_fallback(
    name, headers, expected_delay, provider, analytics_spies, sleeps
):
    success = _response(text="answer", parsed=ExtractedValue(value="parsed"))
    provider.create.side_effect = [_status_error(503, headers=headers), success]
    provider.parse.side_effect = [_status_error(503, headers=headers), success]

    assert _call_helper(name) == (
        "answer" if name.endswith("text") else success.output_parsed
    )
    assert sleeps == [expected_delay]


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
def test_unhinted_503_four_attempt_exhaustion_uses_longer_backoff(
    name, provider, analytics_spies, sleeps
):
    provider.create.side_effect = [_status_error(503) for _ in range(4)]
    provider.parse.side_effect = [_status_error(503) for _ in range(4)]

    assert _call_helper(name) is None

    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 4
    assert sleeps == [5.0, 10.0, 20.0]
    assert analytics_spies.failure.call_count == 4


@pytest.mark.parametrize("name", ["chat_completion_parse", "multichat_completion_parse", "multichat_completion_text"])
@pytest.mark.parametrize("kind", ["refusal", "incomplete"])
def test_refusal_and_incomplete_do_not_release_partial_results_or_retry(
    name, kind, provider, analytics_spies, sleeps
):
    response = _response(
        text="partial text",
        parsed=ExtractedValue(value="partial parsed result"),
        status="incomplete" if kind == "incomplete" else "completed",
        output=(
            [SimpleNamespace(type="message", content=[SimpleNamespace(type="refusal")])]
            if kind == "refusal"
            else []
        ),
        incomplete_details=(
            SimpleNamespace(reason="max_output_tokens")
            if kind == "incomplete"
            else None
        ),
    )
    provider.create.return_value = response
    provider.parse.return_value = response

    assert _call_helper(name) is None

    called = provider.create if name.endswith("text") else provider.parse
    assert called.call_count == 1
    assert sleeps == []
    analytics_spies.scoped.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.guided.assert_called_once_with(response, model="gpt-6.1-sol")
    analytics_spies.failure.assert_not_called()


def test_parties_venue_extraction_uses_typed_sol_parse_and_returns_tuple(
    provider, analytics_spies, sleeps
):
    parsed = ai_base.PartiesVenueResult(
        plaintiff=["Alice", "Acme LLC"],
        defendant=["Bob"],
        venue="Cook County",
    )
    provider.parse.return_value = _response(parsed=parsed)

    assert ai_base.extract_parties_venue("complaint cover text") == (
        ["Alice", "Acme LLC"],
        ["Bob"],
        "Cook County",
    )

    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text_format"] is ai_base.PartiesVenueResult
    assert "complaint cover text" in request["input"][1]["content"]
    provider.create.assert_not_called()


def test_parties_venue_extraction_accepts_luna_medium_override(
    provider, analytics_spies, sleeps
):
    parsed = ai_base.PartiesVenueResult(
        plaintiff=["Alice"], defendant=["Bob"], venue="Cook County"
    )
    provider.parse.return_value = _response(parsed=parsed)

    assert ai_base.extract_parties_venue(
        "complaint cover text", model="gpt-6-luna", reasoning_effort="medium"
    ) == (["Alice"], ["Bob"], "Cook County")

    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6-luna"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text_format"] is ai_base.PartiesVenueResult
    assert "complaint cover text" in request["input"][1]["content"]
    analytics_spies.guided.assert_called_once_with(
        provider.parse.return_value, model="gpt-6-luna"
    )


def test_parties_venue_extraction_returns_empty_tuple_after_parse_failure(
    provider, analytics_spies, sleeps
):
    provider.parse.side_effect = _status_error(400)

    assert ai_base.extract_parties_venue("complaint cover text") == ([], [], "")
    assert provider.parse.call_count == 1
    assert sleeps == []
    analytics_spies.failure.assert_called_once_with(model="gpt-6.1-sol")


def test_title_parties_venue_extraction_keeps_tuple_and_sol_default(
    provider, analytics_spies, sleeps
):
    parsed = ai_base.TitlePartiesVenueResult(
        plaintiff=["Alice"], defendant=["Bob"], title="First Interrogatories", venue="Cook County"
    )
    provider.parse.return_value = _response(parsed=parsed)

    assert ai_base.extract_title_parties_venue("caption text") == (
        ["Alice"], ["Bob"], "First Interrogatories", "Cook County"
    )
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text_format"] is ai_base.TitlePartiesVenueResult
    assert "caption text" in request["input"][1]["content"]
    provider.create.assert_not_called()


def test_title_parties_venue_extraction_luna_override_and_failed_parse(
    provider, analytics_spies, sleeps
):
    parsed = ai_base.TitlePartiesVenueResult(
        plaintiff=["Alice"], defendant=["Bob"], title="Interrogatories", venue="Federal Court"
    )
    provider.parse.return_value = _response(parsed=parsed)
    assert ai_base.extract_title_parties_venue(
        "caption text", model="gpt-6-luna", reasoning_effort="medium"
    ) == (["Alice"], ["Bob"], "Interrogatories", "Federal Court")
    request = provider.parse.call_args.kwargs
    assert request["model"] == "gpt-6-luna"
    assert request["reasoning"] == {"effort": "medium"}

    provider.parse.reset_mock()
    provider.parse.side_effect = _status_error(400)
    assert ai_base.extract_title_parties_venue("caption text") == ([], [], "", "")
    assert provider.parse.call_count == 1


@pytest.mark.parametrize("method", ["chat_completion_j", "multichat_completion_j"])
def test_legacy_json_paths_still_return_raw_text_without_parsing(
    method, provider, analytics_spies, sleeps
):
    malformed = "{not valid application json"
    provider.create.return_value = _response(text=malformed)

    if method == "chat_completion_j":
        result = ai_base.chat_completion_j(
            "prompt", model="gpt-6.1-sol", reasoning_effort="medium"
        )
    else:
        result = ai_base.multichat_completion_j(
            [("old prompt", "old answer")],
            "prompt",
            model="gpt-6.1-sol",
            reasoning_effort="medium",
        )

    assert result == malformed
    request = provider.create.call_args.kwargs
    assert request["model"] == "gpt-6.1-sol"
    assert request["reasoning"] == {"effort": "medium"}
    assert request["text"] == {"format": {"type": "json_object"}}
    provider.parse.assert_not_called()
    assert sleeps == []


@pytest.mark.parametrize("method", ["chat_completion_j", "multichat_completion_j"])
def test_legacy_json_paths_stop_after_four_empty_results(
    method, provider, analytics_spies, sleeps
):
    provider.create.side_effect = [_response(text="") for _ in range(4)]

    if method == "chat_completion_j":
        result = ai_base.chat_completion_j("prompt", model="gpt-6.1-sol")
    else:
        result = ai_base.multichat_completion_j(
            [("old prompt", "old answer")], "prompt", model="gpt-6.1-sol"
        )

    assert result is None
    assert provider.create.call_count == 4
    assert sleeps == [1.0, 2.0, 4.0]
    assert analytics_spies.scoped.call_count == 4
    assert analytics_spies.guided.call_count == 4
    analytics_spies.failure.assert_not_called()


@pytest.mark.parametrize("method", ["chat_completion_j", "multichat_completion_j"])
def test_legacy_json_paths_use_longer_unhinted_503_backoff(
    method, provider, analytics_spies, sleeps
):
    provider.create.side_effect = [_status_error(503) for _ in range(4)]

    if method == "chat_completion_j":
        result = ai_base.chat_completion_j("prompt", model="gpt-6.1-sol")
    else:
        result = ai_base.multichat_completion_j(
            [("old prompt", "old answer")], "prompt", model="gpt-6.1-sol"
        )

    assert result is None
    assert provider.create.call_count == 4
    assert sleeps == [5.0, 10.0, 20.0]
    assert analytics_spies.failure.call_count == 4
