"""Exercise the provider boundary rather than mocking service.call_model."""
import json
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.word_addin.model import complete_json
from backend.word_addin.document_workflow import model_json
from backend.word_addin import service


@pytest.fixture
def provider(monkeypatch):
    base = ModuleType("backend.ai.base")
    base.DEFAULT_MODEL = "lextext-configured-model"
    client = Mock()
    client.with_options.return_value = client
    base.get_openai_client = lambda: client
    # The isolated Word CI does not import the entire AI application or need keys.
    sdk = ModuleType("openai")
    class APIConnectionError(Exception):
        pass
    class APIStatusError(Exception):
        def __init__(self, status):
            self.status_code = status
    sdk.APIConnectionError = APIConnectionError
    sdk.APIStatusError = APIStatusError
    class APITimeoutError(APIConnectionError):
        pass
    sdk.APITimeoutError = APITimeoutError
    monkeypatch.setitem(sys.modules, "backend.ai.base", base)
    monkeypatch.setitem(sys.modules, "openai", sdk)
    monkeypatch.setattr("backend.word_addin.model.time.sleep", lambda _: None)
    return client, sdk


def response(value, status="completed"):
    return SimpleNamespace(output_text=value, status=status)


def test_document_section_without_word_json_in_prompt_reaches_provider(provider):
    client, _ = provider
    def create(**kwargs):
        assert "json" in " ".join(m["content"] for m in kwargs["input"]).lower()
        assert kwargs["text"] == {"format": {"type": "json_object"}}
        return response('{"edits": [], "findings": []}')
    client.responses.create.side_effect = create
    assert model_json({"task": "Return review findings only; edits must be empty."}, "You are LexText.") == {"edits": [], "findings": []}
    client.with_options.assert_called_once_with(timeout=120.0, max_retries=0)
    assert client.responses.create.call_count == 1


@pytest.mark.parametrize("bad", [response("truncated {"), response(""), response("[]"), response('{}', "incomplete")])
def test_incomplete_output_retries_once(provider, bad):
    client, _ = provider
    client.responses.create.side_effect = [bad, response('{"text": "Complete"}')]
    assert complete_json("Task", "System") == {"text": "Complete"}
    assert client.responses.create.call_count == 2


def test_permanent_provider_error_is_not_retried_50_times(provider):
    client, sdk = provider
    client.responses.create.side_effect = sdk.APIStatusError(400)
    with pytest.raises(ValueError, match="document is unchanged"):
        complete_json("private pleading", "System")
    assert client.responses.create.call_count == 1


@pytest.mark.parametrize("status", [429, 503, None])
def test_transient_errors_have_bounded_retries(provider, status):
    client, sdk = provider
    client.responses.create.side_effect = sdk.APIConnectionError() if status is None else sdk.APIStatusError(status)
    with pytest.raises(ValueError):
        complete_json("Task", "System")
    assert client.responses.create.call_count == 2


def test_malformed_output_never_returns_partial_success(provider):
    client, _ = provider
    client.responses.create.return_value = response('{"findings": [')
    with pytest.raises(ValueError, match="after retrying"):
        complete_json("Task", "System")
    assert client.responses.create.call_count == 2


def test_document_review_all_sections_use_json(provider, monkeypatch):
    from tests.test_word_document_workflow import document_payload
    from backend.word_addin.document_workflow import execute_document
    client, _ = provider
    value = document_payload()
    value["action"] = "review_document"
    value["blocks"][0]["text"] = "A" * 8000
    value["blocks"][1]["text"] = "B" * 8000
    value["text"] = "\n".join(b["text"] for b in value["blocks"])
    def create(**kwargs):
        assert "JSON" in kwargs["input"][0]["content"]
        prompt = json.loads(kwargs["input"][1]["content"])
        result = {"approach": "Stress-test all arguments", "global_rules": []} if "document" in prompt else {"edits": [], "findings": []}
        return response(json.dumps(result))
    client.responses.create.side_effect = create
    monkeypatch.setattr(service, "retrieve_sources", lambda *_: ([], []))
    result = execute_document(service.validate_request(value), 1)
    assert result["kind"] == "report"
    assert result["coverage"]["review_sections"] == 2
    assert client.responses.create.call_count == 3


