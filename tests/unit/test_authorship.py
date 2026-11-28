import dataclasses
from typing import Any

from citeforge.authorship import decide, is_own, own_slots
from citeforge.curated import Curated
from citeforge.model import Author, Claim, Identity, Kind, Source, Verdict
from citeforge.sources import openalex
from citeforge.text import name_key
from tests.factories import author, member, record, work
from tests.sources.test_openalex import authorship as oa_authorship
from tests.sources.test_openalex import work as oa_work

ME = "0000-0002-1825-0097"
OTHER = "0000-0001-5109-3700"
LAB = "https://ror.org/01e6qks80"


def identity(**kw: Any) -> Identity:
    fields: dict[str, Any] = {
        "member": member(),
        "orcids": frozenset({ME}),
        "openalex": frozenset({"A5023888391"}),
        "dblp": "192/1659",
        "names": (name_key("Gabriel", "Spadon"),),
        "rors": frozenset({LAB}),
        "inst_names": frozenset({"dalhousie university"}),
    }
    fields.update(kw)
    return Identity(**fields)


def claim(source: Source, rec=None) -> Claim:
    return Claim(source, frozenset({"doi:10.1234/w"}), "T", 2023, record=rec)


def w(*authors: Author, **kw: Any):
    return work(record("doi:10.1234/w", authors=authors or (author("Gabriel Spadon"),), **kw))


NO_CURATION = Curated()


def verdict(i: Identity, wk, claims=(), curated=NO_CURATION):
    d = decide(i, wk, claims, curated)
    return d.verdict, d.rule


def test_rule_1_curated_wins() -> None:
    cur = Curated(reject={("Spadon (bfdGsGUAAAAJ)", "doi:10.1234/w"): "namesake"})
    wk = dataclasses.replace(w(author("Gabriel Spadon", orcid=ME)), aliases=frozenset({"doi:10.1234/w"}))
    assert verdict(identity(), wk, curated=cur) == (Verdict.REJECT, "curated")
    cur = Curated(accept=frozenset({("Spadon (bfdGsGUAAAAJ)", "doi:10.1234/w")}))
    assert verdict(identity(), dataclasses.replace(w(author("Ana Lima")), aliases=wk.aliases), curated=cur) == (
        Verdict.ACCEPT,
        "curated",
    )


def test_no_registry_is_review() -> None:
    oa = work(record("openalex:W1", source=Source.OPENALEX))
    assert verdict(identity(), oa) == (Verdict.REVIEW, "no_registry")


def test_rule_2_orcid_bypasses_name_gate() -> None:
    married = w(author("Gabriela Souza", orcid=ME))
    assert verdict(identity(), married) == (Verdict.ACCEPT, "authorship_orcid")
    via_claim = claim(Source.DBLP, record("dblp:x/S23", source=Source.DBLP, authors=(author("G. Spadon", orcid=ME),)))
    assert verdict(identity(), w(), [via_claim]) == (Verdict.ACCEPT, "authorship_orcid")


def _openalex_claim(institutions: list[dict[str, str]]) -> Claim:
    rec = openalex.parse_work(oa_work(authorships=[
        oa_authorship("Gabriel Spadon", id="https://openalex.org/A5023888391", orcid=f"https://orcid.org/{ME}",
                      institutions=institutions),
    ]))  # fmt: skip
    return Claim(Source.OPENALEX, rec.ids, rec.title, rec.year, record=rec)


def test_openalex_profile_orcid_is_not_authorship_proof() -> None:
    abroad = [{"ror": "https://ror.org/02e91jd64", "display_name": "Universiti Putra Malaysia"}]
    unlisted = w(author("Gabriel Spadon", affiliations=("Putra Business School, Universiti Putra Malaysia",)))
    assert verdict(identity(), unlisted, [_openalex_claim(abroad)]) == (Verdict.REVIEW, "openalex_only")
    home = [{"ror": LAB, "display_name": "Dalhousie University"}]
    assert verdict(identity(), w(), [_openalex_claim(home)]) == (Verdict.ACCEPT, "openalex_affil")


def test_registry_orcid_contradicts_openalex_profile() -> None:
    abroad = [{"ror": "https://ror.org/034t30j35", "display_name": "Chinese Academy of Sciences"}]
    other = w(author("Gabriel Spadon", orcid=OTHER))
    assert verdict(identity(), other, [_openalex_claim(abroad)]) == (Verdict.REJECT, "orcid_contra")


