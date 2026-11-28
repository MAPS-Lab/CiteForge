import io

import httpx
import pytest

from citeforge.http import MAX_REDIRECTS, Http, ParseError
from citeforge.model import Author, Fetch, Kind, Source
from citeforge.sources import doiorg
from tests.conftest import FIXTURES, FakeClock, fixture_transport, json_response, make_http

SYNTHETIC = FIXTURES / "synthetic" / "doiorg"
MEDRA_CHAPTER = "doi:10.1400/227379"
MEDRA_ARTICLE = "doi:10.7413/2281-8138002"
JALC = "doi:10.11517/pjsai.jsai2020.0_1k3es201"
DEAD = "doi:10.31234/osf.io/rqexa"
CROSSREF = "doi:10.1093/nar/gkz935"
CSL_TYPE = "application/vnd.citationstyles.csl+json"


def synthetic(name: str) -> bytes:
    return (SYNTHETIC / name).read_bytes()


@pytest.fixture
def recorded(clock: FakeClock) -> Http:
    return Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)


def counting(handler):
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return handle, seen


def redirect(location: str, status: int = 302) -> httpx.Response:
    return httpx.Response(status, headers={"location": location, "content-type": "text/html"})


def csl_reply(payload: dict) -> httpx.Response:
    return json_response(payload, headers={"content-type": CSL_TYPE})


def test_agency_recorded_batch(recorded: Http) -> None:
    res = doiorg.agency(recorded, [MEDRA_CHAPTER, JALC, DEAD, MEDRA_ARTICLE, CROSSREF, MEDRA_CHAPTER])
    assert res.fetch is Fetch.OK
    assert res.value == {
        MEDRA_CHAPTER: "mEDRA",
        MEDRA_ARTICLE: "mEDRA",
        JALC: "JaLC",
        DEAD: None,
        CROSSREF: "Crossref",
    }


def test_agency_empty_input_sends_nothing(clock: FakeClock) -> None:
    handle, seen = counting(lambda r: json_response([]))
    assert doiorg.agency(make_http(handle, clock), []) == doiorg.Result(Fetch.OK, {})
    assert seen == []


def test_agency_chunks_of_twenty_and_encodes_each_doi(clock: FakeClock) -> None:
    ids = [f"doi:10.1234/w{n:02d}" for n in range(25)] + ["doi:10.1234/ab,cd", "doi:10.1234/x#y?z"]

    def answer(request: httpx.Request) -> httpx.Response:
        raw = request.url.raw_path.decode().removeprefix("/ra/")
        dois = [httpx.URL("https://doi.org/" + part).path.lstrip("/") for part in raw.split(",")]
        return json_response([{"DOI": d.upper(), "RA": "mEDRA"} for d in dois])

    handle, seen = counting(answer)
    res = doiorg.agency(make_http(handle, clock), ids)
    assert res.fetch is Fetch.OK
    assert res.value == dict.fromkeys(ids, "mEDRA")
    assert [len(r.url.raw_path.decode().split(",")) for r in seen] == [20, 7]
    assert b"ab%2Ccd" in seen[0].url.raw_path
    assert b"x%23y%3Fz" in seen[1].url.raw_path


def test_agency_partial_outage_keeps_good_chunks(clock: FakeClock) -> None:
    ids = [f"doi:10.1234/w{n:02d}" for n in range(30)]

    def answer(request: httpx.Request) -> httpx.Response:
        if b"w25" in request.url.raw_path:
            return httpx.Response(503)
        raw = request.url.raw_path.decode().removeprefix("/ra/")
        return json_response([{"DOI": d, "RA": "DataCite"} for d in raw.split(",")])

    res = doiorg.agency(make_http(answer, clock), ids)
    assert res.fetch is Fetch.FAILED
    assert res.reason == "http_error"
    assert res.value == dict.fromkeys(ids[:20], "DataCite")


def test_agency_total_outage_is_failed_never_empty(clock: FakeClock) -> None:
    res = doiorg.agency(make_http(lambda r: httpx.Response(503), clock), [MEDRA_CHAPTER])
    assert res == doiorg.Result(Fetch.FAILED, None, "http_error")


def test_agency_long_retry_after_marks_host_down_without_sleep(clock: FakeClock) -> None:
    handle, seen = counting(lambda r: httpx.Response(429, headers={"retry-after": "6626"}))
    http = make_http(handle, clock)
    res = doiorg.agency(http, [f"doi:10.1234/w{n:02d}" for n in range(45)])
    assert res.fetch is Fetch.FAILED and res.value is None
    assert len(seen) == 1
    assert clock.sleeps == []
    assert http.down(doiorg.HOST) is not None


def test_agency_retries_503_then_succeeds(clock: FakeClock) -> None:
    replies = [httpx.Response(503), json_response([{"DOI": "10.1400/227379", "RA": "mEDRA"}])]
    res = doiorg.agency(make_http(lambda r: replies.pop(0), clock), [MEDRA_CHAPTER])
    assert res == doiorg.Result(Fetch.OK, {MEDRA_CHAPTER: "mEDRA"})


