from __future__ import annotations

import pytest

from citeforge.refresh.privacy import ensure_public_https_url, ensure_safe_durable_text


@pytest.mark.parametrize(
    "url",
    [
        "https://127。0。0。1/private",
        "https://localhos\uff54/private",
        "https://example.com/ada%40example.com",
        "https://example.com/?email=ada%40example.com",
        "https://example.com/?api%5fkey=",
        "https://example.com/?key=value",
        "https://example.com/x\nheader",
    ],
)
def test_public_url_policy_rejects_encoded_private_or_secret_values(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ensure_public_https_url(url)


def test_public_url_policy_allows_unrelated_query_key() -> None:
    assert ensure_public_https_url("https://publisher.example/work?monkey=value")


@pytest.mark.parametrize(
    "text",
    ["(api_key=value)", '{"api_key":"value"}', "[Authorization: Bearer value]", "mailto:ada@example.com"],
)
def test_durable_text_policy_rejects_secret_or_contact_assignments(text: str) -> None:
    with pytest.raises(ValueError):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize("text", ["api.key=value", "API.KEY: value"])
def test_durable_text_policy_rejects_punctuated_secret_assignments(text: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize(
    "text",
    ["api%2ekey=value", "api%252ekey=value", "client%5fsecret=value", "client%255fsecret=value"],
)
def test_durable_text_policy_rejects_percent_encoded_secret_assignments(text: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize("text", ["api&#46;key=value", "api&#x2e;key=value", "api&amp;#46;key=value"])
def test_durable_text_policy_rejects_html_encoded_secret_assignments(text: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize(
    "text",
    [
        "client_secret=value",
        "oauth_token=value",
        "refresh_token=value",
        "credential=value",
        "credentials=value",
        "credential_path=/private/key.json",
        "credentials_file=/private/key.json",
    ],
)
def test_durable_text_policy_rejects_compound_secret_assignments(text: str) -> None:
    with pytest.raises(ValueError, match="secret"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize(
    "key", ["client_secret", "oauth_token", "refresh_token", "credential", "credentials", "credential_path"]
)
def test_public_url_policy_rejects_compound_secret_query_keys(key: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ensure_public_https_url(f"https://publisher.example/work?{key}=value")


def test_durable_text_policy_allows_nonsecret_prose() -> None:
    ensure_safe_durable_text("API key methods in distributed systems")


@pytest.mark.parametrize("text", ["person\uff20example.com", "person\ufe6bexample.com", "person@example\uff0ecom"])
def test_durable_text_policy_rejects_nfkc_disguised_contacts(text: str) -> None:
    with pytest.raises(ValueError, match="contact"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize("text", ["person@example.cóm", "person@éxample.com", "pérson@example.com"])
def test_durable_text_policy_rejects_unicode_contacts(text: str) -> None:
    with pytest.raises(ValueError, match="contact"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize("text", ["data:text/plain,x", "javascript:alert(1)", "urn:isbn:123", "tel:+15551234"])
def test_durable_text_policy_rejects_non_https_embedded_uris(text: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ensure_safe_durable_text(text)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/?\uff54\uff4f\uff4b\uff45\uff4e=value",
        "https://example.test/?api\ufe4dkey=value",
    ],
)
def test_public_url_policy_rejects_nfkc_disguised_secret_keys(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ensure_public_https_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/?api.key=value",
        "https://example.test/?api%2ekey=value",
        "https://example.test/?q=Bearer%20abc.def.ghi",
        "https://example.test/?q=-----BEGIN%20PRIVATE%20KEY-----",
    ],
)
def test_public_url_policy_rejects_punctuated_secret_keys_and_values(url: str) -> None:
    with pytest.raises(ValueError, match="unsafe"):
        ensure_public_https_url(url)
