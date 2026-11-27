import gzip
import hashlib
import io
import json
import socket
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from citeforge.http import Http, _scrub_secrets

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("tests must not open network connections")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def fixture_name(method: str, url: str) -> str:
    return hashlib.sha1((method + _scrub_secrets(url)).encode(), usedforsecurity=False).hexdigest()


def fixture_path(root: Path, method: str, url: httpx.URL | str) -> Path:
    u = httpx.URL(str(url))
    return root / u.host / f"{fixture_name(method, str(u))}.json.gz"


def fixture_transport(root: Path = FIXTURES / "http") -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = fixture_path(root, request.method, request.url)
        if not path.exists():
            raise AssertionError(f"no recorded fixture for {request.method} {_scrub_secrets(str(request.url))}")
        data = json.loads(gzip.decompress(path.read_bytes()))
        return httpx.Response(data["status"], headers=data["headers"], content=data["body"].encode())

    return httpx.MockTransport(handle)


Handler = Callable[[httpx.Request], httpx.Response]


def make_http(handler: Handler, clock: FakeClock | None = None, keys: dict[str, str] | None = None) -> Http:
    clock = clock or FakeClock()
    return Http(io.StringIO(), keys or {}, transport=httpx.MockTransport(handler), clock=clock, sleep=clock.sleep)


def json_response(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        status, headers={"content-type": "application/json", **(headers or {})}, content=json.dumps(payload).encode()
    )


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()
