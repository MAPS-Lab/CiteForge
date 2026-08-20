"""Contracts for PubMed-style "Surname Initials" author lists.

PubMed and the biomedical sources mirroring it emit "Jin Y" and "Koller JM",
where the trailing token is initials. Every other source emits "Given Surname".
Read one name at a time the two forms are indistinguishable, because "Jin Y" and
"Meng He" have the same shape and "He", "Li", "Lv" and "Du" are real surnames,
so the detection is deliberately a property of the whole list.

Getting this wrong produced the citation key ``Y2026:...`` and the filename
``Y2026-AlteredNeurodevelopmental.bib`` for a paper by Jin, and title-cased
"Koller JM" into "Koller Jm", which reads as a surname "Jm".
"""

from __future__ import annotations

from pathlib import Path

import pytest

from citeforge.bibtex_utils import _first_author_lastname, make_bibkey
from citeforge.merge_utils import _fix_author_casing
from citeforge.models import Record
from citeforge.pipeline import postrun
from citeforge.pipeline.postrun import _reconcile_author_prefix, finalize_run
from citeforge.text_utils import (
    author_list_is_initials_surname,
    author_list_is_surname_initials,
    surname_from_initials_form,
)
from tests.factories import article, misc, write_bib

# Real author lists, as the sources emit them.
_PUBMED = "Jin Y and Guo Y and Koller Jm and Grossen Sc and Uhlmann A and Forde Nj"
_PUBMED_THREE_INITIALS = "Duke Aee and Crider R and Harri Bi and Anjorin O and Agyapong Vio and Orji R"
_WESTERN_SHORT_SURNAMES = "Meng He and Qihao Li and Xiaoyan Lv and Hongwei Du"
_WESTERN = "Gabriel Spadon and Ronald Pelot and Stan Matwin"
_SCHOLAR_LEADING_INITIALS = "IV Belizario and D Teodoro and LGM Andrade and G Spadon and JF Rodrigues-Jr"
_SCHOLAR_LEADING_INITIALS_MANGLED = "IV Belizario and D Teodoro and Lgm Andrade and G Spadon and JF Rodrigues-Jr"


@pytest.mark.parametrize(
    ("authors", "expected"),
    [
        (_PUBMED, True),
        (_PUBMED_THREE_INITIALS, True),
        # Every final token is two letters, but none is a single letter, so the
        # list is Given Surname with short surnames. This is the case a
        # length-only test gets wrong.
        (_WESTERN_SHORT_SURNAMES, False),
        (_WESTERN, False),
        # One stray initial is not a pattern.
        ("Hideo Bannai and Travis Gagie and Gonzalo Navarro and Meng He", False),
        # "Last, First" is already unambiguous and is never reinterpreted.
        ("Spadon, Gabriel and Pelot, Ronald", False),
        # A single name carries no list-level evidence either way.
        ("Jin Y", False),
        ("", False),
    ],
    ids=[
        "pubmed",
        "pubmed-three-initials",
        "western-short-surnames",
        "western",
        "one-stray-initial",
        "comma-form",
        "single-author",
        "empty",
    ],
)
def test_surname_initials_detection(authors: str, expected: bool) -> None:
    names = [part.strip() for part in authors.split(" and ") if part.strip()]

    assert author_list_is_surname_initials(names) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jin Y", "Jin"),
        ("Koller Jm", "Koller"),
        ("Duke Aee", "Duke"),
        ("Agyapong Vio", "Agyapong"),
        ("Jedari-Eyvazi F", "Jedari-Eyvazi"),
        # Multi-token surnames survive intact.
        ("Van Den Heuvel Oa", "Van Den Heuvel"),
        # Four or more trailing letters is a word, not an initials cluster.
        ("Members Of The Consortium", "Members Of The Consortium"),
        ("Solo", "Solo"),
    ],
)
def test_surname_from_initials_form(name: str, expected: str) -> None:
    assert surname_from_initials_form(name) == expected


def test_citation_key_and_lastname_use_the_surname_not_the_initials() -> None:
    """Both derivation sites take the surname, never the trailing initials."""
    names = _PUBMED.split(" and ")

    assert _first_author_lastname(_PUBMED) == "jin"
    assert make_bibkey("Altered Neurodevelopmental Trajectories", names, 2026).startswith("Jin2026")


def test_western_lists_are_unaffected() -> None:
    """The trailing token stays the surname for the ordinary form."""
    assert _first_author_lastname(_WESTERN_SHORT_SURNAMES) == "he"
    assert _first_author_lastname(_WESTERN) == "spadon"


@pytest.mark.parametrize(
    ("authors", "expected"),
    [
        # Title-cased initials are restored to caps.
        (_PUBMED, "Jin Y and Guo Y and Koller JM and Grossen SC and Uhlmann A and Forde NJ"),
        # A real two-letter surname in caps is still title-cased, unchanged.
        ("Shu FU and Wen Wu", "Shu Fu and Wen Wu"),
        # Leading initials are left alone.
        ("JI Munro and Meng He", "JI Munro and Meng He"),
        (_WESTERN_SHORT_SURNAMES, _WESTERN_SHORT_SURNAMES),
    ],
    ids=["restores-initials", "real-short-surname", "leading-initials", "western-untouched"],
)
def test_author_casing_respects_the_form(authors: str, expected: str) -> None:
    fixed, _changed = _fix_author_casing(authors)

    assert fixed == expected
    # Idempotent, so the post-run repair reaches a fixpoint rather than oscillating.
    assert _fix_author_casing(fixed)[0] == expected


