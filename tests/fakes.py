"""Local protocol fakes so HTTP, client, and cache tests never touch a socket.

These stand in for ``requests.Session`` and the JSON-fetch helpers with scripted,
deterministic responses. ``block_network`` proves a test opened no real
connection, which is what keeps the deterministic E2E oracle honest.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import requests


class FakeResponse:
    """Minimal stand-in for ``requests.Response`` used by ``_http_request``.

    Carries a status code, headers, and body, and raises ``HTTPError`` from
    ``raise_for_status`` for any 4xx/5xx exactly as ``requests`` does.
    """

    def __init__(self, status_code: int = 200, *, body: bytes = b"{}", headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self.content = body
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Error", response=self)  # type: ignore[arg-type]


class FakeSession:
    """Scriptable stand-in for ``requests.Session``.

    Supply a single response or a sequence of responses (e.g. ``[500, 500, 200]``
    for retry tests). Every ``get``/``post`` pops the next scripted response and
    is counted per verb, so a test can assert exactly how many requests a status
    sequence triggered (proving no retry storm and no POST re-send).
    """

    def __init__(self, responses: list[FakeResponse] | FakeResponse) -> None:
        self._responses = [responses] if isinstance(responses, FakeResponse) else list(responses)
        self._i = 0
        self.get_calls = 0
        self.post_calls = 0
        self.closed = False

    def _next(self) -> FakeResponse:
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.get_calls += 1
        return self._next()

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        self.post_calls += 1
        return self._next()

    def close(self) -> None:
        self.closed = True


def fake_http_json(url_map: dict[str, dict[str, Any]]):  # type: ignore[no-untyped-def]
    """Return a drop-in for ``http_get_json`` that serves canned payloads.

    ``url_map`` maps a substring of the request URL to the JSON dict to return,
    so a client test can supply a realistic API payload without any network.
    A URL matching no key raises, surfacing an unstubbed call rather than
    silently hitting the wire.
    """

    def _get(url: str, timeout: float = 0.0) -> dict[str, Any]:
        for needle, payload in url_map.items():
            if needle in url:
                return payload
        raise AssertionError(f"unstubbed URL in test: {url}")

    return _get


class _NoNetworkSocket(socket.socket):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("network access is blocked in this test")


def install_block_network(monkeypatch: Any) -> None:
    """Patch ``socket.socket`` so any real connection attempt fails loudly.

    Call from a test (or an autouse fixture) that must prove it stays offline.
    """
    monkeypatch.setattr(socket, "socket", _NoNetworkSocket)


def scripted_statuses(*statuses: int, body: bytes = b"{}") -> Iterator[FakeResponse]:
    """Yield a FakeResponse per status code, for building a FakeSession script."""
    for code in statuses:
        yield FakeResponse(code, body=body)
