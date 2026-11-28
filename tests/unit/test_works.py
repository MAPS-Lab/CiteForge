import dataclasses

import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.curated import Curated
from citeforge.model import Author, Group, Kind, Record, Rel, Source, Status
from citeforge.text import name_key
from citeforge.works import apply_links, build, classify, clean_byline, doi_agrees, year_of
from tests.factories import author, record
from tests.unit.test_authorship import identity


@pytest.mark.parametrize(
    ("base", "extra", "kind"),
    [
        (Kind.ARTICLE, {}, Kind.ARTICLE),
        (Kind.PHDTHESIS, {"degree": "Master of Science"}, Kind.MASTERSTHESIS),
        (Kind.PHDTHESIS, {}, Kind.PHDTHESIS),
        (Kind.ARTICLE, {"container": ("Procedia Computer Science",)}, Kind.INPROCEEDINGS),
        (Kind.ARTICLE, {"container": ("Proceedings of the IEEE",)}, Kind.ARTICLE),
        (Kind.ARTICLE, {"container": ("Proceedings of the 5th Conference",)}, Kind.INPROCEEDINGS),
        (Kind.INPROCEEDINGS, {"container": ("Proceedings of the ACM on Human-Computer Interaction",)}, Kind.ARTICLE),
        (Kind.ARTICLE, {"container": ("eScholarship",)}, Kind.PREPRINT),
        (Kind.INPROCEEDINGS, {"container": ("UWSpace",)}, Kind.PHDTHESIS),
    ],
)
def test_kind_refines_the_source_kind(base: Kind, extra: dict, kind: Kind) -> None:
    assert classify(record("doi:10.1234/k", kind=base, **extra)) is kind


@pytest.mark.parametrize(
    ("title", "kind"),
    [
        ("Guest Editorial: Maritime AI", Kind.EDITORIAL),
        ("Series Editorial: Wireless Networks", Kind.EDITORIAL),
        ("Proceedings of the 50th Graphics Interface Conference", Kind.FRONT_MATTER),
        ("Preface", Kind.FRONT_MATTER),
        ("Message from the General Chairs", Kind.FRONT_MATTER),
        ("Conclusions and future work", Kind.FRONT_MATTER),
        ("Correction to: Vessel tracks", Kind.NOTICE),
        ("Erratum", Kind.NOTICE),
        ("Welcome-based routing", Kind.ARTICLE),
    ],
)
def test_title_patterns(title: str, kind: Kind) -> None:
    assert classify(record("doi:10.1234/t", title=title)) is kind


@pytest.mark.parametrize(
    ("title", "pages", "number", "kind"),
    [
        ("P12. A poster", "S45", None, Kind.ABSTRACT),
        ("P12. A poster", "45-52", None, Kind.ARTICLE),
        ("P12. A poster", "45-52", "Suppl 2", Kind.ABSTRACT),
        ("P12. A poster", None, None, Kind.ARTICLE),
        (
            "95. GENOME-WIDE ASSOCIATION STUDY OF 14,000 CASES IDENTIFIES FOUR NEW RISK GENES",
            "e97",
            None,
            Kind.ABSTRACT,
        ),
        ("W25. CHECKING, SYMMETRY/ORDERING, CONTAMINATION/WASHING, AND TABOO OBSESSIONS", "220", None, Kind.ABSTRACT),
        ("13.2 Identification and Characterization of Genome-Wide Associated Loci in OCD", "S296", "10", Kind.ABSTRACT),
        ("2.127 Genetic Correlates of Pediatric OCD", "S205", "10", Kind.ABSTRACT),
        ("3D printing of ship hulls", "220", None, Kind.ARTICLE),
        ("100 Years of ocean science", "e97", None, Kind.ARTICLE),
        ("2. results", "e97", None, Kind.ARTICLE),
    ],
)
def test_abstract_needs_a_numbered_title_and_a_one_page_or_supplement(title, pages, number, kind) -> None:
    assert classify(record("doi:10.1016/j.euroneuro.2022.07.180", title=title, pages=pages, number=number)) is kind


@given(
    st.sampled_from(list(Kind)),
    st.sampled_from(
        [
            "",
            "Procedia CIRP",
            "Proceedings of the IEEE",
            "UWSpace",
            "Proceedings of the ACM on Networking",
            "Software Engineering",
        ]
    ),
)
def test_classify_is_a_fixpoint(kind: Kind, venue: str) -> None:
    r = record("dblp:conf/x/Y", source=Source.DBLP, kind=kind, container=(venue,) if venue else ())
    once = classify(r)
    assert classify(dataclasses.replace(r, kind=once)) is once


