import dataclasses

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from citeforge import cluster as cl
from citeforge.curated import Curated
from citeforge.http import Result
from citeforge.ids import is_copy_id, split_id
from citeforge.model import Claim, Fetch, Kind, Names, Record, Registries, Rel, Source, Status, Work
from citeforge.state import StateRow
from citeforge.text import name_key
from citeforge.works import build
from tests.factories import author, member, record
from tests.unit.test_authorship import identity

ME = "Spadon (bfdGsGUAAAAJ)"
TITLE = "Graph neural networks for vessel trajectory forecasting"


def claim(*ids: str, source: Source = Source.ORCID, title: str = TITLE, rec: Record | None = None) -> Claim:
    return Claim(source, frozenset(ids), title, 2023, record=rec)


def reg(*records: Record, **kw) -> Registries:
    return Registries(found={min(r.ids): r for r in records}, **kw)


def test_verify_detaches_dead_and_mismatched_dois_and_holds_reviews() -> None:
    good = record("doi:10.1234/good")
    wrong = record("doi:10.1234/wrong", title="Protein folding with diffusion", authors=(author("Ana Lima"),))
    middle = record("doi:10.1234/mid", title="Graph networks for ship trajectory forecasts at sea")
    claims = [
        (ME, claim("doi:10.1234/good")),
        (ME, claim("doi:10.1234/wrong", "arxiv:2101.00001")),
        (ME, claim("doi:10.1234/dead")),
        (ME, claim("doi:10.1234/mid")),
    ]
    kept, held, events, _ = cl.verify(
        claims, reg(good, wrong, middle, dead=frozenset({"doi:10.1234/dead"})), {ME: identity()}
    )
    assert [c.ids for _, c in kept] == [{"doi:10.1234/good"}, {"arxiv:2101.00001"}]
    assert [c.ids for _, c in held] == [{"doi:10.1234/mid"}]
    assert [e.kind for e in events] == ["doi_mismatch", "doi_unresolved", "doi_review"]


def test_a_doi_detached_from_an_openalex_claim_stays_apart_from_it() -> None:
    oa = record("openalex:W1", "doi:10.1/wrong", source=Source.OPENALEX)
    other = record("doi:10.1/wrong", title="Soil bacteria in tropical farms", authors=(author("Ana Lima"),))
    registries = reg(other, openalex={"doi:10.1/wrong": oa})
    kept, _, events, registries = cl.verify(
        [(ME, claim(*oa.ids, source=Source.OPENALEX, rec=oa))], registries, {ME: identity()}
    )
    assert [e.kind for e in events] == ["doi_mismatch"]
    groups, _ = cl.cluster(kept, registries, Curated())
    assert sorted(sorted(g.ids) for g in groups) == [["doi:10.1/wrong"], ["openalex:W1"]]
    assert [g.claims for g in groups if g.key == "doi:10.1/wrong"] == [()]


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
    same = record("doi:10.1145/3820171.3806907", relations=frozenset({(Rel.IDENTICAL, "doi:10.1145/3801489.3806907")}))
    groups, events = cl.cluster([], reg(same, record("doi:10.1145/3801489.3806907")), Curated())
    assert [sorted(g.ids) for g in groups] == [["doi:10.1145/3801489.3806907", "doi:10.1145/3820171.3806907"]]
    assert events == []


def test_repository_copy_joins_the_publisher_doi_and_never_leads() -> None:
    pub = record("doi:10.3390/s1", title=TITLE)
    copy = record(
        "doi:10.25560/1",
        title=TITLE,
        source=Source.DATACITE,
        source_type="JournalArticle",
        relations=frozenset({(Rel.VARIANT_OF, "doi:10.3390/s1")}),
    )
    groups, events = cl.cluster([(ME, claim("doi:10.25560/1"))], reg(pub, copy), Curated())
    (g,) = groups
    assert g.ids == {"doi:10.3390/s1", "doi:10.25560/1"} and events == []
    work, _ = build(g, Curated())
    assert work.primary.ids == {"doi:10.3390/s1"} and work.key == "doi:10.3390/s1"


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


