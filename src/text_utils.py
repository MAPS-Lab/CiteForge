from __future__ import annotations

import re
import urllib.parse
from typing import Any, Optional, Dict, List

from rapidfuzz.fuzz import ratio as fuzz_ratio
from unidecode import unidecode

from .exceptions import PARSE_ERRORS, DECODE_ERRORS, NUMERIC_ERRORS
from .config import VALID_YEAR_MIN, VALID_YEAR_MAX


__all__ = [
    "build_url",
    "to_text",
    "strip_accents",
    "normalize_title",
    "trim_title_default",
    "has_placeholder",
    "normalize_person_name",
    "name_signature",
    "extract_last_name",
    "format_author_dirname",
    "parse_authors_any",
    "authors_overlap",
    "author_name_matches",
    "author_in_text",
    "title_similarity",
    "extract_year_from_any",
    "extract_authors_from_any",
    "extract_valid_title",
    "is_valid_value",
    "filter_valid_fields",
    "is_truncated",
    "get_truncation_score",
    "needs_refetch",
    "safe_get_field",
    "safe_get_nested",
    "extract_author_names",
]


def _name_from_dict(d: Dict[str, Any]) -> str:
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


def build_url(base: str, params: Dict[str, Any]) -> str:
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
        parts: List[str] = []
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
        return ", ".join([p for p in parts if p])
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

    Uses unidecode library for comprehensive Unicode to ASCII transliteration.
    """
    try:
        return unidecode(s)
    except PARSE_ERRORS + DECODE_ERRORS:
        return s


def normalize_title(t: Optional[str]) -> str:
    """
    Normalize a title for comparison by stripping accents, lowercasing, removing
    punctuation, brackets, LaTeX formatting, and collapsing repeated whitespace.
    """
    if not t:
        return ""

    import re
    t_str = str(t)

    # Remove LaTeX math delimiters and keep content: $φ$ -> φ
    t_str = re.sub(r'\$([^$]*)\$', r'\1', t_str)

    # Remove LaTeX commands and keep content: \textbf{text} -> text
    t_str = re.sub(r'\\[a-zA-Z]+\{([^}]*)}', r'\1', t_str)

    # Remove remaining backslashes (for commands without braces)
    t_str = re.sub(r'\\[a-zA-Z]+', '', t_str)

    # Standard normalization
    t2 = strip_accents(t_str).lower()
    for ch in ",.;:!?\n\t\r''""'\"-()[]{}":
        t2 = t2.replace(ch, " ")
    return " ".join(t2.split())


def trim_title_default(t: Optional[str]) -> str:
    """
    Clean up a raw title by trimming whitespace and removing trailing full stops while preserving genuine ellipses.
    """
    if t is None:
        return ""
    s = str(t).strip()
    if not s:
        return ""
    # keep ellipses as-is
    if s.endswith("…") or s.endswith("..."):
        return s
    # remove trailing periods (one or two, but not three which would be an ellipsis)
    i = len(s) - 1
    dots = 0
    while i >= 0 and s[i] == '.':
        dots += 1
        i -= 1
    if dots and dots < 3:
        s = s[: len(s) - dots].rstrip()
    return s


def has_placeholder(s: Optional[str]) -> bool:
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
    if "..." in s2 or "…" in s2:
        return True
    if "et al" in low:
        return True
    for bad in ("n/a", "tbd", "unknown", "placeholder"):
        if bad in low:
            return True
    return False


def normalize_person_name(n: Optional[Any]) -> str:
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


def name_signature(n: Optional[Any]) -> Optional[Dict[str, Any]]:
    """
    Derive a compact signature for a person name that keeps the normalized last
    name and initials, working with both "Last, First" and "First Last" formats.
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
        last_norm = re.sub(r"[^a-z0-9]", "", normalize_person_name(last))
        return {"last": last_norm, "initials": initials}
    tokens = n_clean.split()
    if not tokens:
        return None
    last_norm = tokens[-1]
    initials = "".join(t[0] for t in tokens[:-1] if t)
    return {"last": last_norm, "initials": initials}


def extract_last_name(full_name: Optional[str]) -> str:
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

    # Handle "Last, First" format
    if "," in name_str:
        parts = [p.strip() for p in name_str.split(",")]
        last_name = parts[0].strip()
        if last_name:
            return last_name

    # Handle "First Middle Last" format - take the last token
    tokens = name_str.split()
    if tokens:
        return tokens[-1]

    # Fallback to original name
    return name_str


