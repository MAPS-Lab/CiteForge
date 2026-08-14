"""Behavioral coverage for CiteForge's shared LaTeX decoder."""

from __future__ import annotations

import pytest

from citeforge.latex_utils import latex_to_ascii


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (r"M{\"u}ller", "Muller"),
        (r"Fran\c{c}ois", "Francois"),
        (r"\textbf{Nested \emph{Title}}", "Nested Title"),
        (r"{\it Signal} and \& Noise", "Signal and & Noise"),
    ],
)
def test_latex_to_ascii_decodes_standard_forms(source: str, expected: str) -> None:
    """A missing decoder or unhandled form must fail this conversion contract."""
    assert latex_to_ascii(source, math_mode="remove") == expected


def test_latex_to_ascii_math_modes_are_explicit() -> None:
    """A wrong converter mode must not silently change title or serializer policy."""
    assert latex_to_ascii(r"Analysis $\phi$ result", math_mode="remove") == "Analysis  result"
    assert latex_to_ascii(r"Analysis $\phi$ result", math_mode="verbatim") == r"Analysis $\phi$ result"
