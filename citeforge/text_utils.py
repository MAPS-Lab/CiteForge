"""Text and author-name normalization and similarity scoring.

Shared helpers for normalizing titles and author names and for scoring the
similarity between records. The merge, deduplication, and BibTeX-building layers
all compare records through these functions so the notion of the same paper
stays consistent across the pipeline.
"""

from __future__ import annotations

import functools
import html as html_module
import re
import urllib.parse
from typing import Any

from rapidfuzz.fuzz import ratio as fuzz_ratio
from unidecode import unidecode

from .config import (
    PREPRINT_DOI_PREFIXES,
    PREPRINT_SERVERS,
    VALID_YEAR_MAX,
    VALID_YEAR_MIN,
)
from .exceptions import DECODE_ERRORS, NUMERIC_ERRORS, PARSE_ERRORS
from .id_utils import external_ids_match

_ET_AL = "et al."
_ABBREVIATED_AUTHOR_PATTERN = re.compile(r"^[A-Z]\.?[ \t]*[A-Z]?\.?[ \t]*[A-Z]?\.?[ \t]+[A-Z][a-z]+", re.IGNORECASE)

__all__ = [
    "author_in_text",
    "author_name_matches",
    "author_overlap_ratio",
    "authors_overlap",
    "build_url",
    "compute_dedup_score",
    "extract_author_names",
    "extract_authors_from_any",
    "extract_last_name",
    "extract_valid_title",
    "extract_year_from_any",
    "filter_valid_fields",
    "format_author_dirname",
    "get_truncation_score",
    "has_placeholder",
    "is_truncated",
    "is_valid_value",
    "name_signature",
    "normalize_person_name",
    "normalize_title",
    "parse_authors_any",
    "safe_get_field",
    "safe_get_nested",
    "strip_accents",
    "title_is_truncated_match",
    "title_similarity",
    "to_text",
    "trim_title_default",
    "venue_similarity",
]


def _name_from_dict(d: dict[str, Any]) -> str:
    """
    Build a display name from a dictionary that may contain either a full
    "name" field or separate given/family (first/last) components.
    Returns an empty string if nothing usable is present.
    """
    name = str(d.get("name") or "").strip()
    if name:
        return name
    given = str(d.get("given") or d.get("first") or "").strip()
    family = str(d.get("family") or d.get("last") or "").strip()
    return (f"{given} {family}" if (given or family) else "").strip()


def build_url(base: str, params: dict[str, Any]) -> str:
    """
    Attach query parameters to a base URL and return the fully encoded address as a string.
    """
    q = urllib.parse.urlencode(params)
    return f"{base}?{q}"


def to_text(obj: Any) -> str:
    """
    Convert an arbitrary value into a readable string, handling nested
    dictionaries, lists of authors, and other common metadata shapes from APIs.
    """
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        parts: list[str] = []
        for x in obj:
            if isinstance(x, dict):
                nm = x.get("name") or x.get("text") or x.get("summary") or ""
                if not nm:
                    given = x.get("given") or x.get("first") or ""
                    family = x.get("family") or x.get("last") or ""
                    nm = f"{given} {family}".strip()
                parts.append(str(nm).strip())
            else:
                parts.append(str(x).strip())
        return ", ".join(p for p in parts if p)
    if isinstance(obj, dict):
        if obj.get("name"):
            return str(obj["name"])
        if obj.get("summary"):
            return str(obj["summary"])
        if obj.get("text"):
            return str(obj["text"])
        try:
            return ", ".join(str(v) for v in obj.values() if v)
        except PARSE_ERRORS + DECODE_ERRORS:
            return str(obj)
    return str(obj)


def strip_accents(s: str) -> str:
    """
    Remove accents and diacritics from a string so visually similar text from
    different locales can be compared more reliably.

    Uses unidecode for Unicode-to-ASCII transliteration.
    """
    try:
        return unidecode(s)
    except PARSE_ERRORS + DECODE_ERRORS:
        return s


