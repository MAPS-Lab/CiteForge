import functools
import gzip
import io
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from hypothesis import event, given
from hypothesis import strategies as st

from citeforge.http import Http, ParseError
from citeforge.ids import norm_ror, split_id, to_id
from citeforge.model import Fetch, Source
from citeforge.sources import orcid
from citeforge.sources.orcid import Affiliation
from tests.conftest import FIXTURES, FakeClock, fixture_transport, make_http

SPADON = "0000-0001-8437-4349"
CARBERRY = "0000-0002-1825-0097"
NEETHIRAJAN = "0000-0003-0990-0235"
ABSENT = "0000-0002-3580-1778"
SYNTHETIC = FIXTURES / "synthetic" / "orcid"


def recorded() -> Http:
    return Http(io.StringIO(), {}, transport=fixture_transport())


def synthetic(name: str) -> httpx.Response:
    data = json.loads((SYNTHETIC / f"{name}.json").read_text())
    return httpx.Response(data["status"], headers=data["headers"], content=data["body"].encode())


def serve(*names: str) -> tuple[Http, FakeClock, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(names)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return synthetic(queue.pop(0) if len(queue) > 1 else queue[0])

    clock = FakeClock()
    return make_http(handle, clock), clock, seen


def by_id(works: tuple, work_id: str):
    return next(c for c in works if work_id in c.ids)


def test_recorded_record_names_affiliations_and_works() -> None:
    res = orcid.record(recorded(), SPADON)
    assert (res.fetch, res.reason) == (Fetch.OK, "ok")
    rec = res.value
    assert rec is not None
    assert (rec.given, rec.family, rec.credit) == ("Gabriel", "Spadon de Souza", "Spadon, G")
    assert rec.other_names == ("Souza, GS", "Spadon de Souza, G")
    assert rec.affiliations[0] == Affiliation(None, "Dalhousie University", "employment")
    assert {a.kind for a in rec.affiliations} == {"employment", "education"}
    assert len(rec.affiliations) == len(set(rec.affiliations))
    assert len(rec.works) == 34
    assert sum(not c.ids for c in rec.works) == 3
    mdpi = by_id(rec.works, "doi:10.3390/s22166063")
    assert mdpi.source is Source.ORCID and mdpi.record is None
    assert mdpi.year == 2022
    assert by_id(rec.works, "arxiv:1909.04605").year == 2019
    assert all(c.title or c.ids for c in rec.works)
    assert all(i.split(":")[0] in orcid.ID_KINDS for c in rec.works for i in c.ids)


def test_integration_pushed_record_has_ror() -> None:
    rec = orcid.record(recorded(), NEETHIRAJAN).value
    assert rec is not None
    assert Affiliation("https://ror.org/01e6qks80", "Dalhousie University", "employment") in rec.affiliations
    assert len(rec.works) == 290
    assert sum(not c.ids for c in rec.works) == 33
    assert any(c.year is None for c in rec.works)


def test_recorded_404_is_not_found() -> None:
    res = orcid.record(recorded(), ABSENT)
    assert (res.fetch, res.value, res.reason) == (Fetch.NOT_FOUND, None, "not_found")


def test_request_shape_and_input_normalization() -> None:
    http, _, seen = serve("empty_record")
    res = orcid.record(http, f"https://orcid.org/{CARBERRY}")
    assert res.fetch is Fetch.OK
    assert str(seen[0].url) == f"https://pub.orcid.org/v3.0/{CARBERRY}/record"
    assert seen[0].headers["accept"] == "application/json"


@pytest.mark.parametrize("bad", ["0000-0002-1825-0098", "not-an-orcid", ""])
def test_invalid_orcid_sends_no_request(bad: str) -> None:
    http, _, seen = serve("empty_record")
    assert orcid.record(http, bad) == orcid.Result(Fetch.NOT_FOUND, None, "invalid_orcid")
    assert seen == []


def test_edge_record_tolerates_nulls_and_normalizes() -> None:
    http, _, _ = serve("edge_record")
    rec = orcid.record(http, CARBERRY).value
    assert rec is not None
    assert (rec.given, rec.family, rec.credit) == ("", "", None)
    assert rec.other_names == ("J. Carberry",)
    assert rec.affiliations == (
        Affiliation("https://ror.org/05gq2b172", "Brown University", "employment"),
        Affiliation("https://ror.org/0d3z9wp09", "Wesleyan University", "employment"),
        Affiliation(None, "Ringgold Institute", "employment"),
        Affiliation(None, "Bad Ror College", "employment"),
        Affiliation(None, "Brown University", "education"),
    )
    assert [(c.title, c.year) for c in rec.works if not c.ids] == [("Version only", 2023), ("Isbn only", 2019)]
    works = {c.ids: c for c in rec.works if c.ids}
    assert set(works) == {
        frozenset({"doi:10.5555/abc.1", "pmid:31415926"}),
        frozenset({"arxiv:2101.00001"}),
        frozenset({"doi:10.5555/norm"}),
        frozenset({"arxiv:hep-th/9901001"}),
        frozenset({"doi:10.5555/upper"}),
    }
    merged = works[frozenset({"doi:10.5555/abc.1", "pmid:31415926"})]
    assert (merged.title, merged.year) == ("Tiny & Fast Graphs", 2021)
    untitled = works[frozenset({"arxiv:hep-th/9901001"})]
    assert (untitled.title, untitled.year) == ("", None)


def test_empty_record_is_ok_with_no_works() -> None:
    http, _, _ = serve("empty_record")
    res = orcid.record(http, CARBERRY)
    assert res.fetch is Fetch.OK and res.value is not None
    assert (res.value.works, res.value.affiliations, res.value.given) == ((), (), "")


@pytest.mark.parametrize(
    ("name", "reason"),
    [
        ("truncated", "parse_error"),
        ("wrong_root", "parse_error"),
        ("list_root", "parse_error"),
        ("no_works", "parse_error"),
        ("xml_body", "parse_error"),
        ("challenge_html", "challenge"),
        ("locked", "locked"),
        ("deactivated", "deactivated"),
        ("deprecated", "deprecated:0000-0002-0000-0001"),
    ],
)
def test_failures_never_read_as_empty(name: str, reason: str) -> None:
    http, _, seen = serve(name)
    res = orcid.record(http, CARBERRY)
    assert (res.fetch, res.value, res.reason) == (Fetch.FAILED, None, reason)
    assert len(seen) == 1


def test_long_retry_after_marks_host_down_without_sleeping() -> None:
    http, clock, seen = serve("rate_limited")
    res = orcid.record(http, CARBERRY)
    assert (res.fetch, res.reason) == (Fetch.FAILED, "http_error")
    assert clock.sleeps == []
    assert http.down(orcid.HOST) == "retry_after 6626s"
    assert orcid.record(http, SPADON).reason == "down"
    assert len(seen) == 1


def test_transient_503_then_success() -> None:
    http, clock, seen = serve("unavailable", "empty_record")
    res = orcid.record(http, CARBERRY)
    assert res.fetch is Fetch.OK
    assert len(seen) == 2 and len(clock.sleeps) == 1


def test_timeouts_exhaust_retries_then_fail() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    res = orcid.record(make_http(handle), CARBERRY)
    assert (res.fetch, res.reason) == (Fetch.FAILED, "timeout")


def test_parse_record_rejects_non_json() -> None:
    with pytest.raises(ParseError):
        orcid.parse_record(b"\xff\xfe{", CARBERRY)
    with pytest.raises(ParseError):
        orcid.parse_record(b"[" * 100_000, CARBERRY)


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(max_size=8), inner, max_size=3),
    max_leaves=20,
)


