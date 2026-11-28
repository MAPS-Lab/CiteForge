import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from citeforge import cluster as cl
from citeforge.curated import Curated
from citeforge.http import Batch, Result
from citeforge.ids import is_preprint_id, split_id
from citeforge.model import Claim, Fetch, Kind, Record, Registries, Rel, Source
from tests.factories import author, record
from tests.unit.test_authorship import identity

ME = "Spadon (bfdGsGUAAAAJ)"
TITLE = "Graph neural networks for vessel trajectory forecasting"


def claim(*ids: str, source: Source = Source.ORCID, title: str = TITLE, rec: Record | None = None) -> Claim:
    return Claim(source, frozenset(ids), title, 2023, record=rec)


def reg(*records: Record, **kw) -> Registries:
    return Registries(found={min(r.ids): r for r in records}, **kw)


def test_verify_detaches_dead_and_mismatched_dois_and_holds_reviews() -> None:
    good = record("doi:10.1234/good")
    wrong = record("doi:10.1234/wrong", title="Protein folding with diffusion")
    middle = record("doi:10.1234/mid", title="Graph networks for ship trajectory forecasts at sea")
    claims = [
        (ME, claim("doi:10.1234/good")),
        (ME, claim("doi:10.1234/wrong", "arxiv:2101.00001")),
        (ME, claim("doi:10.1234/dead")),
        (ME, claim("doi:10.1234/mid")),
    ]
    kept, held, events = cl.verify(
        claims, reg(good, wrong, middle, dead=frozenset({"doi:10.1234/dead"})), {ME: identity()}
    )
    assert [c.ids for _, c in kept] == [{"doi:10.1234/good"}, {"arxiv:2101.00001"}]
    assert [c.ids for _, c in held] == [{"doi:10.1234/mid"}]
    assert [e.kind for e in events] == ["doi_mismatch", "doi_unresolved", "doi_review"]


def test_strong_edges_and_preprint_relation() -> None:
    pub = record("doi:10.1016/j.x.2024.1", relations=frozenset({(Rel.HAS_PREPRINT, "arxiv:2310.18948")}))
    ax = record("arxiv:2310.18948", source=Source.ARXIV, kind=Kind.PREPRINT)
    groups, _ = cl.cluster([(ME, claim("arxiv:2310.18948"))], reg(pub, ax), Curated())
    (g,) = groups
    assert g.ids == {"doi:10.1016/j.x.2024.1", "arxiv:2310.18948"}
    assert g.key == "doi:10.1016/j.x.2024.1"
    assert len(g.claims) == 1


def test_guard_never_joins_two_published_dois() -> None:
    a = record("doi:10.1234/a", relations=frozenset({(Rel.VERSION_OF, "doi:10.1234/b")}))
    b = record("doi:10.1234/b")
    groups, events = cl.cluster([], reg(a, b), Curated())
    assert len(groups) == 2
    assert [e.kind for e in events] == ["link_refused"]


def test_crosswalk_needs_matching_registry_titles() -> None:
    pub = record("doi:10.1234/p")
    ax_same = record("arxiv:2101.00001", source=Source.ARXIV, kind=Kind.PREPRINT)
    oa = record("doi:10.1234/p", "arxiv:2101.00001", "openalex:W1", source=Source.OPENALEX)
    groups, _ = cl.cluster([(ME, claim("openalex:W1", source=Source.OPENALEX, rec=oa))], reg(pub, ax_same), Curated())
    assert [sorted(g.ids) for g in groups] == [["arxiv:2101.00001", "doi:10.1234/p", "openalex:W1"]]
    ax_other = dataclasses.replace(ax_same, title="Something entirely different about proteins")
    groups, events = cl.cluster(
        [(ME, claim("openalex:W1", source=Source.OPENALEX, rec=oa))], reg(pub, ax_other), Curated()
    )
    assert len(groups) == 2
    assert any(e.kind == "crosswalk_rejected" for e in events)
    assert next(g for g in groups if "openalex:W1" in g.ids).key == "doi:10.1234/p"