@functools.lru_cache(maxsize=4096)
def normalize_title(t: str | None) -> str:
    """
    Normalize a title for comparison by stripping accents, lowercasing, removing
    punctuation, brackets, LaTeX formatting, and collapsing repeated whitespace.
    """
    if not t:
        return ""

    t_str = str(t)

    t_str = html_module.unescape(t_str)
    t_str = re.sub(r"\$([^$]*)\$", r"\1", t_str)
    t_str = re.sub(r"\\frac\{([^}]*)\}\{([^}]*)\}", r"\1/\2", t_str)
    t_str = re.sub(r"\\[a-zA-Z]+\{([^}]*)}", r"\1", t_str)
    t_str = re.sub(r"\\[a-zA-Z]+", "", t_str)
    t2 = strip_accents(t_str).lower()
    t2 = re.sub(r"[,.;:!?\n\t\r'\"\-\(\)\[\]\{\}~]", " ", t2)
    result = " ".join(t2.split())
    # If unidecode stripped everything (e.g., CJK-only title), fall back to
    # the original lowercased+collapsed string so similarity can still work.
    if not result and t_str.strip():
        fallback = t_str.lower()
        fallback = re.sub(r"[,.;:!?\n\t\r'\"\-\(\)\[\]\{\}~]", " ", fallback)
        result = " ".join(fallback.split())
    return result


_ARTIFACT_PREFIXES = ("Check for updates ", "Check for Updates ")

_DANGLING_ENDINGS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "with",
        "by",
        "and",
        "or",
        "but",
        "via",
        "using",
        "based",
    }
)

_TRUNCATION_MARKERS = ("...", "\u2026", "et al", _ET_AL, "[truncated]", "[...]")

# Words that should stay uppercase when converting ALL-CAPS titles to title case
_ACRONYMS = frozenset(
    {
        "a",
        "an",
        "and",
        "as",
        "at",
        "but",
        "by",
        "for",
        "if",
        "in",
        "nor",
        "of",
        "on",
        "or",
        "so",
        "the",
        "to",
        "up",
        "vs",
        "yet",
    }
)


def _fix_allcaps_title(s: str) -> str:
    """Convert ALL-CAPS title to title case, preserving known acronyms.

    Only activates when >60% of alphabetic characters are uppercase,
    indicating publisher metadata in ALL-CAPS rather than intentional styling.
    """
    alpha_chars = [c for c in s if c.isalpha()]
    if not alpha_chars:
        return s
    upper_ratio = sum(1 for c in alpha_chars if c.isupper()) / len(alpha_chars)
    if upper_ratio <= 0.6:
        return s
    # Split on spaces, preserving hyphens within words
    words = s.split()
    result: list[str] = []
    for i, word in enumerate(words):
        if "-" in word:
            parts = [p.capitalize() if len(p) > 1 else p for p in word.split("-")]
            result.append("-".join(parts))
        elif word[0] in "([":
            result.append(word[0] + word[1:].capitalize())
        elif i == 0:
            result.append(word.capitalize())
        elif word.lower() in _ACRONYMS:
            result.append(word.lower())
        else:
            result.append(word.capitalize())
    return " ".join(result)


def trim_title_default(t: str | None) -> str:
    """
    Clean up a raw title by trimming whitespace, removing trailing full stops,
    preserving genuine ellipses, and normalizing ALL-CAPS titles to title case.
    """
    if t is None:
        return ""
    s = str(t).strip()
    if not s:
        return ""
    for prefix in _ARTIFACT_PREFIXES:
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    if s.endswith("…") or s.endswith("..."):
        return _fix_allcaps_title(s)
    s = s.rstrip("*").rstrip()
    i = len(s) - 1
    dots = 0
    while i >= 0 and s[i] == ".":
        dots += 1
        i -= 1
    if dots and dots < 3:
        s = s[: len(s) - dots].rstrip()
    return _fix_allcaps_title(s)