def format_author_dirname(author_name: Optional[str], author_id: str) -> str:
    """
    Format author directory name as "LastName (author_id)".
    Falls back to just author_id if name extraction fails.
    If author_id is empty, uses LastName.
    """
    last_name = extract_last_name(author_name)

    # Sanitize author_id by replacing reserved path characters with dashes
    # Reserved characters: / \ : * ? " < > |
    sanitized_id = re.sub(r'[/\\:*?"<>|]+', '-', author_id)

    if not sanitized_id:
        if last_name and last_name != "Unknown":
            return last_name
        return "unknown"

    if last_name and last_name != "Unknown":
        return f"{last_name} ({sanitized_id})"

    # Fallback to just the sanitized ID if we can't extract a name
    return sanitized_id


def parse_authors_any(authors: Any) -> List[str]:
    """
    Pull author names out of flexible input formats such as lists, dictionaries
    with given/family fields, and BibTeX-style strings with different separators.

    This is a convenience wrapper around extract_authors_from_any for simple use cases.
    """
    return extract_authors_from_any(authors)


def title_similarity(a: Optional[str], b: Optional[str]) -> float:
    """
    Compute a similarity score between two titles after normalization, returning
    a value between 0 and 1 where higher means more similar.

    Uses rapidfuzz for ~10-100x faster fuzzy matching than difflib.SequenceMatcher.
    """
    norm_a = normalize_title(a or "")
    norm_b = normalize_title(b or "")
    if norm_a == norm_b:
        return 1.0
    # rapidfuzz.fuzz.ratio returns 0-100, normalize to 0-1
    return fuzz_ratio(norm_a, norm_b) / 100.0


def authors_overlap(authors_a: Optional[str], authors_b: Optional[str]) -> bool:
    """
    Check whether two author lists share at least one person in common by
    comparing normalized last names and initials and allowing partial matches.
    """
    names_a = parse_authors_any(authors_a or "")
    names_b = parse_authors_any(authors_b or "")
    if not names_a or not names_b:
        return False
    sigs_a = [name_signature(nm) for nm in names_a]
    sigs_b = [name_signature(nm) for nm in names_b]
    for sa in sigs_a:
        if not sa or not sa.get("last"):
            continue
        for sb in sigs_b:
            if not sb or not sb.get("last"):
                continue
            if sa["last"] != sb["last"]:
                continue
            ia = sa.get("initials", "")
            ib = sb.get("initials", "")
            if not ia or not ib or ia == ib or ia.startswith(ib) or ib.startswith(ia):
                return True
    return False


def author_name_matches(target_author: Optional[str], authors: Any) -> bool:
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
        tnorm = normalize_person_name(target_author)
        cnorm = normalize_person_name(nm)
        if tnorm in cnorm or cnorm in tnorm:
            return True
    return False


def author_in_text(target_author: Optional[str], text: Any) -> bool:
    """
    Check whether an author's normalized last name appears as a whole word inside a block of text.
    """
    if not target_author or not text:
        return False
    last = name_signature(target_author)
    if not last:
        return False
    last_tok = last.get("last") or ""
    if not last_tok:
        return False
    txt = normalize_person_name(to_text(text))
    return re.search(rf"\b{re.escape(last_tok)}\b", txt) is not None


def extract_year_from_any(
        obj: Any,
        field_names: Optional[List[str]] = None,
        fallback: Optional[int] = None
) -> Optional[int]:
    """
    Try to recover a four-digit publication year from many possible formats,
    including integers, free text, date dictionaries, Crossref-style date parts,
    and Unix timestamps, falling back when no plausible year is found.
    """
    # plain integer
    if isinstance(obj, int):
        if VALID_YEAR_MIN <= obj <= VALID_YEAR_MAX:
            return obj
        return fallback

    # string with a year somewhere in it
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

    # dictionary - try several strategies
    if isinstance(obj, dict):
        # check custom field names if provided
        if field_names:
            for fname in field_names:
                val = obj.get(fname)
                if val is not None:
                    result = extract_year_from_any(val, field_names=None, fallback=None)
                    if result:
                        return result

        # check common year field names
        for fname in ["year", "publication_year", "pub_year", "date", "published"]:
            val = obj.get(fname)
            if val is not None:
                result = extract_year_from_any(val, field_names=None, fallback=None)
                if result:
                    return result

        # check Crossref/CSL date-parts format
        for fname in ["issued", "published-print", "published-online"]:
            issued = obj.get(fname)
            if isinstance(issued, dict):
                parts = issued.get("date-parts")
                if isinstance(parts, list) and parts and isinstance(parts[0], list):
                    if parts[0] and isinstance(parts[0][0], int):
                        year = parts[0][0]
                        if VALID_YEAR_MIN <= year <= VALID_YEAR_MAX:
                            return year

        # check unix timestamps (OpenReview uses milliseconds)
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

    # list - try first element
    if isinstance(obj, list) and obj:
        return extract_year_from_any(obj[0], field_names=field_names, fallback=fallback)

    return fallback


