"""Opt-in pytest plugin: unit CI must never send fixture content to providers.

Enable with ``python -m pytest -p tests.offline_guard``. Localhost services (for example
the isolated Redis integration database) are allowed. Scheduled live evaluation
uses its separate explicit runner and does not load this plugin.
"""
import ipaddress
import socket
from urllib.parse import urlsplit

import pytest


def _local(address):
    if not isinstance(address, tuple):
        return False
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.hookimpl(tryfirst=True)
def pytest_sessionstart(session):
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(sock, address):
        if sock.family != socket.AF_UNIX and not _local(address):
            raise OSError("External network access is disabled in unit tests; mock the provider boundary.")
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family != socket.AF_UNIX and not _local(address):
            raise OSError("External network access is disabled in unit tests; mock the provider boundary.")
        return original_connect_ex(sock, address)

    guard = pytest.MonkeyPatch()
    guard.setattr(socket.socket, "connect", connect)
    guard.setattr(socket.socket, "connect_ex", connect_ex)
    # Block HTTP before a local proxy can forward a request externally. Socket
    # destination checks alone cannot distinguish a provider from its proxy.
    import requests
    original_send = requests.Session.send

    def send(client, request, **kwargs):
        if not _local((urlsplit(request.url).hostname, 0)):
            raise requests.ConnectionError("External HTTP is disabled in unit tests; mock the provider boundary.")
        return original_send(client, request, **kwargs)

    guard.setattr(requests.Session, "send", send)
    try:
        import httpx
    except ImportError:
        pass
    else:
        original_httpx_send = httpx.Client.send
        original_async_send = httpx.AsyncClient.send

        def httpx_send(client, request, **kwargs):
            if not _local((request.url.host, 0)):
                raise httpx.ConnectError("External HTTP is disabled in unit tests.", request=request)
            return original_httpx_send(client, request, **kwargs)

        async def async_send(client, request, **kwargs):
            if not _local((request.url.host, 0)):
                raise httpx.ConnectError("External HTTP is disabled in unit tests.", request=request)
            return await original_async_send(client, request, **kwargs)

        guard.setattr(httpx.Client, "send", httpx_send)
        guard.setattr(httpx.AsyncClient, "send", async_send)
    session.config.add_cleanup(guard.undo)
