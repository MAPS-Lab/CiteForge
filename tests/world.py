import dataclasses
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from citeforge.http import Batch, Result
from citeforge.model import Author, Claim, Fetch, Kind, Record, Rel, Source
from citeforge.sources import arxiv, crossref, datacite, dblp, doiorg, openalex, orcid
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import Affiliation, OrcidRecord
from tests.factories import record

SPADON_ORCID = "0000-0002-1825-0097"
YE_ORCID = "0000-0001-5109-3700"
STRANGER_ORCID = "0000-0002-1694-233X"
SPADON = "Spadon (bfdGsGUAAAAJ)"
MATWIN = "Matwin (rCoJeuYAAAAJ)"
YE = "Ye (4OQaVGUAAAAJ)"
LAB = "https://ror.org/01e6qks80"
INPUT = (
    "Name,Scholar Link,DBLP Link,ORCID,OpenAlex,Enabled,Exclusion Reason\n"
    f"Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,"
    f"{SPADON_ORCID},A5023888391,true,\n"
    "Stan Matwin,https://scholar.google.com/citations?user=rCoJeuYAAAAJ,https://dblp.org/pid/m/StanMatwin,,,true,\n"
    f"Xiang Ye,https://scholar.google.com/citations?user=4OQaVGUAAAAJ,,{YE_ORCID},,true,\n"
    "Nobody Here,,,,,false,No profile configured\n"
)
GROUP = "Name,Scholar Link,DBLP Link\nStan Matwin,,\nGabriel Spadon,,\n"


def person(name: str, orcid_id: str | None = None, **kw) -> Author:
    given, _, family = name.rpartition(" ")
    return Author(given, family, orcid=orcid_id, **kw)


SP = person("Gabriel Spadon", SPADON_ORCID)
SP_OA = dataclasses.replace(SP, openalex="A5023888391", rors=(LAB,))
MW = person("Stan Matwin")
YE_AUTHOR = person("Xiang Ye", YE_ORCID)


def cr(doi: str, title: str, authors: tuple[Author, ...], year: int = 2024, **kw: Any) -> Record:
    fields: dict[str, Any] = {"container": ("Ocean Engineering",), "publisher": "Elsevier BV", "volume": "300",
                              "pages": "1-10", **kw}  # fmt: skip
    return record(doi, title=title, authors=authors, year=year, **fields)


@dataclasses.dataclass
class World:
    crossref: dict[str, Record]
    arxiv: dict[str, Record]
    orcid_works: dict[str, list[Claim]]
    dblp_claims: dict[str, list[Claim]]
    openalex_claims: list[Claim]
    crossref_down: bool = False
    dblp_challenge: bool = False
    calls: Counter = dataclasses.field(default_factory=Counter)


def default_world() -> World:
    gnn = "doi:10.1016/j.oceaneng.2024.117234"
    records = {
        gnn: cr(
            gnn,
            "Physics-informed graph networks for AIS vessel trajectory forecasting",
            (SP, MW),
            relations=frozenset({(Rel.HAS_PREPRINT, "arxiv:2310.18948")}),
        ),
        "doi:10.1234/namesake": cr(
            "doi:10.1234/namesake",
            "Soil bacteria in tropical farms",
            (person("Gabriel Spadon", STRANGER_ORCID),),
            year=2023,
        ),
        "doi:10.1234/editorial": cr("doi:10.1234/editorial", "Editorial: Maritime data science", (SP,)),
        "doi:10.1234/old": cr("doi:10.1234/old", "An old result on vessel routing", (SP,), year=2018),
        "doi:10.1234/ye": cr("doi:10.1234/ye", "Wireless channel estimation with transformers", (YE_AUTHOR,)),
        "doi:10.1234/wrong": cr("doi:10.1234/wrong", "Protein folding with diffusion models", (person("Ana Lima"),)),
    }
    for n in range(6):
        doi = f"doi:10.1234/bulk{n}"
        records[doi] = cr(doi, f"Ocean analog study number {n} on sea ice", (SP,), year=2022)
    preprint = Record(
        Source.ARXIV,
        frozenset({"arxiv:2310.18948"}),
        Kind.PREPRINT,
        "arxiv",
        "Physics informed graph networks for AIS vessel trajectory forecasting",
        authors=(SP, MW),
        year=2023,
        dates=(2023,),
        primary_class="cs.LG",
        url="https://arxiv.org/abs/2310.18948",
    )

    def oclaim(work_id: str, title: str, year: int = 2024) -> Claim:
        return Claim(Source.ORCID, frozenset({work_id}), title, year, by_self=True)

    dblp_conf = Record(
        Source.DBLP,
        frozenset({"dblp:conf/kdd/SpadonM23"}),
        Kind.INPROCEEDINGS,
        "inproceedings",
        "Maritime traffic graphs at scale",
        authors=(SP, MW),
        year=2023,
        dates=(2023,),
        container=("KDD",),
    )
    dblp_gnn = Record(
        Source.DBLP,
        frozenset({gnn, "dblp:journals/oe/SpadonM24"}),
        Kind.ARTICLE,
        "article",
        records[gnn].title,
        authors=(SP, MW),
        year=2024,
        dates=(2024,),
        container=("Ocean Eng.",),
    )
    dclaims = [Claim(Source.DBLP, r.ids, r.title, r.year, record=r) for r in (dblp_conf, dblp_gnn)]
    oa_namesake = Record(
        Source.OPENALEX,
        frozenset({"doi:10.1234/namesake", "openalex:W77"}),
        Kind.ARTICLE,
        "article",
        "Soil bacteria in tropical farms",
        authors=(dataclasses.replace(person("Gabriel Spadon", STRANGER_ORCID), openalex="A5023888391"),),
        year=2023,
        dates=(2023,),
    )
    oa_gnn = Record(
        Source.OPENALEX,
        frozenset({gnn, "openalex:W1"}),
        Kind.ARTICLE,
        "article",
        records[gnn].title,
        authors=(SP_OA, MW),
        year=2024,
        dates=(2024,),
    )
    spadon_orcid = [
        oclaim(gnn, records[gnn].title),
        oclaim("doi:10.1234/editorial", "Editorial: Maritime data science"),
        oclaim("doi:10.1234/old", "An old result on vessel routing", 2018),
        oclaim("doi:10.1234/wrong", "A totally different paper about ships"),
        *(oclaim(f"doi:10.1234/bulk{n}", f"Ocean analog study number {n} on sea ice", 2022) for n in range(6)),
    ]
    return World(
        crossref=records,
        arxiv={"arxiv:2310.18948": preprint},
        orcid_works={
            SPADON_ORCID: spadon_orcid,
            YE_ORCID: [oclaim("doi:10.1234/ye", "Wireless channel estimation with transformers")],
        },
        dblp_claims={"192/1659": dclaims, "m/StanMatwin": dclaims},
        openalex_claims=[Claim(Source.OPENALEX, r.ids, r.title, r.year, record=r) for r in (oa_namesake, oa_gnn)],
    )


