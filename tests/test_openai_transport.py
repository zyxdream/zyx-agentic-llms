"""Exercise real SDK response decoding without external provider requests."""
import gzip
import json
from unittest.mock import Mock

import httpx2 as httpx
from openai import OpenAI

from backend.ai import base


def test_shared_client_negotiates_supported_compression_and_reuses_client(monkeypatch):
    monkeypatch.setattr(base, 'client', None)
    constructor = Mock(return_value=object())
    monkeypatch.setattr(base, 'OpenAI', constructor)
    first = base.get_openai_client()
    assert base.get_openai_client() is first
    constructor.assert_called_once()
    assert constructor.call_args.kwargs['default_headers'] == {'Accept-Encoding': 'gzip, deflate'}
    assert constructor.call_args.kwargs['max_retries'] == 0


def test_real_sdk_decodes_negotiated_gzip_tool_response(monkeypatch):
    observed = []
    payload = {'id': 'resp_offline', 'object': 'response', 'created_at': 1,
               'model': 'gpt-6-luna', 'status': 'completed',
               'output': [{'id': 'fc_offline', 'type': 'function_call',
                           'call_id': 'call_offline', 'name': 'list_documents',
                           'arguments': '{}', 'status': 'completed'}],
               'usage': {'input_tokens': 8, 'output_tokens': 2, 'total_tokens': 10}}

    def provider(request):
        observed.append(request.headers['accept-encoding'])
        assert observed[-1] == 'gzip, deflate'
        return httpx.Response(200, content=gzip.compress(json.dumps(payload).encode()),
                              headers={'Content-Encoding': 'gzip', 'Content-Type': 'application/json'})

    transport = httpx.Client(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(base, 'client', None)
    monkeypatch.setattr(base, 'OpenAI', lambda **kwargs: OpenAI(
        **{**kwargs, 'api_key': 'offline-placeholder'}, http_client=transport))
    try:
        counter = {}
        result = base.responses_completion({'model': 'gpt-6-luna', 'input': 'Offline test'},
                                           token_counter=counter)
        assert result is not None
        assert result.output[0].name == 'list_documents'
        assert counter == {'total': 10}
        # Worker-scoped timeout copies must preserve compression negotiation.
        assert base.client.with_options(timeout=None).default_headers['Accept-Encoding'] == 'gzip, deflate'
        assert len(observed) == 1
    finally:
        transport.close()
