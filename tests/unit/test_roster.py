from pathlib import Path

import pytest

from citeforge import curated
from citeforge.roster import COLUMNS, RosterError, format_author_dirname, load, load_group

REPO = Path(__file__).resolve().parents[2]
HEADER = ",".join(COLUMNS)


def write(tmp_path: Path, *rows: str, header: str = HEADER) -> Path:
    path = tmp_path / "input.csv"
    path.write_text("\n".join((header, *rows)) + "\n", encoding="utf-8")
    return path


def _committed(strict: bool) -> None:
    members = load(REPO / "data/input.csv", strict=strict)
    assert members and len({m.dirname.casefold() for m in members}) == len(members)
    group = load_group(REPO / "data/a2i2.csv", members)
    assert group and set(group) <= set(members)
    curated.load(REPO / "data/curated.toml").check_members(members)


def test_committed_data_loads_for_identify() -> None:
    _committed(strict=False)


@pytest.mark.xfail(raises=RosterError, strict=True, reason="data/input.csv gains ORCID and OpenAlex with the seeding")
def test_committed_data_loads_for_a_build() -> None:
    _committed(strict=True)


def test_dirname_fallbacks() -> None:
    assert format_author_dirname("Alex Brodsky", "b/AlexBrodsky") == "Brodsky (b-AlexBrodsky)"
    assert format_author_dirname("Doe, Jane", "") == "Doe"
    assert format_author_dirname("", "42-3005") == "42-3005"
    assert format_author_dirname("..", "") == "unknown"
    assert format_author_dirname("A/B", "x") == "A-B (x)"
    assert format_author_dirname("Robert Smith Jr", "abcdefXYZ") == "Smith (abcdefXYZ)"
    assert format_author_dirname("Martin Luther King III", "abcdefXYZ") == "King (abcdefXYZ)"
    assert format_author_dirname("Ana Unknown", "x") == "Unknown (x)"
    assert format_author_dirname("Unknown", "") == "Unknown"
    assert format_author_dirname(None, "") == "unknown"


def test_load_valid_roster(tmp_path: Path) -> None:
    path = write(
        tmp_path,
        "Gabriel Spadon,https://scholar.google.com/citations?user=bfdGsGUAAAAJ,https://dblp.org/pid/192/1659,"
        "0000-0002-1825-0097,A5023888391;A5000000001,true,",
        "Alex Brodsky,,https://dblp.org/pid/b/AlexBrodsky,,,true,",
        "James Fleming,,,,,false,No profile configured",
    )
    spadon, brodsky = load(path)
    assert spadon.dirname == "Spadon (bfdGsGUAAAAJ)"
    assert spadon.orcids == ("0000-0002-1825-0097",)
    assert spadon.openalex == ("A5023888391", "A5000000001")
    assert brodsky.dirname == "Brodsky (b-AlexBrodsky)"


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("Ann Lee,https://scholar.google.com/citations?user=abcdefgh,,,,true,", "needs an ORCID or a DBLP pid"),
        ("Ann Lee,,,0000-0002-1825-0098,,true,", "ORCID value"),
        ("Ann Lee,,,0000-0002-1825-0097,W123456,true,", "OpenAlex value"),
        ("Ann Lee,,,0000-0002-1825-0097,,yes,", "Enabled must be"),
        ("Ann Lee,,,,,false,", "requires an Exclusion Reason"),
        ("Ann Lee,,https://dblp.org/pid/1/2,,,true,why", "cannot have an Exclusion Reason"),
        (",,https://dblp.org/pid/1/2,,,true,", "Name must not be empty"),
        ("Ann Lee,http://scholar.google.com/citations?user=abcdefgh,https://dblp.org/pid/1/2,,,true,", "HTTPS"),
        ("Ann Lee,,https://dblp.org/search?q=x,,,true,", "DBLP Link"),
        ("Ann Lee,,https://dblp.org/pid/1/2.exe,,,true,", "unsupported profile suffix"),
        ("Ann lee@x.org,,https://dblp.org/pid/1/2,,,true,", "contact data"),
        ("Ann Lee,,https://dblp.org/pid/1/2,,true,", "expected 7 columns"),
    ],
)
def test_bad_rows_abort(tmp_path: Path, row: str, message: str) -> None:
    with pytest.raises(RosterError, match=message):
        load(write(tmp_path, row))


def test_duplicates_abort(tmp_path: Path) -> None:
    with pytest.raises(RosterError, match="duplicate ORCID"):
        load(write(tmp_path, "A B,,,0000-0002-1825-0097,,true,", "C D,,,0000-0002-1825-0097,,true,"))
    with pytest.raises(RosterError, match="duplicate OpenAlex"):
        load(
            write(
                tmp_path,
                "A B,,https://dblp.org/pid/1/2,,A1234567,true,",
                "C D,,https://dblp.org/pid/1/3,,A1234567,true,",
            )
        )
    with pytest.raises(RosterError, match="missing required columns: ORCID, OpenAlex"):
        load(write(tmp_path, "A,,,true,", header="Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason"))


def test_group_needs_half_the_members(tmp_path: Path) -> None:
    members = load(
        write(tmp_path, "Ann Lee,,https://dblp.org/pid/1/2,,,true,", "Bo Kim,,https://dblp.org/pid/1/3,,,true,")
    )
    group_path = tmp_path / "a2i2.csv"
    group_path.write_text("Name,Scholar Link,DBLP Link\nANN  LEE,,\nNobody,,\n", encoding="utf-8")
    assert [m.name for m in load_group(group_path, members)] == ["Ann Lee"]
    group_path.write_text("Name\nNobody\nNo One\nAnn Lee\n", encoding="utf-8")
    with pytest.raises(RosterError, match="only 1 of 3"):
        load_group(group_path, members)