def test_fuzzy_link_and_proposal() -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    pub = record("doi:10.1234/p", authors=authors, year=2024)
    pre = record(
        "arxiv:2201.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors, year=2022, title=TITLE + "s"
    )
    groups, events = cl.cluster([], reg(pub, pre), Curated())
    (g,) = groups
    assert g.fuzzy and g.ids == {"doi:10.1234/p", "arxiv:2201.00001"}
    assert [e.kind for e in events] == ["fuzzy_link"]
    near = dataclasses.replace(pre, title="Graph neural nets for ship trajectory forecasting")
    groups, events = cl.cluster([], reg(pub, near), Curated())
    assert len(groups) == 2 and [e.kind for e in events] == ["link_proposal"]
    late = dataclasses.replace(pre, year=2019)
    assert len(cl.cluster([], reg(pub, late), Curated())[0]) == 2


@pytest.mark.parametrize(
    ("copy", "published"),
    [
        (
            ("arxiv:2201.00001", Source.ARXIV, Kind.PREPRINT),
            ("dblp:conf/iclr/Spadon24", Source.DBLP, Kind.INPROCEEDINGS),
        ),
        (("doi:10.5281/zenodo.1", Source.DATACITE, Kind.ARTICLE), ("doi:10.1234/p", Source.CROSSREF, Kind.ARTICLE)),
        (
            ("doi:10.2196/preprints.17813", Source.CROSSREF, Kind.PREPRINT),
            ("doi:10.2196/17813", Source.CROSSREF, Kind.ARTICLE),
        ),
    ],
)
def test_fuzzy_link_hides_copies_behind_the_published_version(copy, published) -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    a = record(copy[0], source=copy[1], kind=copy[2], authors=authors, year=2023)
    b = record(published[0], source=published[1], kind=published[2], authors=authors, year=2024)
    (g,) = cl.cluster([], reg(a, b), Curated())[0]
    assert g.fuzzy and g.ids == {copy[0], published[0]}


