import contextlib
import gzip
import io
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge import http as http_module
from citeforge.http import Http, ParseError
from citeforge.model import Fetch, Kind, Source, Status
from citeforge.sources import openalex
from tests.conftest import FIXTURES, FakeClock, fixture_transport, json_response, make_http
from tests.factories import bib_problems

SYNTHETIC = FIXTURES / "synthetic" / "openalex"
SPADON = "A5073978680"
SPADON_ORCID = "0000-0001-8437-4349"
EXAMPLE_ORCID = "0000-0002-1825-0097"
LAB = "https://ror.org/01e6qks80"


def recorded(clock: FakeClock) -> Http:
    return Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)


def synthetic(name: str) -> httpx.Response:
    data = json.loads((SYNTHETIC / f"{name}.json").read_text(encoding="utf-8"))
    return httpx.Response(data["status"], headers=data["headers"], content=data["body"].encode())


def page(results: list[dict[str, Any]], cursor: str | None = None) -> httpx.Response:
    return json_response({"meta": {"count": len(results), "next_cursor": cursor}, "results": results})


def authorship(name: str, **kw: Any) -> dict[str, Any]:
    return {
        "author": {"id": kw.get("id"), "display_name": name, "orcid": kw.get("orcid")},
        "institutions": kw.get("institutions", []),
        "raw_affiliation_strings": kw.get("raw", []),
        "raw_author_name": name,
    }


def work(wid: str = "W4000000001", **kw: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"https://openalex.org/{wid}",
        "doi": "https://doi.org/10.1016/J.OCEANENG.2024.118303",
        "ids": {},
        "title": "Vessel trajectory prediction",
        "publication_year": 2024,
        "type": "article",
        "authorships": [authorship("Gabriel Spadon", id=f"https://openalex.org/{SPADON}")],
        "primary_location": {"source": {"display_name": "Ocean Engineering", "type": "journal"}},
        "biblio": {"volume": "308", "issue": None, "first_page": "1", "last_page": "12"},
        "language": "en",
        "is_retracted": False,
    }
    item.update(kw)
    return item


def test_authors_by_orcid_recorded(clock: FakeClock) -> None:
    res = openalex.authors_by_orcid(recorded(clock), [SPADON_ORCID, "0000-0002-3770-180X", EXAMPLE_ORCID])
    assert res.fetch is Fetch.OK
    assert res.value is not None
    assert res.value[SPADON_ORCID] == (SPADON,)
    assert len(res.value[EXAMPLE_ORCID]) > 1
    assert list(res.value[EXAMPLE_ORCID]) == sorted(res.value[EXAMPLE_ORCID])


def test_authors_by_orcid_chunks_and_ignores_unrequested(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        values = request.url.params["filter"].removeprefix("orcid:").split("|")
        stray = {"id": "https://openalex.org/A1111", "orcid": "https://orcid.org/0000-0002-1694-233X"}
        return page([{"id": "https://openalex.org/A2222", "orcid": f"https://orcid.org/{values[0]}"}, stray])

    orcids = [SPADON_ORCID] * 3 + [f"0000-0000-0000-{n:04d}" for n in range(120)]
    res = openalex.authors_by_orcid(make_http(handle, clock), orcids)
    assert len(seen) == 3
    assert all(len(r.url.params["filter"].split("|")) <= openalex.ORCIDS_PER_CALL for r in seen)
    assert seen[0].url.params["select"] == "id,orcid"
    assert res.value is not None
    assert "0000-0002-1694-233X" not in res.value


def test_authors_by_orcid_failure_is_not_empty(clock: FakeClock) -> None:
    res = openalex.authors_by_orcid(make_http(lambda r: synthetic("unavailable"), clock), [SPADON_ORCID])
    assert res.fetch is Fetch.FAILED
    assert res.value is None


def test_author_recorded_and_deleted(clock: FakeClock) -> None:
    http = recorded(clock)
    assert openalex.author(http, SPADON).value == (SPADON, SPADON_ORCID)
    gone = openalex.author(http, "A9999999999")
    assert gone.fetch is Fetch.NOT_FOUND
    assert gone.value is None


def test_author_follows_merge_to_current_id(clock: FakeClock) -> None:
    body = {"id": "https://openalex.org/A5000000001", "orcid": None}
    res = openalex.author(make_http(lambda r: json_response(body), clock), "A4000000001")
    assert res.value == ("A5000000001", None)


def test_author_redirect_off_allowlist_fails(clock: FakeClock) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.host)
        return synthetic("redirect_off_allowlist")

    res = openalex.author(make_http(handle, clock), SPADON)
    assert res.fetch is Fetch.FAILED
    assert seen == ["api.openalex.org"]


