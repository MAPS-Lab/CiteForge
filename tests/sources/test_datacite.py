import io
import json

import httpx
import pytest

from citeforge.http import Batch, Http, ParseError
from citeforge.model import Author, Kind, Rel, Source
from citeforge.sources import datacite
from citeforge.works import classify
from tests.conftest import FIXTURES, FakeClock, fixture_transport, json_response, make_http

SYNTHETIC = FIXTURES / "synthetic" / "datacite"
RECORDED = [
    "doi:10.7302/2797",
    "doi:10.14288/1.0392978",
    "doi:10.5281/zenodo.24581518",
    "doi:10.82444/warw.34422843.v1",
    "doi:10.17034/35133349.v1",
    "doi:10.17863/cam.137456",
    "doi:10.22029/jlupub-21814",
    "doi:10.15771/0949-8214_2002_9",
    "doi:10.5281/zenodo.17069890",
    "doi:10.31234/osf.io/rqexa",
]


def body(name: str) -> bytes:
    return (SYNTHETIC / name).read_bytes()


def recorded(clock: FakeClock) -> Http:
    return Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)


@pytest.fixture
def batch(clock: FakeClock) -> Batch:
    return datacite.by_doi(recorded(clock), RECORDED)


def test_recorded_batch_splits_found_and_missing(batch: Batch) -> None:
    assert sorted(batch.found) == sorted(set(RECORDED) - {"doi:10.31234/osf.io/rqexa"})
    assert batch.missing == {"doi:10.31234/osf.io/rqexa"}
    assert batch.failed == frozenset()
    assert all(r.source is Source.DATACITE and r.ids == {k} for k, r in batch.found.items())


def test_recorded_dissertation_takes_school_from_university_publisher(batch: Batch) -> None:
    r = batch.found["doi:10.7302/2797"]
    assert (r.source_type, r.kind, r.year, r.school) == ("Dissertation", Kind.PHDTHESIS, 2021, "University of Michigan")
    assert r.authors == (Author("Janarthanan", "Rajendran"),)


@pytest.mark.parametrize("work_id", ["doi:10.17034/35133349.v1", "doi:10.17863/cam.137456"])
def test_recorded_repository_publisher_is_never_school(batch: Batch, work_id: str) -> None:
    r = batch.found[work_id]
    assert r.kind is Kind.PHDTHESIS
    assert r.school is None


def test_recorded_version_and_identity_relations(batch: Batch) -> None:
    assert batch.found["doi:10.82444/warw.34422843.v1"].relations == {
        (Rel.IDENTICAL, "doi:10.1140/epjc/s10052-025-14940-1"),
        (Rel.IDENTICAL, "doi:10.82444/warw.34422843"),
    }
    assert batch.found["doi:10.5281/zenodo.24581518"].relations == {(Rel.VERSION_OF, "doi:10.5281/zenodo.24581517")}
    assert batch.found["doi:10.5281/zenodo.17069890"].relations == frozenset()
    assert batch.found["doi:10.15771/0949-8214_2002_9"].relations == {
        (Rel.TRANSLATION_OF, "doi:10.15771/0949-8214_2002_8")
    }


def test_recorded_people_ids_and_organisations(batch: Batch) -> None:
    turin = batch.found["doi:10.5281/zenodo.24581518"].authors[0]
    assert turin.rors == ("https://ror.org/048tbm396",)
    assert turin.affiliations == ("University of Turin",)
    assert batch.found["doi:10.5281/zenodo.17069890"].authors[0].orcid == "0009-0004-5593-713X"
    org = batch.found["doi:10.22029/jlupub-21814"].authors[-1]
    assert org == Author("", "", literal="Justus Liebig University Giessen")
    big = batch.found["doi:10.82444/warw.34422843.v1"]
    assert len(big.authors) == 306
    assert big.year == 2026
    assert big.dates == (2025, 2026)


def test_recorded_text_without_type_hint_stays_other(batch: Batch) -> None:
    r = batch.found["doi:10.14288/1.0392978"]
    assert (r.source_type, classify(r)) == ("Text", Kind.OTHER)


