import re
from collections.abc import Iterable, Mapping, Sequence

from citeforge.ids import split_id
from citeforge.model import Author, Kind, Names, Record, Work
from citeforge.tables import (
    GENERIC_SERIES_NAMES,
    PREPRINT_ONLY_PUBLISHERS,
    PREPRINT_SERVERS,
    PUBLISHER_CORRECTIONS,
    VENUE_CASE_CORRECTIONS,
)
from citeforge.text import ascii_family, clean, fold, is_group, recase_title, slug_words

FIELD_ORDER = (
    "title", "author", "year", "journal", "booktitle", "howpublished", "publisher", "volume", "number", "pages",
    "doi", "url", "eprint", "archiveprefix", "primaryclass", "editor", "school", "institution", "language",
)  # fmt: skip
ENTRY_TYPES: Mapping[Kind, str] = {
    Kind.ARTICLE: "article",
    Kind.INPROCEEDINGS: "inproceedings",
    Kind.INCOLLECTION: "incollection",
    Kind.BOOK: "book",
    Kind.PHDTHESIS: "phdthesis",
    Kind.MASTERSTHESIS: "mastersthesis",
    Kind.TECHREPORT: "techreport",
}
STEM_MAX = 60
FAMILY_MAX = 40
PAGES_MAX_DIGITS = 8
_ESCAPES = {
    "\\": r"\textbackslash{}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
}
_ESCAPE_RE = re.compile(r"[\\~^&%$#_{}]")
_PAGE_DASH = re.compile(r"\s*[-‐-―−]+\s*")  # noqa: RUF001
_PAGE_PART_SPLIT = re.compile(r"[-–—,\s]+")  # noqa: RUF001
_PARTICLES = frozenset({"de", "da", "do", "dos", "das", "van", "von", "der", "den", "du", "la", "le", "del", "di"})
_FIELD_LINE = re.compile(r"  ([a-z]+) = \{(.*)\},?")
_HEAD = re.compile(r"@([a-z]+)\{([^,\s]+),")


def escape(value: str, field: str) -> str:
    if field == "doi":
        return value
    if field == "url":
        return value.replace("{", "%7B").replace("}", "%7D")
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(0)], value)


def _needs_brace(segment: str) -> bool:
    letters = [c for c in segment if c.isalpha()]
    caps = sum(c.isupper() for c in letters)
    inner = any(c.isupper() and segment[i - 1].isalpha() for i, c in enumerate(segment) if i > 0)
    return caps >= 2 or inner


def _protect_token(token: str) -> str:
    m = re.fullmatch(r"([^\w\\]*)(.*?)([^\w}]*)", token)
    if not m or not m.group(2):
        return token
    lead, core, tail = m.groups()
    segments = core.split("-")
    if any(_needs_brace(s) for s in segments):
        return f"{lead}{{{core}}}{tail}"
    if len(segments) > 1:
        core = "-".join(f"{{{s}}}" if len(s) == 1 and s.isupper() else s for s in segments)
    return f"{lead}{core}{tail}"


def protect(title: str) -> str:
    return re.sub(r"\S+", lambda m: _protect_token(m.group(0)), title)


def first_non_generic_container(values: Iterable[str]) -> str | None:
    for candidate in values:
        text = candidate.strip()
        if text and text.lower() not in GENERIC_SERIES_NAMES:
            return text
    return None


def _invalid_pages_reason(pages: str) -> tuple[str, int] | None:
    if not re.match(r"^\d", pages):
        return ("no_leading_digit", 0)
    if "." in pages:
        return ("contains_dot", 0)
    parts = _PAGE_PART_SPLIT.split(pages)
    overflow = [p for p in parts if p.strip() and len(re.sub(r"\D", "", p)) > PAGES_MAX_DIGITS]
    if overflow:
        return ("overflow", len(re.sub(r"\D", "", overflow[0])))
    return None


def format_pages(r: Record) -> str | None:
    if r.pages and _invalid_pages_reason(r.pages.strip()) is None:
        parts = [p for p in _PAGE_DASH.split(r.pages.strip()) if p]
        if len(parts) == 2 and parts[0] == parts[1]:
            return parts[0]
        return "--".join(parts)
    if r.article_number and re.fullmatch(r"[A-Za-z]?\d+[A-Za-z]?", r.article_number.strip()):
        return r.article_number.strip()
    return None


def _name(a: Author) -> str:
    if not a.family:
        return "{" + escape(clean(a.literal or ""), "author") + "}"
    family = escape(clean(a.family), "author")
    words = family.split()
    if "," in family or (len(words) > 1 and not any(w in _PARTICLES for w in words)):
        family = "{" + family + "}"
    given = escape(clean(a.given), "author")
    return f"{given} {family}" if given else family


def _names(authors: Sequence[Author]) -> str | None:
    return " and ".join(_name(a) for a in authors) or None


def venue_case(venue: str) -> str:
    return VENUE_CASE_CORRECTIONS.get(venue, recase_title(venue))


def _publisher(r: Record, journal: str | None) -> str | None:
    if journal:
        lower = journal.lower()
        for needle, publisher in PUBLISHER_CORRECTIONS.items():
            if needle in lower:
                return publisher
    if r.publisher and (r.kind is Kind.PREPRINT or r.publisher.strip().lower() not in PREPRINT_ONLY_PUBLISHERS):
        return clean(r.publisher)
    return None


