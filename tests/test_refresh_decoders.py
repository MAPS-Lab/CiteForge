from __future__ import annotations

import pytest

from citeforge.refresh.capabilities import ResponseMediaType
from citeforge.refresh.decoders import decode_response
from citeforge.refresh.transport import RawProviderResponse, SchemaChangedError


def _raw(body: bytes, content_type: str) -> RawProviderResponse:
    return RawProviderResponse(body, content_type, "https://provider.test/resource", {})


@pytest.mark.parametrize("body", [b"{", b'{"x":NaN}', b'{"x":1,"x":2}'])
def test_json_syntax_duplicate_and_nonfinite_are_malformed(body: bytes) -> None:
    with pytest.raises(ValueError, match="JSON"):
        decode_response("crossref.fuzzy_search.v1.decoder", _raw(body, "application/json"))


@pytest.mark.parametrize("body", [b"[]", b"null", b'"value"', b'{"message":[]}'])
def test_json_valid_wrong_shape_is_schema_changed(body: bytes) -> None:
    with pytest.raises(SchemaChangedError):
        decode_response("crossref.fuzzy_search.v1.decoder", _raw(body, "application/json"))


def test_crossref_accepts_its_documented_vendor_json_media_only() -> None:
    normalized, empty = decode_response(
        "crossref.fuzzy_search.v1.decoder",
        _raw(
            b'{"status":"ok","message":{"items":[],"total-results":0}}',
            "application/vnd.crossref-api-message+json",
        ),
    )
    assert normalized == {"results": []} and empty
    with pytest.raises(SchemaChangedError, match="media"):
        decode_response(
            "s2.fuzzy_search.v2.decoder",
            _raw(b'{"data":[]}', "application/vnd.crossref-api-message+json"),
        )


@pytest.mark.parametrize(
    ("decoder_id", "body"),
    [
        ("crossref.fuzzy_search.v1.decoder", b'{"message":{"items":["wrong"]}}'),
        ("s2.fuzzy_search.v2.decoder", b'{"data":["wrong"]}'),
        ("serply.scholar_search.v1.decoder", b'{"articles":["wrong"]}'),
        ("pubmed.title_search.v1.decoder", b'{"esearchresult":{"idlist":[{"wrong":1}]}}'),
    ],
)
def test_provider_list_member_types_are_exact(decoder_id: str, body: bytes) -> None:
    with pytest.raises(SchemaChangedError):
        decode_response(decoder_id, _raw(body, "application/json"))


@pytest.mark.parametrize(
    ("decoder_id", "body", "content_type"),
    [
        ("crossref.fuzzy_search.v1.decoder", b'{"message":{"items":[]}}', "text/html"),
        ("scholar.inventory.v1.decoder", b'{"articles":[]}', "text/html"),
        ("dblp.inventory.v1.decoder", b"<dblpperson/>", "application/json"),
        (
            "arxiv.fuzzy_search.v1.decoder",
            b'<feed xmlns="http://www.w3.org/2005/Atom"/>',
            "text/html",
        ),
    ],
)
def test_typed_decoders_reject_missing_or_incompatible_media(decoder_id: str, body: bytes, content_type: str) -> None:
    with pytest.raises(SchemaChangedError, match="media"):
        decode_response(decoder_id, _raw(body, content_type))


def test_arxiv_empty_feed_must_not_conflict_with_declared_total() -> None:
    body = (
        b'<feed xmlns="http://www.w3.org/2005/Atom" '
        b'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        b"<opensearch:totalResults>1</opensearch:totalResults></feed>"
    )
    with pytest.raises(SchemaChangedError, match="totalResults"):
        decode_response("arxiv.fuzzy_search.v1.decoder", _raw(body, "application/atom+xml"))