def has_placeholder(s: str | None) -> bool:
    """
    Detect whether a string looks like a placeholder value such as "n/a",
    "unknown", "et al", or a run of dots instead of real content.
    """
    if s is None:
        return True
    s2 = str(s).strip()
    if not s2:
        return True
    low = s2.lower()
    if ("..." in s2 or "…" in s2) and len(s2) < 50:
        return True
    if "et al" in low:
        return True
    return any(bad in low for bad in ("n/a", "tbd", "unknown", "placeholder"))


def normalize_person_name(n: Any | None) -> str:
    """
    Normalize a person name for matching by lowercasing it, stripping accents
    and punctuation, and collapsing extra spaces.
    """
    if not n:
        return ""
    n_str = to_text(n)
    n2 = strip_accents(n_str).lower()
    n2 = re.sub(r"[^a-z0-9\s]", " ", n2)
    return " ".join(n2.split())


_NOBLE_PARTICLES = frozenset(
    {
        "van",
        "von",
        "de",
        "del",
        "der",
        "den",
        "di",
        "la",
        "le",
        "al",
        "el",
        "bin",
        "ibn",
        "da",
        "dos",
        "das",
        "du",
        "lo",
    }
)


_INITIALS_EXCLUSIONS = frozenset({"jr", "sr", "ii", "iii", "iv", "md", "phd"})


def _is_initials_token(token: str) -> bool:
    """Check if a token is uppercase initials (1-4 chars, not a suffix/title)."""
    clean = token.replace(".", "").strip()
    return 1 <= len(clean) <= 4 and clean.isalpha() and clean.isupper() and clean.lower() not in _INITIALS_EXCLUSIONS


_RE_NON_ALNUM = r"[^a-z0-9]"


def name_signature(n: Any | None) -> dict[str, Any] | None:
    """
    Derive a compact signature for a person name that keeps the normalized last
    name and initials, working with "Last, First", "First Last", and
    "Lastname INITIALS" (PubMed/Europe PMC) formats.

    Noble particles (van, von, de, etc.) are included in the last name so that
    "Johan van der Waals" and "van der Waals, Johan" produce the same signature.
    """
    if not n:
        return None
    n_clean = normalize_person_name(n)
    if not n_clean:
        return None
    if "," in to_text(n):
        parts = [p.strip() for p in to_text(n).split(",")]
        last = parts[0]
        rest = parts[1] if len(parts) > 1 else ""
        rest_tokens = [t for t in normalize_person_name(rest).split() if t]
        initials = "".join(t[0] for t in rest_tokens if t)
        last_norm = re.sub(_RE_NON_ALNUM, "", normalize_person_name(last))
        return {"last": last_norm, "initials": initials}
    tokens = n_clean.split()
    if not tokens:
        return None
    # Detect PubMed/Europe PMC "Lastname INITIALS" format (e.g. "Alanko JN")
    # by checking if the last token in the original text is all uppercase
    raw_tokens = to_text(n).strip().split()
    if len(tokens) == 2 and len(raw_tokens) == 2 and _is_initials_token(raw_tokens[-1]):
        last_norm = re.sub(_RE_NON_ALNUM, "", tokens[0])
        initials = re.sub(r"[^a-z]", "", tokens[1])
        return {"last": last_norm, "initials": initials}
    last_start = len(tokens) - 1
    for i in range(len(tokens) - 2, -1, -1):
        if tokens[i] in _NOBLE_PARTICLES:
            last_start = i
        else:
            break
    last_tokens = tokens[last_start:]
    first_tokens = tokens[:last_start]
    last_norm = re.sub(_RE_NON_ALNUM, "", "".join(last_tokens))
    initials = "".join(t[0] for t in first_tokens if t)
    return {"last": last_norm, "initials": initials}


def extract_last_name(full_name: str | None) -> str:
    """
    Extract the last name from a full name string, preserving original capitalization.
    Handles both "First Last" and "Last, First" formats.
    Returns the original name if extraction fails.
    """
    if not full_name:
        return "Unknown"

    name_str = str(full_name).strip()
    if not name_str:
        return "Unknown"

    if "," in name_str:
        last_name = name_str.split(",", 1)[0].strip()
        if last_name:
            return last_name

    tokens = name_str.split()
    return tokens[-1] if tokens else name_str


