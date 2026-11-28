import contextlib
import io
from xml.sax.saxutils import escape

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.http import USER_AGENT, Http, ParseError
from citeforge.model import Author, Claim, Fetch, Kind, Record, Rel, Source, Status
from citeforge.sources import dblp
from tests.conftest import FIXTURES, FakeClock, fixture_transport, make_http

SYNTHETIC = FIXTURES / "synthetic" / "dblp"
XML = {"content-type": "application/xml; charset=utf-8"}


def recorded() -> Http:
    clock = FakeClock()
    return Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)


def synthetic(name: str) -> bytes:
    return (SYNTHETIC / name).read_bytes()


def serving(*responses: httpx.Response) -> tuple[Http, list[httpx.Request], FakeClock]:
    seen: list[httpx.Request] = []
    queue = list(responses)
    clock = FakeClock()

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0)

    return make_http(handle, clock), seen, clock


def profile(pid: str) -> dblp.DblpPerson:
    res = dblp.person(recorded(), pid)
    assert res.fetch is Fetch.OK and res.value is not None
    return res.value


def claim(person: dblp.DblpPerson, key: str) -> Claim:
    return next(c for c in person.claims if f"dblp:{key}" in c.ids)


def record(person: dblp.DblpPerson, key: str) -> Record:
    r = claim(person, key).record
    assert r is not None
    return r


def test_spadon_profile_parses_every_entry():
    p = profile("192/1659")
    assert p.names == ("Gabriel Spadon", "Gabriel Spadon de Souza")
    assert p.orcids == ("0000-0001-8437-4349",)
    assert len(p.claims) == 62
    assert all(c.source is Source.DBLP and c.record is not None and c.record.ids == c.ids for c in p.claims)


def test_inproceedings_record_fields():
    p = profile("192/1659")
    c, r = claim(p, "conf/mdm/TeixeiraSLS26"), record(p, "conf/mdm/TeixeiraSLS26")
    assert c.ids == {"dblp:conf/mdm/TeixeiraSLS26", "doi:10.1109/mdm71479.2026.00053"}
    assert (
        c.title
        == r.title
        == ("LOMAD: Local Anomaly Detection in Maritime Trajectories Using LSTM Prediction and Visual Analytics")
    )
    assert (r.kind, r.source_type, r.year, r.dates, c.year) == (
        Kind.INPROCEEDINGS,
        "inproceedings",
        2026,
        (2026,),
        2026,
    )
    assert r.container == ("MDM",)
    assert r.pages == "351-354"
    assert r.url is None
    assert [(a.given, a.family) for a in r.authors] == [
        ("Martim R.", "Teixeira"),
        ("Gabriel", "Spadon"),
        ("Claudio D. G.", "Linhares"),
        ("Amílcar", "Soares"),
    ]


def test_corr_entry_is_an_arxiv_preprint():
    p = profile("192/1659")
    c = claim(p, "journals/corr/abs-2606-17978")
    assert c.ids == {"dblp:journals/corr/abs-2606-17978", "arxiv:2606.17978"}
    r = record(p, "journals/corr/abs-2606-17978")
    assert (r.kind, r.volume) == (Kind.PREPRINT, None)


def test_person_orcid_and_unicode_name_from_profile():
    p = profile("32/5995")
    assert p.names == ("Vlado Keselj", "Vlado Kešelj")
    assert p.orcids == ("0000-0001-8760-3223",)


def test_person_orcid_from_own_author_slots():
    p = profile("m/StanMatwin")
    assert p.orcids == ("0000-0001-6629-8434",)
    edited = record(p, "series/sci/2016-605")
    assert edited.kind is Kind.PROCEEDINGS
    assert edited.authors == ()
    assert (edited.editors[0].family, edited.editors[0].orcid) == ("Matwin", "0000-0001-6629-8434")


def test_dataset_and_thesis_entries():
    p = profile("00/3487")
    data = record(p, "data/12/ZakeriBGL26")
    assert data.kind is Kind.DATASET
    assert data.relations == {(Rel.VERSION_OF, "doi:10.5281/zenodo.18868296")}
    thesis = record(p, "phd/de/Gagie2009")
    assert thesis.kind is Kind.PHDTHESIS
    assert thesis.school == "Bielefeld University"
    assert thesis.url == "http://bieson.ub.uni-bielefeld.de/volltexte/2009/1545/index.html"


def test_request_carries_app_parameter_and_user_agent():
    http, seen, _ = serving(httpx.Response(200, headers=XML, content=synthetic("empty_person.xml")))
    res = dblp.person(http, "00/0002")
    assert res.fetch is Fetch.OK
    assert res.value == dblp.DblpPerson(("Ana Silva",), (), ())
    assert str(seen[0].url) == "https://dblp.org/pid/00/0002.xml?app=citeforge"
    assert seen[0].headers["user-agent"] == USER_AGENT