def test_recorded_batch_is_one_request(clock: FakeClock) -> None:
    http = recorded(clock)
    datacite.by_doi(http, RECORDED)
    assert http.stats()["api.datacite.org"]["requests"] == 1


def test_thesis_parse() -> None:
    records, unreadable = datacite.parse_dois(body("thesis.json"))
    assert unreadable == frozenset()
    r = records["doi:10.5555/thesis.1"]
    assert r.title == "Learning vessel routes: a graph approach"
    assert (r.source_type, r.kind, r.degree, classify(r)) == (
        "Dissertation",
        Kind.PHDTHESIS,
        "Master's Thesis",
        Kind.MASTERSTHESIS,
    )
    assert r.school == "University of Waterloo"
    assert (r.year, r.dates) == (2021, (2020, 2021))
    assert (r.container, r.volume, r.number, r.pages) == (("Theses",), "3", "1", "1-90")
    assert (r.language, r.url, r.publisher) == (
        "en",
        "https://uwspace.example/handle/1",
        "UWSpace (University of Waterloo)",
    )
    assert r.authors == (
        Author(
            "Jane",
            "Doe",
            orcid="0000-0002-1825-0097",
            rors=("https://ror.org/01aeff406",),
            affiliations=("University of Waterloo",),
        ),
        Author("Richard", "Roe", affiliations=("University of Waterloo",)),
        Author("", "", literal="Ocean Tracking Network"),
    )
    assert r.editors == (Author("Edgar", "Poe"),)
    assert r.relations == {
        (Rel.VERSION_OF, "doi:10.5555/thesis.0"),
        (Rel.IDENTICAL, "arxiv:2101.00001"),
        (Rel.TRANSLATION_OF, "doi:10.5555/these.1"),
        (Rel.HAS_TRANSLATION, "doi:10.5555/these.2"),
    }


def test_unusable_items_fail_alone() -> None:
    records, unreadable = datacite.parse_dois(body("bad_creator.json"))
    assert set(records) == {"doi:10.5555/good"}
    assert unreadable == {"doi:10.5555/bad", "doi:10.5555/untitled"}
    good = records["doi:10.5555/good"]
    assert (good.kind, good.publisher, good.authors) == (Kind.INPROCEEDINGS, "Example Press", (Author("J.", "Smith"),))


@pytest.mark.parametrize("name", ["truncated.json", "wrong_root.json"])
def test_malformed_body_raises(name: str) -> None:
    with pytest.raises(ParseError):
        datacite.parse_dois(body(name))


@pytest.mark.parametrize(
    "payload",
    [[1, 2], {"data": [{"id": "x"}]}, {"data": [{"attributes": {"doi": "not a doi"}}]}, {"data": "x"}],
)
def test_wrong_shapes_raise(payload: object) -> None:
    with pytest.raises(ParseError):
        datacite.parse_dois(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("value", "year"),
    [("2021", 2021), (2021, 2021), ("2021-05-01", 2021), ("2021/2022", 2021), ("21", None), ("(:unav)", None),
     (True, None), ("0999", None), (None, None)],
)  # fmt: skip
def test_year_forms(value: object, year: int | None) -> None:
    attrs = {"titles": [{"title": "T"}], "publicationYear": value}
    assert datacite.parse_work("doi:10.5555/y", attrs).year == year


@pytest.mark.parametrize(
    ("resource_type", "source_type"),
    [("Journal contribution", "JournalArticle"), ("Conference paper", "ConferencePaper"),
     ("Doctoral thesis", "Dissertation"), ("Working Paper", "Report"), ("Project deliverable", "Text")],
)  # fmt: skip
def test_text_records_take_type_from_resource_type(resource_type: str, source_type: str) -> None:
    attrs = {"titles": [{"title": "T"}], "types": {"resourceTypeGeneral": "Text", "resourceType": resource_type}}
    assert datacite.parse_work("doi:10.5555/t", attrs).source_type == source_type


