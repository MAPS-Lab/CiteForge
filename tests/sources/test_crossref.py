import gzip
import io
import json
from typing import Any

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.http import USER_AGENT, Http, ParseError
from citeforge.model import Fetch, Kind, Rel, Source, Status
from citeforge.sources import crossref
from tests.conftest import FIXTURES, FakeClock, Handler, fixture_transport, json_response, make_http

SYNTHETIC = FIXTURES / "synthetic" / "crossref"
RECORDED = [
    "doi:10.1093/nar/gkz935",
    "doi:10.1016/s0140-6736(97)11096-0",
    "doi:10.1016/s0140-6736(10)60175-4",
    "doi:10.1109/ojvt.2021.3097560",
    "doi:10.1016/j.oceaneng.2024.117234",
    "doi:10.9999/nonexistent.zzz",
    "doi:10.1364/opticaopen.29459153.v1",
    "doi:10.14264/313126",
    "doi:10.1056/nejmoa2034577",
    "doi:10.1145/3292500.3330701",
    "doi:10.1000/a,b",
]
CARD_TITLE = "The comprehensive antibiotic resistance database"


def recorded_http(clock: FakeClock) -> Http:
    return Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)


def synthetic(name: str) -> httpx.Response:
    data = json.loads((SYNTHETIC / f"{name}.json").read_text())
    return httpx.Response(data["status"], headers=data["headers"], content=data["body"].encode())


def work_list(*items: dict[str, Any], total: int | None = None) -> dict[str, Any]:
    message = {"items": list(items), "total-results": len(items) if total is None else total}
    return {"status": "ok", "message-type": "work-list", "message": message}


def item(doi: str, **kw: Any) -> dict[str, Any]:
    base = {
        "DOI": doi,
        "type": "journal-article",
        "title": ["Graph neural networks for vessel trajectory forecasting"],
        "author": [{"given": "Gabriel", "family": "Spadon", "sequence": "first"}],
        "issued": {"date-parts": [[2023, 5, 1]]},
    }
    return {**base, **kw}


def respond(payload: object) -> Handler:
    return lambda request: json_response(payload)


def filter_dois(request: httpx.Request) -> list[str]:
    return [p.removeprefix("doi:") for p in request.url.params["filter"].split(",")]


def answer_filter(request: httpx.Request) -> httpx.Response:
    return json_response(work_list(*(item(d) for d in filter_dois(request))))


def test_recorded_batch_splits_found_and_missing(clock: FakeClock) -> None:
    http = recorded_http(clock)
    batch = crossref.by_doi(http, RECORDED)
    assert batch.missing == {"doi:10.9999/nonexistent.zzz", "doi:10.1000/a,b"}
    assert batch.failed == frozenset()
    assert set(batch.found) == set(RECORDED) - batch.missing
    assert all(r.source is Source.CROSSREF and set(r.ids) == {k} for k, r in batch.found.items())
    assert http.stats()["api.crossref.org"]["requests"] == 2


