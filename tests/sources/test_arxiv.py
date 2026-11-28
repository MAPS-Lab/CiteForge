import io

import httpx
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from citeforge.http import Http, ParseError
from citeforge.model import Author, Kind, Rel, Source, Status
from citeforge.sources import arxiv
from tests.conftest import FIXTURES, FakeClock, fixture_transport, make_http

SYNTHETIC = FIXTURES / "synthetic" / "arxiv"
ATOM = {"content-type": "application/atom+xml; charset=utf-8"}
RECORDED = (
    "arxiv:2310.18948",
    "arxiv:1909.04605",
    "arxiv:math.DG/0303109",
    "arxiv:hep-th/9901001",
    "arxiv:0910.4008",
    "arxiv:2310.99999",
    "arxiv:1207.7214",
    "arxiv:1706.03762",
)


def synthetic(name: str) -> bytes:
    return (SYNTHETIC / name).read_bytes()


def atom(body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers={**ATOM, **(headers or {})}, content=body)


def feed(*ids: str) -> bytes:
    entries = "".join(
        f"<entry><id>http://arxiv.org/abs/{i}v2</id><title>Paper {i}</title>"
        f"<published>2021-03-04T00:00:00Z</published><updated>2021-03-05T00:00:00Z</updated>"
        f"<author><name>Ana Lima</name></author></entry>"
        for i in ids
    )
    return (
        '<feed xmlns="http://www.w3.org/2005/Atom" xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{len(ids)}</opensearch:totalResults>{entries}</feed>"
    ).encode()


def echo(request: httpx.Request) -> httpx.Response:
    """A well-behaved API: every listed id exists."""
    return atom(feed(*request.url.params["id_list"].split(",")))


def serving(*responses: httpx.Response) -> tuple[Http, FakeClock, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    queue = list(responses)

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return queue.pop(0) if len(queue) > 1 else queue[0]

    clock = FakeClock()
    return make_http(handle, clock), clock, seen


def test_recorded_batch_maps_every_id() -> None:
    clock = FakeClock()
    http = Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)
    batch = arxiv.by_id(http, RECORDED)
    assert batch.missing == {"arxiv:2310.99999"} and not batch.failed
    assert set(batch.found) == set(RECORDED) - batch.missing
    assert http.stats()["export.arxiv.org"]["requests"] == 1

    own = batch.found["arxiv:2310.18948"]
    assert (own.source, own.kind, own.source_type, own.howpublished) == (Source.ARXIV, Kind.PREPRINT, "arxiv", "arXiv")
    assert own.title.startswith("Multi-Path Long-Term Vessel Trajectories Forecasting")
    assert (own.year, own.dates, own.primary_class) == (2023, (2023, 2024), "cs.LG")
    assert own.authors[0] == Author("Gabriel", "Spadon") and len(own.authors) == 9
    assert own.relations == {(Rel.PUBLISHED_AS, "doi:10.1016/j.oceaneng.2024.119138")}
    assert own.url == "https://arxiv.org/abs/2310.18948" and own.status is Status.OK

    assert batch.found["arxiv:0910.4008"].status is Status.WITHDRAWN
    assert batch.found["arxiv:1207.7214"].authors == (Author("", "", literal="The ATLAS Collaboration"),)
    assert batch.found["arxiv:hep-th/9901001"].relations == {(Rel.PUBLISHED_AS, "doi:10.1143/ptp.101.1155")}
    assert batch.found["arxiv:1706.03762"].relations == frozenset()


def test_old_style_subject_class_is_queried_without_it_and_keeps_both_ids() -> None:
    clock = FakeClock()
    http = Http(io.StringIO(), {}, transport=fixture_transport(), clock=clock, sleep=clock.sleep)
    perelman = arxiv.by_id(http, RECORDED).found["arxiv:math.DG/0303109"]
    assert perelman.ids == {"arxiv:math.DG/0303109", "arxiv:math/0303109"}
    assert perelman.url == "https://arxiv.org/abs/math/0303109"
    assert arxiv.query_form("arxiv:math.DG/0303109") == "math/0303109"
    assert arxiv.query_form("arxiv:2310.18948") == "2310.18948"


