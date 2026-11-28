import pytest

from citeforge import discover as disc
from citeforge import enrich as enr
from citeforge import resolve as res
from citeforge.http import Batch, Result
from citeforge.model import Claim, Fetch, Source
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import Affiliation, OrcidRecord
from tests.factories import author, member, record, work
from tests.unit.test_authorship import identity

ME = "0000-0002-1825-0097"
LAB = "https://ror.org/01e6qks80"


def orcid_rec(works=()) -> OrcidRecord:
    return OrcidRecord(
        "Gabriel",
        "Spadon",
        "G. Spadon",
        ("Gabriel Spadon de Souza",),
        (Affiliation("https://ror.org/036rp1748", "Universidade de São Paulo", "education"),),
        works,
    )


def test_resolve_merges_identity_and_checks_pinned_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res.orcid, "record", lambda http, o: Result(Fetch.OK, orcid_rec()))
    monkeypatch.setattr(
        res.dblp, "person", lambda http, p: Result(Fetch.OK, DblpPerson(("Gabriel Spadon 0001",), (ME,), ()))
    )
    monkeypatch.setattr(res.openalex, "authors_by_orcid", lambda http, o: Result(Fetch.OK, {ME: ("A1000001",)}))
    answers = {
        "A2000002": Result(Fetch.OK, ("A2000009", None)),
        "A3000003": Result(Fetch.OK, ("A3000003", "0000-0001-5109-3700")),
        "A4000004": Result(Fetch.NOT_FOUND, None, "not_found"),
        "A5000005": Result(Fetch.FAILED, None, "timeout"),
    }
    monkeypatch.setattr(res.openalex, "author", lambda http, a: answers[a])
    m = member(orcids=(ME,), openalex=tuple(answers))
    ident, orcid_result, person, events = res.resolve(m, None, frozenset({LAB}))  # type: ignore[arg-type]
    assert ident.openalex == {"A1000001", "A2000009", "A5000005"}
    assert [e.kind for e in events] == ["openalex_id_merged", "openalex_orcid_conflict", "openalex_id_deleted"]
    assert ident.rors == {LAB, "https://ror.org/036rp1748"}
    assert "universidade de sao paulo" in ident.inst_names
    assert any("spadon" in k.families for k in ident.names)
    assert orcid_result is not None and orcid_result.fetch is Fetch.OK
    assert person is not None and person.fetch is Fetch.OK


def test_pinned_id_kept_when_member_lists_no_orcid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res.dblp, "person", lambda http, p: Result(Fetch.OK, DblpPerson(("Gabriel Spadon",), (), ())))
    monkeypatch.setattr(res.openalex, "author", lambda http, a: Result(Fetch.OK, ("A3000003", ME)))
    ident, _, _, events = res.resolve(member(openalex=("A3000003",)), None, frozenset())  # type: ignore[arg-type]
    assert ident.openalex == {"A3000003"} and not events


def test_resolve_without_orcid_or_dblp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res.dblp, "person", lambda http, p: Result(Fetch.FAILED, None, "challenge"))
    ident, orcid_result, person, _ = res.resolve(member(), None, frozenset())  # type: ignore[arg-type]
    assert orcid_result is None and person is not None and person.fetch is Fetch.FAILED
    assert ident.names and ident.openalex == frozenset()


def test_learn_institutions_needs_three_strong_works() -> None:
    new = "https://ror.org/03dbr7087"
    me = author("Gabriel Spadon", orcid=ME, rors=(new,))
    works = [work(record(f"doi:10.1234/{n}", authors=(me,))) for n in range(3)]
    i = identity(rors=frozenset())
    assert new not in res.learn_institutions(i, [(w, "orcid_claim") for w in works]).rors
    assert new not in res.learn_institutions(i, [(w, "authorship_orcid") for w in works[:2]]).rors
    assert new in res.learn_institutions(i, [(w, "authorship_orcid") for w in works]).rors


def claims(n: int, source: Source, year: int = 2023) -> tuple[Claim, ...]:
    return tuple(Claim(source, frozenset({f"doi:10.1234/{source}{k}"}), "t", year) for k in range(n))


