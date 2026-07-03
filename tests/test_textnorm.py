"""Pure-unit contracts for :mod:`citeforge.textnorm`.

Drives the real title/booktitle normalization primitives over the shared
adversarial corpus tables. Every expected value here was captured from the live
functions (see the probe in the assignment log), never hand-derived. The
fixpoint / idempotence assertions are load-bearing: the pipeline applies these
fixes in three places across a run (initial fixup, pre-enrichment, post-merge),
so a spurious rewrite on already-correct input would oscillate between
consecutive cache-hit runs and break the byte-identical determinism guarantee.
"""

from __future__ import annotations

import pytest

from citeforge.textnorm import _apply_booktitle_fixups, _fix_fused_compounds, _fix_title_text
from tests.corpus import BOOKTITLE_FIXUP_CASES, FUSED_COMPOUND_CASES

# ---------------------------------------------------------------------------
# _fix_fused_compounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("title_in", "title_out"), FUSED_COMPOUND_CASES)
def test_fix_fused_compounds_corpus(title_in: str, title_out: str) -> None:
    """Each corpus case maps to its captured golden output, incl. the fixpoint."""
    assert _fix_fused_compounds(title_in) == title_out


@pytest.mark.parametrize(("title_in", "title_out"), FUSED_COMPOUND_CASES)
def test_fix_fused_compounds_idempotent(title_in: str, title_out: str) -> None:
    """Re-running the fix on its own output is a no-op (determinism-critical)."""
    once = _fix_fused_compounds(title_in)
    assert _fix_fused_compounds(once) == once


def test_fix_fused_compounds_empty_passthrough() -> None:
    """The empty string short-circuits and returns unchanged."""
    assert _fix_fused_compounds("") == ""


def test_fix_fused_compounds_nonascii_guard_preserves_bytes() -> None:
    """A non-ASCII title with no fused compound is byte-preserved.

    For non-ASCII input the literal pre-guard in ``_fix_fused_compounds`` is
    bypassed and every pattern runs, so this exercises the guard-skip path. The
    output must stay byte-identical to the accented input.
    """
    title = "Étude sur les Réseaux Neuronaux"
    result = _fix_fused_compounds(title)
    assert result == title
    assert result.encode("utf-8") == title.encode("utf-8")


def test_fix_fused_compounds_nonascii_still_repairs_ascii_compound() -> None:
    """The guard-skip path still repairs an ASCII fused compound in a mixed string."""
    assert _fix_fused_compounds("Étude Knowledgedriven") == "Étude Knowledge-Driven"


# ---------------------------------------------------------------------------
# _apply_booktitle_fixups
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("bt_in", "bt_out"), BOOKTITLE_FIXUP_CASES)
def test_apply_booktitle_fixups_corpus(bt_in: str, bt_out: str) -> None:
    """Each corpus booktitle maps to its captured golden output."""
    assert _apply_booktitle_fixups(bt_in) == bt_out


@pytest.mark.parametrize(("bt_in", "bt_out"), BOOKTITLE_FIXUP_CASES)
def test_apply_booktitle_fixups_idempotent(bt_in: str, bt_out: str) -> None:
    """Re-applying the booktitle fixups on their own output is a no-op."""
    once = _apply_booktitle_fixups(bt_in)
    assert _apply_booktitle_fixups(once) == once


# ---------------------------------------------------------------------------
# _fix_title_text — colon/hyphen spacing and acronym casing
# ---------------------------------------------------------------------------

# (title_in, title_out) golden pairs captured live from _fix_title_text.
_TITLE_TEXT_CASES: list[tuple[str, str]] = [
    # Colon glued to a following capital gets a space inserted after the colon.
    ("Deep Learning:A Survey", "Deep Learning: A Survey"),
    # "word- X" collapses the stray space into a real hyphen.
    ("Multi- Task Learning", "Multi-Task Learning"),
    # "word -Y" collapses the stray space the other way.
    ("Multi -Task Learning", "Multi-Task Learning"),
    # Case-sensitive acronym corrections (Iot->IoT, Ai->AI, Nims->NIMS).
    ("Iot Security and Ai Systems", "IoT Security and AI Systems"),
    ("Nims Data Pipeline", "NIMS Data Pipeline"),
    # Combined: fused compound + colon spacing + acronym in one title.
    ("Knowledgedriven Reasoning:An Iot Study", "Knowledge-Driven Reasoning: An IoT Study"),
]


@pytest.mark.parametrize(("title_in", "title_out"), _TITLE_TEXT_CASES)
def test_fix_title_text_golden(title_in: str, title_out: str) -> None:
    """Colon/hyphen spacing and acronym casing match captured golden outputs."""
    assert _fix_title_text(title_in) == title_out


@pytest.mark.parametrize(("title_in", "title_out"), _TITLE_TEXT_CASES)
def test_fix_title_text_idempotent(title_in: str, title_out: str) -> None:
    """A second pass over the fixed title changes nothing (determinism-critical)."""
    once = _fix_title_text(title_in)
    assert _fix_title_text(once) == once


@pytest.mark.parametrize(
    "title",
    [
        "A State-of-the-Art System",
        "A Clean Title Without Issues",
        "Deep Learning: A Survey",
        "IoT Security and AI Systems",
    ],
)
def test_fix_title_text_fixpoint_on_correct_input(title: str) -> None:
    """Already-correct titles pass through unchanged (no spurious rewrite)."""
    assert _fix_title_text(title) == title


def test_fix_title_text_hyphen_lookahead_spares_conjunctions() -> None:
    """ "word- and/or/to " keeps its space (negative lookahead in the hyphen rule)."""
    assert _fix_title_text("Learn- and Adapt") == "Learn- and Adapt"
    assert _fix_title_text("Learn- or Die") == "Learn- or Die"
    assert _fix_title_text("Point- to Point") == "Point- to Point"


def test_fix_title_text_nonascii_preserves_bytes_but_applies_ascii_fixes() -> None:
    """A non-ASCII title is byte-preserved where no ASCII rule matches, yet an
    embedded ASCII acronym literal is still corrected."""
    assert _fix_title_text("Étude sur les Réseaux Neuronaux") == "Étude sur les Réseaux Neuronaux"
    assert _fix_title_text("Análisis de Datos Iot") == "Análisis de Datos IoT"