def shaped(valid: st.SearchStrategy) -> st.SearchStrategy:
    """A field of the ORCID shape that is sometimes replaced by arbitrary JSON."""
    return st.one_of(valid, valid, valid, json_values)


def wrap(*path: str) -> Callable[[object], object]:
    def build(value: object) -> object:
        return functools.reduce(lambda inner, key: {key: inner}, reversed(path), value)

    return build


external_id = st.fixed_dictionaries(
    {
        "external-id-type": shaped(st.sampled_from(["doi", "DOI", "arxiv", "pmid", "eid"])),
        "external-id-value": shaped(st.sampled_from(["10.1234/ABC", "arXiv:2101.00001", "31415926", "x"]) | st.text()),
        "external-id-relationship": shaped(st.sampled_from(["self", "part-of"])),
    }
)
work_summary = st.fixed_dictionaries(
    {
        "title": shaped(st.text(min_size=1, max_size=20).map(wrap("title", "value"))),
        "publication-date": shaped(
            (st.from_regex(r"\d{4}", fullmatch=True) | st.text(max_size=5)).map(wrap("year", "value"))
        ),
        "external-ids": shaped(st.lists(external_id, max_size=3).map(wrap("external-id"))),
    }
)
employment = st.fixed_dictionaries(
    {
        "name": shaped(st.text(max_size=12)),
        "disambiguated-organization": shaped(
            st.fixed_dictionaries(
                {
                    "disambiguation-source": shaped(st.just("ROR")),
                    "disambiguated-organization-identifier": shaped(
                        st.sampled_from(["https://ror.org/01e6qks80", "x"])
                    ),
                }
            )
        ),
    }
).map(wrap("employment-summary", "organization"))
orcid_doc = st.fixed_dictionaries(
    {
        "orcid-identifier": st.just({"path": CARBERRY}),
        "person": shaped(st.text(max_size=8).map(wrap("name", "given-names", "value"))),
        "activities-summary": st.fixed_dictionaries(
            {
                "works": st.lists(
                    shaped(st.lists(work_summary, min_size=1, max_size=2).map(wrap("work-summary"))), max_size=4
                ).map(wrap("group")),
                "employments": shaped(
                    st.lists(st.lists(employment, max_size=2).map(wrap("summaries")), max_size=2).map(
                        wrap("affiliation-group")
                    )
                ),
            }
        ),
    }
)


