"""Pre-compiled fix-pattern tables and regexes for title and booktitle fixes.

Relocated verbatim from main.py. Depends only on stdlib re and src.config, and is
imported by src.fixup.text. No dependency on main (no import cycle).
"""

from __future__ import annotations

import re

from src.config import ACRONYM_CASE_CORRECTIONS, COMPOUND_SUFFIXES, FUSED_COMPOUND_WORDS

# Pre-compiled patterns for _fix_fused_compounds (avoids ~800 re.compile() calls per invocation)
_FUSED_DICT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b" + re.escape(fused) + r"\b", re.IGNORECASE), repl) for fused, repl in FUSED_COMPOUND_WORDS.items()
]
_COMPOUND_SUFFIX_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\b([A-Z][a-z]{2,})(" + re.escape(suffix) + r")\b") for suffix in COMPOUND_SUFFIXES
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
_ACRONYM_CASE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b" + re.escape(wrong) + r"\b"), correct) for wrong, correct in ACRONYM_CASE_CORRECTIONS.items()
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