def format_author_dirname(author_name: str | None, author_id: str) -> str:
    """
    Format author directory name as "LastName (author_id)".
    Falls back to just author_id if name extraction fails.
    If author_id is empty, uses LastName.
    """
    last_name = extract_last_name(author_name)

    sanitized_id = re.sub(r'[/\\:*?"<>|]+', "-", author_id)

    if not sanitized_id:
        if last_name and last_name != "Unknown":
            return last_name
        return "unknown"

    if last_name and last_name != "Unknown":
        return f"{last_name} ({sanitized_id})"

    return sanitized_id


def parse_authors_any(authors: Any) -> list[str]:
    """
    Pull author names out of flexible input formats such as lists, dictionaries
    with given/family fields, and BibTeX-style strings with different separators.

    This is a convenience wrapper around extract_authors_from_any for simple use cases.
    """
    return extract_authors_from_any(authors)


def title_similarity(a: str | None, b: str | None) -> float:
    """
    Compute a similarity score between two titles after normalization, returning
    a value between 0 and 1 where higher means more similar.

    Uses rapidfuzz for fast fuzzy-ratio scoring.
    """
    norm_a = normalize_title(a or "")
    norm_b = normalize_title(b or "")
    if norm_a == norm_b:
        return 1.0
    # rapidfuzz.fuzz.ratio returns 0-100, normalize to 0-1
    return fuzz_ratio(norm_a, norm_b) / 100.0


def title_is_truncated_match(a: str | None, b: str | None, min_length: int = 15) -> bool:
    """
    Check if one title is a truncated version of the other (a strict prefix or
    suffix after normalization).  Scholar sometimes truncates long titles from
    either end, producing entries like "Passive Co-presence" (prefix truncation)
    or "Support Using Semantic GLEAN Workflows" when the full title starts with
    "Decentralized Web-Based Clinical Decision" (suffix truncation).

    Requires the shorter title to be at least *min_length* characters to avoid
    trivially matching short common substrings.
    """
    norm_a = normalize_title(a or "")
    norm_b = normalize_title(b or "")
    if not norm_a or not norm_b or norm_a == norm_b:
        return False
    shorter, longer = (norm_a, norm_b) if len(norm_a) <= len(norm_b) else (norm_b, norm_a)
    return len(shorter) >= min_length and (longer.startswith(shorter) or longer.endswith(shorter))


def authors_overlap(authors_a: str | None, authors_b: str | None) -> bool:
    """
    Check whether two author lists share at least one person in common by
    comparing normalized last names and initials and allowing partial matches.
    """
    names_a = parse_authors_any(authors_a or "")
    names_b = parse_authors_any(authors_b or "")
    if not names_a or not names_b:
        return False
    sigs_a_by_last: dict[str, list[dict[str, Any]]] = {}
    for nm in names_a:
        sig = name_signature(nm)
        if sig and sig.get("last"):
            sigs_a_by_last.setdefault(sig["last"], []).append(sig)
    if not sigs_a_by_last:
        return False
    for nm in names_b:
        sb = name_signature(nm)
        if not sb or not sb.get("last"):
            continue
        matching_sigs = sigs_a_by_last.get(sb["last"])
        if not matching_sigs:
            continue
        for sa in matching_sigs:
            ia = sa.get("initials", "")
            ib = sb.get("initials", "")
            if not ia or not ib or ia == ib or ia.startswith(ib) or ib.startswith(ia):
                return True
    return False


def _author_sig_key(sig: dict[str, Any]) -> str:
    """Build a set key from a name signature, including initials when available."""
    last = sig.get("last", "")
    initials = sig.get("initials", "")
    return f"{last}_{initials}" if initials else last


