"""Shared LaTeX-to-ASCII normalization."""

from __future__ import annotations

from typing import Literal, cast

from pylatexenc.latex2text import LatexNodes2Text, MacroTextSpec, get_default_latex_context_db
from pylatexenc.latexwalker import LatexGroupNode, LatexMacroNode
from unidecode import unidecode

MathMode = Literal["remove", "verbatim"]
_MATH_MODES: tuple[MathMode, ...] = ("remove", "verbatim")
_SERIALIZER_FORMAT_MACROS = (
    "textit",
    "textbf",
    "emph",
    "textsc",
    "texttt",
    "textrm",
    "textsf",
    "underline",
    "uppercase",
    "lowercase",
    "mbox",
    "hbox",
    "text",
)
_OLD_STYLE_FORMAT_MACROS = frozenset({"it", "bf", "em", "sc", "tt", "rm", "sf", "sl"})


class _SerializerLatexNodes2Text(LatexNodes2Text):
    """Keep ordinary BibTeX case groups, not old-style formatting wrappers."""

    def group_node_to_text(self, node: LatexGroupNode) -> str:
        children = node.nodelist
        if (
            node.delimiters == ("{", "}")
            and children
            and isinstance(children[0], LatexMacroNode)
            and children[0].macroname in _OLD_STYLE_FORMAT_MACROS
        ):
            return cast(str, self._groupnodecontents_to_text(node))
        return cast(str, super().group_node_to_text(node))


def _converter(math_mode: MathMode) -> LatexNodes2Text:
    """Build a converter with CiteForge's legacy formatting macros preserved."""
    latex_context = get_default_latex_context_db()
    latex_context.add_context_category(
        "citeforge-formatting",
        macros=[
            *(MacroTextSpec(macro, simplify_repl="%s") for macro in _SERIALIZER_FORMAT_MACROS),
            MacroTextSpec("LaTeX", simplify_repl="LaTeX"),
        ],
        prepend=True,
    )
    converter_type = _SerializerLatexNodes2Text if math_mode == "verbatim" else LatexNodes2Text
    return converter_type(
        latex_context=latex_context,
        math_mode=math_mode,
        strict_latex_spaces="macros",
        keep_braced_groups=math_mode == "verbatim",
    )


_CONVERTERS = {
    mode: _converter(mode)
    for mode in _MATH_MODES
}


def latex_to_ascii(text: str, *, math_mode: MathMode) -> str:
    """Decode LaTeX and transliterate the result without reading TeX inputs."""
    return unidecode(_CONVERTERS[math_mode].latex_to_text(text))