@pytest.mark.parametrize("name", ["ra_truncated.json", "ra_wrong_root.json", "ra_row_without_doi.json"])
def test_agency_malformed_reply_is_failed(clock: FakeClock, name: str) -> None:
    http = make_http(
        lambda r: httpx.Response(200, headers={"content-type": "application/json"}, content=synthetic(name))
    )
    assert doiorg.agency(http, [MEDRA_CHAPTER]) == doiorg.Result(Fetch.FAILED, None, "parse_error")
    with pytest.raises(ParseError):
        doiorg.parse_agencies(synthetic(name))


def test_agency_rows_left_out_or_unknown_are_absent(clock: FakeClock) -> None:
    assert doiorg.parse_agencies(synthetic("ra_empty.json")) == {}
    assert doiorg.parse_agencies(synthetic("ra_odd_rows.json")) == {"doi:10.11517/x.y": None}
    http = make_http(lambda r: httpx.Response(200, content=synthetic("ra_odd_rows.json")), clock)
    res = doiorg.agency(http, [MEDRA_CHAPTER, MEDRA_ARTICLE, "doi:10.11517/x.y"])
    assert res == doiorg.Result(Fetch.OK, {"doi:10.11517/x.y": None})


def test_agency_challenge_page_is_failed(clock: FakeClock) -> None:
    http = make_http(lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html></html>"))
    assert doiorg.agency(http, [MEDRA_CHAPTER]) == doiorg.Result(Fetch.FAILED, None, "challenge")


def test_csl_recorded_medra_chapter_without_date(recorded: Http) -> None:
    res = doiorg.csl(recorded, MEDRA_CHAPTER)
    assert res.fetch is Fetch.OK and res.value is not None
    r = res.value
    assert (r.source, r.kind, r.source_type) == (Source.CSL, Kind.INCOLLECTION, "chapter")
    assert r.ids == {MEDRA_CHAPTER}
    assert r.title == "L'inquisition et l'Islam"
    assert r.authors == (Author("Bernard", "Vincent"),)
    assert r.year is None
    assert r.container[0].startswith("L'inquisizione : atti del Simposio internazionale : Città del Vaticano")
    assert r.publisher == "Biblioteca Apostolica Vaticana"
    assert r.language == "fre"


def test_csl_recorded_medra_article(recorded: Http) -> None:
    r = doiorg.csl(recorded, MEDRA_ARTICLE).value
    assert r is not None
    assert (r.source_type, r.title, r.year) == ("article-journal", "Reliance Imaginale", 2012)
    assert r.container == ("Im@go. Rivista di Studi Sociali sull'Immaginario",)
    assert (r.publisher, r.number, r.pages, r.volume) == ("Mimesis Edizioni", "Anno I - n. 0", "10\u201318", None)


def test_csl_recorded_jalc_keeps_unicode_and_has_no_type(recorded: Http) -> None:
    r = doiorg.csl(recorded, JALC).value
    assert r is not None
    assert r.source_type == ""
    assert r.ids == {JALC}
    assert r.title == "Spatio-Temporal Change Detection Using Granger Causal Relation"
    assert [a.family for a in r.authors] == ["PAVASANT", "NUMAO", "FUKUI"]
    assert (r.year, r.container, r.publisher) == (2020, ("人工知能学会全国大会論文集",), "一般社団法人 人工知能学会")
    assert (r.volume, r.number, r.pages, r.language) == ("JSAI2020", None, "1K3ES201-1K3ES201", "ja")


def test_csl_recorded_dead_doi_is_not_found(recorded: Http) -> None:
    assert doiorg.csl(recorded, DEAD) == doiorg.Result(Fetch.NOT_FOUND, None, "not_found")


def test_csl_sends_accept_and_follows_relative_location(clock: FakeClock) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/10.1400/227379":
            return redirect("/meta/10.1400/227379", 303)
        return csl_reply({"type": "chapter", "title": "T", "DOI": "10.1400/227379"})

    handle, seen = counting(answer)
    res = doiorg.csl(make_http(handle, clock), MEDRA_CHAPTER)
    assert res.fetch is Fetch.OK
    assert [str(r.url) for r in seen] == ["https://doi.org/10.1400/227379", "https://doi.org/meta/10.1400/227379"]
    assert all(r.headers["accept"] == CSL_TYPE for r in seen)


def test_csl_three_hops_allowed_four_is_failed(clock: FakeClock) -> None:
    chain = [
        redirect("https://data.medra.org/a"),
        redirect("https://data.medra.org/b"),
        redirect("https://data.medra.org/c"),
    ]
    replies = [*chain, csl_reply({"title": "T"})]
    assert doiorg.csl(make_http(lambda r: replies.pop(0), clock), MEDRA_CHAPTER).fetch is Fetch.OK
    handle, seen = counting(lambda r: redirect(f"https://data.medra.org/{len(seen)}"))
    assert doiorg.csl(make_http(handle, clock), MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "redirect_limit")
    assert len(seen) == MAX_REDIRECTS + 1


@pytest.mark.parametrize("location", ["https://example.org/landing", "http://data.medra.org/10.1400/227379"])
def test_csl_redirect_off_allowlist_sends_no_request(clock: FakeClock, location: str) -> None:
    handle, seen = counting(lambda r: redirect(location))
    assert doiorg.csl(make_http(handle, clock), MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "redirect_blocked")
    assert len(seen) == 1


def test_csl_follows_publications_office_redirect(clock: FakeClock) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        if request.url.host == "doi.org":
            return redirect("https://ra.publications.europa.eu/data/10.2760%2F53624")
        return csl_reply({"type": "book", "title": "SASLAB final technical report", "DOI": "10.2760/53624"})

    res = doiorg.csl(make_http(answer, clock), "doi:10.2760/53624")
    assert res.fetch is Fetch.OK
    assert res.value is not None and res.value.title == "SASLAB final technical report"


def test_blocked_redirects_never_open_the_circuit(clock: FakeClock) -> None:
    http = make_http(lambda r: redirect("https://example.org/landing"), clock)
    for _ in range(8):
        assert doiorg.csl(http, MEDRA_CHAPTER).reason == "redirect_blocked"
    assert http.down(doiorg.HOST) is None


def test_csl_redirect_without_location_is_failed(clock: FakeClock) -> None:
    http = make_http(lambda r: httpx.Response(302), clock)
    assert doiorg.csl(http, MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "redirect")


def test_csl_404_after_redirect_is_failed_not_dead(clock: FakeClock) -> None:
    replies = [redirect("https://data.medra.org/10.1400%2F227379"), httpx.Response(404)]
    res = doiorg.csl(make_http(lambda r: replies.pop(0), clock), MEDRA_CHAPTER)
    assert res == doiorg.Result(Fetch.FAILED, None, "not_found")


def test_csl_timeouts_are_failed(clock: FakeClock) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    assert doiorg.csl(make_http(answer, clock), MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "timeout")


def test_csl_down_host_sends_nothing(clock: FakeClock) -> None:
    handle, seen = counting(lambda r: httpx.Response(503))
    http = make_http(handle, clock)
    for _ in range(5):
        doiorg.csl(http, MEDRA_CHAPTER)
    sent = len(seen)
    assert http.down(doiorg.HOST) == "circuit"
    assert doiorg.csl(http, MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "down")
    assert len(seen) == sent


@pytest.mark.parametrize(
    "name",
    [
        "csl_truncated.json",
        "csl_wrong_root.json",
        "csl_no_title.json",
        "csl_no_family.json",
        "csl_author_not_list.json",
    ],
)
def test_csl_malformed_is_parse_error(clock: FakeClock, name: str) -> None:
    http = make_http(lambda r: httpx.Response(200, headers={"content-type": CSL_TYPE}, content=synthetic(name)))
    assert doiorg.csl(http, MEDRA_CHAPTER) == doiorg.Result(Fetch.FAILED, None, "parse_error")
    with pytest.raises(ParseError):
        doiorg.parse_csl(synthetic(name))


def test_parse_csl_odd_shapes() -> None:
    r = doiorg.parse_csl(synthetic("csl_shapes.json"))
    assert r.ids == {"arxiv:2310.18948"}
    assert r.title == "Vessel & port forecasting"
    assert r.authors == (
        Author("", "", literal="MAPS Lab Consortium"),
        Author("Vincent", "van Gogh", orcid="0000-0002-1825-0097"),
        Author("Ana de", "Souza"),
    )
    assert r.editors == (Author("Stan", "Matwin"),)
    assert r.year == 2021
    assert r.container == ("Lecture Notes",)
    assert r.event == "Workshop on Ocean Data"
    assert (r.volume, r.number, r.pages, r.article_number) == ("12", "3", "101-115", "e42")
    assert (r.school, r.publisher, r.degree) == ("Dalhousie University", None, "Master's thesis")


def test_csl_adds_requested_id_to_record(clock: FakeClock) -> None:
    http = make_http(lambda r: csl_reply({"title": "T", "DOI": "10.9999/alias"}), clock)
    r = doiorg.csl(http, MEDRA_CHAPTER).value
    assert r is not None and r.ids == {MEDRA_CHAPTER, "doi:10.9999/alias"}


@pytest.mark.parametrize(
    ("date", "year"),
    [
        ({"date-parts": [[2019, 10, 29]]}, 2019),
        ({"date-parts": [["2019"]]}, 2019),
        ({"date-parts": [["²⁰¹⁹"]]}, None),
        ({"date-parts": [[None]]}, None),
        ({"date-parts": [[]]}, None),
        ({"date-parts": [[True]]}, None),
        ({"date-parts": [[20190]]}, None),
        ({"raw": "Spring 2018"}, 2018),
        ({"literal": "n.d."}, None),
        ("2019", None),
    ],
)
def test_year_shapes(date: object, year: int | None) -> None:
    assert doiorg._year(date) == year