def author_overlap_ratio(authors_a: str | None, authors_b: str | None) -> float:
    """Jaccard coefficient on normalized author signatures between two author lists.

    Uses last_name + initials when both sides have initials, falling back to
    last-name-only matching otherwise.
    """
    names_a = parse_authors_any(authors_a or "")
    names_b = parse_authors_any(authors_b or "")
    if not names_a or not names_b:
        return 0.0
    sigs_a_raw = [sig for nm in names_a if (sig := name_signature(nm)) and sig.get("last")]
    sigs_b_raw = [sig for nm in names_b if (sig := name_signature(nm)) and sig.get("last")]
    if not sigs_a_raw or not sigs_b_raw:
        return 0.0
    a_has_initials = all(s.get("initials") for s in sigs_a_raw)
    b_has_initials = all(s.get("initials") for s in sigs_b_raw)
    if a_has_initials and b_has_initials:
        sigs_a = {_author_sig_key(s) for s in sigs_a_raw}
        sigs_b = {_author_sig_key(s) for s in sigs_b_raw}
    else:
        # Fall back to last-name-only when one side lacks initials
        sigs_a = {s["last"] for s in sigs_a_raw}
        sigs_b = {s["last"] for s in sigs_b_raw}
    intersection = len(sigs_a & sigs_b)
    union = len(sigs_a | sigs_b)
    return intersection / union if union > 0 else 0.0


def venue_similarity(fields_a: dict[str, Any], fields_b: dict[str, Any]) -> float:
    """Compute string similarity between two venue names (journal/booktitle/howpublished).

    Pure venue-string similarity only. The preprint-vs-published (XOR) split is NOT
    encoded here: it is a single explicit signal in ``compute_dedup_score`` (Signal 6),
    so rewarding it here too would double-count the same piece of evidence and can tip
    two distinct works over the duplicate threshold.
    """
    a_venue = (fields_a.get("journal") or fields_a.get("booktitle") or fields_a.get("howpublished") or "").strip()
    b_venue = (fields_b.get("journal") or fields_b.get("booktitle") or fields_b.get("howpublished") or "").strip()
    if not a_venue or not b_venue:
        return 0.0
    a_norm = normalize_title(a_venue)
    b_norm = normalize_title(b_venue)
    if a_norm == b_norm:
        return 1.0
    return fuzz_ratio(a_norm, b_norm) / 100.0


def _is_preprint_fields(fields: dict[str, Any]) -> bool:
    """Check if fields look like a preprint based on DOI prefix or journal name."""
    doi = str(fields.get("doi") or "").lower()
    if any(doi.startswith(p) for p in PREPRINT_DOI_PREFIXES):
        return True
    journal = str(fields.get("journal") or "").lower()
    return any(ps in journal for ps in PREPRINT_SERVERS)


def compute_dedup_score(fields_a: dict[str, Any], fields_b: dict[str, Any], count_preprint_xor: bool = True) -> float:
    """Additive composite score from up to 6 signals for multi-signal deduplication.

    ``count_preprint_xor`` controls the preprint-vs-published split (Signal 6). Callers
    that reach this composite only after already establishing the XOR split as a
    precondition pass ``count_preprint_xor=False`` so the same evidence is not counted
    twice (the split contributes here exactly once, and never via ``venue_similarity``).
    """
    score = 0.0

    # Signal 1: Title similarity (weight 0.40)
    title_sim = title_similarity(str(fields_a.get("title") or ""), str(fields_b.get("title") or ""))
    score += 0.40 * title_sim

    # Signal 2: Author overlap ratio (weight 0.25)
    overlap = author_overlap_ratio(fields_a.get("author"), fields_b.get("author"))
    score += 0.25 * overlap

    # Signal 3: Year match (0.10 exact, 0.05 for ±1)
    a_year = extract_year_from_any(fields_a.get("year"), fallback=None)
    b_year = extract_year_from_any(fields_b.get("year"), fallback=None)
    if a_year and b_year:
        diff = abs(a_year - b_year)
        if diff == 0:
            score += 0.10
        elif diff == 1:
            score += 0.05

    # Signal 4: Venue similarity (weight 0.15)
    score += 0.15 * venue_similarity(fields_a, fields_b)

    # Signal 5: External ID match (0.15)
    if external_ids_match(fields_a, fields_b):
        score += 0.15

    # Signal 6: Preprint-vs-published split (0.10) -- the single place the XOR is counted.
    if count_preprint_xor and (_is_preprint_fields(fields_a) ^ _is_preprint_fields(fields_b)):
        score += 0.10

    return score