def test_year_rule_order() -> None:
    cr = record("doi:10.1093/nar/gkz935", year=2019)
    oa = record("doi:10.1093/nar/gkz935", source=Source.OPENALEX, year=2020)
    ax = record("arxiv:2101.00001", source=Source.ARXIV, year=2018)
    dblp = record("dblp:x/y", source=Source.DBLP, year=2021)
    assert year_of([oa, cr]) == 2019
    assert year_of([oa, dblp, ax]) == 2018
    assert year_of([oa]) == 2020
    assert year_of([record("doi:10.1234/n", year=None)]) is None


def test_doi_agrees_bands() -> None:
    me = identity()
    reg = record("doi:10.1234/r", title="Graph neural networks for vessel trajectory forecasting")
    assert doi_agrees("Graph Neural Networks for Vessel Trajectory Forecasting", me, reg) == "ok"
    assert doi_agrees("Graph neural networks for vessel trajectory forecasting in the Arctic sea ice", me, reg) == "ok"
    assert doi_agrees("Graph networks for ship trajectory forecasts at sea", me, reg) == "review"
    assert doi_agrees("Protein folding with diffusion", me, reg) == "review"
    assert doi_agrees("Protein folding with diffusion", me, dataclasses.replace(reg, authors=())) == "detach"
    stranger = dataclasses.replace(reg, authors=(author("Ana Lima"),))
    assert doi_agrees(reg.title, me, stranger) == "detach"
    foreign = dataclasses.replace(reg, authors=(author("Gabriel Spadon", orcid="0000-0001-5109-3700"),))
    assert doi_agrees(reg.title, me, foreign) == "ok"


def test_doi_agrees_reads_every_language_of_a_bilingual_record() -> None:
    english = "Expert participation in the health technology assessment process: a scoping review"
    portuguese = "Participação de especialistas no processo de avaliação de tecnologias em saúde: revisão de escopo"
    reg = record("doi:10.1590/0034-7167-2025-0157", title=english, alt_titles=(portuguese,))
    assert doi_agrees(portuguese, identity(), reg) == "ok"
    assert doi_agrees(portuguese, identity(), dataclasses.replace(reg, alt_titles=())) == "review"


def test_doi_agrees_holds_a_consortium_byline_instead_of_detaching() -> None:
    title = "Complement genes contribute sex-biased vulnerability in diverse disorders"
    group = Author("", "", literal="Schizophrenia Working Group of the Psychiatric Genomics Consortium")
    reg = record("doi:10.1038/s41586-020-2277-x", title=title, authors=(author("Nolan Kamitaki"), group))
    assert doi_agrees(title, identity(), reg) == "group"
    assert doi_agrees("Protein folding with diffusion models at scale", identity(), reg) == "detach"
    assert doi_agrees(title, identity(), dataclasses.replace(reg, authors=(author("Nolan Kamitaki"),))) == "detach"


def _group(*records: Record, fuzzy: bool = False) -> Group:
    ids = frozenset(i for r in records for i in r.ids)
    return Group(min(ids), ids, records, (), fuzzy)


def test_build_names_the_source_that_filled_author_affiliations() -> None:
    doi = "doi:10.31219/osf.io/z79vk_v1"
    cr = record(doi, authors=(author("Ana Lima"),), source_type="posted-content", kind=Kind.PREPRINT)
    oa = record(doi, "openalex:W9", source=Source.OPENALEX, authors=(author("Ana Lima", affiliations=("Toronto",)),))
    w, _ = build(_group(cr, oa), Curated())
    assert w.primary.authors[0].affiliations == ("Toronto",)
    assert dict(w.provenance)["author_affiliations"] is Source.OPENALEX
    ids_only = dataclasses.replace(oa, authors=(author("Ana Lima", orcid="0000-0002-1825-0097"),))
    w, _ = build(_group(cr, ids_only), Curated())
    assert "author_affiliations" not in dict(w.provenance) and dict(w.provenance)["author_ids"] is Source.OPENALEX