def test_two_ids_for_one_paper_share_one_query_and_one_record() -> None:
    http, _, seen = serving(atom(feed("math/0303109")))
    batch = arxiv.by_id(http, ["arxiv:math/0303109", "arxiv:math.DG/0303109", "arxiv:math/0303109"])
    assert len(seen) == 1 and seen[0].url.params["id_list"] == "math/0303109"
    assert batch.found["arxiv:math/0303109"] is batch.found["arxiv:math.DG/0303109"]


def test_chunks_of_100_sorted_and_spaced() -> None:
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return echo(request)

    clock = FakeClock()
    ids = [f"arxiv:2101.{n:05d}" for n in range(250, 0, -1)]
    batch = arxiv.by_id(make_http(handle, clock), ids)
    assert set(batch.found) == set(ids) and not batch.missing and not batch.failed
    lists = [r.url.params["id_list"].split(",") for r in seen]
    assert [len(x) for x in lists] == [100, 100, 50]
    assert [i for x in lists for i in x] == sorted(i.removeprefix("arxiv:") for i in ids)
    assert all(r.url.path == "/api/query" and r.url.params["max_results"] == "100" for r in seen)
    assert clock.sleeps == [3.0, 3.0]


def test_empty_input_and_malformed_ids_send_nothing() -> None:
    http, _, seen = serving(atom(feed()))
    assert arxiv.by_id(http, []) == arxiv.Batch({}, frozenset(), frozenset())
    batch = arxiv.by_id(http, ["arxiv:not-an-id", "doi:10.1/x", "arxiv:2101.00001v2"])
    assert batch.missing == {"arxiv:not-an-id", "doi:10.1/x", "arxiv:2101.00001v2"} and not seen


def test_entry_shapes_and_per_entry_failures() -> None:
    ids = [f"arxiv:2101.0000{n}" for n in range(1, 6)]
    http, _, _ = serving(atom(synthetic("shapes.xml")))
    batch = arxiv.by_id(http, ids)
    assert set(batch.found) == {"arxiv:2101.00001", "arxiv:2101.00002", "arxiv:2101.00004"}
    assert batch.failed == {"arxiv:2101.00003", "arxiv:2101.00005"}
    assert batch.found["arxiv:2101.00002"].authors == (Author("", "", literal="Wei Z."),)
    assert not batch.missing

    rich = batch.found["arxiv:2101.00001"]
    assert rich.title == "Learning $O(n)$ the AIS Graph Model"
    assert (rich.year, rich.dates) == (2021, (2021, 2022))
    assert rich.authors == (
        Author("Ana", "Lima", affiliations=("Dalhousie University",)),
        Author("João", "Souza"),
        Author("", "", literal="LIGO Scientific Collaboration"),
    )
    assert rich.relations == {(Rel.PUBLISHED_AS, "doi:10.1234/example.2022")}
    assert rich.status is Status.OK

    pulled = batch.found["arxiv:2101.00004"]
    assert pulled.status is Status.WITHDRAWN and pulled.primary_class is None
    assert pulled.relations == frozenset() and pulled.year == 2020


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("error_entry.xml", "incorrect id format"),
        ("wrong_root.xml", "unexpected root"),
        ("truncated.xml", "arxiv:"),
        ("total_mismatch.xml", "totalResults"),
    ],
)
def test_parse_feed_rejects_broken_payloads(name: str, message: str) -> None:
    with pytest.raises(ParseError, match=message):
        arxiv.parse_feed(synthetic(name))


@pytest.mark.parametrize("name", ["error_entry.xml", "wrong_root.xml", "truncated.xml", "total_mismatch.xml"])
def test_broken_payload_fails_the_chunk_and_marks_nothing_missing(name: str) -> None:
    http, _, _ = serving(atom(synthetic(name)))
    batch = arxiv.by_id(http, ["arxiv:2101.00001", "arxiv:2101.00002"])
    assert batch.failed == {"arxiv:2101.00001", "arxiv:2101.00002"} and not batch.found and not batch.missing


def test_empty_feed_for_several_ids_is_a_failure_not_a_verdict() -> None:
    http, _, _ = serving(atom(synthetic("empty.xml")))
    batch = arxiv.by_id(http, ["arxiv:2101.00001", "arxiv:2101.00002"])
    assert batch.failed == {"arxiv:2101.00001", "arxiv:2101.00002"} and not batch.missing


def test_empty_feed_for_one_id_means_it_does_not_exist() -> None:
    http, _, _ = serving(atom(synthetic("empty.xml")))
    assert arxiv.by_id(http, ["arxiv:2310.99999"]).missing == {"arxiv:2310.99999"}


