import dataclasses
import re
from pathlib import Path

import bibtexparser
import pytest
from bibtexparser.bparser import BibTexParser
from hypothesis import given, settings
from hypothesis import strategies as st
from pylatexenc.latex2text import LatexNodes2Text

from citeforge import publish
from citeforge.bibtex import assign_names, escape, preprint_server, protect, read_entry, render
from citeforge.ids import is_preprint_doi
from citeforge.model import Author, Kind, Names, Source
from citeforge.tables import PREPRINT_SERVERS
from citeforge.text import clean
from tests.factories import author, bib_problems, record, work

printable = st.text(
    st.characters(categories=["L", "N", "P", "S"], exclude_characters="\\\u02c6"), min_size=1, max_size=40
).map(lambda s: s.strip())
LIGATURES = ("--", "``", "''", "!`", "?`")
_latex = LatexNodes2Text()


def parse(text: str) -> dict[str, str]:
    parser = BibTexParser(common_strings=True)
    parser.ignore_nonstandard_types = False
    (entry,) = bibtexparser.loads(text, parser=parser).entries
    return entry


def split_authors(value: str) -> list[str]:
    depth, parts, cur = 0, [], ""
    tokens = re.split(r"(\{|\}| and )", value)
    for tok in tokens:
        if tok == "{":
            depth += 1
        elif tok == "}":
            depth -= 1
        if tok == " and " and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += tok
    return [*parts, cur]


@given(printable.filter(lambda s: not any(lig in s for lig in LIGATURES)))
def test_escape_round_trips_through_latex(s: str) -> None:
    # TeX typesets these ligatures as dashes and quotes, which is the intended reading of such titles.
    assert _latex.latex_to_text(escape(s, "title")).replace("\u02c6", "^") == s


@given(st.text(st.characters(categories=["L", "N", "Zs"], include_characters="-"), max_size=40))
def test_protect_only_adds_braces(t: str) -> None:
    assert protect(t).replace("{", "").replace("}", "") == t


def test_protect_cases() -> None:
    assert protect("COVID-19 and mRNA in R-index of iPhone users") == (
        "{COVID-19} and {mRNA} in {R}-index of {iPhone} users"
    )
    assert protect("Physics-Informed models, SAR.") == "Physics-Informed models, {SAR}."
    assert protect("A study") == "A study"


def test_escape_fields() -> None:
    assert escape("10.1000/a_b{c}", "doi") == "10.1000/a_b{c}"
    assert escape("https://x.org/a{b}", "url") == "https://x.org/a%7Bb%7D"
    assert escape("50% & $5 #1 a_b ~ ^", "title") == r"50\% \& \$5 \#1 a\_b \textasciitilde{} \textasciicircum{}"


def test_render_article_shape_and_website_parse() -> None:
    r = record(
        "doi:10.1016/j.oceaneng.2024.118303",
        title="Enhancing short-term vessel trajectory prediction with AIS &amp; SAR",
        authors=(
            author("Md Mahbub Alam"),
            author("Gabriel Spadon"),
            Author("", "", literal="The ATLAS Collaboration"),
            Author("Ana", "Pérez García"),
            Author("Luis", "da Silva"),
        ),
        container=("Ocean Engineering",),
        publisher="Elsevier BV",
        volume="308",
        pages="118303\u2013118310",
        url="https://example.org/ignored",
        year=2024,
    )
    text = render(work(r), Names("Alam2024-EnhancingShort", "Alam2024:EnhancingShortTermVessel"))
    assert text == (
        "@article{Alam2024:EnhancingShortTermVessel,\n"
        "  title = {Enhancing short-term vessel trajectory prediction with {AIS} \\& {SAR}},\n"
        "  author = {Md Mahbub Alam and Gabriel Spadon and {The ATLAS Collaboration} and Ana {Pérez García}"
        " and Luis da Silva},\n"
        "  year = {2024},\n"
        "  journal = {Ocean Engineering},\n"
        "  publisher = {Elsevier BV},\n"
        "  volume = {308},\n"
        "  pages = {118303--118310},\n"
        "  doi = {10.1016/j.oceaneng.2024.118303}\n"
        "}\n"
    )
    entry = parse(text)
    assert entry["ENTRYTYPE"] == "article"
    assert len(split_authors(entry["author"])) == 5
    assert read_entry(text)["doi"] == "10.1016/j.oceaneng.2024.118303"


