from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import format_datetime

import pytest
import requests

from citeforge import http_utils
from citeforge.config import HTTP_BACKOFF_MAX, SESSION_ROTATION_THRESHOLD
from citeforge.exceptions import DecodeError
from citeforge.http_utils import _cookie_header, _decode_json_bytes, _scrub_secrets, decode_json_mapping
from tests.corpus import RETRY_AFTER_CASES
from tests.fakes import FakeResponse, FakeSession


class ScriptSession:
    """Session double that can return responses or raise scripted exceptions."""

    def __init__(self, effects: list[FakeResponse | requests.exceptions.RequestException]) -> None:
        self.effects = effects
        self.calls = 0
        self.closed = False

    def _send(self) -> FakeResponse:
        effect = self.effects[min(self.calls, len(self.effects) - 1)]
        self.calls += 1
        if isinstance(effect, BaseException):
            raise effect
        return effect

    def get(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._send()

    def post(self, *args: object, **kwargs: object) -> FakeResponse:
        return self._send()

    def close(self) -> None:
        self.closed = True


class TestSecretRedaction:
    """API keys and tokens passed as query params must never reach logs or exception text.

    Gemini uses ``?key=`` and SerpAPI uses ``&api_key=``. These URLs must not reach
    a WARN log or an exception message, since run logs can be committed to a public
    branch.
    """

    @pytest.mark.parametrize(
        ("raw", "secret"),
        [
            ("https://generativelanguage.googleapis.com/v1beta?key=AIzaSECRET", "AIzaSECRET"),
            ("https://serpapi.com/search?engine=google_scholar_author&api_key=abc123SECRET", "abc123SECRET"),
            ("429 Client Error for url: https://x/y?token=tok_SECRET&q=1", "tok_SECRET"),
            ("https://x?apikey=SECRET", "SECRET"),
            ("https://x?access_token=SECRET", "SECRET"),
        ],
    )
    def test_scrub_removes_secret(self, raw: str, secret: str) -> None:
        scrubbed = _scrub_secrets(raw)
        assert secret not in scrubbed
        assert "REDACTED" in scrubbed

    def test_scrub_preserves_nonsecret_params(self) -> None:
        url = "https://api.crossref.org/works?query=deep+learning&rows=5&mailto=a@b.com"
        assert _scrub_secrets(url) == url

    def test_scrub_redacts_value_not_param_name_and_keeps_siblings(self) -> None:
        assert _scrub_secrets("https://x?api_key=SECRET&keep=1") == "https://x?api_key=REDACTED&keep=1"

    def test_decode_json_error_redacts_url_but_still_raises(self) -> None:
        url = "https://generativelanguage.googleapis.com/v1beta?key=AIzaSECRETKEY"
        with pytest.raises(ValueError) as excinfo:
            _decode_json_bytes(b"<html>not json</html>", url)
        message = str(excinfo.value)
        assert "AIzaSECRETKEY" not in message
        assert "REDACTED" in message

    def test_decode_json_valid_passthrough(self) -> None:
        assert _decode_json_bytes(b'{"a": 1}', "https://x?key=S") == {"a": 1}


class TestDecodeJsonMapping:
    @pytest.mark.parametrize("raw", [b"[]", b"null", b'"unexpected"'])
    def test_rejects_valid_json_with_a_non_mapping_root(self, raw: bytes) -> None:
        with pytest.raises(DecodeError, match="JSON object"):
            decode_json_mapping(raw, "https://provider.example/records")

    def test_leaves_provider_envelope_validation_to_the_adapter(self) -> None:
        response_without_a_provider_envelope = {"unrecognized": []}
        assert (
            decode_json_mapping(b'{"unrecognized": []}', "https://provider.example/records")
            == response_without_a_provider_envelope
        )


def test_cookie_header_uses_only_cookie_pairs() -> None:
    """Set-Cookie attributes are not forwarded in a request Cookie header."""
    assert _cookie_header("sid=abc; Path=/; HttpOnly; SameSite=Lax") == "sid=abc"


class TestRetryBounding:
    """Tenacity is the sole controller and bounds each logical request to three sends."""

    def test_requests_adapters_disable_transport_retries(self) -> None:
        session = http_utils._new_session()
        assert session.get_adapter("https://").max_retries.total == 0
        assert session.get_adapter("http://").max_retries.total == 0

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_get_retries_selected_status_to_success(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        session = FakeSession([FakeResponse(status), FakeResponse(status), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        assert http_utils._http_request("GET", "https://example.com/x", {}, 1.0) == b"{}"
        assert session.get_calls == 3

    @pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
    def test_get_exhaustion_raises_final_http_error(self, monkeypatch: pytest.MonkeyPatch, status: int) -> None:
        final = FakeResponse(status)
        session = FakeSession([FakeResponse(status), FakeResponse(status), final])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(requests.exceptions.HTTPError) as excinfo:
            http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert session.get_calls == 3
        assert excinfo.value.response is final

    @pytest.mark.parametrize(("status", "calls"), [(408, 1), (429, 3), (500, 1), (502, 1), (503, 3), (504, 1)])
    def test_post_retries_only_explicit_rate_responses(
        self, monkeypatch: pytest.MonkeyPatch, status: int, calls: int
    ) -> None:
        session = FakeSession([FakeResponse(status), FakeResponse(status), FakeResponse(status)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(requests.exceptions.HTTPError):
            http_utils._http_request("POST", "https://example.com/x", {}, 1.0, json_payload={"q": 1})
        assert session.post_calls == calls

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_nonretryable_4xx_sent_once(self, monkeypatch: pytest.MonkeyPatch, method: str) -> None:
        session = FakeSession(FakeResponse(404))
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(requests.exceptions.HTTPError):
            http_utils._http_request(method, "https://example.com/x", {}, 1.0, json_payload={})
        assert session.get_calls + session.post_calls == 1


class TestExceptionRetryPolicy:
    @pytest.mark.parametrize(
        "error_type",
        [requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError],
    )
    def test_get_retries_transient_exception_and_reraises_original_type(
        self, monkeypatch: pytest.MonkeyPatch, error_type: type[requests.exceptions.RequestException]
    ) -> None:
        session = ScriptSession([error_type("failed?key=SECRET")])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(error_type) as excinfo:
            http_utils._http_request("GET", "https://example.com/x?key=SECRET", {}, 1.0)
        assert session.calls == 3
        assert "SECRET" not in str(excinfo.value)
        assert "REDACTED" in str(excinfo.value)

    @pytest.mark.parametrize(
        "error_type",
        [
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            requests.exceptions.ChunkedEncodingError,
        ],
    )
    def test_post_transport_exception_is_never_retried(
        self, monkeypatch: pytest.MonkeyPatch, error_type: type[requests.exceptions.RequestException]
    ) -> None:
        session = ScriptSession([error_type("failed")])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(error_type):
            http_utils._http_request("POST", "https://example.com/x", {}, 1.0, json_payload={})
        assert session.calls == 1

    @pytest.mark.parametrize(
        "error_type",
        [
            requests.exceptions.InvalidURL,
            requests.exceptions.MissingSchema,
            requests.exceptions.InvalidHeader,
            requests.exceptions.TooManyRedirects,
        ],
    )
    def test_invalid_get_request_is_never_retried(
        self, monkeypatch: pytest.MonkeyPatch, error_type: type[requests.exceptions.RequestException]
    ) -> None:
        session = ScriptSession([error_type("invalid")])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(error_type):
            http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert session.calls == 1


class TestParseRetryAfter:
    """_parse_retry_after interprets numeric delays, HTTP dates, and junk deterministically.

    Numeric and unparseable cases come from the corpus table; the HTTP-date cases depend
    on the clock, so a past date must clamp to 0.0 and a future date (against a frozen now)
    must return the exact positive delta.
    """

    @pytest.mark.parametrize(("header", "expected"), RETRY_AFTER_CASES)
    def test_table_cases(self, header: str | None, expected: float) -> None:
        assert http_utils._parse_retry_after(header) == expected

    def test_past_http_date_clamped_to_zero(self) -> None:
        # An HTTP date in the past must never yield a negative wait; it clamps to 0.0.
        assert http_utils._parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0

    def test_future_http_date_returns_positive_delta(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
                return fixed_now.astimezone(tz) if tz is not None else fixed_now

        monkeypatch.setattr(http_utils, "datetime", _FrozenDateTime)
        header = format_datetime(fixed_now + timedelta(seconds=300), usegmt=True)
        result = http_utils._parse_retry_after(header)
        assert result == pytest.approx(300.0, abs=1.0)
        assert result > 0.0


class TestBackoffCapAndPostRetry:
    """The manual 429/503 loop caps its sleep at HTTP_BACKOFF_MAX and never auto-resends a POST body."""

    def test_retry_after_sleep_capped_at_backoff_max(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        monkeypatch.setattr(http_utils.time, "sleep", lambda s: sleeps.append(s))
        session = FakeSession([FakeResponse(429, headers={"Retry-After": "1000"}), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        http_utils._THREAD_LOCAL.session_request_count = 0

        out = http_utils._http_request("GET", "https://example.com/x", {"Accept": "*/*"}, 1.0)

        assert out == b"{}"
        # A 1000 s Retry-After is clamped to the configured ceiling, not slept verbatim.
        assert sleeps == [HTTP_BACKOFF_MAX]

    def test_post_500_sent_once_then_httperror_propagates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = FakeSession([FakeResponse(500)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0

        with pytest.raises(requests.exceptions.HTTPError):
            http_utils._http_request("POST", "https://example.com/x", {"Accept": "*/*"}, 1.0, json_payload={"q": 1})

        # The non-idempotent body is sent exactly once; no silent re-send on a hard 500.
        assert session.post_calls == 1
        assert session.get_calls == 0

    def test_post_429_429_200_reaches_success_in_three_calls(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = FakeSession([FakeResponse(429), FakeResponse(429), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0

        out = http_utils._http_request("POST", "https://example.com/x", {"Accept": "*/*"}, 1.0, json_payload={"q": 1})

        assert out == b"{}"
        # Manual 429 handling re-sends the POST twice, succeeding on the third call.
        assert session.post_calls == 3
        assert session.get_calls == 0

    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("4", 4.0),
            ("0", 1.0),
            ("-2", 1.0),
            ("junk", 1.0),
            ("Wed, 21 Oct 2015 07:28:00 GMT", 1.0),
            ("1000", HTTP_BACKOFF_MAX),
        ],
    )
    def test_retry_after_and_fallback_waits(
        self, monkeypatch: pytest.MonkeyPatch, header: str, expected: float
    ) -> None:
        sleeps: list[float] = []
        session = FakeSession([FakeResponse(429, headers={"Retry-After": header}), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", sleeps.append)
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert sleeps == [expected]

    def test_http_date_retry_after_is_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fixed_now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        class _FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz: tzinfo | None = None) -> datetime:  # type: ignore[override]
                return fixed_now.astimezone(tz) if tz is not None else fixed_now

        sleeps: list[float] = []
        header = format_datetime(fixed_now + timedelta(seconds=4), usegmt=True)
        session = FakeSession([FakeResponse(503, headers={"Retry-After": header}), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "datetime", _FrozenDateTime)
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", sleeps.append)
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert sleeps == [4.0]

    def test_retry_wait_occurs_with_semaphore_free(self, monkeypatch: pytest.MonkeyPatch) -> None:
        semaphore = threading.Semaphore(1)
        was_free: list[bool] = []
        session = FakeSession([FakeResponse(429), FakeResponse(200)])

        def check_sleep(_seconds: float) -> None:
            acquired = semaphore.acquire(blocking=False)
            was_free.append(acquired)
            if acquired:
                semaphore.release()

        monkeypatch.setattr(http_utils, "_GLOBAL_SEMAPHORE", semaphore)
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", check_sleep)
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert was_free == [True]

    def test_fallback_waits_are_deterministic_exponential_delays(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sleeps: list[float] = []
        session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(200)])
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(
            http_utils.random,
            "uniform",
            lambda *_a: pytest.fail("retry waits must not use random jitter"),
        )
        monkeypatch.setattr(http_utils.time, "sleep", sleeps.append)
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://example.com/x", {}, 1.0)
        assert sleeps == [1.0, 2.0]


class TestLogicalAndTransportAccounting:
    def test_single_send_preserves_buffered_default_and_allows_durable_streaming(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        streams: list[object] = []

        class Session:
            def get(self, _url: str, **kwargs: object) -> FakeResponse:
                streams.append(kwargs.get("stream", "absent"))
                return FakeResponse(200)

        monkeypatch.setattr(http_utils, "_get_session", lambda: Session())
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils.send_http_once("GET", "https://example.com/x", {}, 1.0)
        http_utils.send_http_once("GET", "https://example.com/x", {}, 1.0, stream=True)
        assert streams == [False, True]

    @pytest.mark.parametrize(("timeout", "expected"), [(5.0, (5.0, 5.0)), (20.0, (10.0, 20.0))])
    def test_timeout_tuple_preserves_connect_cap(
        self, monkeypatch: pytest.MonkeyPatch, timeout: float, expected: tuple[float, float]
    ) -> None:
        seen: dict[str, object] = {}

        class Session:
            def get(self, _url: str, **kwargs: object) -> FakeResponse:
                seen.update(kwargs)
                return FakeResponse(200)

        monkeypatch.setattr(http_utils, "_get_session", lambda: Session())
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://example.com/x", {}, timeout)
        assert seen["timeout"] == expected

    def test_logical_setup_once_and_session_count_per_send(self, monkeypatch: pytest.MonkeyPatch) -> None:
        session = FakeSession([FakeResponse(500), FakeResponse(500), FakeResponse(200)])
        limiter_calls = 0
        header_calls = 0

        class Limiter:
            def acquire(self) -> None:
                nonlocal limiter_calls
                limiter_calls += 1

        def randomize(headers: dict[str, str]) -> dict[str, str]:
            nonlocal header_calls
            header_calls += 1
            return headers

        http_utils.reset_api_call_counts()
        monkeypatch.setattr(http_utils, "_get_rate_limiter", lambda _namespace: Limiter())
        monkeypatch.setattr(http_utils, "_randomize_headers", randomize)
        monkeypatch.setattr(http_utils, "_get_session", lambda: session)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        http_utils._http_request("GET", "https://api.crossref.org/works", {}, 1.0)
        assert http_utils.get_api_call_counts()["crossref"] == 1
        assert limiter_calls == 1
        assert header_calls == 1
        assert http_utils._THREAD_LOCAL.session_request_count == 3

    def test_session_can_rotate_between_retry_attempts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        first = FakeSession(FakeResponse(500))
        second = FakeSession(FakeResponse(200))
        monkeypatch.setattr(http_utils, "_new_session", lambda: second)
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        try:
            http_utils._THREAD_LOCAL.session = first
            http_utils._THREAD_LOCAL.session_request_count = SESSION_ROTATION_THRESHOLD - 1
            assert http_utils._http_request("GET", "https://example.com/x", {}, 1.0) == b"{}"
            assert first.get_calls == 1
            assert first.closed is True
            assert second.get_calls == 1
            assert http_utils._THREAD_LOCAL.session_request_count == 1
        finally:
            http_utils._THREAD_LOCAL.session = None
            http_utils._THREAD_LOCAL.session_request_count = 0


class TestSessionRotation:
    """_get_session rotates the per-thread Session at SESSION_ROTATION_THRESHOLD, closing the old one."""

    def test_reuses_below_threshold_and_rotates_at_threshold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        factory_calls = {"n": 0}

        def _factory() -> FakeSession:
            factory_calls["n"] += 1
            return FakeSession(FakeResponse(200))

        monkeypatch.setattr(http_utils, "_new_session", _factory)
        try:
            http_utils._THREAD_LOCAL.session = None
            http_utils._THREAD_LOCAL.session_request_count = 0

            first = http_utils._get_session()
            assert isinstance(first, FakeSession)
            assert factory_calls["n"] == 1

            # Below the threshold: same Session, no fresh build, old one still open.
            http_utils._THREAD_LOCAL.session_request_count = SESSION_ROTATION_THRESHOLD - 1
            same = http_utils._get_session()
            assert same is first
            assert factory_calls["n"] == 1
            assert first.closed is False

            # At the threshold: old Session closed, a fresh one built, counter reset to 0.
            http_utils._THREAD_LOCAL.session_request_count = SESSION_ROTATION_THRESHOLD
            rotated = http_utils._get_session()
            assert rotated is not first
            assert first.closed is True
            assert factory_calls["n"] == 2
            assert http_utils._THREAD_LOCAL.session_request_count == 0
        finally:
            http_utils._THREAD_LOCAL.session = None
            http_utils._THREAD_LOCAL.session_request_count = 0


class TestDecodeJsonBodyScrub:
    """A non-JSON error body carrying a query-string secret is redacted before it reaches the DecodeError."""

    def test_secret_in_body_redacted_not_leaked(self) -> None:
        raw = b"upstream 400 ?api_key=SECRETVALUE&q=1 <html>not json</html>"
        with pytest.raises(DecodeError) as excinfo:
            http_utils._decode_json_bytes(raw, "https://api.crossref.org/works")
        message = str(excinfo.value)
        assert "SECRETVALUE" not in message
        assert "REDACTED" in message
