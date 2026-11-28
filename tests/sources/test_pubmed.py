import httpx
import pytest

from citeforge.http import ParseError
from citeforge.model import Claim, Fetch, Source
from citeforge.run import _with_pubmed_dois
from citeforge.sources import pubmed
from tests.conftest import FakeClock, json_response, make_http


def summary(*rows: tuple[str, list[dict]]) -> dict:
    result: dict = {"uids": [uid for uid, _ in rows]}
    for uid, articleids in rows:
        result[uid] = {"uid": uid, "articleids": articleids} if articleids else {"uid": uid, "error": "cannot get"}
    return {"result": result}


RLBWT = (
    "39157646",
    [{"idtype": "pubmed", "value": "39157646"}, {"idtype": "doi", "value": "10.4230/LIPIcs.SEA.2022.16"}],
)


def test_dois_maps_each_pmid_and_skips_missing_or_doi_less(clock: FakeClock) -> None:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return json_response(summary(RLBWT, ("999999999", []), ("7", [{"idtype": "pii", "value": "16"}])))

    res = pubmed.dois(make_http(answer, clock), ["pmid:39157646", "pmid:999999999", "pmid:7", "doi:10.1/x"])
    assert res.fetch is Fetch.OK
    assert res.value == {"pmid:39157646": "doi:10.4230/lipics.sea.2022.16"}
    (request,) = seen
    assert request.url.params["id"] == "39157646,7,999999999"
    assert request.url.params["tool"] == "citeforge" and "email" not in request.url.params


def test_dois_chunks_and_keeps_good_chunks_on_failure(clock: FakeClock) -> None:
    replies = [json_response(summary(RLBWT)), httpx.Response(500)]
    ids = ["pmid:39157646", *(f"pmid:{n}" for n in range(1, pubmed.CHUNK + 1))]
    res = pubmed.dois(make_http(lambda r: replies.pop(0) if len(replies) > 1 else httpx.Response(500), clock), ids)
    assert res.fetch is Fetch.FAILED
    assert res.value == {"pmid:39157646": "doi:10.4230/lipics.sea.2022.16"}


def test_two_dois_in_one_summary_are_ambiguous() -> None:
    body = summary(("5", [{"idtype": "doi", "value": "10.1/a"}, {"idtype": "doi", "value": "10.1/b"}]))
    assert pubmed.parse_summaries(json_response(body).content) == {}


@pytest.mark.parametrize("body", [b"<html>", b"[]", b'{"result": {}}', b'{"result": {"uids": "x"}}'])
def test_malformed_body_is_parse_error(body: bytes) -> None:
    with pytest.raises(ParseError):
        pubmed.parse_summaries(body)


def test_doi_less_claims_gain_the_pubmed_doi(clock: FakeClock) -> None:
    http = make_http(lambda r: json_response(summary(RLBWT)), clock)
    bare = Claim(Source.OPENALEX, frozenset({"pmid:39157646", "openalex:W4401713284"}), "RLBWT Tricks", 2022)
    with_doi = Claim(Source.ORCID, frozenset({"pmid:39157646", "doi:10.1109/dcc52660.2022.00055"}), "RLBWT", 2022)
    out, failed = _with_pubmed_dois([("A", bare), ("B", with_doi)], http)
    assert out[0][1].ids == bare.ids | {"doi:10.4230/lipics.sea.2022.16"}
    assert out[1] == ("B", with_doi)
    assert failed == frozenset()
    assert _with_pubmed_dois([("B", with_doi)], make_http(lambda r: pytest.fail("no request"), clock)) == (
        [("B", with_doi)],
        frozenset(),
    )
    down = make_http(lambda r: httpx.Response(500), clock)
    assert _with_pubmed_dois([("A", bare)], down) == ([("A", bare)], frozenset({"pmid:39157646"}))