def install(monkeypatch: pytest.MonkeyPatch, world: World) -> None:
    def count(name: str) -> None:
        world.calls[name] += 1

    def orcid_record(http, orcid_id):
        count("orcid.record")
        names = {SPADON_ORCID: ("Gabriel", "Spadon"), YE_ORCID: ("Xiang", "Ye")}[orcid_id]
        affiliations = (Affiliation(LAB, "Dalhousie University", "employment"),)
        return Result(Fetch.OK, OrcidRecord(*names, None, (), affiliations, tuple(world.orcid_works[orcid_id])))

    def dblp_person(http, pid):
        count("dblp.person")
        if world.dblp_challenge:
            return Result(Fetch.FAILED, None, "challenge")
        name = {"192/1659": "Gabriel Spadon", "m/StanMatwin": "Stan Matwin"}[pid]
        return Result(Fetch.OK, DblpPerson((name,), (), tuple(world.dblp_claims[pid])))

    def authors_by_orcid(http, orcids):
        count("openalex.authors_by_orcid")
        return Result(Fetch.OK, {o: ("A5023888391",) for o in orcids if o == SPADON_ORCID})

    def author(http, aid):
        count("openalex.author")
        return Result(Fetch.OK, (aid, SPADON_ORCID))

    def list_works(http, ids, since):
        count("openalex.list_works")
        return Result(Fetch.OK, tuple(c for c in world.openalex_claims if (c.year or 0) >= since))

    def batch(name: str, table: dict[str, Record], down: Callable[[], bool] = lambda: False):
        def fn(http, ids: Sequence[str]) -> Batch:
            count(name)
            ids = list(ids)
            if down():
                return Batch({}, frozenset(), frozenset(ids))
            return Batch(
                {i: table[i] for i in ids if i in table}, frozenset(i for i in ids if i not in table), frozenset()
            )

        return fn

    monkeypatch.setattr(orcid, "record", orcid_record)
    monkeypatch.setattr(dblp, "person", dblp_person)
    monkeypatch.setattr(openalex, "authors_by_orcid", authors_by_orcid)
    monkeypatch.setattr(openalex, "author", author)
    monkeypatch.setattr(openalex, "list_works", list_works)
    monkeypatch.setattr(openalex, "works_by_doi", batch("openalex.works_by_doi", {}))
    monkeypatch.setattr(crossref, "by_doi", batch("crossref.by_doi", world.crossref, lambda: world.crossref_down))
    monkeypatch.setattr(crossref, "search", lambda http, t, f, y: (count("crossref.search"), Result(Fetch.OK, ()))[1])
    monkeypatch.setattr(datacite, "by_doi", batch("datacite.by_doi", {}))
    monkeypatch.setattr(
        doiorg,
        "agency",
        lambda http, ids: (count("doiorg.agency"), Result(Fetch.OK, dict.fromkeys(ids, "Crossref")))[1],
    )
    monkeypatch.setattr(doiorg, "csl", lambda http, i: Result(Fetch.FAILED, None, "unused"))
    monkeypatch.setattr(arxiv, "by_id", batch("arxiv.by_id", world.arxiv))


def repo(tmp_path: Path, input_csv: str = INPUT) -> Path:
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "data/input.csv").write_text(input_csv, encoding="utf-8")
    (tmp_path / "data/a2i2.csv").write_text(GROUP, encoding="utf-8")
    (tmp_path / "data/curated.toml").write_text(f'[lab]\nror = ["{LAB}"]\n', encoding="utf-8")
    return tmp_path


def tree(root: Path) -> dict[str, bytes]:
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}
