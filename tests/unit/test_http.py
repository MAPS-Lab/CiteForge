import io
import json

import httpx
import pytest

from citeforge.http import (
    CIRCUIT_FAILURES,
    HOSTS,
    MAX_RETRY_WAIT_S,
    USER_AGENT,
    Http,
    _parse_retry_after,
    _scrub_secrets,
)
from citeforge.model import Fetch
from tests.conftest import FakeClock, json_response, make_http

CROSSREF = HOSTS["api.crossref.org"]
OPENALEX = HOSTS["api.openalex.org"]
DBLP = HOSTS["dblp.org"]
DOI = HOSTS["doi.org"]


def sequence(*responses: httpx.Response | Exception):
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return item

    return handle, seen


def test_spacing_and_headers(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}))
    http = make_http(handle, clock)
    assert http.get(CROSSREF, "/works").fetch is Fetch.OK
    assert http.get(CROSSREF, "/works").fetch is Fetch.OK
    assert clock.sleeps == [CROSSREF.min_interval_s]
    assert seen[0].headers["user-agent"] == USER_AGENT
    assert "mailto" not in str(seen[0].url)


def test_dblp_gets_app_param(clock: FakeClock) -> None:
    handle, seen = sequence(httpx.Response(200, headers={"content-type": "application/xml"}, content=b"<x/>"))
    make_http(handle, clock).get(DBLP, "/pid/1/2.xml")
    assert seen[0].url.params["app"] == "citeforge"


def test_retry_after_honoured_exactly(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}, 429, {"retry-after": "6"}), json_response({}))
    reply = make_http(handle, clock).get(CROSSREF, "/works")
    assert reply.fetch is Fetch.OK
    assert clock.sleeps == [6.0]
    assert len(seen) == 2


def test_long_retry_after_marks_host_down_without_sleep(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}, 429, {"retry-after": "6626"}))
    http = make_http(handle, clock, keys={"api.openalex.org": "SECRETKEY"})
    reply = http.get(OPENALEX, "/works")
    assert reply.fetch is Fetch.FAILED
    assert http.down(OPENALEX) == "retry_after 6626s"
    assert clock.sleeps == []
    assert http.get(OPENALEX, "/works").reason == "down"
    assert len(seen) == 1


def test_backoff_then_failed_is_never_empty(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}, 503))
    reply = make_http(handle, clock).get(CROSSREF, "/works")
    assert reply.fetch is Fetch.FAILED
    assert reply.reason == "http_error"
    assert len(seen) == 4
    assert len(clock.sleeps) == 3
    for slept, base in zip(clock.sleeps, (1.0, 2.0, 4.0), strict=True):
        assert 0.75 * base <= slept <= 1.25 * base


def test_timeout_is_retried(clock: FakeClock) -> None:
    handle, _ = sequence(httpx.ConnectTimeout("slow"), httpx.ConnectError("reset"), json_response({"ok": 1}))
    http = make_http(handle, clock)
    reply = http.get(CROSSREF, "/works")
    assert reply.fetch is Fetch.OK
    assert json.loads(reply.body) == {"ok": 1}
    assert http.stats()["api.crossref.org"]["timeouts"] == 1
    assert http.stats()["api.crossref.org"]["retries"] == 2


def test_corrupt_body_is_retried_then_failed(clock: FakeClock) -> None:
    answers: list[bool] = []

    def handle(request: httpx.Request) -> httpx.Response:
        answers.append(True)
        if len(answers) > limit:
            return json_response({"ok": 1})
        headers = {"content-type": "application/json", "content-encoding": "gzip"}
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"not gzip"))

    limit = 99
    reply = make_http(handle, clock).get(CROSSREF, "/works")
    assert (reply.fetch, reply.reason, len(answers)) == (Fetch.FAILED, "decode_error", 4)
    answers.clear()
    limit = 1
    assert make_http(handle, clock).get(CROSSREF, "/works").fetch is Fetch.OK


def test_circuit_breaker_after_five_failures(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}, 403))
    http = make_http(handle, clock)
    for _ in range(5):
        assert http.get(CROSSREF, "/works").reason == "http_error"
    assert http.down(CROSSREF) == "circuit"
    assert http.get(CROSSREF, "/works").reason == "down"
    assert len(seen) == 5


def test_not_found_resets_failures(clock: FakeClock) -> None:
    handle, _ = sequence(json_response({}, 403), json_response({}, 403), json_response({}, 404))
    http = make_http(handle, clock)
    http.get(CROSSREF, "/a")
    http.get(CROSSREF, "/b")
    assert http.get(CROSSREF, "/c").fetch is Fetch.NOT_FOUND
    assert http.stats()["api.crossref.org"]["not_found"] == 1


def test_budget(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}))
    http = make_http(handle, clock)
    for _ in range(HOSTS["export.arxiv.org"].budget):
        http.get(CROSSREF, "/w")
    for _ in range(CROSSREF.budget - HOSTS["export.arxiv.org"].budget):
        http.get(CROSSREF, "/w")
    reply = http.get(CROSSREF, "/w")
    assert reply.reason == "budget"
    assert http.down(CROSSREF) == "budget"
    assert len(seen) == CROSSREF.budget


