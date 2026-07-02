"""Title and booktitle text-normalization primitives.

Pure functions over pre-compiled regex tables that repair recurrent metadata
defects in scholarly titles and booktitles: fused compound words (hyphens
stripped by Google Scholar), acronym casing, verbose conference metadata,
non-bibliographic "garbage" titles, and DBLP author-name corruption.

Depends only on stdlib ``re`` and ``citeforge.config`` (no dependency on ``main`` or
the pipeline), so the canonicalization layer and the orchestrator can both build
on it without an import cycle. Pattern data (compound-word and acronym-case
dictionaries) is sourced from ``citeforge.config`` per the config-driven convention;
this module owns only the compiled matching logic.
"""

from __future__ import annotations

import re

from citeforge.config import ACRONYM_CASE_CORRECTIONS, COMPOUND_SUFFIXES, FUSED_COMPOUND_WORDS

# Pre-compiled patterns for _fix_fused_compounds (avoids ~800 re.compile() calls per invocation)
# Each compiled pattern carries a cheap literal pre-guard. A ``\b<word>\b``
# pattern cannot match unless the literal word/suffix is a substring of the
# working string (word boundaries only add constraints), so a substring test
# lets us skip the vast majority of re.sub calls that would match nothing.
# Literals are sourced from citeforge.config (config-driven convention).
_FUSED_DICT_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b" + re.escape(fused) + r"\b", re.IGNORECASE), repl, fused.lower())
    for fused, repl in FUSED_COMPOUND_WORDS.items()
]
_COMPOUND_SUFFIX_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b([A-Z][a-z]{2,})(" + re.escape(suffix) + r")\b"), suffix) for suffix in COMPOUND_SUFFIXES
]

# Pre-compiled patterns for garbage title detection
_GARBAGE_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_GARBAGE_POSTAL_RE = re.compile(r"\b[A-Z]\d[A-Z]\s*\d[A-Z]\d\b")
_GARBAGE_DEPT_RE = re.compile(r"^\s*(Department|Faculty|School|Institute)\s+of\b", re.IGNORECASE)
_GARBAGE_PHONE_RE = re.compile(r"\+?\d{1,4}[\.\-]\d{2,4}[\.\-]\d{2,}")
_GARBAGE_VOLUME_RE = re.compile(r"\bComplete\s+Volume\b", re.IGNORECASE)
_GARBAGE_SERIES_VOL_RE = re.compile(r"^(OASIcs|LIPIcs|LNI|LNCS|Dagstuhl)\b.*\bVolume\s+\d+\b", re.IGNORECASE)
_GARBAGE_FESTSCHRIFT_RE = re.compile(r"\bFestschrift\b", re.IGNORECASE)
_GARBAGE_FESTSCHRIFT_META_RE = re.compile(r",\s+[A-Z][a-z]+,\s+[A-Z][a-z]+\b.*\d{4}")
_GARBAGE_PROCEEDINGS_RE = re.compile(r"^Proceedings\s+of\s+(the\s+)?\d{4}\s+", re.IGNORECASE)
_GARBAGE_CORRECTION_RE = re.compile(r"^Correction(s)?\s+(to|of)\s*:", re.IGNORECASE)
_GARBAGE_EASYCHAIR_RE = re.compile(r"\bEasyChair\s+Preprint\b", re.IGNORECASE)

# Pre-compiled patterns for acronym case corrections in titles
_ACRONYM_CASE_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (re.compile(r"\b" + re.escape(wrong) + r"\b"), correct, wrong)
    for wrong, correct in ACRONYM_CASE_CORRECTIONS.items()
]

# Pre-compiled pattern for verbose LNCS/Springer booktitle metadata
# Strips conference location, dates, and "Proceedings" suffix appended by Crossref
_VERBOSE_BOOKTITLE_RE = re.compile(
    r"\d+(st|nd|rd|th)\s+(International|Annual|European|Asian|Australasian)\s+"
    r"(Conference|Workshop|Symposium)\b.*,\s*Proceedings\s*$",
    re.IGNORECASE,
)

# Pre-compiled title spacing and case patterns shared by _fix_title_text
_COLON_SPACE_RE = re.compile(r"(\S):([A-Z])")
_HYPHEN_SPACE_RE = re.compile(r"(\w)- (?!and |or |to )")
_SPACE_HYPHEN_RE = re.compile(r"(\w) -(\w)")