def test_list_works_recorded(clock: FakeClock) -> None:
    res = openalex.list_works(recorded(clock), [SPADON], 2023)
    assert res.fetch is Fetch.OK
    assert res.value is not None
    claims = {next(i for i in c.ids if i.startswith("openalex:")): c for c in res.value}
    assert len(claims) == 22
    for c in res.value:
        assert c.source is Source.OPENALEX
        assert c.record is not None
        assert c.year is not None and c.year >= 2023
        assert c.record.year == c.year
        me = [a for a in c.record.authors if a.openalex == SPADON]
        assert me and me[0].orcid is None and me[0].rors
    crosswalked = claims["openalex:W4402209710"]
    assert crosswalked.ids == {"doi:10.1016/j.oceaneng.2024.119138", "openalex:W4402209710"}
    assert claims["openalex:W4400834265"].ids >= {"pmid:39030401", "doi:10.1038/s41598-024-67552-2"}
    preprint = claims["openalex:W4416697841"].record
    assert preprint is not None
    assert preprint.ids == {"arxiv:2509.01836", "openalex:W4416697841"}
    assert preprint.kind is Kind.PREPRINT
    assert preprint.howpublished == "arXiv"
    records = {k: c.record for k, c in claims.items() if c.record is not None}
    assert records["openalex:W4399450758"].container == ("Ocean Engineering",)
    assert records["openalex:W4412021421"].kind is Kind.INPROCEEDINGS
    assert LAB in next(a for a in preprint.authors if a.openalex == SPADON).rors


