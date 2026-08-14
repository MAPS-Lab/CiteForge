"""Local protocol fakes for deterministic HTTP, client, and cache tests."""

from __future__ import annotations

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

    def close(self) -> None:
        return None


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
