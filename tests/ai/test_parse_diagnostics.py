"""Diagnostic capture preserves real SDK parsing, request content and retry behavior."""
import json
from pathlib import Path
import stat
from unittest.mock import Mock

import httpx
from openai import OpenAI
from pydantic import ValidationError
import pytest

from backend.ai import base, parse_diagnostics
from backend.util import case_assistant_workflow as workflows
from backend.util.document_drafts import MarkdownDraftResult


PROMPT = 'Private request prompt sentinel; do not save request content.'
API_KEY = 'synthetic-api-key-not-for-capture'
COOKIE = 'synthetic-cookie-not-for-capture'


def response_body(text):
    return json.dumps({
        'id': 'resp_diagnostic_test', 'object': 'response', 'created_at': 1,
        'model': 'gpt-6.1-sol', 'status': 'completed', 'error': None,
        'incomplete_details': None,
        'output': [{'id': 'msg_diagnostic_test', 'type': 'message',
            'role': 'assistant', 'status': 'completed',
            'content': [{'type': 'output_text', 'text': text, 'annotations': []}]}],
        'usage': {'input_tokens': 10, 'output_tokens': 20, 'total_tokens': 30},
    }, ensure_ascii=False).encode('utf-8')


@pytest.fixture(autouse=True)
def isolated_diagnostic_settings(monkeypatch):
    monkeypatch.delenv('LLM_PARSE_DIAGNOSTICS_DIR', raising=False)
    monkeypatch.setattr(base, 'record_scoped_llm_response', Mock())
    monkeypatch.setattr(base, 'record_guided_llm_response', Mock())
    monkeypatch.setattr(base, 'record_guided_llm_failure', Mock())
    monkeypatch.setattr(base, 'sleep', Mock())


def call_helper(monkeypatch, body, requests, helper_name='chat_completion_parse'):
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=body, headers={
            'content-type': 'application/json', 'x-request-id': 'req_diagnostic_test',
            'set-cookie': COOKIE,
        })

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = OpenAI(api_key=API_KEY, base_url='http://127.0.0.1/v1',
                        http_client=transport, max_retries=0)
        monkeypatch.setattr(base, 'get_openai_client', lambda: client)
        if helper_name == 'multichat_completion_parse':
            return base.multichat_completion_parse([], PROMPT, MarkdownDraftResult,
                sys_prompt='Same drafting instructions.', model='gpt-6.1-sol',
                reasoning_effort='high')
        return base.chat_completion_parse(PROMPT, MarkdownDraftResult,
            sys_prompt='Same drafting instructions.', model='gpt-6.1-sol',
            reasoning_effort='high', max_output_tokens=1234, raise_on_incomplete=True)


@pytest.mark.parametrize('helper_name', ['chat_completion_parse', 'multichat_completion_parse'])
@pytest.mark.parametrize('diagnostics_enabled', [False, True])
@pytest.mark.parametrize('phase', [None, 'final_answer'])
def test_sdk_parses_final_draft_and_leaves_commentary_unparsed(
        tmp_path, monkeypatch, helper_name, diagnostics_enabled, phase):
    expected = {'title': 'Table', 'markdown': '# Table\n\n| ID | Amount |\n|---|---:|\n| 000001 | 12.50 |\n',
                'update_court_format': False}
    envelope = json.loads(response_body(json.dumps(expected)))
    if phase is not None:
        final = envelope['output'][0]
        final['phase'] = phase
        envelope['output'].insert(0, {
            'id': 'msg_commentary', 'type': 'message', 'role': 'assistant',
            'status': 'completed', 'phase': 'commentary',
            'content': [{'type': 'output_text', 'text': 'I have finished the reconciliation.',
                         'annotations': []}],
        })
    directory = tmp_path / 'diagnostics'
    if diagnostics_enabled:
        monkeypatch.setenv('LLM_PARSE_DIAGNOSTICS_DIR', str(directory))
    requests = []

    result = call_helper(monkeypatch, json.dumps(envelope).encode(), requests, helper_name)

    assert result.model_dump() == expected
    assert len(requests) == 1
    assert requests[0]['text']['format']['strict'] is True
    assert requests[0]['reasoning'] == {'effort': 'high'}
    assert not directory.exists()
    base.sleep.assert_not_called()
    base.record_guided_llm_failure.assert_not_called()


@pytest.mark.parametrize('helper_name', ['chat_completion_parse', 'multichat_completion_parse'])
def test_sdk_still_rejects_invalid_final_json_after_commentary(monkeypatch, helper_name):
    envelope = json.loads(response_body('{"title":"Incomplete",'))
    final = envelope['output'][0]
    final['phase'] = 'final_answer'
    commentary = dict(final, id='msg_commentary', phase='commentary', content=[{
        'type': 'output_text', 'text': 'Preparing the report.', 'annotations': [],
    }])
    envelope['output'].insert(0, commentary)
    requests = []

    assert call_helper(monkeypatch, json.dumps(envelope).encode(), requests, helper_name) is None

    assert len(requests) == 1
    base.record_guided_llm_failure.assert_called_once()
    base.sleep.assert_not_called()


