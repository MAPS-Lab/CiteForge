import pytest

from citeforge import discover as disc
from citeforge import enrich as enr
from citeforge import resolve as res
from citeforge.http import Batch, Result
from citeforge.model import Claim, Fetch, Source
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import Affiliation, OrcidRecord
from citeforge.text import name_matches
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
    monkeypatch.setattr(res.openalex, "author", lambda http, a: answers[a])  # A1000001 would raise KeyError
    m = member(orcids=(ME,), openalex=("A1000001", *answers))
    ident, orcid_result, person, lookup, events = res.resolve(m, None, frozenset({LAB}))  # type: ignore[arg-type]
    assert lookup is Fetch.OK
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
    ident, _, _, lookup, events = res.resolve(member(openalex=("A3000003",)), None, frozenset())  # type: ignore[arg-type]
    assert lookup is None
    assert ident.openalex == {"A3000003"} and not events


def test_resolve_without_orcid_or_dblp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res.dblp, "person", lambda http, p: Result(Fetch.FAILED, None, "challenge"))
    ident, orcid_result, person, _, _ = res.resolve(member(), None, frozenset())  # type: ignore[arg-type]
    assert orcid_result is None and person is not None and person.fetch is Fetch.FAILED
    assert ident.names and ident.openalex == frozenset()


