from citeforge.model import Author, Kind, Member, Record, Source, Status, Work


def author(name: str, **kw: object) -> Author:
    given, _, family = name.rpartition(" ")
    return Author(given, family, **kw)  # type: ignore[arg-type]


def record(
    *ids: str,
    title: str = "Graph neural networks for vessel trajectory forecasting",
    authors: tuple[Author, ...] = (),
    source: Source = Source.CROSSREF,
    kind: Kind = Kind.ARTICLE,
    year: int | None = 2023,
    **kw: object,
) -> Record:
    return Record(
        source=source,
        ids=frozenset(ids),
        kind=kind,
        source_type=kw.pop("source_type", "journal-article"),  # type: ignore[arg-type]
        title=title,
        authors=authors or (author("Gabriel Spadon"), author("Stan Matwin")),
        year=year,
        dates=(year,) if year else (),
        **kw,  # type: ignore[arg-type]
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


def member(name: str = "Gabriel Spadon", dirname: str = "Spadon (bfdGsGUAAAAJ)", **kw: object) -> Member:
    fields: dict[str, object] = {"row": 41, "scholar": "bfdGsGUAAAAJ", "dblp": "192/1659", "orcids": (), "openalex": ()}
    fields.update(kw)
    return Member(name=name, dirname=dirname, **fields)  # type: ignore[arg-type]