def test_build_prefers_registry_and_fills_only_from_same_doi() -> None:
    doi = "doi:10.1016/j.oceaneng.2024.117234"
    cr = record(doi, container=("Ocean Engineering",), pages=None, publisher="Elsevier BV")
    oa = record(
        doi,
        "openalex:W1",
        source=Source.OPENALEX,
        pages="117234",
        volume="300",
        authors=(
            author("Gabriel Spadon", openalex="A5023888391", rors=("https://ror.org/01e6qks80",)),
            author("Stan Matwin"),
        ),
    )
    ax = record(
        "arxiv:2310.18948", source=Source.ARXIV, kind=Kind.PREPRINT, volume="99", primary_class="cs.LG", year=2023
    )
    w, events = build(_group(oa, ax, cr), Curated())
    assert w.primary.source is Source.CROSSREF
    assert w.key == doi
    assert w.primary.pages == "117234" and w.primary.volume == "300"
    assert w.primary.authors[0].openalex == "A5023888391"
    assert w.primary.authors[0].rors == ("https://ror.org/01e6qks80",)
    assert w.eprint is ax
    assert w.primary.primary_class is None
    assert dict(w.provenance)["pages"] is Source.OPENALEX
    assert all(source is not Source.ARXIV for _, source in w.provenance)
    assert w.sources == {Source.CROSSREF, Source.OPENALEX, Source.ARXIV}
    assert events == ()
    fuzzy, _ = build(_group(cr, ax, fuzzy=True), Curated())
    assert fuzzy.eprint is None


def test_build_version_of_record_fallbacks() -> None:
    dblp = record("dblp:conf/x/S23", source=Source.DBLP, kind=Kind.INPROCEEDINGS)
    ax = record("arxiv:2101.00001", source=Source.ARXIV, kind=Kind.PREPRINT)
    assert build(_group(ax, dblp), Curated())[0].primary.ids == dblp.ids
    corr = dataclasses.replace(dblp, kind=Kind.PREPRINT, ids=frozenset({"dblp:journals/corr/abs-1"}))
    assert build(_group(corr, ax), Curated())[0].primary.ids == ax.ids
    oa = record("openalex:W9", source=Source.OPENALEX)
    only, _ = build(_group(oa), Curated())
    assert only.primary == oa and only.key == "openalex:W9"
    copy = record("doi:10.5281/zenodo.1", source=Source.DATACITE, year=2022)
    publisher = record("doi:10.3390/x1", year=2023)
    assert build(_group(copy, publisher, fuzzy=True), Curated())[0].primary.ids == publisher.ids


def test_build_exclusions_and_overrides() -> None:
    editorial = record("doi:10.1234/e", title="Editorial")
    assert build(_group(editorial), Curated())[0].excluded == "kind:editorial"
    retracted = record("doi:10.1234/r", status=Status.RETRACTED)
    assert build(_group(retracted), Curated())[0].excluded == "status:retracted"
    cur = Curated(
        reject={("*", "doi:10.1234/c"): "namesake"},
        overrides={"doi:10.1234/c": (("booktitle", "KDD"), ("year", "2024"))},
    )
    w, _ = build(_group(record("doi:10.1234/c", kind=Kind.INPROCEEDINGS, source_type="proceedings-article")), cur)
    assert w.excluded == "curated:namesake"
    assert w.primary.container == ("KDD",) and w.year == 2024
    assert dict(w.provenance)["year"] is Source.CURATED


def test_byline_rules() -> None:
    orcid = "0000-0002-1825-0097"
    dup = (author("Ana Lima", orcid=orcid), author("Bo Kim"), author("Ana Lima", orcid=orcid))
    assert len(clean_byline(dup)[0]) == 2
    no_orcid = (author("Ana Lima"), author("Ana Lima"))
    assert len(clean_byline(no_orcid)[0]) == 2
    big = tuple(author(f"Per Son{i % 40}") for i in range(60))
    assert len(clean_byline(big)[0]) == 40
    org = (author("Ana Lima"), Author("", "", literal="Dalhousie University"), Author("", "", literal="ATLAS Team"))
    kept, dropped = clean_byline(org)
    assert [a.literal for a in kept] == [None, "ATLAS Team"]
    assert dropped == ["author_affiliation_dropped:Dalhousie University"]