def author_name_matches(target_author: str | None, authors: Any) -> bool:
    """
    Check whether a specific author appears in a candidate author list, preferring
    last name plus initials and falling back to looser substring checks when needed.
    """
    if not target_author:
        return False
    target_sig = name_signature(target_author)
    if not target_sig or not target_sig.get("last"):
        return False
    cand_names = parse_authors_any(authors)
    if not cand_names:
        return False
    for nm in cand_names:
        sig = name_signature(nm)
        if not sig:
            continue
        if sig["last"] != target_sig["last"]:
            continue
        ti = target_sig.get("initials", "")
        ci = sig.get("initials", "")
        if not ti or not ci:
            return True
        if ti == ci or ti.startswith(ci) or ci.startswith(ti):
            return True
        # Handle middle initials: "CH" matches "CRH" when first initials
        # agree and the shorter is a subsequence of the longer
        if ti[0] == ci[0]:
            short, long = (ti, ci) if len(ti) <= len(ci) else (ci, ti)
            j = 0
            for ch in long:
                if j < len(short) and ch == short[j]:
                    j += 1
            if j == len(short):
                return True
        tnorm = normalize_person_name(target_author)
        cnorm = normalize_person_name(nm)
        if tnorm in cnorm or cnorm in tnorm:
            return True
    # Fallback: try reversed-word matching for "Lastname Firstname" format
    # (CSL and some APIs return names without comma: "Rudzicz Frank" instead of "Rudzicz, Frank")
    target_last = target_sig["last"]
    for nm in cand_names:
        tokens = nm.strip().split()
        if len(tokens) >= 2 and tokens[0].lower().rstrip(",") == target_last:
            return True
    return False


def author_in_text(target_author: str | None, text: Any) -> bool:
    """
    Check whether an author's normalized last name appears as a whole word inside a block of text.
    """
    if not target_author or not text:
        return False
    sig = name_signature(target_author)
    last_tok = (sig or {}).get("last", "")
    if not last_tok:
        return False
    txt = normalize_person_name(to_text(text))
    return re.search(rf"\b{re.escape(last_tok)}\b", txt) is not None


def extract_year_from_any(obj: Any, field_names: list[str] | None = None, fallback: int | None = None) -> int | None:
    """
    Try to recover a four-digit publication year from many possible formats,
    including integers, free text, date dictionaries, Crossref-style date parts,
    and Unix timestamps, falling back when no plausible year is found.
    """
    if isinstance(obj, int):
        return obj if VALID_YEAR_MIN <= obj <= VALID_YEAR_MAX else fallback

    if isinstance(obj, str):
        m = re.search(r"(19|20)\d{2}", obj)
        if m:
            try:
                year = int(m.group(0))
                if VALID_YEAR_MIN <= year <= VALID_YEAR_MAX:
                    return year
            except PARSE_ERRORS:
                pass
        return fallback

    if isinstance(obj, dict):
        _default_year_fields = ["year", "publication_year", "pub_year", "date", "published"]
        _search_fields = list(field_names) + _default_year_fields if field_names else _default_year_fields
        for fname in _search_fields:
            val = obj.get(fname)
            if val is not None:
                result = extract_year_from_any(val, field_names=None, fallback=None)
                if result:
                    return result

        for fname in ["issued", "published-print", "published-online"]:
            issued = obj.get(fname)
            if isinstance(issued, dict):
                parts = issued.get("date-parts")
                if (
                    isinstance(parts, list)
                    and parts
                    and isinstance(parts[0], list)
                    and parts[0]
                    and isinstance(parts[0][0], int)
                ):
                    year = parts[0][0]
                    if VALID_YEAR_MIN <= year <= VALID_YEAR_MAX:
                        return year

        for fname in ["cdate", "tcdate", "timestamp"]:
            ms = obj.get(fname)
            if isinstance(ms, (int, float)):
                try:
                    from datetime import datetime, timezone

                    year = datetime.fromtimestamp(float(ms) / 1000.0, timezone.utc).year
                    if VALID_YEAR_MIN <= year <= VALID_YEAR_MAX:
                        return year
                except (*NUMERIC_ERRORS, OSError):
                    pass

    if isinstance(obj, list) and obj:
        return extract_year_from_any(obj[0], field_names=field_names, fallback=fallback)

    return fallback