def preprint_server(r: Record) -> str | None:
    if r.howpublished:
        return clean(r.howpublished)
    for work_id in sorted(r.ids):
        kind, value = split_id(work_id)
        if kind == "arxiv":
            return "arXiv"
        for prefix, server in PREPRINT_SERVERS:
            if kind == "doi" and value.startswith(prefix):
                return server
    return first_non_generic_container(r.container)


def _first(ids: frozenset[str], kind: str) -> str | None:
    values = sorted(v for k, v in map(split_id, ids) if k == kind)
    return values[0] if values else None


def fields(w: Work) -> dict[str, str]:
    r = w.primary
    container = first_non_generic_container(clean(c) for c in r.container)
    journal = booktitle = howpublished = None
    if w.kind is Kind.ARTICLE:
        journal = container
    elif w.kind in (Kind.INPROCEEDINGS, Kind.INCOLLECTION):
        booktitle = container or (clean(r.event) if r.event else None)
    elif w.kind is Kind.PREPRINT:
        howpublished = preprint_server(r)
    arxiv = _first(w.eprint.ids if w.eprint else r.ids, "arxiv")
    primary_class = (w.eprint or r).primary_class
    doi = _first(r.ids, "doi")
    out = {
        "title": protect(escape(recase_title(clean(r.title)), "title")),
        "author": _names(r.authors),
        "year": str(w.year) if w.year is not None else None,
        "journal": venue_case(journal) if journal else None,
        "booktitle": venue_case(booktitle) if booktitle else None,
        "howpublished": howpublished,
        "publisher": None if w.kind is Kind.PREPRINT else _publisher(r, journal),
        "volume": clean(r.volume) if r.volume else None,
        "number": clean(r.number) if r.number else None,
        "pages": format_pages(r),
        "doi": doi,
        "url": None if doi or not r.url else r.url.strip(),
        "eprint": arxiv,
        "archiveprefix": "arXiv" if arxiv else None,
        "primaryclass": primary_class if arxiv else None,
        "editor": _names(r.editors),
        "school": clean(r.school) if r.school and w.kind in (Kind.PHDTHESIS, Kind.MASTERSTHESIS) else None,
        "institution": clean(r.institution) if r.institution and w.kind is Kind.TECHREPORT else None,
        "language": clean(r.language) if r.language else None,
    }
    escaped_already = {"title", "author", "editor"}
    return {k: v if k in escaped_already else escape(v, k) for k, v in out.items() if v is not None and v.strip()}


def entry_type(kind: Kind) -> str:
    return ENTRY_TYPES.get(kind, "misc")


def render(w: Work, names: Names) -> str:
    values = fields(w)
    body = ",\n".join(f"  {k} = {{{values[k]}}}" for k in FIELD_ORDER if k in values)
    return f"@{entry_type(w.kind)}{{{names.citekey},\n{body}\n}}\n"


def _family_part(w: Work) -> str:
    for a in w.primary.authors:
        if a.family and (family := ascii_family(a.family)):
            return family[:FAMILY_MAX]
    for a in w.primary.authors:
        if a.literal and is_group(a.literal):
            words = [p[:1].upper() + p[1:] for p in fold(a.literal).split()[:3]]
            if words:
                return "".join(words)[:20]
    return "anon"


def base_of(w: Work) -> str:
    return f"{_family_part(w)}{w.year if w.year is not None else '0000'}"


def _stem_base(stem: str) -> str:
    return stem.split("-", 1)[0]


def assign_names(works: Sequence[Work], previous: Mapping[str, Names]) -> dict[str, Names]:
    ordered = sorted(works, key=lambda w: w.key)
    result: dict[str, Names] = {}
    taken: set[str] = set()
    for w in ordered:
        prev = previous.get(w.key)
        if prev and _stem_base(prev.stem) == base_of(w) and prev.stem.casefold() not in taken:
            result[w.key] = prev
            taken.add(prev.stem.casefold())
    pending = [w for w in ordered if w.key not in result]
    words = {w.key: slug_words(w.primary.title) for w in pending}
    by_base: dict[str, list[Work]] = {}
    for w in pending:
        by_base.setdefault(base_of(w), []).append(w)
    for base, group in by_base.items():
        for w in group:
            mine = words[w.key]
            n = min(2, len(mine))
            rivals = [words[o.key] for o in group if o is not w and words[o.key] != mine]
            while n < len(mine) and any(r[:n] == mine[:n] for r in rivals):
                n += 1
            root = f"{base}-{''.join(mine[:n])}" if mine else base
            root = root[:STEM_MAX]
            stem, suffix = root, ord("b")
            while stem.casefold() in taken:
                tail = chr(suffix) if suffix <= ord("z") else str(suffix - ord("a"))
                stem = root[: STEM_MAX - len(tail)] + tail
                suffix += 1
            taken.add(stem.casefold())
            result[w.key] = Names(stem, f"{base}:{''.join(mine[:4])}" if mine else base)
    return result


def read_entry(text: str) -> dict[str, str]:
    lines = text.split("\n")
    head = _HEAD.fullmatch(lines[0]) if lines else None
    if head is None or lines[-2:] != ["}", ""]:
        raise ValueError("not a CiteForge BibTeX entry")
    entry = {"ENTRYTYPE": head.group(1), "ID": head.group(2)}
    body = lines[1:-2]
    for n, line in enumerate(body):
        m = _FIELD_LINE.fullmatch(line)
        if m is None or line.endswith(",") == (n == len(body) - 1):
            raise ValueError(f"malformed field line {line!r}")
        if m.group(1) in entry:
            raise ValueError(f"field {m.group(1)} repeats")
        entry[m.group(1)] = m.group(2)
    return entry
