from __future__ import annotations

import pytest

from src.http_utils import _decode_json_bytes, _scrub_secrets


class TestSecretRedaction:
    """API keys/tokens passed as query params must never reach logs or exception text (defect C2).

    Gemini uses ``?key=`` and SerpAPI uses ``&api_key=``; those URLs previously
    leaked into WARN logs (committed to a public branch) via exception messages.
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