def test_render_preprint_and_eprint() -> None:
    pre = record(
        "arxiv:2310.18948",
        source=Source.ARXIV,
        kind=Kind.PREPRINT,
        primary_class="cs.LG",
        url="https://arxiv.org/abs/2310.18948",
    )
    text = render(work(pre), Names("Spadon2023-X", "Spadon2023:X"))
    e = read_entry(text)
    assert e["ENTRYTYPE"] == "misc"
    assert e["howpublished"] == "arXiv"
    assert e["eprint"] == "2310.18948" and e["archiveprefix"] == "arXiv" and e["primaryclass"] == "cs.LG"
    assert e["url"] == "https://arxiv.org/abs/2310.18948"
    pub = record(
        "doi:10.1016/j.oceaneng.2024.117234",
        kind=Kind.INPROCEEDINGS,
        container=("Lecture Notes in Computer Science", "Discovery Science"),
        article_number="e12",
    )
    e = read_entry(render(work(pub, eprint=pre), Names("S", "S")))
    assert e["booktitle"] == "Discovery Science"
    assert e["eprint"] == "2310.18948"
    assert e["pages"] == "e12"
    assert "url" not in e and "howpublished" not in e


def test_invalid_pages_dropped() -> None:
    for pages in ("2025.11.07.685935", "e123", "123456789012"):
        assert "pages" not in read_entry(render(work(record("doi:10.1234/p", pages=pages)), Names("a", "a")))


@settings(max_examples=60)
@given(st.text(min_size=1, max_size=60), st.lists(st.text(max_size=20), min_size=1, max_size=4))
def test_every_render_parses(title: str, names: list[str]) -> None:
    authors = tuple(Author(n[:3], n[3:] or "X") for n in names)
    text = render(work(record("doi:10.1234/z", title=title, authors=authors)), Names("a", "Key2023:A"))
    entry = parse(text)
    assert entry["ID"] == "Key2023:A"
    named = [a for a in authors if clean(a.family) or clean(a.given)]
    if named:
        assert len(split_authors(entry["author"])) == len(named)
    else:
        assert "author" not in entry
    read_entry(text)


def test_blank_authors_are_skipped_and_checked() -> None:
    authors = (Author("   ", " "), Author("", "", literal=" "), Author("Gabriel", "Spadon"))
    text = render(work(record("doi:10.1234/z", authors=authors)), Names("a", "a"))
    assert read_entry(text)["author"] == "Gabriel Spadon"
    files = {Path("M (x)/a.bib"): text.replace("Gabriel Spadon", " and Gabriel Spadon")}
    assert "M (x)/a.bib: empty name in author" in publish.check(files)


def _w(doi: str, title: str, family: str = "Spadon", year: int = 2023):
    return work(record(f"doi:{doi}", title=title, authors=(Author("G", family),), year=year))


def test_assign_names_collisions() -> None:
    ws = [
        _w("10.1234/a", "Vessel trajectory prediction"),
        _w("10.1234/b", "Vessel trajectory clustering"),
        _w("10.1234/c", "Ocean analogs"),
        _w("10.1234/d", "Ocean analogs"),
        _w("10.1234/e", "Ocean analogs", family="Müller-Lüdenscheidt"),
        _w("10.1234/f", "Determining topic evolution and sentiment polarity for albinism in online health communities"),
        _w("10.1234/g", "Determining topic evolution and sentiment polarity for albinism in online health communities"),
    ]
    names = assign_names(ws, {})
    assert names["doi:10.1234/a"].stem == "Spadon2023-VesselTrajectoryPrediction"
    assert names["doi:10.1234/b"].stem == "Spadon2023-VesselTrajectoryClustering"
    assert names["doi:10.1234/c"].stem == "Spadon2023-OceanAnalogs"
    assert names["doi:10.1234/d"].stem == "Spadon2023-OceanAnalogsb"
    assert names["doi:10.1234/e"].stem == "MullerLudenscheidt2023-OceanAnalogs"
    assert names["doi:10.1234/f"].stem == "Spadon2023-DeterminingTopic"
    assert names["doi:10.1234/g"].stem == "Spadon2023-DeterminingTopicb"
    assert names["doi:10.1234/a"].citekey == "Spadon2023:VesselTrajectoryPrediction"


