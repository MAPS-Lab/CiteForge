import gzip
import io
import json
from pathlib import Path

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.http import Http, ParseError
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
    assert len(rec.works) == 31
    mdpi = by_id(rec.works, "doi:10.3390/s22166063")
    assert mdpi.source is Source.ORCID and mdpi.record is None
    assert mdpi.year == 2022 and mdpi.by_self is False
    assert by_id(rec.works, "doi:10.1109/access.2022.3197215").by_self is True
    assert by_id(rec.works, "arxiv:1909.04605").year == 2019
    assert all(c.ids and all(i.split(":")[0] in orcid.ID_KINDS for i in c.ids) for c in rec.works)


def test_by_self_only_when_the_source_is_the_id_itself() -> None:
    rec = orcid.record(recorded(), CARBERRY).value
    assert rec is not None
    assert by_id(rec.works, "doi:10.5555/12345680").by_self is True
    assert sum(c.by_self is False for c in rec.works) == 1


def test_integration_pushed_record_has_ror_and_no_self_claims() -> None:
    rec = orcid.record(recorded(), NEETHIRAJAN).value
    assert rec is not None
    assert Affiliation("https://ror.org/01e6qks80", "Dalhousie University", "employment") in rec.affiliations
    assert sum(c.by_self is True for c in rec.works) == 1
    assert len(rec.works) == 257
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
    works = {c.ids: c for c in rec.works}
    assert set(works) == {
        frozenset({"doi:10.5555/abc.1", "pmid:31415926"}),
        frozenset({"arxiv:2101.00001"}),
        frozenset({"doi:10.5555/norm"}),
        frozenset({"arxiv:hep-th/9901001"}),
        frozenset({"doi:10.5555/upper"}),
    }
    merged = works[frozenset({"doi:10.5555/abc.1", "pmid:31415926"})]
    assert (merged.title, merged.year, merged.by_self) == ("Tiny & Fast Graphs", 2021, True)
    untitled = works[frozenset({"arxiv:hep-th/9901001"})]
    assert (untitled.title, untitled.year, untitled.by_self) == ("", None, False)


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
section_keys = st.sampled_from(["person", "activities-summary", "works", "group", "work-summary", "employments"])


@given(st.dictionaries(section_keys, json_values, max_size=4), json_values)
def test_parse_record_only_raises_parse_error(extra: dict, groups: object) -> None:
    doc = {"orcid-identifier": {"path": CARBERRY}, "activities-summary": {"works": {"group": groups}}, **extra}
    try:
        rec = orcid.parse_record(json.dumps(doc).encode(), CARBERRY)
    except ParseError:
        return
    assert all(c.ids and c.source is Source.ORCID for c in rec.works)
    assert all(isinstance(a.name, str) and a.name for a in rec.affiliations)


def test_fixtures_hold_no_contact_fields() -> None:
    recorded_files = [gzip.decompress(p.read_bytes()) for p in (FIXTURES / "http" / "pub.orcid.org").glob("*.json.gz")]
    for text in [*recorded_files, *(p.read_bytes() for p in Path(SYNTHETIC).glob("*.json"))]:
        assert b'"emails"' not in text and b'"addresses"' not in text