# Pre-compiled booktitle cleanup patterns (venue abbreviations, typos, spacing)
_BOOKTITLE_FIXUPS: list[tuple[re.Pattern[str], str]] = [
    # "on on " → "on " (duplicate preposition from ACM metadata)
    (re.compile(r"\bon on\b"), "on"),
    # "of the YYYY on ACM" → "of the YYYY ACM" (Crossref 2024 ACM metadata gap)
    (re.compile(r"of the (\d{4}) on (ACM|IEEE)\b"), r"of the \1 \2"),
    # "Nations of the Americas Chapter" → "North American Chapter" (NAACL 2025 Crossref error)
    (re.compile(r"Nations of the Americas Chapter"), "North American Chapter"),
    # "Health(SeGAH)" → "Health (SeGAH)" (missing space before acronym)
    (re.compile(r"Health\(SeGAH\)"), "Health (SeGAH)"),
    # "Intl Conf" → "International Conference"
    (re.compile(r"\bIntl Conf\b"), "International Conference"),
    # "Int'l" → "International"
    (re.compile(r"\bInt'l\b"), "International"),
    # "NeuriPS" → "NeurIPS" (venue typo from API sources)
    (re.compile(r"\bNeuriPS\b"), "NeurIPS"),
    # CHCCS publisher name used as venue → Graphics Interface conference
    (re.compile(r"^Canada Human-Computer Communications Society$"), "Graphics Interface"),
    # "Conference On" → "Conference on" (lowercase preposition; must run before truncation completions)
    (re.compile(r"\bConference On\b"), "Conference on"),
    # "YYYY ACM on Conference" → "YYYY ACM Conference" (Crossref spurious "on")
    (re.compile(r"(\d{4}) ACM on ([A-Z])"), r"\1 ACM \2"),
    # "of the YYYY on Innovation" → "of the YYYY ACM Conference on Innovation" (ITiCSE gap)
    (re.compile(r"of the (\d{4}) on Innovation"), r"of the \1 ACM Conference on Innovation"),
    # "ITiCSE'NN: Proceedings..." prefix → strip non-standard prefix
    (re.compile(r"ITiCSE'\d{2}:\s*"), ""),
    # "SEET-Software" → "SEET - Software" (missing spaces around dash)
    (re.compile(r"^SEET-Software"), "SEET - Software"),
    # Truncated SerpAPI booktitles — complete known conference name suffixes
    (re.compile(r"Conference on Innovation$"), "Conference on Innovation and Technology in Computer Science Education"),
    (re.compile(r"Applications of Computer$"), "Applications of Computer Vision"),
    (re.compile(r"Analyzing and Interpreting$"), "Analyzing and Interpreting Neural Networks for NLP"),
    (re.compile(r"Conference on Persuasive$"), "Conference on Persuasive Technology"),
    # FAccT: "Fairness Accountability and Transparency" → commas
    (re.compile(r"Fairness Accountability and Transparency"), "Fairness, Accountability, and Transparency"),
    # "Conference Information" → "Conference on Information" (missing "on")
    (re.compile(r"Conference Information Visualisation"), "Conference on Information Visualisation"),
    # "YYYY the Nth" → "YYYY The Nth" (capitalize after year)
    (re.compile(r"(\d{4}) the (\d)"), r"\1 The \2"),
    # "Conference: (VTC" → "Conference (VTC" (stray colon before acronym)
    (re.compile(r"Conference: \("), "Conference ("),
    # "Persuasive Technology PERSUASIVE YYYY" → strip redundant acronym
    (re.compile(r"(Persuasive Technology(?:\s+Adjunct)?),?\s+PERSUASIVE(?:\s+\d{4})?$"), r"\1"),
    # Truncated "\& International..." suffix → strip
    (re.compile(r"\s*\\?&\s*International$"), ""),
]


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
    for acr_pat, acr_repl, acr_lit in _ACRONYM_CASE_PATTERNS:
        # Case-sensitive pattern (no IGNORECASE): the literal must appear verbatim to match.
        if acr_lit not in result:
            continue
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
    # Literal pre-guards skip the ~800 mostly-no-op re.sub calls per title.
    # For the IGNORECASE fused patterns the guard is applied only while the
    # working string is pure ASCII: for ASCII text, ``lit in result.lower()`` is
    # an exact necessary condition for a ``\b<lit>\b`` IGNORECASE match. A
    # non-ASCII string skips the guard (runs every pattern) because re.IGNORECASE
    # folds a few non-ASCII characters (long s, dotted/dotless i) to ASCII letters
    # in ways str.lower() does not, so the ASCII-only guard stays byte-exact.
    lowered = result.lower()
    ascii_only = result.isascii()
    # Pass 1: Dictionary-based fixes (highest priority, handles acronyms & irregulars)
    for pattern, replacement, lit in _FUSED_DICT_PATTERNS:
        if ascii_only and lit not in lowered:
            continue
        new = pattern.sub(replacement, result)
        if new != result:
            result = new
            lowered = result.lower()
            ascii_only = result.isascii()
    # Pass 2: Suffix-based detection for remaining fused compounds.
    # Matches title-cased words: [A-Z][a-z]{2,} prefix + known compound suffix.
    # The suffix group is a case-sensitive literal, so ``suffix in result`` is an
    # exact necessary condition regardless of ASCII-ness.
    for sfx_pat, suffix in _COMPOUND_SUFFIX_PATTERNS:
        if suffix not in result:
            continue
        new = sfx_pat.sub(lambda m: m.group(1) + "-" + m.group(2).capitalize(), result)
        if new != result:
            result = new
            lowered = result.lower()
            ascii_only = result.isascii()
    # Pass 3: Dictionary again (suffix pass may expose new \b boundaries)
    for pattern, replacement, lit in _FUSED_DICT_PATTERNS:
        if ascii_only and lit not in lowered:
            continue
        new = pattern.sub(replacement, result)
        if new != result:
            result = new
            lowered = result.lower()
            ascii_only = result.isascii()
    return result