def extract_authors_from_any(
        obj: Any,
        field_names: Optional[List[str]] = None,
        sanitize_dblp: bool = False,
        name_key: str = "name",
        given_key: Optional[str] = None,
        family_key: Optional[str] = None
) -> List[str]:
    """
    Extract a list of author names from flexible metadata structures such as lists,
    dicts, and formatted strings, optionally cleaning DBLP-specific name artifacts.
    """
    authors: List[str] = []

    if obj is None:
        return authors

    # dict - look for author fields
    if isinstance(obj, dict):
        # try custom field names if provided
        if field_names:
            for fname in field_names:
                val = obj.get(fname)
                if val is not None:
                    authors = extract_authors_from_any(
                        val, field_names=None, sanitize_dblp=sanitize_dblp,
                        name_key=name_key, given_key=given_key, family_key=family_key
                    )
                    if authors:
                        return authors

        # try common field names
        for fname in ["authors", "author", "authorids", "creators", "contributors"]:
            val = obj.get(fname)
            if val is not None:
                authors = extract_authors_from_any(
                    val, field_names=None, sanitize_dblp=sanitize_dblp,
                    name_key=name_key, given_key=given_key, family_key=family_key
                )
                if authors:
                    return authors

        # single author dict with name components
        if given_key and family_key:
            # Use specified keys (e.g., Crossref style)
            given = (obj.get(given_key) or "").strip()
            family = (obj.get(family_key) or "").strip()
            nm = f"{given} {family}".strip() if (given or family) else ""
        else:
            # Auto-detect common patterns
            nm = _name_from_dict(obj)

        if nm:
            if sanitize_dblp:
                from .api_clients import _sanitize_dblp_author
                nm = _sanitize_dblp_author(nm)
            if nm:
                authors.append(nm)
        return authors

    # list of authors
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, str):
                nm = item.strip()
                if nm:
                    if sanitize_dblp:
                        from .api_clients import _sanitize_dblp_author
                        nm = _sanitize_dblp_author(nm)
                    if nm:
                        authors.append(nm)
            elif isinstance(item, dict):
                # extract from dict using specified or auto-detected keys
                if given_key and family_key:
                    given = (item.get(given_key) or "").strip()
                    family = (item.get(family_key) or "").strip()
                    nm = f"{given} {family}".strip() if (given or family) else ""
                else:
                    # Try name key first, then auto-detect given/family
                    nm = (item.get(name_key) or "").strip()
                    if not nm:
                        given = (item.get("given") or item.get("first") or "").strip()
                        family = (item.get("family") or item.get("last") or "").strip()
                        nm = f"{given} {family}".strip() if (given or family) else ""

                if nm:
                    if sanitize_dblp:
                        from .api_clients import _sanitize_dblp_author
                        nm = _sanitize_dblp_author(nm)
                    if nm:
                        authors.append(nm)
            else:
                nm = str(item).strip()
                if nm:
                    authors.append(nm)
        return authors

    # string - parse with separators
    if isinstance(obj, str):
        obj_str = obj.strip()
        if not obj_str:
            return authors

        # try different separator formats
        if " and " in obj_str:
            # BibTeX style
            parts = [p.strip() for p in obj_str.split(" and ")]
            authors = [p for p in parts if p]
        elif " et al." in obj_str or " et al" in obj_str:
            # Handle "Name et al." pattern
            clean_str = obj_str.replace(" et al.", "").replace(" et al", "")
            # If there are commas, split by comma
            if "," in clean_str:
                parts = [p.strip() for p in clean_str.split(",")]
                authors = [p for p in parts if p]
                authors.append("et al.")
            else:
                authors = [clean_str, "et al."]
        elif ";" in obj_str:
            # semicolon-separated
            parts = [p.strip() for p in obj_str.split(";")]
            authors = [p for p in parts if p]
        elif "," in obj_str and " " in obj_str:
            # Could be comma-separated list or single "Last, First" name
            # If there are multiple commas, likely a list
            if obj_str.count(",") > 1:
                parts = [p.strip() for p in obj_str.split(",")]
                authors = [p for p in parts if p]
            else:
                # Single comma - could be "Last, First" OR abbreviated list like "A Smith, B Jones"
                # Check if it looks like abbreviated author names (e.g., "H Huang, DV Arnold")
                parts = [p.strip() for p in obj_str.split(",")]
                if len(parts) == 2:
                    # Check if both parts look like abbreviated names with structure "INITIALS SURNAME"
                    # Pattern: 1-3 capital letters (with optional periods) + SPACE + surname
                    # Examples: "H Huang", "DV Arnold", "H. Huang", "D.V. Arnold", "JK Rowling"
                    # Non-examples: "Smith" (no space), "John" (no initials)
                    import re
                    # Pattern requires: initials (1-3 caps with optional periods) + mandatory space + surname
                    abbreviated_pattern = re.compile(r'^[A-Z]\.?\s*[A-Z]?\.?\s*[A-Z]?\.?\s+[A-Z][a-z]+', re.IGNORECASE)
                    if all(abbreviated_pattern.match(p) for p in parts):
                        # Both parts look like abbreviated names - treat as list
                        authors = parts
                    elif all(" " in p.strip() for p in parts):
                        # Both parts contain spaces (e.g. "First Last, First Last") - treat as list
                        authors = parts
                    else:
                        # Likely "Last, First" format for a single author
                        authors = [obj_str]
                else:
                    authors = [obj_str]
        else:
            # single author
            authors = [obj_str]

        return authors

    # fallback - just convert to string
    s = str(obj).strip()
    if s:
        authors.append(s)

    return authors