def test_copy_links_to_its_clearly_closest_partner_and_never_to_a_notice_or_abstract() -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    title = "Meta-analysis of genome-wide association studies of hoarding symptoms"
    pre = record("doi:10.1101/2022.06.19.1", kind=Kind.PREPRINT, authors=authors, year=2022, title=title)
    pub = record("doi:10.1038/s41398-022-1", authors=authors, year=2022, title=title)
    notice = record("doi:10.1038/s41398-022-2", authors=authors, year=2022, title="Correction: " + title)
    abstract = record(
        "doi:10.1016/j.euro.2023.1", authors=authors, year=2023, title="T25. " + title.upper(), pages="S1"
    )
    other = record(
        "doi:10.1109/tem.2021.1", authors=authors, year=2021, title=title.replace("symptoms", "symptom data")
    )
    groups, events = cl.cluster([], reg(pre, pub, notice, abstract, other), Curated())
    assert {"doi:10.1101/2022.06.19.1", "doi:10.1038/s41398-022-1"} in [g.ids for g in groups]
    assert ("fuzzy_link", "doi:10.1101/2022.06.19.1", "doi:10.1038/s41398-022-1") in [
        (e.kind, e.id, e.detail) for e in events
    ]


def test_copy_with_partners_closer_than_the_margin_is_only_proposed() -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    arx = record("arxiv:2201.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors, year=2023)
    a = record("doi:10.1234/a", authors=authors, year=2024, title=TITLE + "s")
    b = record("doi:10.1234/b", authors=authors, year=2024, title=TITLE + "es")
    groups, events = cl.cluster([], reg(arx, a, b), Curated())
    assert len(groups) == 3
    assert [(e.kind, e.detail) for e in events] == [("link_proposal", "doi:10.1234/a doi:10.1234/b")]


@pytest.mark.parametrize(
    ("pre_title", "pub_title"),
    [
        (
            "Reinforcement learning for port traffic control: Part II",
            "Reinforcement learning for port traffic control: Part I",
        ),
        ("Succinct data structures for range maximum queries", "Succinct data structures for range minimum queries"),
        ("Benchmarking vessel trajectory models 2", "Benchmarking vessel trajectory models 3"),
    ],
)
def test_fuzzy_link_never_joins_sibling_titles(pre_title: str, pub_title: str) -> None:
    authors = (author("Gabriel Spadon"), author("Stan Matwin"))
    pre = record("arxiv:2401.00001", source=Source.ARXIV, kind=Kind.PREPRINT, authors=authors, title=pre_title)
    pub = record("doi:10.1/pub", authors=authors, title=pub_title)
    groups, events = cl.cluster([], reg(pre, pub), Curated())
    assert len(groups) == 2 and [e.kind for e in events] == ["link_proposal"]


def test_fuzzy_link_keeps_spelling_variants_together() -> None:
    assert not cl._distinct_titles("Modeling ship traffic", "Modelling ship traffic")
    assert not cl._distinct_titles("Graph networks for vessel forecasting", "Graph networks for vessel forecasts")
    assert cl._distinct_titles("Mixing layers: Part I", "Mixing layers")


def test_registry_version_link_to_a_repository_copy_is_kept() -> None:
    a = author("Ada Lovelace")
    journal = record("doi:10.1000/j.1", title="Deep sea mapping with sonar", authors=(a,), year=2023)
    copy = record(
        "doi:10.5281/zenodo.77",
        source=Source.DATACITE,
        kind=Kind.PREPRINT,
        source_type="Preprint",
        title="Deep sea mapping with sonar: preprint version",
        authors=(a,),
        year=2022,
        relations=frozenset({(Rel.VERSION_OF, "doi:10.1000/j.1")}),
    )
    claims = [(ME, claim(*copy.ids, title=copy.title)), (ME, claim(*journal.ids, title=journal.title))]
    groups, events = cl.cluster(claims, reg(journal, copy), Curated())
    assert [g.ids for g in groups] == [{"doi:10.1000/j.1", "doi:10.5281/zenodo.77"}] and events == []


def test_zenodo_concept_and_version_dois_are_one_work() -> None:
    title = "The T-Stick: Maintaining A 12 Year-Old Digital Musical Instrument"
    by = (author("Alex Nieva"), author("Marcelo Wanderley"))
    concept = record("doi:10.5281/zenodo.1302544", source=Source.DATACITE, source_type="Text", title=title, authors=by)
    version = dataclasses.replace(
        concept,
        ids=frozenset({"doi:10.5281/zenodo.1302545"}),
        source_type="ConferencePaper",
        relations=frozenset({(Rel.VERSION_OF, "doi:10.5281/zenodo.1302544")}),
    )
    claims = [
        (ME, claim("doi:10.5281/zenodo.1302544", "openalex:W2982397947", source=Source.OPENALEX, title=title)),
        (ME, claim("doi:10.5281/zenodo.1302545", "dblp:conf/nime/NievaWMW18", source=Source.DBLP, title=title)),
    ]
    groups, events = cl.cluster(claims, reg(concept, version), Curated())
    assert len(groups) == 1 and events == []


def test_profile_prefers_the_published_registry_record_over_its_preprints() -> None:
    by = (author("Suresh Neethirajan"),)
    title = "Artificial Intelligence and Sensor Technologies in Dairy Livestock Export"
    pub = record("doi:10.3390/s23167045", title=title, authors=by)
    odd = record(
        "doi:10.20944/preprints202304.0409.v1",
        kind=Kind.PREPRINT,
        source_type="posted-content",
        title="The Ethical Frontier: Navigating the Metaverse in Modern Animal Farming",
        authors=by,
        relations=frozenset({(Rel.PREPRINT_OF, "doi:10.3390/s23167045")}),
    )
    real = dataclasses.replace(
        odd, ids=frozenset({"doi:10.20944/preprints202307.0473.v1"}), title=title, relations=frozenset()
    )
    groups, events = cl.cluster([], reg(pub, odd, real), Curated())
    assert [g.ids for g in groups] == [pub.ids | odd.ids | real.ids]
    assert [e.kind for e in events] == ["fuzzy_link"]


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
        assert sum(1 for i in g.ids if split_id(i)[0] == "doi" and not is_copy_id(i)) <= 1


def _search(groups, previous=None, min_year=2020, registries=None):
    linked = cl.previous_links(previous or {})
    return cl.search_unidentified(groups, None, {ME: identity()}, min_year, linked, registries or reg())  # type: ignore[arg-type]


def _dblp_group(title: str = TITLE, year: int = 2023):
    dblp_rec = record("dblp:conf/x/S23", source=Source.DBLP, title=title, year=year)
    pairs = [(ME, claim("dblp:conf/x/S23", source=Source.DBLP, title=title, rec=dblp_rec))]
    return pairs, cl.cluster(pairs, reg(), Curated())[0]


def test_search_unidentified(monkeypatch: pytest.MonkeyPatch) -> None:
    hit = record("doi:10.1234/found", year=2023)
    seen = []

    def fake_search(http, title, family, year):
        seen.append((title, family, year))
        return Result(Fetch.OK, (hit,))

    monkeypatch.setattr(cl.crossref, "search", fake_search)
    pairs, groups = _dblp_group()
    found, events = _search(groups)
    assert seen == [(TITLE, "spadon", 2023)]
    assert [e.kind for e in events] == ["search_linked"]
    assert found == [("dblp:conf/x/S23", hit)]
    (g,), _ = cl.cluster(pairs, reg(), Curated(), _search)
    assert g.ids == {"dblp:conf/x/S23", "doi:10.1234/found"} and g.key == "doi:10.1234/found"
    longer = dataclasses.replace(hit, title="Graph neural networks for vessel trajectory forecasting at scale")
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.OK, (longer,)))
    found, events = _search(groups)
    assert [e.kind for e in events] == ["search_review"] and found == []


