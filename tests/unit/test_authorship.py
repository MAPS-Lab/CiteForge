import dataclasses
from typing import Any

from citeforge.authorship import decide
from citeforge.curated import Curated
from citeforge.model import Author, Claim, Identity, Kind, Source, Verdict
from citeforge.text import name_key
from tests.factories import author, member, record, work

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
    via_claim = claim(Source.OPENALEX, record("doi:10.1234/w", authors=(author("G. Spadon", orcid=ME),)))
    assert verdict(identity(), w(), [via_claim]) == (Verdict.ACCEPT, "authorship_orcid")


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


def test_dblp_primary_and_curated_record_are_registries_enough() -> None:
    dblp_only = work(record("dblp:conf/x/S23", source=Source.DBLP, kind=Kind.INPROCEEDINGS))
    assert verdict(identity(), dblp_only, [claim(Source.DBLP)]) == (Verdict.ACCEPT, "dblp_listed")