@pytest.mark.parametrize('status,code,message,calls', [
    (401, None, 'administrator', 1),
    (429, 'insufficient_quota', 'billing configuration', 1),
    (429, None, 'rate-limited', 2),
    (503, None, 'temporarily unavailable', 2),
    (400, None, 'rejected this request', 1),
])
def test_provider_diagnostics_are_actionable_and_redacted(provider, caplog, status, code, message, calls):
    client, sdk = provider
    error = sdk.APIStatusError(status)
    error.body = {'error': {'code': code, 'message': 'PRIVATE PLEADING'}}
    error.request_id = 'req_test123'
    client.responses.create.side_effect = error
    with pytest.raises(ValueError, match=message) as caught:
        complete_json('PRIVATE PLEADING', 'System')
    assert 'Reference:' in str(caught.value)
    assert 'PRIVATE PLEADING' not in str(caught.value) + caplog.text
    assert 'req_test123' in caplog.text
    assert client.responses.create.call_count == calls


def test_retry_after_is_respected_and_long_wait_is_not_immediately_retried(provider, monkeypatch):
    from backend.word_addin import model
    client, sdk = provider
    sleep = Mock()
    monkeypatch.setattr(model.time, 'sleep', sleep)
    error = sdk.APIStatusError(429)
    error.response = SimpleNamespace(headers={'retry-after': '4'})
    client.responses.create.side_effect = [error, response('{"text":"Ready"}')]
    assert complete_json('Task', 'System')['text'] == 'Ready'
    sleep.assert_called_once_with(4)
    error.response.headers['retry-after'] = '120'
    client.responses.create.reset_mock(side_effect=True)
    client.responses.create.side_effect = error
    with pytest.raises(ValueError, match='rate-limited'):
        complete_json('Task', 'System')
    assert client.responses.create.call_count == 1


def test_reported_short_rewrite_uses_interactive_profile_and_real_stages(provider, monkeypatch):
    from tests.test_word_addin import payload
    from backend.word_addin import research
    client, _ = provider
    monkeypatch.setattr(sys.modules['backend.ai.base'], 'DEFAULT_MODEL', 'gpt-5.5')
    monkeypatch.setattr(service, 'retrieve_sources', lambda *_: ([], []))
    monkeypatch.setattr(research, 'retrieve_authorities', lambda *_: pytest.fail('A rewrite must not search'))
    client.responses.create.return_value = response('{"text":"A clearer passage.","notes":[],"findings":[]}')
    stages = []
    result = service.execute(service.validate_request(payload(
        text='A passage. ' * 258,
        instructions='Write more forcefully and crisply, in elite litigation style')), 1, stages.append)
    assert result['kind'] == 'revision'
    assert client.responses.create.call_count == 1
    assert client.responses.create.call_args.kwargs['reasoning'] == {'effort': 'low'}
    client.with_options.assert_called_once_with(timeout=60.0, max_retries=0)
    assert stages == ['Preparing your task…', 'Drafting your proposed edits…', 'Checking your proposed edits…']


def test_interactive_timeout_does_not_start_another_slow_generation(provider):
    client, sdk = provider
    client.responses.create.side_effect = sdk.APITimeoutError()
    with pytest.raises(ValueError, match='took too long'):
        complete_json('Task', 'System', interactive=True)
    assert client.responses.create.call_count == 1


def test_interactive_retry_obeys_total_budget(provider, monkeypatch):
    from backend.word_addin import model
    client, sdk = provider
    clock = [0.0]
    monkeypatch.setattr(model.time, 'monotonic', lambda: clock[0])
    def late_error(**kwargs):
        clock[0] = 89.0
        raise sdk.APIStatusError(503)
    client.responses.create.side_effect = late_error
    with pytest.raises(ValueError, match='temporarily unavailable'):
        complete_json('Task', 'System', interactive=True)
    assert client.responses.create.call_count == 1


@pytest.mark.parametrize('name', ['gpt-5.5-pro', 'lextext-configured-model'])
def test_unknown_or_pro_models_do_not_receive_unsupported_reasoning(provider, monkeypatch, name):
    client, _ = provider
    monkeypatch.setattr(sys.modules['backend.ai.base'], 'DEFAULT_MODEL', name)
    client.responses.create.return_value = response('{"text":"Ready"}')
    complete_json('Task', 'System', interactive=True)
    assert 'reasoning' not in client.responses.create.call_args.kwargs


def test_standard_analysis_keeps_existing_model_reasoning(provider, monkeypatch):
    client, _ = provider
    monkeypatch.setattr(sys.modules['backend.ai.base'], 'DEFAULT_MODEL', 'gpt-5.5')
    client.responses.create.return_value = response('{"text":"Ready"}')
    complete_json('Analyze opposing arguments', 'System')
    assert 'reasoning' not in client.responses.create.call_args.kwargs
    client.with_options.assert_called_once_with(timeout=120.0, max_retries=0)