def test_search_refuses_two_same_title_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    conf = record("doi:10.1109/dcc.2022.55", year=2023, kind=Kind.INPROCEEDINGS)
    journal = record("doi:10.4230/lipics.sea.2022.16", year=2023)
    preprint = record("doi:10.1101/2022.01.01.1", year=2023, kind=Kind.PREPRINT)
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.OK, (conf, journal)))
    _, groups = _dblp_group()
    found, events = _search(groups)
    assert found == [] and [e.kind for e in events] == ["search_review"]
    assert "rivals=doi:10.4230/lipics.sea.2022.16" in events[0].detail
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.OK, (conf, preprint)))
    found, _ = _search(groups)
    assert found == [("dblp:conf/x/S23", conf)]


def test_search_failure_is_reported_per_member(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.FAILED, None, "timeout"))
    _, groups = _dblp_group()
    found, events = _search(groups)
    assert found == [] and [(e.kind, e.member, e.detail) for e in events] == [("search_failed", ME, "timeout")]


def test_search_reuses_the_previous_link_without_a_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cl.crossref, "search", lambda *a: pytest.fail("searched again"))
    hit = record("doi:10.1234/found", year=2023)
    w = Work("doi:10.1234/found", hit, None, frozenset(), Kind.ARTICLE, Status.OK, 2023, None)
    w = dataclasses.replace(w, aliases=frozenset({"doi:10.1234/found", "dblp:conf/x/S23"}))
    previous = {w.key: StateRow(w, Names("Spadon2023-Graph", "Spadon2023:Graph"))}
    _, groups = _dblp_group()
    fresh = dataclasses.replace(hit, volume="17", pages="1-9", status=Status.RETRACTED)
    found, events = _search(groups, previous, registries=reg(fresh))
    assert found == [("dblp:conf/x/S23", fresh)] and [e.kind for e in events] == ["search_linked"]
    failed = Registries(failed=frozenset({"doi:10.1234/found"}))
    found, _ = _search(groups, previous, registries=failed)
    assert found == [("dblp:conf/x/S23", hit)]