def test_challenges(clock: FakeClock) -> None:
    anubis = httpx.Response(202, headers={"content-type": "text/html"}, content=b"<html>checking</html>")
    handle, _ = sequence(anubis)
    assert make_http(handle, clock).get(DBLP, "/pid/x.xml").reason == "challenge"
    html_ok = httpx.Response(200, headers={"content-type": "text/html"}, content=b"<!DOCTYPE html><p>")
    handle, _ = sequence(html_ok)
    assert make_http(handle, clock).get(CROSSREF, "/works").reason == "challenge"
    wrong = httpx.Response(200, headers={"content-type": "text/plain"}, content=b"{}")
    handle, _ = sequence(wrong)
    assert make_http(handle, clock).get(CROSSREF, "/works").reason == "parse_error"


def test_redirect_reply_and_allowlist(clock: FakeClock) -> None:
    moved = httpx.Response(302, headers={"location": "https://data.medra.org/10.1400%2F245393"})
    handle, seen = sequence(moved)
    http = make_http(handle, clock)
    for _ in range(6):
        reply = http.get(DOI, "/10.1400/245393")
        assert reply.reason == "redirect"
        assert reply.location == "https://data.medra.org/10.1400%2F245393"
    assert http.down(DOI) is None
    assert len(seen) == 6


@pytest.mark.parametrize(
    "location", ["https://evil.example.com/x", "http://data.medra.org/x", "https://doi.org:8443/x"]
)
def test_redirect_off_allowlist_sends_no_request(clock: FakeClock, location: str) -> None:
    handle, seen = sequence(httpx.Response(302, headers={"location": location}))
    reply = make_http(handle, clock).get(DOI, "/10.1400/245393", follow_redirects=True)
    assert reply.reason == "redirect_blocked"
    assert len(seen) == 1


def test_followed_redirect_reports_the_final_url_and_keeps_params_on_the_host(clock: FakeClock) -> None:
    xml = httpx.Response(200, headers={"content-type": "application/xml"}, content=b"<x/>")
    handle, seen = sequence(httpx.Response(301, headers={"location": "/pid/9/9.xml"}), xml)
    reply = make_http(handle, clock).get(DBLP, "/pid/1/2.xml", follow_redirects=True)
    assert reply.fetch is Fetch.OK
    assert reply.url == str(seen[1].url) == "https://dblp.org/pid/9/9.xml?app=citeforge"


def test_followed_redirect_to_another_host_drops_the_key(clock: FakeClock) -> None:
    handle, seen = sequence(httpx.Response(302, headers={"location": "https://doi.org/x"}), json_response({}))
    make_http(handle, clock, keys={"api.openalex.org": "SECRETKEY"}).get(OPENALEX, "/works", follow_redirects=True)
    assert "api_key" in seen[0].url.params and "SECRETKEY" not in str(seen[1].url)


def test_log_lines_are_scrubbed(clock: FakeClock) -> None:
    log = io.StringIO()
    handle, seen = sequence(json_response({}))
    http = Http(log, {"api.openalex.org": "SECRETKEY"}, httpx.MockTransport(handle), clock=clock, sleep=clock.sleep)
    http.get(OPENALEX, "/works", {"filter": "doi:10.1/x"})
    assert seen[0].url.params["api_key"] == "SECRETKEY"
    line = json.loads(log.getvalue())
    assert "SECRETKEY" not in log.getvalue()
    assert line["host"] == "api.openalex.org"
    assert line["status"] == 200
    assert line["attempt"] == 1
    assert set(line) == {"t", "host", "member", "path", "status", "attempt", "ms", "bytes", "retry_after", "reason"}


def test_scrub_and_retry_after_parsing() -> None:
    assert _scrub_secrets("/w?api_key=abc&x=1") == "/w?api_key=REDACTED&x=1"
    assert _scrub_secrets("/w?token=abc") == "/w?token=REDACTED"
    assert _parse_retry_after("12") == 12.0
    assert _parse_retry_after("-3") == 0.0
    assert _parse_retry_after("garbage") == 0.0
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
    assert _parse_retry_after(None) == 0.0


def test_retry_after_on_the_last_attempt_delays_the_next_call(clock: FakeClock) -> None:
    sent: list[float] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(clock.now)
        return json_response({}, 429, {"retry-after": "60"})

    http = make_http(handle, clock)
    http.get(CROSSREF, "/works")
    http.get(CROSSREF, "/works")
    assert len(sent) > 4
    assert sent[4] - sent[3] >= 60.0


def test_throttling_host_goes_down_once_its_wait_budget_is_spent(clock: FakeClock) -> None:
    handle, seen = sequence(json_response({}, 429, {"retry-after": "120"}))
    http = make_http(handle, clock)
    calls = 0
    while http.down(CROSSREF) is None:
        http.get(CROSSREF, "/works")
        calls += 1
    down = http.down(CROSSREF)
    assert down is not None and down.startswith("retry_wait")
    assert sum(clock.sleeps) <= MAX_RETRY_WAIT_S
    assert calls == 1 and len(seen) == 3


@pytest.mark.parametrize("status", [400, 409])
def test_record_level_answers_never_open_the_circuit(clock: FakeClock, status: int) -> None:
    handle, seen = sequence(json_response({"error-code": 9007}, status))
    http = make_http(handle, clock)
    for _ in range(CIRCUIT_FAILURES + 3):
        assert http.get(CROSSREF, "/works").reason == "http_error"
    assert http.down(CROSSREF) is None and len(seen) == CIRCUIT_FAILURES + 3