def test_recorded_fields(clock: FakeClock) -> None:
    found = crossref.by_doi(recorded_http(clock), RECORDED).found
    card = found["doi:10.1093/nar/gkz935"]
    assert (card.year, card.dates, card.volume, card.pages) == (2019, (2019,), None, None)
    assert card.article_number == "gkz935"
    assert card.language == "en"
    assert card.title.startswith("CARD 2020: antibiotic resistome surveillance")

    wakefield = found["doi:10.1016/s0140-6736(97)11096-0"]
    assert wakefield.status is Status.RETRACTED
    assert (wakefield.year, wakefield.volume, wakefield.number, wakefield.pages) == (1998, "351", "9103", "637-641")
    notice = found["doi:10.1016/s0140-6736(10)60175-4"]
    assert notice.relations == {(Rel.RETRACTS, "doi:10.1016/s0140-6736(97)11096-0")}
    assert notice.status is Status.OK

    ye = found["doi:10.1109/ojvt.2021.3097560"]
    assert (ye.authors[0].family, ye.authors[0].orcid) == ("Ye", "0000-0002-8208-6295")

    preprint = found["doi:10.1364/opticaopen.29459153.v1"]
    assert preprint.kind is Kind.PREPRINT
    assert preprint.relations == {(Rel.PREPRINT_OF, "doi:10.1364/oe.572415")}
    assert preprint.school is None and preprint.institution is None

    thesis = found["doi:10.14264/313126"]
    assert (thesis.kind, thesis.school, thesis.degree) == (Kind.PHDTHESIS, "The University of Queensland", "PhD Thesis")
    assert thesis.year == 1999
    assert [(a.given, a.family) for a in thesis.authors] == [("Baojun", "Zhao")]

    kdd = found["doi:10.1145/3292500.3330701"]
    assert (kdd.kind, kdd.source_type, kdd.pages) == (Kind.INPROCEEDINGS, "proceedings-article", "2623-2631")
    assert kdd.event == "KDD '19: The 25th ACM SIGKDD Conference on Knowledge Discovery and Data Mining"
    assert kdd.authors[0].affiliations == ("Preferred Networks, Inc., Tokyo, Japan",)


def test_recorded_rors_groups_and_series(clock: FakeClock) -> None:
    ids = ["doi:10.33590/dermatolamj/agdvvm75", "doi:10.1007/978-3-031-70518-2_11", "doi:10.1051/0004-6361/201833910"]
    found = crossref.by_doi(recorded_http(clock), ids).found
    assert found["doi:10.33590/dermatolamj/agdvvm75"].authors[0].rors == ("https://ror.org/03wmf1y16",)
    planck = found["doi:10.1051/0004-6361/201833910"]
    assert [a.literal for a in planck.authors if a.literal] == ["Planck Collaboration"]
    chapter = found["doi:10.1007/978-3-031-70518-2_11"]
    assert chapter.container == (
        "Lecture Notes in Networks and Systems",
        "Artificial Intelligence Algorithm Design for Systems",
    )
    assert chapter.kind is Kind.INCOLLECTION


def test_recorded_fixtures_hold_no_abstracts_or_references() -> None:
    for path in (FIXTURES / "http" / "api.crossref.org").glob("*.json.gz"):
        body = json.loads(gzip.decompress(path.read_bytes()))["body"]
        assert '"abstract"' not in body and '"reference"' not in body


def test_recorded_search_with_and_without_year(clock: FakeClock) -> None:
    http = recorded_http(clock)
    hits = crossref.search(http, CARD_TITLE, "Alcock", 2020)
    assert hits.fetch is Fetch.OK and hits.value
    assert "doi:10.1093/nar/gkz935" in {i for r in hits.value for i in r.ids}
    assert all(r.year is not None and 2019 <= r.year <= 2021 for r in hits.value)
    open_ended = crossref.search(http, CARD_TITLE, "Alcock", None)
    assert open_ended.fetch is Fetch.OK and len(open_ended.value or ()) == 5