def extract_valid_title(
        obj: Any,
        field_names: Optional[List[str]] = None,
        check_placeholder: bool = True
) -> Optional[str]:
    """
    Pull a title from an object using common field names, discard placeholder-like
    values, and return a trimmed version or None when no usable title is available.
    """
    title = None

    # extract from dict
    if isinstance(obj, dict):
        names = field_names or ["title"]
        for fname in names:
            val = obj.get(fname)
            if val:
                # check nested dicts
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

    # skip placeholders if requested
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
        return len(val) > 0

    return True


def filter_valid_fields(
        fields: Dict[str, Any],
        check_placeholder: bool = True
) -> Dict[str, Any]:
    """
    Remove keys whose values are empty, None, or placeholder-like so the
    remaining dictionary contains only useful metadata fields.
    """
    return {
        k: v for k, v in fields.items()
        if is_valid_value(v, check_placeholder=check_placeholder)
    }


def is_truncated(text: Optional[str]) -> bool:
    """
    Detect if text is truncated by checking for ellipsis, et al., or other truncation markers.
    """
    if not text or not isinstance(text, str):
        return False

    text_stripped = text.strip()

    # Check for explicit truncation markers
    truncation_markers = [
        "...",
        "…",
        "et al",
        "et al.",
        "[truncated]",
        "[...]",
    ]

    text_lower = text_stripped.lower()
    for marker in truncation_markers:
        if marker in text_lower:
            return True

    return False


def get_truncation_score(article_data: Dict[str, Any]) -> float:
    """
    Calculate a truncation score for an article by checking key fields, returning
    a value between 0.0 (complete) and 1.0 (fully truncated).
    """
    fields_to_check = []
    truncated_count = 0

    # Check title
    title = article_data.get("title")
    if title:
        fields_to_check.append(title)
        if is_truncated(title):
            truncated_count += 1

    # Check authors
    author_info = article_data.get("author_info")
    if isinstance(author_info, list) and author_info:
        # Convert to string to check for truncation markers
        author_str = str(author_info)
        fields_to_check.append(author_str)
        if is_truncated(author_str):
            truncated_count += 1
    elif author_info:  # Non-list author info
        fields_to_check.append(str(author_info))
        if is_truncated(str(author_info)):
            truncated_count += 1

    # Check publication info
    pub_info = article_data.get("publication_info") or article_data.get("snippet")
    if pub_info:
        fields_to_check.append(str(pub_info))
        if is_truncated(str(pub_info)):
            truncated_count += 1

    if not fields_to_check:
        return 0.0

    return truncated_count / len(fields_to_check)


def needs_refetch(article_data: Dict[str, Any], threshold: float = 0.5) -> bool:
    """
    Determine if article data needs re-fetching from Scholar based on truncation score.
    """
    score = get_truncation_score(article_data)
    return score >= threshold


def safe_get_field(
    obj: Dict[str, Any],
    field: str,
    *,
    default: str = "",
    strip: bool = True,
    required: bool = False,
    check_placeholder: bool = False
) -> Optional[str]:
    """
    Safely extract and validate a string field from a dictionary, handling None values,
    lists, whitespace, and optionally checking for placeholders.
    """
    value = obj.get(field)

    if value is None:
        return None if required else default

    # Handle list values (common in API responses like Crossref, DOI, etc.)
    # Extract first element if list, otherwise convert to string
    if isinstance(value, list):
        if not value:  # Empty list
            return None if required else default
        value = value[0]  # Take first element

    value = str(value)

    if strip:
        value = value.strip()

    if not value:
        return None if required else default

    if check_placeholder and has_placeholder(value):
        return None if required else default

    return value


def safe_get_nested(obj: Any, *keys: str, default=None) -> Any:
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
    return current if current is not None else default


def extract_author_names(
    authors_field: Any,
    *,
    name_key: str = "name",
    given_key: Optional[str] = None,
    family_key: Optional[str] = None
) -> List[str]:
    """
    Extract author names from various formats including list of dicts, list of strings,
    comma-separated strings, and single dict or string.
    """
    return extract_authors_from_any(
        authors_field,
        name_key=name_key,
        given_key=given_key,
        family_key=family_key
    )