def extract_authors_from_any(
    obj: Any,
    field_names: list[str] | None = None,
    sanitize_dblp: bool = False,
    name_key: str = "name",
    given_key: str | None = None,
    family_key: str | None = None,
) -> list[str]:
    """
    Extract a list of author names from flexible metadata structures such as lists,
    dicts, and formatted strings, optionally cleaning DBLP-specific name artifacts.
    """
    authors: list[str] = []

    if obj is None:
        return authors

    # Lazy import to avoid circular dependency; cached after first call
    if sanitize_dblp:
        from .clients.helpers import _sanitize_dblp_author

        _sanitize: Any = _sanitize_dblp_author
    else:
        _sanitize = None

    if isinstance(obj, dict):
        _default_fields = ["authors", "author", "authorids", "creators", "contributors"]
        _search_fields = list(field_names) + _default_fields if field_names else _default_fields
        for fname in _search_fields:
            val = obj.get(fname)
            if val is not None:
                authors = extract_authors_from_any(
                    val,
                    field_names=None,
                    sanitize_dblp=sanitize_dblp,
                    name_key=name_key,
                    given_key=given_key,
                    family_key=family_key,
                )
                if authors:
                    return authors

        if given_key and family_key:
            given = (obj.get(given_key) or "").strip()
            family = (obj.get(family_key) or "").strip()
            nm = f"{given} {family}".strip() if (given or family) else ""
        else:
            nm = _name_from_dict(obj)

        if nm:
            nm = _sanitize(nm) if _sanitize else nm
            if nm:
                authors.append(nm)
        return authors

    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                nm = item.strip()
                if nm:
                    nm = _sanitize(nm) if _sanitize else nm
                    if nm:
                        authors.append(nm)
            elif isinstance(item, dict):
                if given_key and family_key:
                    given = (item.get(given_key) or "").strip()
                    family = (item.get(family_key) or "").strip()
                    nm = f"{given} {family}".strip() if (given or family) else ""
                else:
                    nm = (item.get(name_key) or "").strip()
                    if not nm:
                        given = (item.get("given") or item.get("first") or "").strip()
                        family = (item.get("family") or item.get("last") or "").strip()
                        nm = f"{given} {family}".strip() if (given or family) else ""

                if nm:
                    nm = _sanitize(nm) if _sanitize else nm
                    if nm:
                        authors.append(nm)
            else:
                nm = str(item).strip()
                if nm:
                    authors.append(nm)
        return authors

    if isinstance(obj, str):
        obj_str = obj.strip()
        if not obj_str:
            return authors

        if " and " in obj_str:
            parts = [p.strip() for p in obj_str.split(" and ")]
            authors = [p for p in parts if p]
        elif " et al." in obj_str or " et al" in obj_str:
            clean_str = obj_str.replace(" et al.", "").replace(" et al", "")
            # If there are commas, split by comma
            if "," in clean_str:
                parts = [p.strip() for p in clean_str.split(",")]
                authors = [p for p in parts if p]
                authors.append(_ET_AL)
            else:
                authors = [clean_str, _ET_AL]
        elif ";" in obj_str:
            parts = [p.strip() for p in obj_str.split(";")]
            authors = [p for p in parts if p]
        elif "," in obj_str and " " in obj_str:
            parts = [p.strip() for p in obj_str.split(",")]
            if obj_str.count(",") > 1:
                authors = [p for p in parts if p]
            elif len(parts) == 2 and (
                all(_ABBREVIATED_AUTHOR_PATTERN.match(p) for p in parts) or all(" " in p.strip() for p in parts)
            ):
                authors = parts
            else:
                authors = [obj_str]
        else:
            authors = [obj_str]

        return authors

    s = str(obj).strip()
    if s:
        authors.append(s)

    return authors