@given(orcid_doc)
def test_parse_record_only_raises_parse_error(doc: dict) -> None:
    try:
        rec = orcid.parse_record(json.dumps(doc).encode(), CARBERRY)
    except ParseError:
        event("parse_error")
        return
    event("works" if rec.works else "no works")
    for c in rec.works:
        assert c.source is Source.ORCID and (c.ids or c.title)
        assert all(split_id(i)[0] in orcid.ID_KINDS and to_id(split_id(i)[0], split_id(i)[1]) == i for i in c.ids)
    for a in rec.affiliations:
        assert a.name and (a.ror is None or norm_ror(a.ror) == a.ror)


def test_fixtures_hold_no_contact_fields() -> None:
    recorded_files = [gzip.decompress(p.read_bytes()) for p in (FIXTURES / "http" / "pub.orcid.org").glob("*.json.gz")]
    for text in [*recorded_files, *(p.read_bytes() for p in Path(SYNTHETIC).glob("*.json"))]:
        assert b'"emails"' not in text and b'"addresses"' not in text


def test_work_without_a_resolvable_id_stays_a_title_claim() -> None:
    def group(title: str | None, kind: str) -> dict:
        ext = {"external-id-type": kind, "external-id-value": "2-s2.0-1", "external-id-relationship": "self"}
        summary = {"title": {"title": {"value": title}} if title else None,
                   "publication-date": {"year": {"value": "2024"}}, "external-ids": {"external-id": [ext]}}  # fmt: skip
        return {"work-summary": [summary]}

    doc = {
        "orcid-identifier": {"path": CARBERRY},
        "person": {"name": {}},
        "activities-summary": {"works": {"group": [group("Paper with no DOI", "eid"), group(None, "eid")]}},
    }
    rec = orcid.parse_record(json.dumps(doc).encode(), CARBERRY)
    assert [(c.ids, c.title, c.year) for c in rec.works] == [(frozenset(), "Paper with no DOI", 2024)]
