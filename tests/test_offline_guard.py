"""Prove the guard blocks before transport, including a loopback HTTP proxy."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

from tests import offline_guard


def test_provider_request_never_reaches_http_adapter_even_through_local_proxy(monkeypatch):
    cleanup = []
    offline_guard.pytest_sessionstart(SimpleNamespace(config=SimpleNamespace(add_cleanup=cleanup.append)))
    transport = Mock(side_effect=AssertionError("No transport should execute"))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", transport)
    try:
        with pytest.raises(requests.ConnectionError, match="disabled"):
            requests.post("https://provider.invalid/citations", json={"text": "Fictional test passage"}, proxies={"https": "http://127.0.0.1:9999"})
        transport.assert_not_called()
    finally:
        for undo in reversed(cleanup):
            undo()


def test_httpx_provider_request_never_reaches_its_transport(monkeypatch):
    httpx = pytest.importorskip("httpx")
    cleanup = []
    offline_guard.pytest_sessionstart(SimpleNamespace(config=SimpleNamespace(add_cleanup=cleanup.append)))
    transport = Mock(side_effect=AssertionError("No transport should execute"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", transport)
    try:
        # A mock transport makes this test incapable of networking even if the
        # guard regresses; host proxy configuration is irrelevant to the test.
        with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client, pytest.raises(httpx.ConnectError, match="disabled"):
            client.post("https://provider.invalid/model", json={"text": "Fictional test passage"})
        transport.assert_not_called()
    finally:
        for undo in reversed(cleanup):
            undo()