def test_unrequested_and_repeated_entries_are_ignored() -> None:
    http, _, _ = serving(atom(feed("2101.00001", "2101.00001", "2101.09999")))
    batch = arxiv.by_id(http, ["arxiv:2101.00001", "arxiv:2101.00002", "arxiv:2101.00003"])
    assert set(batch.found) == {"arxiv:2101.00001"}
    assert batch.missing == {"arxiv:2101.00002", "arxiv:2101.00003"}


def test_server_error_then_success_is_retried() -> None:
    http, clock, seen = serving(atom(b"", 503), atom(feed("2101.00001")))
    batch = arxiv.by_id(http, ["arxiv:2101.00001"])
    assert set(batch.found) == {"arxiv:2101.00001"} and len(seen) == 2 and len(clock.sleeps) == 1


def test_long_retry_after_marks_the_host_down_without_sleeping() -> None:
    http, clock, seen = serving(atom(b"", 429, {"retry-after": "6626"}))
    ids = [f"arxiv:2101.{n:05d}" for n in range(1, 151)]
    batch = arxiv.by_id(http, ids)
    assert batch.failed == set(ids) and not batch.found and not batch.missing
    assert len(seen) == 1 and clock.sleeps == []
    assert http.down(arxiv.HOST) == "retry_after 6626s"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(202, headers={"content-type": "text/html"}, content=b"<html>checking your browser</html>"),
        httpx.Response(200, headers={"content-type": "text/html"}, content=b"<!doctype html><p>challenge</p>"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{}"),
        atom(synthetic("error_entry.xml"), 400),
        atom(b"", 404),
    ],
)
def test_non_feed_replies_fail_the_chunk(response: httpx.Response) -> None:
    http, _, _ = serving(response)
    batch = arxiv.by_id(http, ["arxiv:2101.00001"])
    assert batch.failed == {"arxiv:2101.00001"} and not batch.missing


def test_timeouts_exhaust_retries_and_fail() -> None:
    calls = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    batch = arxiv.by_id(make_http(handle), ["arxiv:2101.00001"])
    assert batch.failed == {"arxiv:2101.00001"} and calls == 4


def test_a_down_host_sends_no_request() -> None:
    http, _, seen = serving(atom(b"", 503))
    for _ in range(5):
        arxiv.by_id(http, ["arxiv:2101.00001"])
    sent = len(seen)
    assert http.down(arxiv.HOST) == "circuit"
    assert arxiv.by_id(http, ["arxiv:2101.00002"]).failed == {"arxiv:2101.00002"} and len(seen) == sent


_ARXIV_IDS = st.builds(lambda a, b: f"arxiv:{2000 + a}.{b:05d}", st.integers(1, 99), st.integers(1, 99999))
_REPLIES = st.sampled_from(["ok", "partial", "empty", "error", "garbage", "503"])


@settings(max_examples=60, deadline=None)
@given(ids=st.lists(_ARXIV_IDS, max_size=230), replies=st.lists(_REPLIES, min_size=1, max_size=4))
def test_every_id_lands_in_exactly_one_bucket(ids: list[str], replies: list[str]) -> None:
    queue = list(replies)

    def handle(request: httpx.Request) -> httpx.Response:
        asked = request.url.params["id_list"].split(",")
        match queue.pop(0) if len(queue) > 1 else queue[0]:
            case "ok":
                return atom(feed(*asked))
            case "partial":
                return atom(feed(*asked[::2]))
            case "empty":
                return atom(feed())
            case "error":
                return atom(synthetic("error_entry.xml"))
            case "garbage":
                return atom(b"<feed")
            case _:
                return atom(b"", 503)

    batch = arxiv.by_id(make_http(handle), ids)
    buckets = [set(batch.found), set(batch.missing), set(batch.failed)]
    assert set().union(*buckets) == set(ids)
    assert sum(len(b) for b in buckets) == len(set(ids))
    assert all(r.kind is Kind.PREPRINT and work_id in r.ids for work_id, r in batch.found.items())


def test_parse_entry_needs_an_abs_id() -> None:
    node = arxiv.parse_feed(synthetic("error_entry.xml").replace(b"/api/errors#", b"/api/x#"))[0]
    with pytest.raises(ParseError, match="abs id"):
        arxiv.parse_entry(node, frozenset())