def test_request_shape_is_generic_and_chunked(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return answer_filter(request)

    ids = [f"doi:10.1000/x{n:03d}" for n in range(120)]
    batch = crossref.by_doi(make_http(handle, clock), list(reversed(ids)))
    assert set(batch.found) == set(ids) and not batch.missing and not batch.failed
    assert [len(filter_dois(r)) for r in seen] == [50, 50, 20]
    assert [i for r in seen for i in filter_dois(r)] == [i.removeprefix("doi:") for i in ids]
    for r in seen:
        assert r.url.path == "/works" and r.url.params["rows"] == "100"
        assert r.headers["user-agent"] == USER_AGENT
        assert "mailto" not in str(r.url) and "mailto" not in r.headers.get("user-agent", "")
    assert crossref.by_doi(make_http(handle, clock), []) == crossref.Batch({}, frozenset(), frozenset())


def test_rejected_chunk_falls_back_to_single_lookups(clock: FakeClock) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/works":
            return synthetic("validation_400")
        doi = request.url.path.removeprefix("/works/")
        if doi == "10.1000/gone":
            return httpx.Response(404, headers={"content-type": "text/plain"}, content=b"Resource not found.")
        if doi == "10.1000/bad":
            return synthetic("validation_400")
        return json_response({"status": "ok", "message-type": "work", "message": item(doi)})

    batch = crossref.by_doi(make_http(handle, clock), ["doi:10.1000/ok", "doi:10.1000/gone", "doi:10.1000/bad"])
    assert set(batch.found) == {"doi:10.1000/ok"}
    assert batch.missing == {"doi:10.1000/gone", "doi:10.1000/bad"}
    assert not batch.failed


def test_comma_doi_uses_the_single_route(clock: FakeClock) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.raw_path.decode())
        if request.url.path == "/works":
            return answer_filter(request)
        return json_response({"status": "ok", "message-type": "work", "message": item("10.1000/a,b")})

    batch = crossref.by_doi(make_http(handle, clock), ["doi:10.1000/a,b", "doi:10.1000/plain"])
    assert set(batch.found) == {"doi:10.1000/a,b", "doi:10.1000/plain"}
    assert "/works/10.1000/a%2Cb" in paths


@pytest.mark.parametrize("payload", ["truncated", "wrong_root", "unavailable"])
def test_unreadable_chunk_fails_without_reading_as_missing(clock: FakeClock, payload: str) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return synthetic(payload) if "x000" in request.url.params["filter"] else answer_filter(request)

    ids = [f"doi:10.1000/x{n:03d}" for n in range(60)]
    batch = crossref.by_doi(make_http(handle, clock), ids)
    assert batch.failed == set(ids[:50])
    assert set(batch.found) == set(ids[50:])
    assert not batch.missing


def test_empty_result_reads_as_missing(clock: FakeClock) -> None:
    batch = crossref.by_doi(make_http(lambda r: synthetic("empty"), clock), ["doi:10.1000/a", "doi:10.1000/b"])
    assert batch.missing == {"doi:10.1000/a", "doi:10.1000/b"} and not batch.failed and not batch.found


def test_truncated_listing_marks_unmatched_failed(clock: FakeClock) -> None:
    batch = crossref.by_doi(
        make_http(respond(work_list(item("10.1000/a"), total=2)), clock), ["doi:10.1000/a", "doi:10.1000/b"]
    )
    assert set(batch.found) == {"doi:10.1000/a"} and batch.failed == {"doi:10.1000/b"} and not batch.missing


def test_malformed_item_fails_only_its_doi(clock: FakeClock) -> None:
    bad = item("10.1000/b", author="not a list")
    batch = crossref.by_doi(
        make_http(respond(work_list(item("10.1000/a"), bad)), clock),
        ["doi:10.1000/a", "doi:10.1000/b", "doi:10.1000/c"],
    )
    assert set(batch.found) == {"doi:10.1000/a"}
    assert batch.failed == {"doi:10.1000/b"} and batch.missing == {"doi:10.1000/c"}


def test_item_without_doi_fails_every_unmatched_id(clock: FakeClock) -> None:
    payload = work_list({"type": "journal-article"}, item("10.1000/a"))
    batch = crossref.by_doi(make_http(respond(payload), clock), ["doi:10.1000/a", "doi:10.1000/b"])
    assert set(batch.found) == {"doi:10.1000/a"} and batch.failed == {"doi:10.1000/b"} and not batch.missing


def test_long_retry_after_marks_host_down_without_sleeping(clock: FakeClock) -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return synthetic("rate_limited")

    http = make_http(handle, clock)
    ids = [f"doi:10.1000/x{n:03d}" for n in range(60)]
    batch = crossref.by_doi(http, ids)
    assert batch.failed == set(ids) and not batch.missing
    assert calls == 1 and clock.sleeps == []
    assert http.down(crossref.HOST) == "retry_after 6626s"
    later = crossref.search(http, CARD_TITLE, "Alcock", 2020)
    assert (later.fetch, later.reason, calls) == (Fetch.FAILED, "down", 1)