def test_one_published_version_absorbs_several_copies() -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    arx = record("arxiv:2201.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors, year=2023)
    ssrn = record("doi:10.2139/ssrn.1", kind=Kind.PREPRINT, authors=authors, year=2023)
    pub = record("doi:10.1234/p", authors=authors, year=2024)
    (g,) = cl.cluster([], reg(arx, ssrn, pub), Curated())[0]
    assert g.ids == {"arxiv:2201.00001", "doi:10.2139/ssrn.1", "doi:10.1234/p"}


def test_copy_with_two_published_partners_stays_apart() -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    arx = record("arxiv:2201.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors, year=2023)
    conf = record("doi:10.1234/conf", authors=authors, year=2024, kind=Kind.INPROCEEDINGS)
    journal = record("doi:10.1234/journal", authors=authors, year=2025)
    assert len(cl.cluster([], reg(arx, conf, journal), Curated())[0]) == 3


def test_dblp_preprint_is_not_a_published_version() -> None:
    authors = (author("Gabriel Spadon"),)
    a = record("arxiv:2201.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors)
    b = record("dblp:journals/corr/abs-2201-00001x", source=Source.DBLP, kind=Kind.PREPRINT, authors=authors)
    assert len(cl.cluster([], reg(a, b), Curated())[0]) == 2


def test_two_published_dois_never_joined_by_title() -> None:
    a, b = record("doi:10.1234/a", year=2021), record("doi:10.1234/b", year=2025)
    assert len(cl.cluster([], reg(a, b), Curated())[0]) == 2


def test_curated_link_and_records() -> None:
    pub = record("doi:10.1234/p")
    ax = record("arxiv:2310.18948", source=Source.ARXIV, kind=Kind.PREPRINT, title="Unrelated preprint title")
    manual = record("manual:spadon-2021-thesis", source=Source.CURATED, kind=Kind.PHDTHESIS)
    cur = Curated(links=(("arxiv:2310.18948", "doi:10.1234/p"),), records=(manual,))
    groups, _ = cl.cluster([], reg(pub, ax), cur)
    assert sorted(len(g.ids) for g in groups) == [1, 2]


ids = st.sampled_from(
    ["doi:10.1234/a", "doi:10.1234/b", "arxiv:2101.00001", "doi:10.21203/rs.3.rs-1/v1", "openalex:W1", "dblp:conf/x/A"]
)


@settings(max_examples=50)
@given(
    st.lists(
        st.tuples(st.sampled_from(["A (1)", "B (2)"]), st.frozensets(ids, min_size=1, max_size=3)),
        min_size=1,
        max_size=8,
    ),
    st.randoms(),
)
def test_cluster_invariant_under_shuffle_and_duplication(raw, rnd) -> None:
    base = record("doi:10.1234/a")
    registries = reg(base, record("doi:10.1234/b"), record("arxiv:2101.00001", source=Source.ARXIV))
    pairs = [(m, claim(*s, source=Source.OPENALEX)) for m, s in raw]
    first, _ = cl.cluster(pairs, registries, Curated())
    shuffled = pairs + pairs[: len(pairs) // 2]
    rnd.shuffle(shuffled)
    second, _ = cl.cluster(shuffled, registries, Curated())
    assert first == second
    for g in first:
        assert sum(1 for i in g.ids if split_id(i)[0] == "doi" and not is_preprint_id(i)) <= 1


def test_search_unidentified(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = record("doi:10.1234/found", year=2023)
    seen = []

    def fake_search(http, title, family, year):
        seen.append((title, family, year))
        return Result(Fetch.OK, (hit,))

    monkeypatch.setattr(cl.crossref, "search", fake_search)
    dblp_rec = record("dblp:conf/x/S23", source=Source.DBLP)
    groups, _ = cl.cluster([(ME, claim("dblp:conf/x/S23", source=Source.DBLP, rec=dblp_rec))], reg(), Curated())
    out, events = cl.search_unidentified(groups, None, {ME: identity()}, {})  # type: ignore[arg-type]
    assert seen == [(TITLE, "spadon", 2023)]
    assert [e.kind for e in events] == ["search_linked"]
    assert out[0].ids == {"dblp:conf/x/S23", "doi:10.1234/found"}
    monkeypatch.setattr(
        cl.crossref,
        "search",
        lambda *a: Result(
            Fetch.OK,
            (dataclasses.replace(hit, title="Graph neural networks for vessel trajectory forecasting at scale"),),
        ),
    )
    out, events = cl.search_unidentified(groups, None, {ME: identity()}, {})  # type: ignore[arg-type]
    assert [e.kind for e in events] == ["search_review"]
    assert out == groups


def test_search_covers_a_listed_version_that_only_has_an_arxiv_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(cl.crossref, "search", lambda http, *a: seen.append(a) or Result(Fetch.OK, ()))
    listed = record("dblp:conf/nips/S23", "arxiv:2201.00001", source=Source.DBLP, kind=Kind.INPROCEEDINGS)
    corr = record("dblp:journals/corr/S23", "arxiv:2201.00002", source=Source.DBLP, kind=Kind.PREPRINT)
    pairs = [(ME, claim(*r.ids, source=Source.DBLP, rec=r)) for r in (listed, corr)]
    groups, _ = cl.cluster(pairs, reg(), Curated())
    cl.search_unidentified(groups, None, {ME: identity()}, {})  # type: ignore[arg-type]
    assert len(seen) == 1


def test_a_link_from_the_last_run_is_checked_by_doi_instead_of_searched(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = record("doi:10.1234/found", year=2023)
    searches, lookups = [], []
    monkeypatch.setattr(cl.crossref, "search", lambda http, *a: searches.append(a) or Result(Fetch.OK, (hit,)))

    def by_doi(http, ids):
        lookups.append(list(ids))
        return Batch({i: hit for i in ids if i == "doi:10.1234/found"}, frozenset(), frozenset())

    monkeypatch.setattr(cl.crossref, "by_doi", by_doi)
    dblp_rec = record("dblp:conf/x/S23", source=Source.DBLP)
    groups, _ = cl.cluster([(ME, claim("dblp:conf/x/S23", source=Source.DBLP, rec=dblp_rec))], reg(), Curated())
    cold = cl.search_unidentified(groups, None, {ME: identity()}, {})  # type: ignore[arg-type]
    warm = cl.search_unidentified(groups, None, {ME: identity()}, {"dblp:conf/x/S23": "doi:10.1234/found"})  # type: ignore[arg-type]
    assert warm == cold and len(searches) == 1 and lookups == [["doi:10.1234/found"]]
    stale = cl.search_unidentified(groups, None, {ME: identity()}, {"dblp:conf/x/S23": "doi:10.1234/gone"})  # type: ignore[arg-type]
    assert stale == cold and len(searches) == 2