def test_edge_cases():
    p = dblp.parse_person(synthetic("edge_cases.xml"))
    assert p.names == ("Wei Ye", "Wéi Yê")
    assert p.orcids == ("0000-0002-1825-0097",)
    keys = sorted(i for c in p.claims for i in c.ids if i.startswith("dblp:"))
    assert keys == [
        "dblp:data/10/Ye22",
        "dblp:journals/corr/abs-cs-0601001",
        "dblp:journals/x/YeK23",
        "dblp:journals/x/YeNoYear",
        "dblp:phd/x/Ye2021",
        "dblp:series/x/2020-1",
    ]
    old = record(p, "journals/corr/abs-cs-0601001")
    assert "arxiv:cs/0601001" in old.ids
    assert (old.kind, old.status) == (Kind.PREPRINT, Status.WITHDRAWN)
    article = record(p, "journals/x/YeK23")
    assert article.ids == {"dblp:journals/x/YeK23", "doi:10.1109/x.2023.0042"}
    assert article.title == "Cooling in situ with H2O and CO2"
    assert article.authors[0] == Author("Wei", "Ye", orcid="0000-0002-1825-0097")
    assert article.authors[1] == Author("", "", literal="Kiran K")
    assert (article.volume, article.number, article.container, article.url) == ("7", "2", ("J. X",), None)
    thesis = record(p, "phd/x/Ye2021")
    assert thesis.title == "Why Do Ships Drift?"
    assert thesis.url == "https://repository.example.org/handle/1/2"
    assert thesis.school == "Example University"
    book = record(p, "series/x/2020-1")
    assert (book.kind, book.publisher) == (Kind.PROCEEDINGS, "Springer")
    software = record(p, "data/10/Ye22")
    assert software.kind is Kind.SOFTWARE
    assert software.relations == {(Rel.VERSION_OF, "doi:10.5281/zenodo.199")}
    undated = record(p, "journals/x/YeNoYear")
    assert (claim(p, "journals/x/YeNoYear").year, undated.year, undated.dates) == (None, None, ())
    assert "arxiv:2310.18948" in undated.ids


@pytest.mark.parametrize(
    ("status", "content_type", "body", "reason"),
    [
        (202, "text/html", synthetic("anubis.html"), "challenge"),
        (200, "text/html; charset=utf-8", synthetic("anubis.html"), "challenge"),
        (200, "application/xml", synthetic("truncated.xml"), "parse_error"),
        (200, "application/xml", synthetic("wrong_root.xml"), "parse_error"),
        (200, "application/xml", synthetic("undefined_entity.xml"), "parse_error"),
        (200, "application/xml", synthetic("laughs.xml"), "parse_error"),
        (200, "application/xml", b"", "parse_error"),
        (200, "application/json", b"{}", "parse_error"),
    ],
)
def test_bad_replies_are_failed_never_empty(status: int, content_type: str, body: bytes, reason: str):
    http, _, _ = serving(httpx.Response(status, headers={"content-type": content_type}, content=body))
    assert dblp.person(http, "00/0000") == dblp.Result(Fetch.FAILED, None, reason)


def test_oversized_body_is_a_parse_error(monkeypatch: pytest.MonkeyPatch):
    body = synthetic("empty_person.xml")
    monkeypatch.setattr(dblp, "MAX_BODY_BYTES", len(body) - 1)
    with pytest.raises(ParseError, match="exceeds"):
        dblp.parse_person(body)
    http, _, _ = serving(httpx.Response(200, headers=XML, content=body))
    assert dblp.person(http, "00/0002").reason == "parse_error"


def test_unknown_pid_is_not_found():
    http, _, _ = serving(httpx.Response(404, headers={"content-type": "text/html"}, content=b"<html></html>"))
    assert dblp.person(http, "00/0000") == dblp.Result(Fetch.NOT_FOUND, None, "not_found")


def test_server_error_is_retried():
    http, seen, clock = serving(
        httpx.Response(503, headers={"content-type": "text/plain"}),
        httpx.Response(200, headers=XML, content=synthetic("empty_person.xml")),
    )
    assert dblp.person(http, "00/0002").fetch is Fetch.OK
    assert len(seen) == 2 and clock.sleeps


def test_long_retry_after_marks_host_down_without_sleeping():
    http, seen, clock = serving(httpx.Response(429, headers={"retry-after": "6626"}))
    assert dblp.person(http, "00/0000") == dblp.Result(Fetch.FAILED, None, "http_error")
    assert dblp.person(http, "00/0001") == dblp.Result(Fetch.FAILED, None, "down")
    assert len(seen) == 1 and clock.sleeps == []


def test_changed_pid_redirect_is_followed():
    http, seen, _ = serving(
        httpx.Response(301, headers={"location": "/pid/00/0002.xml"}),
        httpx.Response(200, headers=XML, content=synthetic("empty_person.xml")),
    )
    assert dblp.person(http, "00/0001").fetch is Fetch.OK
    assert str(seen[1].url) == "https://dblp.org/pid/00/0002.xml?app=citeforge"


def test_redirect_off_allowlist_is_blocked():
    http, seen, _ = serving(httpx.Response(302, headers={"location": "https://example.org/pid/00/0002.xml"}))
    assert dblp.person(http, "00/0001") == dblp.Result(Fetch.FAILED, None, "redirect_blocked")
    assert len(seen) == 1


def test_redirect_loop_stops():
    loop = [httpx.Response(301, headers={"location": "/pid/00/0001.xml"}) for _ in range(dblp.MAX_REDIRECTS + 1)]
    http, seen, _ = serving(*loop)
    assert dblp.person(http, "00/0001") == dblp.Result(Fetch.FAILED, None, "redirect")
    assert len(seen) == dblp.MAX_REDIRECTS + 1


@given(st.binary(max_size=512))
def test_parse_raises_only_parse_error(body: bytes):
    with contextlib.suppress(ParseError):
        dblp.parse_person(body)


@given(st.text(max_size=200))
def test_parse_survives_arbitrary_titles_and_names(text: str):
    clean = "".join(ch for ch in text if ch.isprintable())
    body = (
        f'<dblpperson name="{escape(clean, {chr(34): "&quot;"})}" pid="1/1" n="1"><r>'
        f'<article key="journals/x/A1"><author pid="1/1">{escape(clean)}</author>'
        f"<title>{escape(clean)}</title><year>2024</year></article></r></dblpperson>"
    ).encode()
    p = dblp.parse_person(body)
    for c in p.claims:
        assert c.title and c.record is not None and c.record.authors
        assert all(a.family or a.literal for a in c.record.authors)