def test_search_runs_again_when_the_linked_doi_is_gone(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(cl.crossref, "search", lambda http, *a: seen.append(a) or Result(Fetch.OK, ()))
    hit = record("doi:10.1234/found", year=2023)
    w = Work("doi:10.1234/found", hit, None, frozenset(), Kind.ARTICLE, Status.OK, 2023, None)
    w = dataclasses.replace(w, aliases=frozenset({"doi:10.1234/found", "dblp:conf/x/S23"}))
    previous = {w.key: StateRow(w, Names("Spadon2023-Graph", "Spadon2023:Graph"))}
    found, _ = _search(_dblp_group()[1], previous, registries=reg(dead=frozenset({"doi:10.1234/found"})))
    assert found == [] and len(seen) == 1


def test_search_sends_a_compound_surname_as_its_words(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(
        cl.crossref, "search", lambda http, title, family, year: seen.append(family) or Result(Fetch.OK, ())
    )
    nur = identity(member=member(name="Nur Zincir-Heywood", dirname=ME), names=(name_key("Nur", "Zincir-Heywood"),))
    cl.search_unidentified(_dblp_group()[1], None, {ME: nur}, 2020, {}, reg())  # type: ignore[arg-type]
    assert seen == ["zincir heywood"]


@pytest.mark.parametrize(
    ("listed", "found"),
    [
        (
            "Reinforcement learning for port traffic control: Part II",
            "Reinforcement learning for port traffic control: Part I",
        ),
        ("Succinct data structures for range maximum queries", "Succinct data structures for range minimum queries"),
    ],
)
def test_search_never_links_a_sibling_title(monkeypatch: pytest.MonkeyPatch, listed: str, found: str) -> None:
    hit = record("doi:10.1/sibling", title=found, year=2023)
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.OK, (hit,)))
    links, events = _search(_dblp_group(title=listed)[1])
    assert links == [] and [e.kind for e in events] == ["search_review"]


