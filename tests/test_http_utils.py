from __future__ import annotations

import pytest
import requests

from citeforge import http_utils
from citeforge.http_utils import _decode_json_bytes, _scrub_secrets


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