def test_an_orcid_other_name_on_another_surname_is_dropped() -> None:
    rec = OrcidRecord(
        "Suresh", "Neethirajan", None, ("Suresh Raja", "Suresh Raja Neethirajan", "Neethirajan, S"), (), ()
    )
    names = res._names(member(name="Suresh Neethirajan"), rec, None)
    assert all("neethirajan" in k.families for k in names)
    assert not name_matches(names, author("Sanjay Raja")) and not name_matches(names, author("S. Raja"))
    assert name_matches(names, author("Suresh Raja Neethirajan"))


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
    monkeypatch.setattr(disc.openalex, "list_works", lambda http, ids, since, known=frozenset(): Result(Fetch.OK, ()))
    orcid = Result(Fetch.OK, orcid_rec(claims(6, Source.ORCID) + claims(1, Source.ORCID, 2010)))
    dblp = Result(Fetch.OK, DblpPerson((), (), claims(2, Source.DBLP)))
    listing = disc.discover(identity(), orcid, dblp, None, None, 2018)  # type: ignore[arg-type]
    assert len(listing.claims) == 8
    assert listing.fetch == {Source.ORCID: Fetch.OK, Source.DBLP: Fetch.OK, Source.OPENALEX: Fetch.FAILED}
    empty_orcid = Result(Fetch.OK, orcid_rec())
    old_dblp = Result(Fetch.OK, DblpPerson((), (), claims(6, Source.DBLP, 2010)))
    listing = disc.discover(identity(openalex=frozenset()), empty_orcid, old_dblp, None, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch == {Source.ORCID: Fetch.OK, Source.DBLP: Fetch.OK} and listing.claims == ()
    challenge = Result(Fetch.FAILED, None, "challenge")
    listing = disc.discover(identity(openalex=frozenset()), None, challenge, None, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch == {Source.DBLP: Fetch.FAILED} and listing.claims == ()


def test_failed_openalex_lookup_marks_the_listing_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(res.orcid, "record", lambda http, o: Result(Fetch.OK, orcid_rec()))
    monkeypatch.setattr(res.dblp, "person", lambda http, p: Result(Fetch.OK, DblpPerson((), (), ())))
    monkeypatch.setattr(res.openalex, "authors_by_orcid", lambda http, o: Result(Fetch.FAILED, None, "down"))
    ident, orcid_result, person, lookup, _ = res.resolve(member(orcids=(ME,)), None, frozenset())  # type: ignore[arg-type]
    assert ident.openalex == frozenset() and lookup is Fetch.FAILED
    listing = disc.discover(ident, orcid_result, person, lookup, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch[Source.OPENALEX] is Fetch.FAILED
    monkeypatch.setattr(
        disc.openalex,
        "list_works",
        lambda *args: Result(Fetch.OK, claims(1, Source.OPENALEX)),
    )
    listing = disc.discover(identity(), orcid_result, person, Fetch.FAILED, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch[Source.OPENALEX] is Fetch.FAILED and len(listing.claims) == 1


def test_discover_window_compares_listings_over_the_same_years(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(disc.openalex, "list_works", lambda http, ids, since, known=frozenset(): Result(Fetch.OK, ()))
    old = Result(Fetch.OK, DblpPerson((), (), claims(6, Source.DBLP, 2010)))
    listing = disc.discover(identity(), None, old, Fetch.OK, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch == {Source.DBLP: Fetch.OK, Source.OPENALEX: Fetch.OK} and listing.claims == ()
    recent = Result(Fetch.OK, DblpPerson((), (), claims(5, Source.DBLP, 2019)))
    listing = disc.discover(identity(), None, recent, Fetch.OK, None, 2018)  # type: ignore[arg-type]
    assert listing.fetch[Source.OPENALEX] is Fetch.FAILED


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
            [
                "doi:10.1234/dc",
                "doi:10.1234/me",
                "doi:10.1234/dead",
                "doi:10.1234/ra",
                "doi:10.5072/t",
                "doi:10.1234/lag",
            ],
            ["doi:10.1234/down"],
        ),
    )
    monkeypatch.setattr(
        enr.datacite,
        "by_doi",
        batch(
            "datacite",
            ["doi:10.1234/dc"],
            [
                "doi:10.1234/me",
                "doi:10.1234/dead",
                "doi:10.1234/down",
                "doi:10.1234/ra",
                "doi:10.5072/t",
                "doi:10.1234/lag",
            ],
        ),
    )
    agencies = {
        "doi:10.1234/me": "mEDRA",
        "doi:10.1234/dead": None,
        "doi:10.1234/ra": "KISTI",
        "doi:10.5072/t": "DataCite",
        "doi:10.1234/lag": "Crossref",
    }
    monkeypatch.setattr(enr.doiorg, "agency", lambda http, ids: Result(Fetch.OK, agencies))
    monkeypatch.setattr(
        enr.doiorg,
        "csl",
        lambda http, i: (
            Result(Fetch.FAILED, None, "redirect_blocked")
            if i == "doi:10.1234/ra"
            else Result(Fetch.OK, record(i, source=Source.CSL))
        ),
    )
    monkeypatch.setattr(enr.arxiv, "by_id", batch("arxiv", ["arxiv:2101.00001"], ["arxiv:2101.99999"]))
    monkeypatch.setattr(enr.openalex, "works_by_doi", batch("openalex", ["doi:10.1234/dc"]))
    ids = [
        "doi:10.1234/cr",
        "doi:10.1234/dc",
        "doi:10.1234/me",
        "doi:10.1234/dead",
        "doi:10.1234/down",
        "doi:10.1234/ra",
        "doi:10.5072/t",
        "doi:10.1234/lag",
        "arxiv:2101.00001",
        "arxiv:2101.99999",
        "doi:10.1234/cr",
    ]
    have = {"doi:10.1234/cr": record("doi:10.1234/cr", source=Source.OPENALEX)}
    r = enr.fetch(ids, None, have)  # type: ignore[arg-type]
    assert set(r.found) == {"doi:10.1234/cr", "doi:10.1234/dc", "doi:10.1234/me", "arxiv:2101.00001"}
    assert r.found["doi:10.1234/me"].source is Source.CSL
    assert r.dead == {"doi:10.1234/dead", "arxiv:2101.99999"}
    # DataCite's API omits a DOI that is not findable, so its absence is final; Crossref's only lags
    assert r.failed == {"doi:10.1234/down", "doi:10.1234/lag"}
    assert r.unlisted == {"doi:10.5072/t"}
    assert "doi:10.1234/cr" not in calls["openalex"] and "doi:10.1234/dead" not in calls["openalex"]
    assert set(r.openalex) == {"doi:10.1234/cr", "doi:10.1234/dc"}