def test_discover_filters_years_and_suspects_empty_listing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disc.openalex, "list_works", lambda http, ids, since: Result(Fetch.OK, ()))
    orcid = Result(Fetch.OK, orcid_rec(claims(6, Source.ORCID) + claims(1, Source.ORCID, 2010)))
    dblp = Result(Fetch.OK, DblpPerson((), (), claims(2, Source.DBLP)))
    listing = disc.discover(identity(), orcid, dblp, None, 2018)  # type: ignore[arg-type]
    assert len(listing.claims) == 8
    assert listing.fetch == {Source.ORCID: Fetch.OK, Source.DBLP: Fetch.OK, Source.OPENALEX: Fetch.FAILED}
    empty_orcid = Result(Fetch.OK, orcid_rec())
    old_dblp = Result(Fetch.OK, DblpPerson((), (), claims(6, Source.DBLP, 2010)))
    listing = disc.discover(identity(openalex=frozenset()), empty_orcid, old_dblp, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch == {Source.ORCID: Fetch.OK, Source.DBLP: Fetch.OK} and listing.claims == ()
    listing = disc.discover(identity(openalex=frozenset()), None, Result(Fetch.FAILED, None, "challenge"), None, 2018)  # type: ignore[arg-type]
    assert listing.fetch == {Source.DBLP: Fetch.FAILED} and listing.claims == ()


def test_enrich_routes_misses(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, list] = {}

    def batch(name, found=(), missing=(), failed=()):
        def fn(http, ids):
            calls[name] = list(ids)
            return Batch(
                {i: record(i, source=Source.CROSSREF) for i in found if i in ids},
                frozenset(i for i in missing if i in ids),
                frozenset(i for i in failed if i in ids),
            )

        return fn

    monkeypatch.setattr(
        enr.crossref,
        "by_doi",
        batch(
            "crossref",
            ["doi:10.1234/cr"],
            ["doi:10.1234/dc", "doi:10.1234/me", "doi:10.1234/dead"],
            ["doi:10.1234/down"],
        ),
    )
    monkeypatch.setattr(
        enr.datacite,
        "by_doi",
        batch("datacite", ["doi:10.1234/dc"], ["doi:10.1234/me", "doi:10.1234/dead", "doi:10.1234/down"]),
    )
    monkeypatch.setattr(
        enr.doiorg, "agency", lambda http, ids: Result(Fetch.OK, {"doi:10.1234/me": "mEDRA", "doi:10.1234/dead": None})
    )
    monkeypatch.setattr(enr.doiorg, "csl", lambda http, i: Result(Fetch.OK, record(i, source=Source.CSL)))
    monkeypatch.setattr(enr.arxiv, "by_id", batch("arxiv", ["arxiv:2101.00001"], ["arxiv:2101.99999"]))
    monkeypatch.setattr(enr.openalex, "works_by_doi", batch("openalex", ["doi:10.1234/dc"]))
    ids = [
        "doi:10.1234/cr",
        "doi:10.1234/dc",
        "doi:10.1234/me",
        "doi:10.1234/dead",
        "doi:10.1234/down",
        "arxiv:2101.00001",
        "arxiv:2101.99999",
        "doi:10.1234/cr",
    ]
    have = {"doi:10.1234/cr": record("doi:10.1234/cr", source=Source.OPENALEX)}
    r = enr.fetch(ids, None, have)  # type: ignore[arg-type]
    assert set(r.found) == {"doi:10.1234/cr", "doi:10.1234/dc", "doi:10.1234/me", "arxiv:2101.00001"}
    assert r.found["doi:10.1234/me"].source is Source.CSL
    assert r.dead == {"doi:10.1234/dead", "arxiv:2101.99999"}
    assert r.failed == {"doi:10.1234/down"}
    assert "doi:10.1234/cr" not in calls["openalex"] and "doi:10.1234/dead" not in calls["openalex"]
    assert set(r.openalex) == {"doi:10.1234/cr", "doi:10.1234/dc"}
