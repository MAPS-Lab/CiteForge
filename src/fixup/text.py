"""Shared title and booktitle text-transform helpers.

Relocated verbatim from main.py. Pure functions over the pre-compiled patterns in
src.fixup.patterns. No dependency on main (no import cycle).
"""

from __future__ import annotations

import re

from src.fixup.patterns import (
    _ACRONYM_CASE_PATTERNS,
    _BOOKTITLE_FIXUPS,
    _COLON_SPACE_RE,
    _COMPOUND_SUFFIX_PATTERNS,
    _FUSED_DICT_PATTERNS,
    _GARBAGE_CORRECTION_RE,
    _GARBAGE_DEPT_RE,
    _GARBAGE_EASYCHAIR_RE,
    _GARBAGE_EMAIL_RE,
    _GARBAGE_FESTSCHRIFT_META_RE,
    _GARBAGE_FESTSCHRIFT_RE,
    _GARBAGE_PHONE_RE,
    _GARBAGE_POSTAL_RE,
    _GARBAGE_PROCEEDINGS_RE,
    _GARBAGE_SERIES_VOL_RE,
    _GARBAGE_VOLUME_RE,
    _HYPHEN_SPACE_RE,
    _SPACE_HYPHEN_RE,
    _VERBOSE_BOOKTITLE_RE,
)


def _apply_booktitle_fixups(bt: str) -> str:
    """Strip verbose conference metadata and apply pre-compiled booktitle cleanup patterns."""
    if _VERBOSE_BOOKTITLE_RE.search(bt):
        stripped = _VERBOSE_BOOKTITLE_RE.sub("", bt).rstrip(" ,")
        if stripped:
            bt = stripped
    for pat, repl in _BOOKTITLE_FIXUPS:
        bt = pat.sub(repl, bt)
    return bt


def _fix_title_text(title: str) -> str:
    """Fix fused compounds, colon-space, hyphen-space, and acronym case."""
    result = _fix_fused_compounds(title)
    result = _COLON_SPACE_RE.sub(r"\1: \2", result)
    result = _HYPHEN_SPACE_RE.sub(r"\1-", result)
    result = _SPACE_HYPHEN_RE.sub(r"\1-\2", result)
    for acr_pat, acr_repl in _ACRONYM_CASE_PATTERNS:
        result = acr_pat.sub(acr_repl, result)
    return result


def _is_garbage_title(title: str) -> bool:
    """Detect non-bibliographic titles from Scholar/DBLP artifacts.

    Catches institutional addresses, contact info, and other metadata
    that occasionally appear as "paper titles" in Scholar results.
    """
    if not title:
        return False
    return bool(
        _GARBAGE_EMAIL_RE.search(title)
        or _GARBAGE_POSTAL_RE.search(title)
        or _GARBAGE_DEPT_RE.search(title)
        or _GARBAGE_PHONE_RE.search(title)
        or _GARBAGE_VOLUME_RE.search(title)
        or _GARBAGE_SERIES_VOL_RE.search(title)
        or (_GARBAGE_FESTSCHRIFT_RE.search(title) and _GARBAGE_FESTSCHRIFT_META_RE.search(title))
        or _GARBAGE_PROCEEDINGS_RE.match(title)
        or _GARBAGE_CORRECTION_RE.match(title)
        or _GARBAGE_EASYCHAIR_RE.search(title)
    )


def _is_corrupted_title(title: str) -> bool:
    """Detect DBLP-corrupted titles containing author names instead of real titles.

    Matches patterns like "Li2 ()" -- author name + numeric affiliation + empty parens.
    """
    return len(re.findall(r"\b[A-Z][a-z]+\d+\s*\(\)", title)) >= 2


def _fix_fused_compounds(title: str) -> str:
    """Fix fused compound words in titles (hyphens stripped by Google Scholar).

    Three-pass approach:
    1. Dictionary lookup for special cases (acronyms, irregular patterns).
    2. Suffix-based detection for common compound adjective suffixes
       (e.g. "Knowledgedriven" → "Knowledge-Driven").
    3. Dictionary lookup again — catches entries newly exposed by the suffix
       pass (e.g. "Doubleedgeassisted" → suffix splits to "Doubleedge-Assisted"
       → dict converts "Doubleedge" to "Double-Edge").
    """
    if not title:
        return title
    result = title
    # Pass 1: Dictionary-based fixes (highest priority, handles acronyms & irregulars)
    for pattern, replacement in _FUSED_DICT_PATTERNS:
        result = pattern.sub(replacement, result)
    # Pass 2: Suffix-based detection for remaining fused compounds.
    # Matches title-cased words: [A-Z][a-z]{2,} prefix + known compound suffix.
    for sfx_pat in _COMPOUND_SUFFIX_PATTERNS:
        result = sfx_pat.sub(lambda m: m.group(1) + "-" + m.group(2).capitalize(), result)
    # Pass 3: Dictionary again (suffix pass may expose new \b boundaries)
    for pattern, replacement in _FUSED_DICT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result