def test_org_only_byline_takes_person_byline_and_sorted_byline_repair() -> None:
    doi = "doi:10.52202/079017-1"
    org = record(doi, authors=(Author("", "", literal="Some Consortium"),))
    persons = record(doi, source=Source.DBLP, authors=(author("Zoe Zed"), author("Al Alpha")))
    assert [a.family for a in build(_group(org, persons), Curated())[0].primary.authors] == ["Zed", "Alpha"]
    sorted_cr = record(doi, authors=(author("Al Alpha"), author("Bo Beta"), author("Cy Gamma")))
    ax = record(
        "arxiv:2401.00001",
        source=Source.ARXIV,
        kind=Kind.PREPRINT,
        authors=(author("Cy Gamma"), author("Al Alpha"), author("Bo Beta")),
    )
    w, _ = build(_group(sorted_cr, ax), Curated())
    assert [a.family for a in w.primary.authors] == ["Gamma", "Alpha", "Beta"]
    assert dict(w.provenance)["author_order"] is Source.ARXIV


def test_apply_links_notices_retractions_translations() -> None:
    target = build(_group(record("doi:10.1234/t")), Curated())[0]
    notice = build(
        _group(
            record("doi:10.1234/n", title="Correction to: X", relations=frozenset({(Rel.CORRECTS, "doi:10.1234/t")}))
        ),
        Curated(),
    )[0]
    retraction = build(
        _group(
            record("doi:10.1234/x", title="Retraction note", relations=frozenset({(Rel.RETRACTS, "doi:10.1234/t")}))
        ),
        Curated(),
    )[0]
    translation = build(
        _group(record("doi:10.1234/pt", relations=frozenset({(Rel.TRANSLATION_OF, "doi:10.1234/t")}))), Curated()
    )[0]
    out = {w.key: w for w in apply_links([target, notice, retraction, translation])}
    t = out["doi:10.1234/t"]
    assert t.linked == {"doi:10.1234/n", "doi:10.1234/x", "doi:10.1234/pt"}
    assert t.status is Status.RETRACTED and t.excluded == "status:retracted"
    assert out["doi:10.1234/n"].excluded == "kind:notice"
    assert out["doi:10.1234/pt"].excluded == "translation_of:doi:10.1234/t"


def test_name_key_reexport_sanity() -> None:
    assert name_key("Gabriel", "Spadon").initials == "g"


def test_crossref_other_with_a_container_takes_the_openalex_kind() -> None:
    doi = "doi:10.1002/9781119675525.ch1"
    book = ("Communication Networks and Service Management in the Era of Artificial Intelligence and Machine Learning",)
    cr = record(doi, title="Overview of Network and Service Management", kind=Kind.OTHER, container=book)
    oa = dataclasses.replace(
        cr,
        source=Source.OPENALEX,
        ids=frozenset({doi, "openalex:W1"}),
        source_type="book-chapter",
        kind=Kind.INCOLLECTION,
    )
    assert build(_group(cr, oa), Curated())[0].kind is Kind.INCOLLECTION
    assert build(_group(cr), Curated())[0].excluded == "kind:other"
    bare = dataclasses.replace(cr, container=())
    assert build(_group(bare, dataclasses.replace(oa, container=())), Curated())[0].excluded == "kind:other"


def test_chapters_of_an_authored_book_fold_into_the_book() -> None:
    me = (author("Thomas P. Trappenberg"),)
    title = "Fundamentals of Computational Neuroscience"
    book = build(
        _group(
            record(
                "doi:10.1093/oso/9780192869364.001.0001",
                title=title,
                authors=me,
                kind=Kind.BOOK,
                container=(),
            )
        ),
        Curated(),
    )[0]
    assert book.kind is Kind.BOOK and book.excluded is None
    chapter = build(
        _group(
            record(
                "doi:10.1093/oso/9780192869364.003.0001",
                title="Introduction and outlook",
                authors=me,
                kind=Kind.INCOLLECTION,
                container=(title,),
            )
        ),
        Curated(),
    )[0]
    guest = build(
        _group(
            record(
                "doi:10.1093/oso/9780192869364.003.0099",
                title="A guest chapter",
                authors=(*me, author("Ana Lima")),
                kind=Kind.INCOLLECTION,
                container=(title,),
            )
        ),
        Curated(),
    )[0]
    out = {w.key: w for w in apply_links([book, chapter, guest])}
    assert out[chapter.key].excluded == f"chapter_of:{book.key}"
    assert out[book.key].linked == {chapter.key} and out[book.key].excluded is None
    assert out[guest.key].excluded is None
    assert apply_links([chapter])[0].excluded is None