def test_reconcile_rewrites_a_stale_author_prefix() -> None:
    """A stale author prefix is renamed and rekeyed, not left behind."""
    entry = {
        "type": "misc",
        "key": "Y2026:NeuroTicTrajectories",
        "fields": {"title": "Altered Neurodevelopmental Trajectories", "author": _PUBMED, "year": "2026"},
    }

    key, renamed = _reconcile_author_prefix(entry, "Y2026-AlteredNeurodevelopmental.bib")

    assert key == "Jin2026:NeuroTicTrajectories"
    assert renamed == "Jin2026-AlteredNeurodevelopmental.bib"


def test_reconcile_leaves_a_correct_name_alone() -> None:
    """No churn: the title portion is never re-derived, only the surname."""
    entry = {
        "type": "article",
        "key": "Spadon2024:MaritimeTracking",
        "fields": {"title": "Maritime Tracking", "author": _WESTERN, "year": "2024"},
    }

    key, renamed = _reconcile_author_prefix(entry, "Spadon2024-SomeGeminiShortenedTitle.bib")

    assert key == "Spadon2024:MaritimeTracking"
    assert renamed is None


def test_postrun_renames_and_rekeys_a_stale_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End to end through finalize_run, because the rename is a real file move."""
    out_dir = tmp_path / "out"
    author = out_dir / "Doe (abc123)"
    stale = write_bib(
        author,
        misc(
            key="Y2026:NeuroTicTrajectories",
            title="Altered Neurodevelopmental Trajectories",
            author=_PUBMED,
            year="2026",
        ),
        "Y2026-AlteredNeurodevelopmental.bib",
    )
    monkeypatch.setattr(postrun, "DEFAULT_A2I2_INPUT", str(tmp_path / "absent.csv"))

    report = finalize_run(str(out_dir), [Record(name="Doe, Jane", scholar_id="abc123")], 1, 1, None)

    assert not stale.exists()
    renamed = author / "Jin2026-AlteredNeurodevelopmental.bib"
    assert renamed.exists()
    assert report.files_renamed == 1
    content = renamed.read_text(encoding="utf-8")
    assert content.startswith("@misc{Jin2026:NeuroTicTrajectories,")
    # The initials are restored in the same pass.
    assert "Koller JM" in content


def test_postrun_keeps_a_stale_name_when_the_target_is_taken(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename that would overwrite another entry is refused, not forced."""
    out_dir = tmp_path / "out"
    author = out_dir / "Doe (abc123)"
    stale = write_bib(
        author,
        misc(
            key="Y2026:NeuroTicTrajectories",
            title="Altered Neurodevelopmental Trajectories",
            author=_PUBMED,
            year="2026",
        ),
        "Y2026-AlteredNeurodevelopmental.bib",
    )
    occupied = write_bib(
        author,
        article(
            key="Jin2026:Other",
            title="A Different Paper Entirely",
            author="Jin Y and Guo Y",
            year="2026",
            journal="Some Journal",
        ),
        "Jin2026-AlteredNeurodevelopmental.bib",
    )
    occupied_bytes = occupied.read_bytes()
    monkeypatch.setattr(postrun, "DEFAULT_A2I2_INPUT", str(tmp_path / "absent.csv"))

    report = finalize_run(str(out_dir), [Record(name="Doe, Jane", scholar_id="abc123")], 1, 1, None)

    assert stale.exists()
    assert occupied.read_bytes() == occupied_bytes
    assert report.files_renamed == 0


@pytest.mark.parametrize(
    ("authors", "expected"),
    [
        (_SCHOLAR_LEADING_INITIALS, True),
        (_SCHOLAR_LEADING_INITIALS_MANGLED, True),
        # Two single-letter leads is what carries the decision. One is not a
        # pattern: a real short given name ("D Teodoro" alone) is common.
        ("D Teodoro and Gabriel Spadon", False),
        # A genuinely mangled ALL-CAPS surname-first list has no single-letter
        # lead, since no surname is one letter.
        ("SMITH John and DOE Jane", False),
        (_WESTERN, False),
        (_PUBMED, False),
    ],
    ids=[
        "scholar-leading-initials",
        "scholar-leading-initials-already-mangled",
        "one-stray-single-lead",
        "mangled-surname-first-not-matched",
        "western",
        "pubmed-trailing-not-leading",
    ],
)
def test_leading_initials_detection(authors: str, expected: bool) -> None:
    names = [part.strip() for part in authors.split(" and ") if part.strip()]

    assert author_list_is_initials_surname(names) is expected


def test_leading_initials_are_restored_to_caps() -> None:
    """A Scholar-style leading-initials list keeps or restores initials caps,
    even where an earlier pass already lowered one ("Lgm" -> "LGM"), and never
    touches the trailing surname."""
    fixed, changed = _fix_author_casing(_SCHOLAR_LEADING_INITIALS_MANGLED)

    assert fixed == _SCHOLAR_LEADING_INITIALS
    assert changed is True
    # Idempotent, so the load-repair reaches a fixpoint rather than oscillating.
    assert _fix_author_casing(fixed) == (_SCHOLAR_LEADING_INITIALS, False)


def test_leading_initials_do_not_affect_unrelated_lists() -> None:
    """Lists with no leading-initials evidence are unaffected by the new rule."""
    assert _fix_author_casing(_WESTERN) == (_WESTERN, False)
    assert _fix_author_casing(_PUBMED)[0] == "Jin Y and Guo Y and Koller JM and Grossen SC and Uhlmann A and Forde NJ"
