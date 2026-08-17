"""Pure policy for text that may become durable refresh evidence."""

from __future__ import annotations

import html
import ipaddress
import re
import socket
import unicodedata
from urllib.parse import parse_qsl, unquote, urlsplit

from ..config import REDACT_QUERY_PARAM_NAMES

_CONTACT = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_UNICODE_CONTACT = re.compile(r"(?u)(?<!\S)[^\s@]+@[^\s@]+\.[^\s@.]+(?!\S)")
_URI = re.compile(r"(?i)(?:(?:https?|file|mailto|data|javascript|urn|tel):|//)[^\s{}<>]+")


def _normalized_secret_key(key: str) -> str:
    normalized = unicodedata.normalize("NFKC", key)
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", normalized)
    return re.sub(r"[._-]+", "_", normalized.casefold()).strip("_")


_SECRET_QUERY_KEYS = frozenset(_normalized_secret_key(name) for name in REDACT_QUERY_PARAM_NAMES) | {
    "authorization",
    "cookie",
    "password",
    "secret",
}
_SECRET_ASSIGNMENT = re.compile(r"(?i)(?<![A-Z0-9])[\"']?(?P<key>[A-Z][A-Z0-9_.-]*)[\"']?\s*[:=]")
_SECRET_KEY_PARTS = frozenset({"authorization", "cookie", "credential", "credentials", "password", "secret", "token"})
_SECRET_COMPOUND_KEYS = frozenset(
    {"clientsecret", "accesstoken", "refreshtoken", "sessioncookie", "proxyauthorization"}
)
_SECRET_VALUE = re.compile(r"(?i)(?:\bbearer\s+[A-Z0-9._~+/-]+=*|-----BEGIN\s+(?:[A-Z]+\s+)*PRIVATE\s+KEY-----)")


def _detection_text(value: str) -> str:
    decoded = unicodedata.normalize("NFKC", value)
    for _ in range(4):
        next_value = unicodedata.normalize("NFKC", unquote(html.unescape(decoded)))
        if next_value == decoded:
            return decoded
        decoded = next_value
    if unicodedata.normalize("NFKC", unquote(html.unescape(decoded))) != decoded:
        raise ValueError("durable evidence contains unstable encoded text")
    return decoded


def _contains_secret_assignment(text: str) -> bool:
    for match in _SECRET_ASSIGNMENT.finditer(text):
        key = _normalized_secret_key(match.group("key"))
        if (key in _SECRET_QUERY_KEYS and key != "key") or any(part in _SECRET_KEY_PARTS for part in key.split("_")):
            return True
    return False


def _is_secret_query_key(key: str) -> bool:
    normalized = _normalized_secret_key(key)
    compact = normalized.replace("_", "")
    return (
        normalized in _SECRET_QUERY_KEYS
        or compact in _SECRET_COMPOUND_KEYS
        or any(part in _SECRET_KEY_PARTS for part in normalized.split("_"))
    )


def ensure_public_https_url(url: str) -> str:
    """Return one public HTTPS URL or raise before durable persistence."""
    try:
        decoded_url = _detection_text(url)
    except ValueError as exc:
        raise ValueError("URL is unsafe for durable evidence") from exc
    if (
        any(unicodedata.category(character) == "Cc" for character in decoded_url)
        or _CONTACT.search(decoded_url)
        or _contains_secret_assignment(decoded_url)
    ):
        raise ValueError("URL is unsafe for durable evidence")
    try:
        parsed = urlsplit(decoded_url)
        host = (parsed.hostname or "").rstrip(".")
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL is unsafe for durable evidence") from exc
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        try:
            address = ipaddress.ip_address(socket.inet_aton(host))
        except OSError:
            address = None
    if (
        parsed.scheme != "https"
        or not host
        or not host.isascii()
        or "%" in parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or parsed.fragment
        or host.casefold() == "localhost"
        or host.casefold().endswith((".localhost", ".local", ".internal"))
        or (address is not None and not address.is_global)
        or any(
            _is_secret_query_key(key) or _contains_secret_assignment(value) or _SECRET_VALUE.search(value) is not None
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    ):
        raise ValueError("URL is unsafe for durable evidence")
    return url


def ensure_safe_durable_text(text: str) -> None:
    """Reject contacts and unsafe URI-like values anywhere in durable text."""
    detection_text = _detection_text(text)
    if _CONTACT.search(detection_text) or _UNICODE_CONTACT.search(detection_text):
        raise ValueError("durable evidence contains private contact data")
    if _contains_secret_assignment(detection_text):
        raise ValueError("durable evidence contains secret-bearing text")
    if any(unicodedata.category(character) == "Cc" for character in text):
        raise ValueError("durable evidence contains control characters")
    for match in _URI.findall(detection_text):
        ensure_public_https_url(match.rstrip(".,;)"))


def ensure_safe_durable_key(key: str) -> None:
    """Reject secret-bearing mapping keys before they enter durable evidence."""
    ensure_safe_durable_text(key)
    detection_key = _detection_text(key)
    if _normalized_secret_key(detection_key) != "key" and _is_secret_query_key(detection_key):
        raise ValueError("durable evidence contains a secret-bearing key")