def test_transient_errors_retry_then_succeed(clock: FakeClock) -> None:
    replies = iter([synthetic("unavailable"), httpx.Response(429, headers={"retry-after": "3"})])

    def handle(request: httpx.Request) -> httpx.Response:
        return next(replies, None) or answer_filter(request)

    batch = crossref.by_doi(make_http(handle, clock), ["doi:10.1000/a"])
    assert set(batch.found) == {"doi:10.1000/a"}
    assert 3.0 in clock.sleeps


def test_search_request_parameters(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(work_list(item("10.1000/a"), {"DOI": "10.1000/b"}))

    http = make_http(handle, clock)
    res = crossref.search(http, "<i>Graph</i> neural   networks", " Spadon ", 2023)
    assert res.fetch is Fetch.OK and [sorted(r.ids) for r in res.value or ()] == [["doi:10.1000/a"]]
    params = seen[0].url.params
    assert params["query.bibliographic"] == "Graph neural networks"
    assert (params["query.author"], params["rows"]) == ("Spadon", "5")
    assert params["filter"] == "from-pub-date:2022,until-pub-date:2024"
    crossref.search(http, "Graph neural networks", "", None)
    assert "filter" not in seen[1].url.params and "query.author" not in seen[1].url.params
    assert crossref.search(http, "  <b></b> ", "Spadon", 2023) == crossref.Result(Fetch.OK, ())
    assert len(seen) == 2


@pytest.mark.parametrize("payload", ["truncated", "wrong_root", "unavailable"])
def test_search_failure_is_failed(clock: FakeClock, payload: str) -> None:
    res = crossref.search(make_http(lambda r: synthetic(payload), clock), CARD_TITLE, "Alcock", 2020)
    assert res.fetch is Fetch.FAILED and res.value is None


def date(*parts: Any) -> dict[str, Any]:
    return {"date-parts": [list(parts)]}


@pytest.mark.parametrize(
    ("dates", "year"),
    [
        ({"published-print": date(2021, 3), "issued": date(2020, 11)}, 2021),
        ({"journal-issue": {"published-print": date(2022)}, "issued": date(2021)}, 2022),
        ({"issued": date(2019, 10, 29)}, 2019),
        ({"issued": date(None), "approved": date(1999, 1, 1)}, 1999),
        ({"issued": {"date-parts": [[]]}, "posted": date(2024, 2)}, 2024),
        ({"issued": date("2018")}, 2018),
        ({"issued": date(True)}, None),
        ({"issued": date(20201)}, None),
        ({"issued": {"date-parts": "2020"}}, None),
        ({}, None),
    ],
)
def test_year_rule(dates: dict[str, Any], year: int | None) -> None:
    record = crossref.parse_work({**item("10.1000/a"), "issued": None, **dates})
    assert record.year == year


def test_dates_collect_every_stated_year() -> None:
    record = crossref.parse_work(
        item(
            "10.1000/a",
            issued=date(2023, 12),
            **{"published-online": date(2023, 12), "published-print": date(2024, 2), "accepted": date(2023, 9)},
        )
    )
    assert (record.year, record.dates) == (2024, (2023, 2024))


@pytest.mark.parametrize(
    ("title", "subtitle", "expected"),
    [
        (["Graph networks"], ["A survey"], "Graph networks: A survey"),
        (["Graph networks: a survey"], ["A survey"], "Graph networks: a survey"),
        (["  ", "Graph networks"], None, "Graph networks"),
        ([], ["Only a subtitle"], "Only a subtitle"),
        (["Fe<sub>3</sub>O<sub>4</sub> &amp; \n water"], None, "Fe3O4 & water"),
    ],
)
def test_title_and_subtitle(title: list[str], subtitle: list[str] | None, expected: str) -> None:
    assert crossref.parse_work(item("10.1000/a", title=title, subtitle=subtitle)).title == expected


def test_byline_rules() -> None:
    authors = [
        {"sequence": "additional", "affiliation": []},
        {"given": "Stan", "family": "Matwin", "sequence": "additional"},
        {"given": "Gabriel", "family": "Spadon", "sequence": "first", "ORCID": "http://orcid.org/0000-0002-1825-0097"},
        {"name": "MAPS Lab Consortium", "sequence": "additional"},
        {"given": "Sutrisno", "sequence": "additional"},
        {
            "given": "Ana",
            "family": "Souza",
            "ORCID": "https://orcid.org/0000-0002-1825-0098",
            "affiliation": [
                {"name": "Dalhousie University", "id": [{"id": "01E6QKS80", "id-type": "ror"}]},
                {"name": "Dalhousie University", "id": [{"id": "https://ror.org/01e6qks80/", "id-type": "ROR"}]},
                {"id": [{"id": "grid.55602.34", "id-type": "GRID"}]},
                "not an object",
            ],
        },
    ]
    record = crossref.parse_work(item("10.1000/a", author=authors, editor=[{"given": "Eve", "family": "Editor"}]))
    assert [(a.given, a.family, a.literal) for a in record.authors] == [
        ("Gabriel", "Spadon", None),
        ("Stan", "Matwin", None),
        ("", "", "MAPS Lab Consortium"),
        ("", "", "Sutrisno"),
        ("Ana", "Souza", None),
    ]
    assert record.authors[0].orcid == "0000-0002-1825-0097"
    assert record.authors[4].orcid is None
    assert record.authors[4].rors == ("https://ror.org/01e6qks80",)
    assert record.authors[4].affiliations == ("Dalhousie University",)
    assert [(e.given, e.family) for e in record.editors] == [("Eve", "Editor")]


def test_one_letter_family_is_kept() -> None:
    record = crossref.parse_work({**item("10.1000/a"), "author": [{"given": "Tomohiro", "family": "I"}]})
    assert [(a.given, a.family) for a in record.authors] == [("Tomohiro", "I")]


@pytest.mark.parametrize(
    "bad",
    [
        {"author": "Gabriel Spadon"},
        {"author": ["Gabriel Spadon"]},
        {"DOI": "not-a-doi"},
        {"DOI": None},
        {"type": ""},
        {"title": [], "subtitle": None},
    ],
)
def test_malformed_work_raises_parse_error(bad: dict[str, Any]) -> None:
    with pytest.raises(ParseError):
        crossref.parse_work({**item("10.1000/a"), **bad})


def test_relations_and_updates() -> None:
    relation = {
        "has-preprint": [{"id-type": "arxiv", "id": "arXiv:2310.18948v2"}, {"id-type": "doi", "id": "10.1000/A"}],
        "is-version-of": [{"id-type": "uri", "id": "https://arxiv.org/abs/2101.00001"}],
        "has-translation": [{"id-type": "uri", "id": "https://doi.org/10.1000/PT"}],
        "is-supplemented-by": [{"id-type": "doi", "id": "10.1000/data"}],
        "is-preprint-of": "not a list",
        "is-identical-to": [{"id-type": "issn", "id": "1234-5678"}, "junk"],
    }
    updates = [
        {"type": "Retration", "DOI": "10.1000/target1"},
        {"type": "expression-of-concern", "DOI": "10.1000/target2"},
        {"type": "new_version", "DOI": "10.1000/target3"},
        {"type": "correction", "DOI": "10.1000/a"},
        {"type": "sample_update", "DOI": "10.1000/target4"},
        {"type": "erratum", "DOI": "bad"},
    ]
    record = crossref.parse_work(item("10.1000/a", relation=relation, **{"update-to": updates}))
    assert record.relations == {
        (Rel.HAS_PREPRINT, "arxiv:2310.18948"),
        (Rel.VERSION_OF, "arxiv:2101.00001"),
        (Rel.HAS_TRANSLATION, "doi:10.1000/pt"),
        (Rel.RETRACTS, "doi:10.1000/target1"),
        (Rel.CORRECTS, "doi:10.1000/target2"),
        (Rel.VERSION_OF, "doi:10.1000/target3"),
    }


@pytest.mark.parametrize(
    ("updated_by", "status"),
    [
        ([{"type": "correction", "DOI": "10.1000/n1"}], Status.OK),
        ([{"type": "withdrawal", "DOI": "10.1000/n1"}], Status.WITHDRAWN),
        ([{"type": "removal", "DOI": "10.1000/n1"}], Status.WITHDRAWN),
        ([{"type": "withdrawal", "DOI": "10.1000/n1"}, {"type": "Retraction", "DOI": "10.1000/n2"}], Status.RETRACTED),
        ([{"type": "partial_retraction", "DOI": "10.1000/n1"}], Status.OK),
        ("retraction", Status.OK),
        (None, Status.OK),
    ],
)
def test_updated_by_sets_status(updated_by: object, status: Status) -> None:
    record = crossref.parse_work(item("10.1000/a", **{"updated-by": updated_by}))
    assert record.status is status and not record.relations


def test_venue_fields_and_school_by_type() -> None:
    fields = {
        "container-title": ["Lecture Notes in Computer Science", "", "Lecture Notes in Computer Science", "ECML"],
        "event": {"name": "ECML PKDD 2023"},
        "publisher": "Springer Nature Switzerland",
        "volume": 14169,
        "issue": "",
        "page": "3-19",
        "article-number": "e42",
        "language": "en",
        "degree": ["", "Master of Science"],
        "institution": [{"name": "Dalhousie University"}, {"name": "Other"}],
    }
    chapter = crossref.parse_work(item("10.1000/a", type="book-chapter", **fields))
    assert chapter.container == ("Lecture Notes in Computer Science", "ECML")
    assert (chapter.event, chapter.publisher, chapter.volume, chapter.number) == (
        "ECML PKDD 2023",
        "Springer Nature Switzerland",
        "14169",
        None,
    )
    assert (chapter.pages, chapter.article_number, chapter.language) == ("3-19", "e42", "en")
    assert chapter.school is None and chapter.institution is None
    assert chapter.kind is Kind.INCOLLECTION
    thesis = crossref.parse_work(item("10.1000/a", type="dissertation", **fields))
    assert (thesis.school, thesis.degree, thesis.institution) == ("Dalhousie University", "Master of Science", None)
    report = crossref.parse_work(item("10.1000/a", type="report", **fields))
    assert (report.institution, report.school, report.kind) == ("Dalhousie University", None, Kind.TECHREPORT)
    other = crossref.parse_work(item("10.1000/a", type="Grant"))
    assert (other.kind, other.source_type) == (Kind.OTHER, "grant")


def test_arxiv_datacite_doi_becomes_arxiv_id() -> None:
    assert crossref.parse_work(item("10.48550/arXiv.2310.18948")).ids == {"arxiv:2310.18948"}


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=8),
    lambda children: st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4),
    max_leaves=20,
)
crossref_keys = st.sampled_from(
    ["DOI", "type", "title", "subtitle", "author", "editor", "issued", "published-print", "journal-issue",
     "relation", "update-to", "updated-by", "container-title", "event", "institution", "degree", "page", "volume"]
)  # fmt: skip


@given(st.dictionaries(crossref_keys, json_values, max_size=10))
def test_parse_work_only_raises_parse_error(overrides: dict[str, Any]) -> None:
    try:
        record = crossref.parse_work({**item("10.1000/a"), **overrides})
    except ParseError:
        return
    assert record.title and record.ids
    assert all(a.family or a.literal for a in record.authors)


@given(json_values)
def test_arbitrary_body_never_escapes_as_another_error(value: object) -> None:
    batch = crossref.by_doi(make_http(respond(value)), ["doi:10.1000/a"])
    assert batch.found.keys() | batch.missing | batch.failed == {"doi:10.1000/a"}