def extract_valid_title(obj: Any, field_names: list[str] | None = None, check_placeholder: bool = True) -> str | None:
    """
    Pull a title from an object using common field names, discard placeholder-like
    values, and return a trimmed version or None when no usable title is available.
    """
    title = None

    if isinstance(obj, dict):
        names = field_names or ["title"]
        for fname in names:
            val = obj.get(fname)
            if val:
                if isinstance(val, dict):
                    title = extract_valid_title(val, field_names=field_names, check_placeholder=check_placeholder)
                else:
                    title = str(val).strip()
                if title:
                    break
    else:
        title = str(obj).strip()

    if not title:
        return None

    if check_placeholder and has_placeholder(title):
        return None

    return trim_title_default(title)


def is_valid_value(val: Any, check_placeholder: bool = True) -> bool:
    """
    Decide whether a value is worth keeping by rejecting None, empty containers,
    and placeholder-like strings when placeholder checking is enabled.
    """
    if val is None:
        return False

    if isinstance(val, str):
        s = val.strip()
        if not s:
            return False
        return not has_placeholder(s) if check_placeholder else True

    if isinstance(val, (list, dict)):
        return bool(val)

    return True


def filter_valid_fields(fields: dict[str, Any], check_placeholder: bool = True) -> dict[str, Any]:
    """
    Remove keys whose values are empty, None, or placeholder-like so the
    remaining dictionary contains only useful metadata fields.
    """
    return {k: v for k, v in fields.items() if is_valid_value(v, check_placeholder=check_placeholder)}


def is_truncated(text: str | None) -> bool:
    """
    Detect if text is truncated by checking for ellipsis, et al., or other truncation markers.
    """
    if not text or not isinstance(text, str):
        return False

    text_stripped = text.strip()
    text_lower = text_stripped.lower()
    if any(marker in text_lower for marker in _TRUNCATION_MARKERS):
        return True

    last_word = text_lower.rstrip(".,:;").rsplit(None, 1)[-1] if text_lower.strip() else ""
    return last_word in _DANGLING_ENDINGS


def get_truncation_score(article_data: dict[str, Any]) -> float:
    """
    Calculate a truncation score for an article by checking key fields, returning
    a value between 0.0 (complete) and 1.0 (fully truncated).
    """
    candidates = [
        article_data.get("title"),
        article_data.get("author_info"),
        article_data.get("publication_info") or article_data.get("snippet"),
    ]
    fields_to_check = [str(v) for v in candidates if v]
    if not fields_to_check:
        return 0.0
    return sum(1 for f in fields_to_check if is_truncated(f)) / len(fields_to_check)


def safe_get_field(
    obj: dict[str, Any],
    field: str,
    *,
    default: str = "",
    strip: bool = True,
    required: bool = False,
    check_placeholder: bool = False,
) -> str | None:
    """
    Safely extract and validate a string field from a dictionary, handling None values,
    lists, whitespace, and optionally checking for placeholders.
    """
    value = obj.get(field)

    if value is None:
        return None if required else default

    if isinstance(value, list):
        if not value:
            return None if required else default
        value = value[0]

    value = str(value)

    if strip:
        value = value.strip()

    if not value:
        return None if required else default

    if check_placeholder and has_placeholder(value):
        return None if required else default

    return value


def safe_get_nested(obj: Any, *keys: str, default: Any = None) -> Any:
    """
    Safely get a nested dictionary value with null-safety, traversing multiple keys
    and returning a default if any key is missing.
    """
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return default
        current = current.get(key)
        if current is None:
            return default
    return current


def extract_author_names(
    authors_field: Any, *, name_key: str = "name", given_key: str | None = None, family_key: str | None = None
) -> list[str]:
    """
    Extract author names from various formats including list of dicts, list of strings,
    comma-separated strings, and single dict or string.
    """
    return extract_authors_from_any(authors_field, name_key=name_key, given_key=given_key, family_key=family_key)