def test_empty_result_is_missing_not_failed(clock: FakeClock) -> None:
    http = make_http(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, content=body("empty.json"))
    )
    b = datacite.by_doi(http, ["doi:10.5555/a", "arxiv:2101.00001"])
    assert (b.found, b.missing, b.failed) == ({}, {"doi:10.5555/a", "arxiv:2101.00001"}, frozenset())


def test_chunks_of_100_and_partial_outage(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        ids = request.url.params["ids"].split(",")
        if "10.5555/150" in ids:
            return json_response({}, 503)
        items = [{"id": d, "attributes": {"doi": d.upper(), "titles": [{"title": d}]}} for d in ids]
        return json_response({"data": items})

    ids = [f"doi:10.5555/{n}" for n in range(250)]
    b = datacite.by_doi(make_http(handle, clock), ids)
    lists = [r for r in seen if r.url.path == "/dois"]
    assert {len(r.url.params["ids"].split(",")) for r in lists} == {100, 50}
    assert all(r.url.params["page[size]"] == "100" and r.url.params["affiliation"] == "true" for r in lists)
    assert len(b.failed) == 100 and "doi:10.5555/150" in b.failed
    assert len(b.found) == 150 and b.missing == frozenset()
    assert set(b.found) | b.failed == set(ids)


def test_comma_doi_goes_alone(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body("single.json"))

    b = datacite.by_doi(make_http(handle, clock), ["doi:10.5555/a,b"])
    assert [r.url.path for r in seen] == ["/dois/10.5555/a,b"]
    assert b.found["doi:10.5555/a,b"].authors == (Author("", "", literal="ICES Working Group"),)
    assert classify(b.found["doi:10.5555/a,b"]) is Kind.PREPRINT


def test_comma_doi_not_found_is_missing(clock: FakeClock) -> None:
    b = datacite.by_doi(make_http(lambda r: json_response({"errors": []}, 404), clock), ["doi:10.5555/a,b"])
    assert b.missing == {"doi:10.5555/a,b"} and not b.failed


@pytest.mark.parametrize(
    "response",
    [
        json_response({}, 500),
        json_response({}, 429, {"retry-after": "6626"}),
        json_response({}, 404),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>challenge</html>"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"data": [{"id": "10.5'),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b'{"errors": []}'),
    ],
)
def test_failures_never_read_as_missing(clock: FakeClock, response: httpx.Response) -> None:
    b = datacite.by_doi(make_http(lambda r: response, clock), ["doi:10.5555/a", "doi:10.5555/b"])
    assert (b.found, b.missing, b.failed) == ({}, frozenset(), {"doi:10.5555/a", "doi:10.5555/b"})


def test_timeouts_then_success_and_down_host(clock: FakeClock) -> None:
    calls = {"n": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, headers={"content-type": "application/json"}, content=body("bad_creator.json"))

    b = datacite.by_doi(make_http(flaky, clock), ["doi:10.5555/good"])
    assert set(b.found) == {"doi:10.5555/good"}

    http = make_http(lambda r: json_response({}, 429, {"retry-after": "6626"}), clock)
    datacite.by_doi(http, ["doi:10.5555/a"])
    assert http.down(datacite.HOST) == "retry_after 6626s"
    assert datacite.by_doi(http, ["doi:10.5555/b"]).failed == {"doi:10.5555/b"}
    assert http.stats()["api.datacite.org"]["requests"] == 1


def test_no_request_for_no_dois(clock: FakeClock) -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unexpected request")

    assert datacite.by_doi(make_http(refuse, clock), []) == Batch({}, frozenset(), frozenset())


def test_unusable_item_fails_in_batch(clock: FakeClock) -> None:
    content = body("bad_creator.json")
    http = make_http(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, content=content), clock
    )
    b = datacite.by_doi(http, ["doi:10.5555/bad", "doi:10.5555/good", "doi:10.5555/gone"])
    assert (set(b.found), b.failed, b.missing) == ({"doi:10.5555/good"}, {"doi:10.5555/bad"}, {"doi:10.5555/gone"})