def test_list_works_pages_with_cursor(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []
    pages = {
        "*": page([work("W4000000001")], "c2"),
        "c2": page([work("W4000000002"), work("W4000000001")], "c3"),
        "c3": page([]),
    }

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return pages[request.url.params["cursor"]]

    res = openalex.list_works(make_http(handle, clock), [SPADON, "A5000000002"], 2018)
    assert res.value is not None
    assert [sorted(c.ids)[1] for c in res.value] == ["openalex:W4000000001", "openalex:W4000000002"]
    params = seen[0].url.params
    assert params["filter"] == f"authorships.author.id:A5000000002|{SPADON},from_publication_date:2018-01-01"
    assert params["per_page"] == "200"
    assert params["select"] == openalex.WORK_FIELDS
    assert len(seen) == 3


def _crowd(n: int, member_at: int | None) -> list[dict[str, Any]]:
    people = [authorship(f"Author{i:03d} Person", id=f"https://openalex.org/A{5000000100 + i}") for i in range(n)]
    if member_at is not None:
        people[member_at] = authorship("Gabriel Spadon", id=f"https://openalex.org/{SPADON}")
    return people


def test_list_works_refetches_a_byline_cut_before_the_member(clock: FakeClock) -> None:
    seen: list[str] = []
    full = work(authorships=_crowd(260, 119), is_authors_truncated=False)
    cut = {**full, "authorships": full["authorships"][:100], "is_authors_truncated": True}
    kept = work("W4000000002", authorships=_crowd(100, 3), is_authors_truncated=True)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/works/W4000000001":
            assert request.url.params["select"] == openalex.WORK_FIELDS
            return json_response(full)
        return page([cut, kept])

    res = openalex.list_works(make_http(handle, clock), [SPADON], 2018)
    assert res.value is not None
    by_id = {min(i for i in c.ids if i.startswith("openalex:")): c.record for c in res.value}
    refetched, untouched = by_id["openalex:W4000000001"], by_id["openalex:W4000000002"]
    assert refetched is not None and untouched is not None
    assert len(refetched.authors) == 260 and refetched.authors[119].openalex == SPADON
    assert len(untouched.authors) == 100
    assert seen == ["/works", "/works/W4000000001"]


def test_list_works_refetches_newest_first_skips_known_dois_and_stops_at_the_cap(
    clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    cut = [
        work(f"W40000000{n:02d}", doi=f"https://doi.org/10.1234/c{n}", publication_year=2020 + n,
             authorships=_crowd(100, None), is_authors_truncated=True)
        for n in range(4)
    ]  # fmt: skip

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return json_response(cut[int(request.url.path[-2:])]) if request.url.path.startswith("/works/W") else page(cut)

    monkeypatch.setattr(http_module, "MEMBER_REQUESTS", 3)
    http = make_http(handle, clock)
    with http_module.on_behalf_of("Spadon (bfdGsGUAAAAJ)"):
        res = openalex.list_works(http, [SPADON], 2018, frozenset({"doi:10.1234/c3"}))
    assert res.value is not None and len(res.value) == 4
    assert seen == ["/works", "/works/W4000000002", "/works/W4000000001"]  # c3 is known, c0 is past the cap


def test_list_works_fails_when_a_cut_byline_cannot_be_refetched(clock: FakeClock) -> None:
    cut = work(authorships=_crowd(100, None), is_authors_truncated=True)

    def handle(request: httpx.Request) -> httpx.Response:
        return synthetic("unavailable") if request.url.path.startswith("/works/W") else page([cut])

    res = openalex.list_works(make_http(handle, clock), [SPADON], 2018)
    assert res.fetch is Fetch.FAILED and res.value is None


def test_list_works_failure_mid_paging_returns_nothing(clock: FakeClock) -> None:
    responses = [page([work("W4000000001")], "c2"), synthetic("unavailable")]
    res = openalex.list_works(make_http(lambda r: responses.pop(0) if len(responses) > 1 else responses[0], clock),
                              [SPADON], 2020)  # fmt: skip
    assert res.fetch is Fetch.FAILED
    assert res.value is None


def test_list_works_repeated_cursor_fails(clock: FakeClock) -> None:
    res = openalex.list_works(make_http(lambda r: page([work()], "same"), clock), [SPADON], 2020)
    assert (res.fetch, res.reason) == (Fetch.FAILED, "parse_error")


@pytest.mark.parametrize("name", ["truncated", "wrong_root", "html_challenge"])
def test_list_works_malformed_is_failed(clock: FakeClock, name: str) -> None:
    res = openalex.list_works(make_http(lambda r: synthetic(name), clock), [SPADON], 2020)
    assert res.fetch is Fetch.FAILED
    assert res.value is None
    assert res.reason in {"parse_error", "challenge"}


def test_list_works_empty_is_ok(clock: FakeClock) -> None:
    res = openalex.list_works(make_http(lambda r: synthetic("empty"), clock), [SPADON], 2020)
    assert (res.fetch, res.value) == (Fetch.OK, ())


def test_rate_limit_marks_host_down_without_sleep(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return synthetic("rate_limited")

    http = make_http(handle, clock)
    assert openalex.list_works(http, [SPADON], 2020).fetch is Fetch.FAILED
    assert openalex.author(http, SPADON).fetch is Fetch.FAILED
    assert http.down(openalex.HOST) == "retry_after 6626s"
    assert clock.sleeps == []
    assert len(seen) == 1


def test_unavailable_then_ok_retries(clock: FakeClock) -> None:
    responses = [synthetic("unavailable"), page([work()])]
    res = openalex.list_works(make_http(lambda r: responses.pop(0), clock), [SPADON], 2020)
    assert res.fetch is Fetch.OK
    assert res.value is not None and len(res.value) == 1


def test_bad_record_keeps_claim_without_record(clock: FakeClock) -> None:
    bad = work("W4000000003", authorships=[authorship("")])
    res = openalex.list_works(make_http(lambda r: page([bad, work("W4000000004", title=None)]), clock), [SPADON], 2020)
    assert res.value is not None
    assert len(res.value) == 1
    assert res.value[0].record is None
    assert res.value[0].ids == {"openalex:W4000000003", "doi:10.1016/j.oceaneng.2024.118303"}


def test_api_key_sent_and_never_logged(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return page([])

    log = io.StringIO()
    http = Http(log, {"api.openalex.org": "sk-test-0123456789"}, transport=httpx.MockTransport(handle),
                clock=clock, sleep=clock.sleep)  # fmt: skip
    openalex.list_works(http, [SPADON], 2020)
    assert seen[0].url.params["api_key"] == "sk-test-0123456789"
    assert "sk-test-0123456789" not in log.getvalue()
    assert "api_key=REDACTED" in log.getvalue()


def test_works_by_doi_recorded(clock: FakeClock) -> None:
    ids = [
        "doi:10.1016/j.oceaneng.2024.119138",
        "doi:10.1038/s41598-024-67552-2",
        "doi:10.1234/does-not-exist-citeforge",
    ]
    batch = openalex.works_by_doi(recorded(clock), ids)
    assert set(batch.found) == set(ids[:2])
    assert batch.missing == {ids[2]}
    assert batch.failed == frozenset()
    assert batch.found[ids[0]].source is Source.OPENALEX
    assert ids[0] in batch.found[ids[0]].ids


def test_works_by_doi_chunks_and_isolates_failed_chunk(clock: FakeClock) -> None:
    calls: list[list[str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        dois = request.url.params["filter"].removeprefix("doi:").split("|")
        calls.append(dois)
        if len(calls) == 2:
            return synthetic("wrong_root")
        return page([work(f"W{4000000000 + n}", doi=f"https://doi.org/{d}") for n, d in enumerate(dois)])

    ids = [f"doi:10.1000/x{n:03d}" for n in range(250)]
    batch = openalex.works_by_doi(make_http(handle, clock), ids)
    assert [len(c) for c in calls] == [100, 100, 50]
    assert batch.failed == {f"doi:{d}" for d in calls[1]}
    assert len(batch.found) == 150
    assert batch.missing == frozenset()


def test_works_by_doi_duplicates_are_deterministic_and_bad_records_fail(clock: FakeClock) -> None:
    doi = "https://doi.org/10.1000/dup"
    results = [
        work("W4000000009", doi=doi),
        work("W4000000002", doi=doi),
        work("W4000000005", doi="https://doi.org/10.1000/bad", title=""),
    ]
    for order in (results, results[::-1]):
        batch = openalex.works_by_doi(make_http(lambda r, o=order: page(o), clock),
                                      ["doi:10.1000/dup", "doi:10.1000/bad"])  # fmt: skip
        assert "openalex:W4000000002" in batch.found["doi:10.1000/dup"].ids
        assert batch.failed == {"doi:10.1000/bad"}


def test_works_by_doi_quotes_reserved_characters(clock: FakeClock) -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["filter"])
        return page([])

    openalex.works_by_doi(make_http(handle, clock), ["doi:10.1000/a,b", "doi:10.1000/c"])
    assert seen == ['doi:"10.1000/a,b"|10.1000/c']


def test_parse_arxiv_doi_is_preprint_whatever_the_type() -> None:
    r = openalex.parse_work(work(doi="https://doi.org/10.48550/arXiv.2310.18948v2", type="article"))
    assert r.ids == {"arxiv:2310.18948", "openalex:W4000000001"}
    assert r.kind is Kind.PREPRINT


def test_parse_drops_arxiv_crosswalk_on_doi_work() -> None:
    r = openalex.parse_work(work(ids={"arxiv": "https://arxiv.org/abs/2310.18948"}))
    assert not any(i.startswith("arxiv:") for i in r.ids)
    assert r.kind is Kind.ARTICLE


def test_parse_fields() -> None:
    r = openalex.parse_work(work(is_retracted=True, publication_year=2025))
    assert (r.year, r.status) == (2025, Status.RETRACTED)
    assert (r.volume, r.pages, r.language) == ("308", "1-12", "en")
    single = openalex.parse_work(work(biblio={"first_page": "118303", "last_page": "118303"}))
    assert single.pages == "118303"


def test_parse_conference_venue_is_inproceedings() -> None:
    item = work(primary_location={"source": {"display_name": "SIGSPATIAL", "type": "conference"}})
    assert openalex.parse_work(item).kind is Kind.INPROCEEDINGS


def test_parse_authors() -> None:
    inst = [{"ror": LAB, "display_name": "Dalhousie University"}]
    r = openalex.parse_work(work(authorships=[
        authorship("Spadon, Gabriel", id=f"https://openalex.org/{SPADON}", orcid=f"https://orcid.org/{SPADON_ORCID}",
                   institutions=inst, raw=["Faculty of Computer Science, Dalhousie University"]),
        authorship("ATLAS Collaboration"),
        authorship("Dalhousie University"),
    ]))  # fmt: skip
    me, group, org = r.authors
    assert (me.given, me.family, me.orcid, me.openalex, me.rors) == ("Gabriel", "Spadon", None, SPADON, (LAB,))
    assert me.affiliations == ("Faculty of Computer Science, Dalhousie University", "Dalhousie University")
    assert (group.literal, group.family) == ("ATLAS Collaboration", "")
    assert org.literal == "Dalhousie University"


def test_parse_nameless_author_fails() -> None:
    with pytest.raises(ParseError):
        openalex.parse_work(work(authorships=[{"author": {"display_name": ""}, "raw_author_name": None}]))


@pytest.mark.parametrize("name", ["Spadon G", "Gabriel S.", "Tomohiro I"])
def test_one_letter_family_becomes_literal(name: str) -> None:
    record = openalex.parse_work(work(authorships=[{"author": {"display_name": name}, "raw_author_name": None}]))
    assert [(a.literal, a.family) for a in record.authors] == [(name, "")]


@pytest.mark.parametrize("bad", [{"id": None}, {"title": None}, {"publication_year": "2024"}, {"authorships": {}}])
def test_parse_malformed_work_fails(bad: dict[str, Any]) -> None:
    with pytest.raises(ParseError):
        openalex.parse_work(work(**bad))


_json = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=12),
    lambda inner: (
        st.lists(inner, max_size=3)
        | st.dictionaries(st.sampled_from(["id", "display_name", "doi", "source", "ror"]), inner, max_size=3)
    ),
    max_leaves=5,
)
_keys = st.sampled_from(["id", "doi", "ids", "title", "publication_year", "type", "authorships",
                         "primary_location", "biblio", "language", "is_retracted"])  # fmt: skip


@given(st.dictionaries(_keys, _json))
def test_parse_work_only_raises_parse_error(item: dict[str, Any]) -> None:
    try:
        record = openalex.parse_work({**work(), **item})
    except ParseError:
        return
    assert bib_problems(record) == []


@given(st.lists(st.text(max_size=16), min_size=8, max_size=8))
def test_every_parsed_work_renders_a_valid_entry(texts: list[str]) -> None:
    title, first, last, volume, issue, venue, name, url = texts
    item = work(
        title=title,
        biblio={"first_page": first, "last_page": last, "volume": volume, "issue": issue},
        primary_location={"source": {"display_name": venue, "type": "journal"}, "landing_page_url": url},
        authorships=[authorship(name), authorship("Gabriel Spadon")],
    )
    try:
        record = openalex.parse_work(item)
    except ParseError:
        return
    assert bib_problems(record) == []


def test_control_characters_never_reach_the_entry() -> None:
    record = openalex.parse_work(work(biblio={"first_page": "12\x1b", "last_page": None}))
    assert record.pages == "12\x1b" and bib_problems(record) == []
    assert openalex.parse_work(work(doi="10.1234/ab\x1bc")).ids == {"openalex:W4000000001"}
    location = {"source": {"display_name": "V", "type": "journal"}, "landing_page_url": "https://x.org/a\x1bb"}
    assert bib_problems(openalex.parse_work(work(doi=None, primary_location=location))) == []


@given(st.binary(max_size=64))
def test_parse_page_only_raises_parse_error(body: bytes) -> None:
    with contextlib.suppress(ParseError):
        openalex.parse_page(body)


def test_recorded_fixtures_hold_no_key() -> None:
    for path in Path(FIXTURES / "http" / "api.openalex.org").glob("*.json.gz"):
        assert b"api_key" not in gzip.decompress(path.read_bytes())