def test_search_skips_groups_that_cannot_reach_the_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(cl.crossref, "search", lambda http, *a: seen.append(a) or Result(Fetch.OK, ()))
    _search(_dblp_group(year=2018)[1], min_year=2020)
    assert seen == []
    _search(_dblp_group(year=2019)[1], min_year=2020)
    assert len(seen) == 1


def test_preprint_links_once_search_joins_its_two_published_listings(monkeypatch: pytest.MonkeyPatch) -> None:
    authors = (author("Samuel Acquaviva"), author("Yewen Pu"), author("Marta Kryven"))
    title = "Communicating natural programs to humans and machines"
    arx = record("arxiv:2106.07824", source=Source.ARXIV, kind=Kind.PREPRINT, title=title, authors=authors, year=2021)
    nips = record(
        "dblp:conf/nips/AcquavivaPKS22", source=Source.DBLP, kind=Kind.INPROCEEDINGS, title=title, authors=authors
    )
    nips = dataclasses.replace(nips, year=2022)
    doi = record("doi:10.52202/068431-0270", kind=Kind.INPROCEEDINGS, title=title, authors=authors, year=2022)
    pairs = [(ME, claim(*r.ids, source=Source.DBLP, title=title, rec=r)) for r in (nips,)]
    monkeypatch.setattr(cl.crossref, "search", lambda *a: Result(Fetch.OK, (doi,)))
    kryven = identity(names=(name_key("Marta", "Kryven"),))

    def search(gs):
        return cl.search_unidentified(gs, None, {ME: kryven}, 2020, {}, reg())  # type: ignore[arg-type]

    without, _ = cl.cluster(pairs, reg(arx, doi), Curated())
    assert len(without) == 3
    (g,), events = cl.cluster(pairs, reg(arx, doi), Curated(), search)
    assert g.ids == {"arxiv:2106.07824", "dblp:conf/nips/AcquavivaPKS22", "doi:10.52202/068431-0270"}
    assert {"search_linked", "fuzzy_link"} <= {e.kind for e in events}


@pytest.mark.parametrize(
    ("pre", "pub", "years"),
    [
        (("samir", "rahman"), ("mohammed samir", "masudur rahman"), (2025, 2026)),
        (("brandt", "maza"), ("brandt", "moreno maza"), (2021, 2021)),
        (("muse", "shah"), ("muse", "b shah"), (2024, 2025)),
        (("muse", "rahman"), ("muse", "rahman"), (2022, 2020)),
    ],
)
def test_fuzzy_ok_accepts_split_surnames_and_postprints(pre, pub, years) -> None:
    assert cl._fuzzy_ok(cl._Profile("t", years[0], pre), cl._Profile("t", years[1], pub))


def test_fuzzy_ok_still_refuses_other_bylines_and_distant_years() -> None:
    p = cl._Profile("t", 2021, ("brandt", "maza", "chen"))
    assert not cl._fuzzy_ok(p, cl._Profile("t", 2021, ("brandt", "lee", "kim")))
    assert not cl._fuzzy_ok(p, cl._Profile("t", 2018, ("brandt", "maza", "chen")))
    assert not cl._fuzzy_ok(p, cl._Profile("t", 2025, ("brandt", "maza", "chen")))
    assert not cl._fuzzy_ok(cl._Profile("t", 2021, ("brandt",) * 3), cl._Profile("t", 2021, ("brandt", "x", "y")))


def test_search_covers_a_listed_version_that_only_has_an_arxiv_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(cl.crossref, "search", lambda http, *a: seen.append(a) or Result(Fetch.OK, ()))
    listed = record("dblp:conf/nips/S23", "arxiv:2201.00001", source=Source.DBLP, kind=Kind.INPROCEEDINGS)
    corr = record("dblp:journals/corr/S23", "arxiv:2201.00002", source=Source.DBLP, kind=Kind.PREPRINT)
    pairs = [(ME, claim(*r.ids, source=Source.DBLP, rec=r)) for r in (listed, corr)]
    groups, _ = cl.cluster(pairs, reg(), Curated())
    _search(groups)
    assert len(seen) == 1