def test_citekeys_unique_when_titles_share_four_words() -> None:
    a = _w("10.1101/2024.03.06.24303776", "Genome-wide association study of hoarding symptoms", "Strom", 2024)
    ocd = "Genome-wide association study of obsessive-compulsive symptoms"
    b = _w("10.1038/s41380-024-02489-6", ocd, "Strom", 2024)
    c = _w("10.1101/2024.07.03.24309466", ocd, "Strom", 2024)
    names = assign_names([a, b, c], {})
    assert len({n.citekey.casefold() for n in names.values()}) == 3
    assert names[a.key].citekey == "Strom2024:GenomeWideAssociationStudyHoarding"
    shared = Names("Strom2024-Old", "Strom2024:GenomeWideAssociationStudy")
    kept = assign_names([a, b], {b.key: shared, a.key: dataclasses.replace(shared, stem="Strom2024-Older")})
    assert kept[b.key] == shared
    assert kept[a.key] == Names("Strom2024-Older", "Strom2024:GenomeWideAssociationStudyb")


@pytest.mark.parametrize(("prefix", "server"), PREPRINT_SERVERS)
def test_every_preprint_prefix_names_its_server(prefix: str, server: str) -> None:
    doi = prefix + "x1"
    r = record(f"doi:{doi}", kind=Kind.PREPRINT, container=("Some Container",))
    assert is_preprint_doi(doi)
    assert preprint_server(r) == ("arXiv" if prefix == "10.48550/arxiv" else server)


def test_assign_names_keeps_previous_until_base_changes() -> None:
    w = _w("10.1234/a", "Vessel trajectory prediction")
    prev = {"doi:10.1234/a": Names("Spadon2023-Old", "Spadon2023:Old")}
    assert assign_names([w], prev)["doi:10.1234/a"].stem == "Spadon2023-Old"
    moved = _w("10.1234/a", "Vessel trajectory prediction", year=2024)
    assert assign_names([moved], prev)["doi:10.1234/a"].stem == "Spadon2024-VesselTrajectory"


def test_assign_names_fallbacks_and_cap() -> None:
    group = work(
        record(
            "doi:10.1234/g", authors=(Author("", "", literal="COVID-19 Genomics UK Consortium"),), title="", year=None
        )
    )
    long = _w("10.1234/l", " ".join(["Supercalifragilistic"] * 10), family="Wolfeschlegelsteinhausenbergerdorff")
    names = assign_names([group, long], {})
    assert names["doi:10.1234/g"].stem == "Covid19Genomics0000"
    assert len(names["doi:10.1234/l"].stem) <= 60


titles = st.lists(
    st.sampled_from(["Ocean analogs", "Vessel tracks", "Vessel tracks at sea", "AIS", ""]), min_size=1, max_size=8
)


@given(titles, st.randoms())
def test_assign_names_unique_deterministic_stable(ts: list[str], rnd) -> None:
    ws = [_w(f"10.1234/{i}", t) for i, t in enumerate(ts)]
    names = assign_names(ws, {})
    stems = [n.stem.casefold() for n in names.values()]
    assert len(set(stems)) == len(stems)
    shuffled = ws[:]
    rnd.shuffle(shuffled)
    assert assign_names(shuffled, {}) == names
    unrelated = _w("10.1234/zz", "Unrelated", family="Other")
    assert {k: v for k, v in assign_names([*ws, unrelated], names).items() if k != unrelated.key} == names
    for n in names.values():
        assert re.fullmatch(r"[A-Za-z0-9]+-?[A-Za-z0-9]*", n.stem)


_texts = st.text(max_size=12)
_people = st.lists(st.builds(Author, _texts, _texts, st.none() | _texts), max_size=3).map(tuple)


@settings(max_examples=150, deadline=None)
@given(
    st.sampled_from(list(Kind)),
    st.fixed_dictionaries({
        f: st.none() | _texts
        for f in ("event", "publisher", "volume", "number", "pages", "article_number", "url", "language", "school",
                  "institution", "howpublished", "primary_class", "degree")
    }),
    _texts,
    st.lists(_texts, max_size=2).map(tuple),
    _people,
    _people,
    st.booleans(),
)  # fmt: skip
def test_any_record_renders_a_valid_entry(kind, fields, title, container, authors, editors, with_doi) -> None:
    ids = ("doi:10.1234/z", "arxiv:2310.18948") if with_doi else ("arxiv:2310.18948",)
    r = record(*ids, kind=kind, title=title or "T", container=container, editors=editors, **fields)
    r = dataclasses.replace(r, authors=authors)
    assert bib_problems(r) == []