def test_rule_3_not_named() -> None:
    assert verdict(identity(), w(author("Ana Lima")), [claim(Source.DBLP)]) == (Verdict.REJECT, "not_named")
    group_only = w(Author("", "", literal="ATLAS Collaboration"))
    assert verdict(identity(), group_only) == (Verdict.REJECT, "not_named")
    assert verdict(identity(), group_only, [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")


def test_rule_4_orcid_contra_never_accepts() -> None:
    namesake = w(author("Gabriel Spadon", orcid=OTHER))
    for claims in ([], [claim(Source.DBLP)], [claim(Source.ORCID)], [claim(Source.OPENALEX)]):
        assert verdict(identity(), namesake, claims) == (Verdict.REJECT, "orcid_contra")
    assert verdict(identity(orcids=frozenset()), namesake, [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")


def test_rule_4_yields_when_the_slot_names_the_home_institution() -> None:
    second_orcid = w(author("Gabriel Spadon", orcid=OTHER, rors=(LAB,)))
    assert verdict(identity(), second_orcid, [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")
    assert verdict(identity(), second_orcid, []) == (Verdict.REJECT, "no_evidence")


def test_rules_5_to_8() -> None:
    assert verdict(identity(), w(), [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")
    assert verdict(identity(), w(), [claim(Source.ORCID)]) == (Verdict.ACCEPT, "orcid_claim")
    abroad = author("Gabriel Spadon", affiliations=("University of Elsewhere",))
    assert verdict(identity(), w(abroad), [claim(Source.ORCID)]) == (Verdict.REVIEW, "orcid_affil_contra")
    home = author("Gabriel Spadon", openalex="A5023888391", rors=(LAB,))
    assert verdict(identity(), w(home), [claim(Source.OPENALEX)]) == (Verdict.ACCEPT, "openalex_affil")
    by_string = author("Gabriel Spadon", openalex="A5023888391", affiliations=("Faculty of CS, Dalhousie University",))
    assert verdict(identity(), w(by_string), [claim(Source.OPENALEX)]) == (Verdict.ACCEPT, "openalex_affil")
    bare = author("Gabriel Spadon", openalex="A5023888391")
    assert verdict(identity(), w(bare), [claim(Source.OPENALEX)]) == (Verdict.REVIEW, "openalex_only")
    assert verdict(identity(), w(), []) == (Verdict.REJECT, "no_evidence")


def test_affiliation_names_match_whole_words_only() -> None:
    western = identity(rors=frozenset({LAB}), inst_names=frozenset({"western university"}))
    abroad = author(
        "Gabriel Spadon", openalex="A5023888391", affiliations=("Feinberg School, Northwestern University",)
    )
    assert verdict(western, w(abroad), [claim(Source.OPENALEX)]) == (Verdict.REVIEW, "openalex_only")
    home = author("Gabriel Spadon", openalex="A5023888391", affiliations=("Dept. of CS, Western University, Canada",))
    assert verdict(western, w(home), [claim(Source.OPENALEX)]) == (Verdict.ACCEPT, "openalex_affil")


def test_orcid_listed_work_is_held_only_on_registry_affiliations_of_a_member_with_known_institutions() -> None:
    abroad = author("Gabriel Spadon", affiliations=("University of British Columbia",))
    no_affiliations = identity(inst_names=frozenset())
    assert verdict(no_affiliations, w(abroad), [claim(Source.ORCID)]) == (Verdict.ACCEPT, "orcid_claim")
    filled = dataclasses.replace(w(abroad), provenance=(("author_affiliations", Source.OPENALEX),))
    assert verdict(identity(), filled, [claim(Source.ORCID)]) == (Verdict.ACCEPT, "orcid_claim")
    oa = record("doi:10.1234/w", source=Source.OPENALEX, authors=(abroad,))
    assert verdict(identity(), w(), [claim(Source.ORCID), claim(Source.OPENALEX, oa)]) == (
        Verdict.ACCEPT,
        "orcid_claim",
    )
    assert verdict(identity(), w(abroad), [claim(Source.ORCID)]) == (Verdict.REVIEW, "orcid_affil_contra")


def test_dblp_primary_and_curated_record_are_registries_enough() -> None:
    dblp_only = work(record("dblp:conf/x/S23", source=Source.DBLP, kind=Kind.INPROCEEDINGS))
    assert verdict(identity(), dblp_only, [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")


def test_a_dblp_or_orcid_byline_names_the_member_when_the_arxiv_byline_is_mistyped() -> None:
    i = identity(names=(name_key("Janarthanan", "Rajendran"),), orcids=frozenset(), dblp="x/1")
    typed = (author("Shagun Sodhani"), author("Janarthanan Janarthanan"), author("Sarath Chandar"))
    arx = record("arxiv:2207.04354", source=Source.ARXIV, kind=Kind.PREPRINT, authors=typed)
    listed = (author("Shagun Sodhani"), author("Janarthanan Rajendran"), author("Sarath Chandar"))
    dblp = record("arxiv:2207.04354", source=Source.DBLP, kind=Kind.PREPRINT, authors=listed)
    assert verdict(i, work(arx), [claim(Source.DBLP, dblp)]) == (Verdict.ACCEPT, "dblp_listed")
    assert verdict(i, work(arx), [claim(Source.ORCID)]) == (Verdict.REVIEW, "not_named_unregistered")
    assert verdict(i, work(arx), [claim(Source.OPENALEX, dblp)]) == (Verdict.REJECT, "not_named")
    registered = record("doi:10.1234/w", authors=typed)
    assert verdict(i, work(registered), [claim(Source.ORCID)]) == (Verdict.REJECT, "not_named")


def test_a_present_orcid_decides_whether_a_slot_is_the_member() -> None:
    i = identity()
    assert is_own(i, author("Gabriel Spadon"))
    assert is_own(i, author("Someone Else", orcid=ME))
    assert not is_own(i, author("Gabriel Spadon", orcid=OTHER))
    assert is_own(i, author("G. Other", openalex="A5023888391"))
    slots = own_slots(i, w(author("Gabriel Spadon", orcid=OTHER), author("Stan Matwin")), [])
    assert slots == []


def test_a_titled_or_swapped_byline_is_the_member_only_on_a_listed_work() -> None:
    milios = identity(names=(name_key("Evangelos", "Milios"),), orcids=frozenset(), rors=frozenset())
    chapter = w(author("Sanjukta Goswami"), Author("Dr. Evangelos E.", "Milios"))
    assert verdict(milios, chapter, [claim(Source.ORCID)]) == (Verdict.ACCEPT, "orcid_claim")
    he = identity(names=(name_key("Meng", "He"),), orcids=frozenset(), rors=frozenset())
    swapped = w(author("Chuan He"), Author("He", "Meng"))
    assert verdict(he, swapped, [claim(Source.ORCID)]) == (Verdict.ACCEPT, "orcid_claim")
    assert verdict(he, swapped) == (Verdict.REJECT, "not_named")