def test_arxiv_atom_strict_root_entry_and_authoritative_empty() -> None:
    empty = (
        b'<feed xmlns="http://www.w3.org/2005/Atom" '
        b'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        b"<opensearch:totalResults>0</opensearch:totalResults></feed>"
    )
    normalized, authoritative_empty = decode_response(
        "arxiv.fuzzy_search.v1.decoder", _raw(empty, "application/atom+xml")
    )
    assert normalized == {"entries": []}
    assert authoritative_empty
    wrong = b"<results/>"
    with pytest.raises(SchemaChangedError, match="Atom"):
        decode_response("arxiv.fuzzy_search.v1.decoder", _raw(wrong, "application/atom+xml"))
    malformed_entry = b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>x</id></entry></feed>'
    with pytest.raises(SchemaChangedError, match="entry"):
        decode_response("arxiv.fuzzy_search.v1.decoder", _raw(malformed_entry, "application/atom+xml"))


def test_arxiv_paginated_feed_accepts_total_larger_than_current_page() -> None:
    body = b"""<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">
      <opensearch:totalResults>10</opensearch:totalResults>
      <entry><id>https://arxiv.org/abs/2601.12345</id><title>Safe title</title>
      <published>2026-01-02T00:00:00Z</published><author><name>Ada Lovelace</name></author>
      <link rel="alternate" href="https://arxiv.org/abs/2601.12345"/></entry></feed>"""
    normalized, empty = decode_response("arxiv.fuzzy_search.v1.decoder", _raw(body, "application/atom+xml"))
    assert not empty
    assert normalized["entries"][0]["arxiv_id"] == "2601.12345"


@pytest.mark.parametrize(
    ("decoder_id", "body"),
    [
        ("doi_csl.csl_lookup.v1.decoder", b'{"title":"Safe","type":{}}'),
        (
            "crossref.fuzzy_search.v1.decoder",
            b'{"status":"ok","message":{"total-results":1,"items":[{"title":["Safe"],"type":{}}]}}',
        ),
        (
            "openalex.fuzzy_search.v1.decoder",
            b'{"meta":{"count":1},"results":[{"id":"W1","title":"Safe","type":{}}]}',
        ),
        (
            "s2.fuzzy_search.v2.decoder",
            b'{"total":1,"data":[{"paperId":"P1","title":"Safe","publicationTypes":[{}]}]}',
        ),
        (
            "europepmc.fuzzy_search.v1.decoder",
            b'{"hitCount":1,"resultList":{"result":[{"title":"Safe","pubType":{}}]}}',
        ),
    ],
)
def test_reducer_used_provider_type_fields_reject_wrong_types(decoder_id: str, body: bytes) -> None:
    with pytest.raises(SchemaChangedError):
        decode_response(decoder_id, _raw(body, "application/json"))


def test_doi_bibtex_requires_expected_media_and_exactly_one_entry() -> None:
    body = b"@article{Ada2024, title={Analytical Engine}, author={Lovelace, Ada}, year={2024}, doi={10.1/x}}"
    normalized, empty = decode_response("doi_bibtex.bibtex_lookup.v1.decoder", _raw(body, "application/x-bibtex"))
    assert not empty
    assert normalized["metadata"]["fields"]["doi"] == "10.1/x"
    for invalid in (b"", b"not bibtex", body + b"\n" + body.replace(b"Ada2024", b"Ada2025")):
        with pytest.raises((ValueError, SchemaChangedError)):
            decode_response("doi_bibtex.bibtex_lookup.v1.decoder", _raw(invalid, "application/x-bibtex"))
    with pytest.raises(SchemaChangedError, match="media"):
        decode_response("doi_bibtex.bibtex_lookup.v1.decoder", _raw(body, "text/html"))


@pytest.mark.parametrize(
    "body",
    [
        b"arbitrary prefix\n@article{Ada2024, title={Safe}}",
        b"@article{Ada2024, title={Safe}}\narbitrary suffix",
        b"@comment{not metadata}\n@article{Ada2024, title={Safe}}",
        b'@preamble{"not metadata"}\n@article{Ada2024, title={Safe}}',
        b'@string{label="Safe"}\n@article{Ada2024, title=label}',
    ],
)
def test_doi_bibtex_rejects_unconsumed_arbitrary_text(body: bytes) -> None:
    with pytest.raises(ValueError, match="BibTeX"):
        decode_response("doi_bibtex.bibtex_lookup.v1.decoder", _raw(body, "application/x-bibtex"))


