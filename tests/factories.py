from pathlib import Path
from typing import Any

from citeforge import publish
from citeforge.bibtex import render
from citeforge.model import Author, Kind, Member, Names, Record, Source, Status, Work


def author(name: str, **kw: Any) -> Author:
    given, _, family = name.rpartition(" ")
    return Author(given, family, **kw)


def record(
    *ids: str,
    title: str = "Graph neural networks for vessel trajectory forecasting",
    authors: tuple[Author, ...] = (),
    source: Source = Source.CROSSREF,
    kind: Kind = Kind.ARTICLE,
    year: int | None = 2023,
    **kw: Any,
) -> Record:
    return Record(
        source=source,
        ids=frozenset(ids),
        kind=kind,
        source_type=kw.pop("source_type", "journal-article"),
        title=title,
        authors=authors or (author("Gabriel Spadon"), author("Stan Matwin")),
        year=year,
        dates=(year,) if year else (),
        **kw,
    )


def work(r: Record, *, eprint: Record | None = None, key: str | None = None, excluded: str | None = None) -> Work:
    return Work(
        key=key or min(r.ids),
        primary=r,
        eprint=eprint,
        linked=frozenset(),
        kind=r.kind,
        status=Status.OK,
        year=r.year,
        excluded=excluded,
        provenance=(),
    )


def member(name: str = "Gabriel Spadon", dirname: str = "Spadon (bfdGsGUAAAAJ)", **kw: Any) -> Member:
    fields: dict[str, Any] = {"row": 41, "scholar": "bfdGsGUAAAAJ", "dblp": "192/1659", "orcids": (), "openalex": ()}
    fields.update(kw)
    return Member(name=name, dirname=dirname, **fields)


def bib_problems(r: Record) -> list[str]:
    files = {Path("M (x)/Key2023-A.bib"): render(work(r), Names("Key2023-A", "Key2023:A"))}
    return [p for p in publish.check(files) if not p.startswith(("summary.csv", "baseline.json"))]