@pytest.mark.parametrize('helper_name', ['chat_completion_parse', 'multichat_completion_parse'])
@pytest.mark.parametrize('status', ['refusal', 'incomplete'])
def test_sdk_does_not_release_refused_or_incomplete_drafts(monkeypatch, helper_name, status):
    envelope = json.loads(response_body(json.dumps({
        'title': 'Partial', 'markdown': '# Partial', 'update_court_format': False,
    })))
    envelope['output'][0]['phase'] = 'final_answer'
    if status == 'refusal':
        envelope['output'][0]['content'] = [{'type': 'refusal', 'refusal': 'Cannot provide this draft.'}]
    else:
        envelope['status'] = 'incomplete'
        envelope['incomplete_details'] = {'reason': 'max_output_tokens'}
    requests = []
    body = json.dumps(envelope).encode()

    if status == 'incomplete' and helper_name == 'chat_completion_parse':
        from backend.ai.token_budget import IncompleteStructuredOutput
        with pytest.raises(IncompleteStructuredOutput):
            call_helper(monkeypatch, body, requests, helper_name)
    else:
        assert call_helper(monkeypatch, body, requests, helper_name) is None

    assert len(requests) == 1
    base.sleep.assert_not_called()


def test_success_preserves_outgoing_json_and_result_without_writing_capture(tmp_path, monkeypatch):
    expected = {'title': 'Table', 'markdown': '# Table\n\n| ID | Amount |\n|---|---:|\n| 000001 | 12.50 |\n',
                'update_court_format': False}
    body = response_body(json.dumps(expected))
    requests = []
    plain = call_helper(monkeypatch, body, requests)
    directory = tmp_path / 'diagnostics'
    monkeypatch.setenv('LLM_PARSE_DIAGNOSTICS_DIR', str(directory))
    captured = call_helper(monkeypatch, body, requests)

    assert plain.model_dump() == captured.model_dump() == expected
    assert requests[0] == requests[1]
    assert requests[0]['reasoning'] == {'effort': 'high'}
    assert requests[0]['max_output_tokens'] == 1234
    assert requests[0]['text']['format']['type'] == 'json_schema'
    assert requests[0]['text']['format']['strict'] is True
    assert not directory.exists()
    base.sleep.assert_not_called()


@pytest.mark.parametrize(('text', 'location', 'error_type'), [
    (json.dumps({'title': 'Table', 'markdown': '# Table'}), ['update_court_format'], 'missing'),
    ('{"title":"Table","markdown":', [], 'json_invalid'),
])
def test_validation_failure_keeps_existing_outcome_and_captures_exact_response(
        tmp_path, monkeypatch, text, location, error_type):
    body = response_body(text)
    requests = []
    assert call_helper(monkeypatch, body, requests) is None
    directory = tmp_path / 'diagnostics'
    monkeypatch.setenv('LLM_PARSE_DIAGNOSTICS_DIR', str(directory))
    with workflows.execution('wf_diagnostic_test', 'lease-token-not-for-capture'):
        assert call_helper(monkeypatch, body, requests) is None

    assert len(requests) == 2  # One HTTP request per helper invocation; no extra retry.
    assert requests[0] == requests[1]
    base.sleep.assert_not_called()
    assert base.record_guided_llm_failure.call_count == 2
    base.record_scoped_llm_response.assert_not_called()

    captures = list(directory.iterdir())
    assert len(captures) == 1
    capture = captures[0]
    assert (capture / 'response-body.json').read_bytes() == body
    metadata_bytes = (capture / 'diagnostic.json').read_bytes()
    metadata = json.loads(metadata_bytes)
    assert metadata['exception_type'] == 'ValidationError'
    assert metadata['errors'][0]['loc'] == location
    assert metadata['errors'][0]['type'] == error_type
    assert 'input' not in metadata['errors'][0]
    assert metadata['http_status'] == 200
    assert metadata['request_id'] == 'req_diagnostic_test'
    assert metadata['response_id'] == 'resp_diagnostic_test'
    assert metadata['response_status'] == 'completed'
    assert metadata['incomplete_details'] is None
    assert metadata['usage']['total_tokens'] == 30
    assert metadata['workflow_id'] == 'wf_diagnostic_test'
    assert metadata['schema_name'] == 'MarkdownDraftResult'
    assert metadata['response_schema'] == MarkdownDraftResult.model_json_schema()
    assert metadata['model'] == 'gpt-6.1-sol'
    assert metadata['reasoning'] == {'effort': 'high'}
    assert 'model_validate_json' in metadata['traceback']
    for secret in [PROMPT, API_KEY, COOKIE, 'lease-token-not-for-capture']:
        assert secret.encode() not in metadata_bytes
    assert stat.S_IMODE(capture.stat().st_mode) == 0o700
    assert stat.S_IMODE((capture / 'diagnostic.json').stat().st_mode) == 0o600
    assert stat.S_IMODE((capture / 'response-body.json').stat().st_mode) == 0o600


def test_capture_filesystem_failure_does_not_replace_original_validation_error(tmp_path, monkeypatch):
    directory = tmp_path / 'diagnostics'
    monkeypatch.setenv('LLM_PARSE_DIAGNOSTICS_DIR', str(directory))
    body = response_body(json.dumps({'title': 'Table', 'markdown': '# Table'}))
    count = []

    def respond(request):
        count.append(request)
        return httpx.Response(200, content=body, headers={'content-type': 'application/json'})

    def cannot_write(*args, **kwargs):
        raise PermissionError('Diagnostic directory is unwritable')

    monkeypatch.setattr(Path, 'mkdir', cannot_write)
    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:
        client = OpenAI(api_key=API_KEY, base_url='http://127.0.0.1/v1',
                        http_client=transport, max_retries=0)
        with pytest.raises(ValidationError) as error:
            parse_diagnostics.parse_response(client, {'model': 'gpt-6.1-sol', 'input': PROMPT,
                'text_format': MarkdownDraftResult})
    assert error.value.errors(include_input=False)[0]['loc'] == ('update_court_format',)
    assert len(count) == 1
    assert not directory.exists()
