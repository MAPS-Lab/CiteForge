from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo
from email.utils import format_datetime

import pytest
import requests

from citeforge import http_utils
from citeforge.config import HTTP_BACKOFF_MAX, SESSION_ROTATION_THRESHOLD
from citeforge.exceptions import DecodeError
from citeforge.http_utils import _decode_json_bytes, _scrub_secrets
from tests.corpus import RETRY_AFTER_CASES
from tests.fakes import FakeResponse, FakeSession


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


class TestRetryBounding:
    """Persistent 5xx must not compound urllib3 x manual retries into ~9 requests,
    and non-idempotent POST must not be auto-retried by urllib3.
    429/503 stays single-layer (excluded from urllib3, handled by the manual loop).
    """

    def test_post_excluded_from_urllib3_retry(self) -> None:
        assert "POST" not in http_utils._RETRY_STRATEGY.allowed_methods
        assert "GET" in http_utils._RETRY_STRATEGY.allowed_methods

    def test_retry_error_not_redriven(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = {"n": 0}

        class _Sess:
            def get(self, *args: object, **kwargs: object) -> object:
                calls["n"] += 1
                raise requests.exceptions.RetryError("urllib3 exhausted 500s")

        monkeypatch.setattr(http_utils, "_get_session", lambda: _Sess())
        http_utils._THREAD_LOCAL.session_request_count = 0
        with pytest.raises(requests.exceptions.RetryError):
            http_utils._http_request("GET", "https://example.com/x", {"Accept": "*/*"}, 1.0)
        # One session.get call, not 3 manual iterations (which were 3 urllib3 each = 9).
        assert calls["n"] == 1

    def test_429_still_retried_by_manual_loop(self, monkeypatch: pytest.MonkeyPatch) -> None:
        attempts = {"n": 0}

        class _Resp:
            def __init__(self, status: int) -> None:
                self.status_code = status
                self.headers: dict[str, str] = {}
                self.content = b"ok"

            def raise_for_status(self) -> None:
                return None

        class _Sess:
            def get(self, *args: object, **kwargs: object) -> _Resp:
                attempts["n"] += 1
                return _Resp(429 if attempts["n"] < 3 else 200)

        monkeypatch.setattr(http_utils, "_get_session", lambda: _Sess())
        monkeypatch.setattr(http_utils.time, "sleep", lambda *_a: None)
        http_utils._THREAD_LOCAL.session_request_count = 0
        out = http_utils._http_request("GET", "https://example.com/x", {"Accept": "*/*"}, 1.0)
        assert out == b"ok"
        assert attempts["n"] == 3


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