@pytest.mark.parametrize(
    "body",
    [
        b"@article{Ada2024, title={A study (part I}, author={Ada Lovelace}}",
        b'@article{Ada2024, title={A "study}, author={Ada Lovelace}}',
        b"@article(Ada2024, title={A study ) part I}, author={Ada Lovelace})",
        b"@article(Ada2024, title={A study (part I}, author={Ada Lovelace})",
        b'@article(Ada2024, title={A "study}, author={Ada Lovelace})',
        b'@article{Ada2024, title="A {study"} text", author={Ada Lovelace}}',
        b'@article(Ada2024, title="A {study"} text", author={Ada Lovelace})',
    ],
)
def test_doi_bibtex_complete_scanner_respects_outer_delimiter_and_brace_shielding(body: bytes) -> None:
    normalized, empty = decode_response("doi_bibtex.bibtex_lookup.v1.decoder", _raw(body, "application/x-bibtex"))
    assert not empty
    assert normalized["metadata"]["fields"]["title"]


def test_html_doi_probe_uses_structured_evidence_and_never_confirms_empty() -> None:
    body = b'<html><head><meta name="citation_doi" content="10.1000/ABC"></head></html>'
    normalized, empty = decode_response("web.doi_probe.v1.decoder", _raw(body, "text/html; charset=utf-8"))
    assert normalized == {"doi": "10.1000/abc"}
    assert not empty
    none, empty = decode_response(
        "web.doi_probe.v1.decoder", _raw(b"<html><head></head><body>No identifier</body></html>", "text/html")
    )
    assert none == {"doi": None}
    assert not empty
    conflicting = b'<meta name="citation_doi" content="10.1/one"><link rel="canonical" href="https://doi.org/10.1/two">'
    with pytest.raises(SchemaChangedError, match="conflicting"):
        decode_response("web.doi_probe.v1.decoder", _raw(conflicting, "text/html"))


def test_raw_provider_response_is_frozen_bounded_and_secret_safe() -> None:
    raw = RawProviderResponse(
        b"{}",
        "Application/JSON; Charset=UTF-8",
        "https://api.example.test/path?q=safe&api_key=secret",
        {"Content-Type": "application/json", "Set-Cookie": "secret", "X-Request-Id": "safe"},
    )
    assert raw.content_type == "application/json"
    assert raw.final_url.startswith("https://api.example.test/path-sha256/")
    assert dict(raw.headers)["content-type"] == "application/json"
    assert dict(raw.headers)["x-request-id"] != "safe"
    assert "secret" not in repr(raw).casefold()
    path_secret = RawProviderResponse(
        b"{}", "application/json", "https://api.example.test/private/secret-token", {"ETag": "secret"}
    )
    assert "private" not in repr(path_secret).casefold()
    assert "secret" not in repr(path_secret).casefold()
    with pytest.raises(TypeError):
        raw.headers["x"] = "y"  # type: ignore[index]
    with pytest.raises(ValueError, match="header"):
        RawProviderResponse(b"{}", "application/json", "https://x.test", {"X-Request-Id": "ok\r\nbad"})
    for unsafe in ("https://user:secret@x.test/path", "https://x.test:8443/path"):
        with pytest.raises(ValueError, match="URL"):
            RawProviderResponse(b"{}", "application/json", unsafe, {})


def test_decoder_media_types_cover_json_xml_bibtex_and_html() -> None:
    assert set(ResponseMediaType) == {
        ResponseMediaType.JSON,
        ResponseMediaType.XML,
        ResponseMediaType.BIBTEX,
        ResponseMediaType.HTML,
    }
