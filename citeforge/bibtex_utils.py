"""BibTeX parsing, serialization, and matching helpers.

Parses BibTeX into field dictionaries and serializes them back with a stable
field order, and provides citation-key and filename helpers. The serializer is
deterministic so cache-hit runs produce
byte-identical `.bib` files.
"""

from __future__ import annotations

import html
import re
from typing import Any

from .cache import response_cache
from .config import (
    AUTHOR_NAME_SUFFIXES,
    BIBTEX_FILENAME_MAX_LENGTH,
    BIBTEX_KEY_MAX_WORDS,
    CACHE_TTL_GEMINI_DAYS,
)
from .log_utils import LogCategory, logger
from .text_utils import (
    extract_year_from_any,
    normalize_title,
    strip_accents,
)

_TITLE_STOP_WORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "the",
        "on",
        "for",
        "of",
        "and",
        "to",
        "in",
        "with",
        "using",
        "via",
        "from",
        "by",
        "at",
        "into",
        "through",
    }
)

# Compiled once at import; the parse/serialize/key helpers below run per entry.
_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_NON_WORD_RE = re.compile(r"\W+")
_ENTRY_HEAD_RE = re.compile(r"@\s*([a-zA-Z]+)\s*\{\s*([^,\s]+)\s*,")
_SINGLE_LINE_ENTRY_RE = re.compile(r"@\s*[a-zA-Z]+\s*\{\s*[^,\s]+\s*,\s*(.+)\s*\}\s*$", re.DOTALL)
_FIELD_ASSIGN_RE = re.compile(r"^\s*([a-zA-Z][a-zA-Z0-9_\-]*)\s*=\s*(.*)$")
_QUOTED_VALUE_RE = re.compile(r'^"([^"]*)"')
_FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_TITLE_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\n\r\t]")


def make_bibkey(title: str, authors: list[str], year: int, fallback: str = "entry") -> str:
    """Build a compact citation key from the first author's surname, the year,
    and the first title word, falling back to a generic label."""
    last = _NON_ALNUM_RE.sub("", authors[0].split()[-1]) if authors and authors[0] else ""
    title_words = title.split()
    word = _NON_ALNUM_RE.sub("", title_words[0]) if title_words else ""
    y = str(year) if year else ""
    parts = [p for p in [last, y, word] if p]
    base = "".join(parts) if parts else fallback
    base = _NON_WORD_RE.sub("", base)
    return base or fallback


def build_minimal_bibtex(title: str, authors: list[str], year: int, keyhint: str) -> str:
    """Create a minimal @misc entry from a title, optional authors, and
    optional year."""
    key = make_bibkey(title, authors, year, fallback=_NON_WORD_RE.sub("", keyhint) or "entry")
    lines = [f"@misc{{{key},", f"  title = {{{title}}},"]
    if authors:
        lines.append(f"  author = {{{' and '.join(authors)}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _parse_bibtex_head(bibtex: str) -> dict[str, str] | None:
    """Pull the entry type and citation key from the @type{key, opening of a
    BibTeX entry, or None when the pattern is absent."""
    m = _ENTRY_HEAD_RE.search(bibtex)
    if not m:
        return None
    return {"type": m.group(1).strip(), "key": m.group(2).strip()}


def _extract_balanced_braces(text: str, start: int) -> str | None:
    """Extract the text inside a balanced brace pair starting at *start*,
    preserving nested braces."""
    if start >= len(text) or text[start] != "{":
        return None
    depth = 0
    result: list[str] = []
    for ch in text[start:]:
        if ch == "{":
            depth += 1
            if depth > 1:  # Don't include the outermost braces
                result.append(ch)
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return "".join(result)
            result.append(ch)
        else:
            result.append(ch)
    return None  # unbalanced


def _assign_field_value(fields: dict[str, str], field_name: str, full_value: str) -> None:
    """Assign a parsed value to *fields*, handling brace-wrapped, quoted, and
    plain text values in one place."""
    if full_value.startswith("{"):
        val = _extract_balanced_braces(full_value, 0)
        fields[field_name] = val.strip() if val is not None else full_value.strip().strip("{}")
    elif full_value.startswith('"'):
        m2 = _QUOTED_VALUE_RE.match(full_value)
        fields[field_name] = m2.group(1).strip() if m2 else full_value.strip()
    else:
        fields[field_name] = full_value.strip()


def parse_bibtex_to_dict(bibtex: str) -> dict[str, Any] | None:
    """Parse a BibTeX string into {type, key, fields}, handling nested braces,
    multi-line fields, and the single-line entries common in API responses."""
    head = _parse_bibtex_head(bibtex)
    if not head:
        logger.debug(f"header_fail | input={bibtex[:60]}", category=LogCategory.PARSE)
        return None
    fields: dict[str, str] = {}

    single_line_pattern = _SINGLE_LINE_ENTRY_RE.search(bibtex)

    if single_line_pattern and "\n" not in bibtex.strip():
        fields_text = single_line_pattern.group(1).strip()

        brace_depth = 0
        in_quote = False
        field_start = 0
        field_parts = []

        for i, char in enumerate(fields_text):
            if char == "{" and not in_quote:
                brace_depth += 1
            elif char == "}" and not in_quote:
                brace_depth -= 1
            elif char == '"' and brace_depth == 0:
                in_quote = not in_quote
            elif char == "," and brace_depth == 0 and not in_quote:
                field_parts.append(fields_text[field_start:i].strip())
                field_start = i + 1

        if field_start < len(fields_text):
            last_part = fields_text[field_start:].strip()
            if last_part:
                field_parts.append(last_part)

        # Now parse each field
        for part in field_parts:
            m = _FIELD_ASSIGN_RE.match(part)
            if m:
                field_name = m.group(1).lower()
                field_value = m.group(2).strip()
                _assign_field_value(fields, field_name, field_value)

        return {"type": head["type"].lower(), "key": head["key"], "fields": fields}

    # Multi-line format parsing
    current_field = None
    accumulator: list[str] = []

    for line in bibtex.split("\n"):
        m = _FIELD_ASSIGN_RE.match(line)

        if m:
            if current_field and accumulator:
                full_value = " ".join(accumulator)
                _assign_field_value(fields, current_field, full_value)

            current_field = m.group(1).lower()
            rest = m.group(2).strip()
            accumulator = [rest]

            if rest.startswith("{"):
                val = _extract_balanced_braces(rest, 0)
                if val is not None:
                    fields[current_field] = val.strip()
                    current_field = None
                    accumulator = []
            elif rest.startswith('"'):
                m2 = _QUOTED_VALUE_RE.match(rest)
                if m2:
                    fields[current_field] = m2.group(1).strip()
                    current_field = None
                    accumulator = []
        elif current_field:
            stripped = line.strip()
            if stripped:
                accumulator.append(stripped)
                full_value = " ".join(accumulator)
                if full_value.startswith("{"):
                    val = _extract_balanced_braces(full_value, 0)
                    if val is not None:
                        fields[current_field] = val.strip()
                        current_field = None
                        accumulator = []

    if current_field and accumulator:
        full_value = " ".join(accumulator)
        _assign_field_value(fields, current_field, full_value)

    return {"type": head["type"].lower(), "key": head["key"], "fields": fields}


# Canonical BibTeX field emission order. Fields not listed are appended in
# sorted() order afterwards. This ordering is part of the byte-identity output
# contract; do not reorder without updating the
# golden serializer test.
PREFERRED_FIELD_ORDER: tuple[str, ...] = (
    "title",
    "author",
    "year",
    "journal",
    "booktitle",
    "howpublished",
    "publisher",
    "volume",
    "number",
    "pages",
    "doi",
    "url",
    "eprint",
    "archiveprefix",
    "primaryclass",
)


# Serializer cleanup tables, compiled once at import. bibtex_from_dict runs
# per entry, so these must not be rebuilt inside the call.
_LATEX_FORMAT_CMD_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(r"\\" + cmd + r"\s*\{")
    for cmd in (
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
)
# For each old-style command, match {\xx content} first, then {\xx{content}}.
_OLD_STYLE_CMD_PATTERNS: tuple[tuple[re.Pattern[str], re.Pattern[str]], ...] = tuple(
    (
        re.compile(r"\{\\" + cmd + r"\s+([^}]+)\}"),
        re.compile(r"\{\\" + cmd + r"\s*\{([^}]+)\}\}"),
    )
    for cmd in ("it", "bf", "em", "sc", "tt", "rm", "sf", "sl")
)
_LATEX_SPECIAL_CHARS = {
    r"\&": "&",
    r"\%": "%",
    r"\$": "$",
    r"\#": "#",
    r"\_": "_",
    r"\{": "{",
    r"\}": "}",
}
_TILDE_RE = re.compile(r"(?<![:/])~")
_MULTI_SPACE_RE = re.compile(r"  +")
_APOS_YEAR_RE = re.compile(r"\s+'(\d{2})\b")
_UNICODE_TO_ASCII = {
    "\u2019": "'",  # Right single quotation mark → apostrophe
    "\u2018": "'",  # Left single quotation mark → apostrophe
    "\u201c": '"',  # Left double quotation mark → quote
    "\u201d": '"',  # Right double quotation mark → quote
    "\u2013": "-",  # En dash → hyphen
    "\u2014": "--",  # Em dash → double hyphen
    "\u2026": "...",  # Horizontal ellipsis → three dots
    "\u00a0": " ",  # Non-breaking space → regular space
}


def _strip_latex_formatting(val: str) -> str:
    r"""Remove LaTeX formatting commands while preserving their content.

    Handles \command{...} (textit, textbf, emph, etc.), old-style {\xx ...}
    commands (it, bf, em, etc.), escaped special characters
    (\&, \%, \$, \#, \_, \{, \}), tildes, and dashes (-- / ---).
    """
    # Fast path: every transform below requires a backslash (commands and
    # escaped specials), a tilde, a "--" run, or a double space. If none are
    # present the function is a no-op, so skip the whole command scan.
    if "\\" not in val and "~" not in val and "--" not in val and "  " not in val:
        return val

    prev_val = None
    while prev_val != val:
        prev_val = val
        for cmd_pattern in _LATEX_FORMAT_CMD_PATTERNS:
            # Match \command{...} with balanced braces
            while True:
                match = cmd_pattern.search(val)
                if not match:
                    break
                # Find the matching closing brace
                start = match.end() - 1  # Position of opening brace
                depth = 0
                end = start
                for i in range(start, len(val)):
                    if val[i] == "{":
                        depth += 1
                    elif val[i] == "}":
                        depth -= 1
                        if depth == 0:
                            end = i
                            break
                if depth == 0:
                    # Extract content and replace
                    content = val[start + 1 : end]
                    val = val[: match.start()] + content + val[end + 1 :]
                else:
                    # Unbalanced braces, skip this match
                    break

    for pattern, pattern2 in _OLD_STYLE_CMD_PATTERNS:
        val = pattern.sub(r"\1", val)
        val = pattern2.sub(r"\1", val)

    for latex_char, plain_char in _LATEX_SPECIAL_CHARS.items():
        val = val.replace(latex_char, plain_char)

    val = _TILDE_RE.sub(" ", val)

    val = val.replace("---", "--")
    val = val.replace("--", "-")

    val = _MULTI_SPACE_RE.sub(" ", val)

    return val


def _normalize_to_ascii(val: str) -> str:
    """Normalize Unicode to ASCII for BibTeX compatibility.

    Decodes HTML entities, strips LaTeX formatting, converts accented
    characters via unidecode, and replaces curly quotes and dashes.
    """
    # html.unescape only changes a string containing an '&' entity.
    if "&" in val:
        val = html.unescape(val)
    val = _strip_latex_formatting(val)

    # strip_accents and every _UNICODE_TO_ASCII key are non-ASCII, so both
    # are no-ops on an already-ASCII string; skip them in that common case.
    if not val.isascii():
        val = strip_accents(val)
        for unicode_char, ascii_char in _UNICODE_TO_ASCII.items():
            val = val.replace(unicode_char, ascii_char)

    # The apostrophe-year fixup requires a literal single quote to match.
    if "'" in val:
        val = _APOS_YEAR_RE.sub(r"'\1", val)

    return val


def _sanitize_title(title_val: str | None) -> str | None:
    """Drop a duplicated after-colon suffix and trailing periods (ellipses
    are preserved) from a title at serialization time."""
    if title_val is None:
        return None
    t = title_val.strip()
    dup_suffix_removed = False
    trailing_period = False

    # Remove duplicated suffix after colon
    if ":" in t:
        parts = t.split(":")
        if len(parts) >= 3:  # Has at least 2 colons
            # Check if last two parts are the same (after stripping whitespace)
            last_part = parts[-1].strip()
            second_last_part = parts[-2].strip()
            if last_part and last_part == second_last_part and len(last_part) > 15:
                # Remove the duplicated last part
                t = ":".join(parts[:-1]).strip()
                dup_suffix_removed = True

    # trim trailing periods unless it's an ellipsis
    if t.endswith("...") or t.endswith("\u2026"):
        if dup_suffix_removed:
            logger.debug(
                "title_sanitize | dup_suffix_removed=True | trailing_period=False",
                category=LogCategory.SERIAL,
            )
        return t
    if t.endswith("."):
        trailing_period = True
        t = t[:-1].rstrip()

    if dup_suffix_removed or trailing_period:
        logger.debug(
            f"title_sanitize | dup_suffix_removed={dup_suffix_removed} | trailing_period={trailing_period}",
            category=LogCategory.SERIAL,
        )
    return t


def bibtex_from_dict(entry: dict[str, Any]) -> str:
    """Format a dict-based BibTeX entry back into text, listing common
    citation fields first and remaining fields in a stable sorted order."""
    etype = (entry.get("type") or "misc").lower()
    key = entry.get("key") or "entry"
    fields: dict[str, str] = entry.get("fields") or {}
    preferred = list(PREFERRED_FIELD_ORDER)
    lines = [f"@{etype}{{{key},"]
    preferred_set = set(preferred)
    ordered_keys = list(preferred) + sorted(k for k in fields if k not in preferred_set)
    for k in ordered_keys:
        val = fields.get(k)
        if val is not None and str(val).strip():
            val = _normalize_to_ascii(str(val))
            if k == "title":
                val = _sanitize_title(val) or val
            # Escape bare & for valid BibTeX (but not in URLs/DOIs)
            if k not in ("url", "doi") and "&" in val and r"\&" not in val:
                val = val.replace("&", r"\&")
            lines.append(f"  {k} = {{{val}}},")
    if len(lines) > 1 and lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _short_title_for_key(title: str, max_words: int = BIBTEX_KEY_MAX_WORDS, gemini_api_key: str | None = None) -> str:
    """Pick a few informative title words, skipping stop words, and join them
    into a compact phrase for keys and filenames.

    With a Gemini API key, checks the ResponseCache first, calls the Gemini
    API on a miss, caches successful responses, and falls back to the
    algorithmic path on failure.

    The cache is used only when max_words equals BIBTEX_KEY_MAX_WORDS. A
    larger max_words means a filename-collision disambiguation pass, which
    bypasses the cache to pull more title words algorithmically.
    """
    normalized_title = normalize_title(title)
    use_cache = max_words == BIBTEX_KEY_MAX_WORDS

    if gemini_api_key and use_cache:
        cached = response_cache.get("gemini", normalized_title)
        if cached is not None:
            saved_short = (
                _CONTROL_CHARS_RE.sub("", cached.get("short_title", "")) if not cached.get("_negative") else ""
            )
            if saved_short:
                return saved_short
            # Fall through to algorithmic path for negative/empty cache hits
        else:
            from .clients.utility_apis import gemini_generate_short_title

            logger.debug(f"gemini_api_call | title={title[:60]}", category=LogCategory.CITEKEY)
            gemini_result = gemini_generate_short_title(title, gemini_api_key, max_words)

            if gemini_result:
                logger.debug(f"gemini_api_success | short={gemini_result}", category=LogCategory.CITEKEY)
                response_cache.put(
                    "gemini",
                    normalized_title,
                    {"short_title": gemini_result},
                    ttl_days=CACHE_TTL_GEMINI_DAYS,
                )
                return gemini_result

    words = [w for w in _TITLE_WORD_SPLIT_RE.split(title) if w]
    picks: list[str] = []
    for w in words:
        if w.lower() not in _TITLE_STOP_WORDS:
            picks.append(w)
            if len(picks) >= max_words:
                break
    if not picks and words:
        picks = words[:max_words]
    return "".join(w[:1].upper() + w[1:] for w in picks)


def _first_author_lastname(authors_field: str | None) -> str | None:
    """Derive the first author's last name from a BibTeX-style author field,
    handling "First Last" and "Last, First" formats.

    Strips academic suffixes (Jr, Sr, II, III, etc.) so that names like
    "Jose F. Rodrigues Jr" produce "rodrigues" instead of "jr".
    """
    if not authors_field:
        return None
    separator = " and " if " and " in authors_field else ";"
    parts = [p.strip() for p in authors_field.split(separator) if p.strip()]
    if not parts:
        return None
    first = parts[0]
    if "," in first:
        last = first.split(",")[0].strip()
    else:
        toks = first.split()
        while len(toks) > 1 and toks[-1].rstrip(".").lower() in AUTHOR_NAME_SUFFIXES:
            toks.pop()
        last = toks[-1] if toks else first
    last = _NON_ALNUM_RE.sub("", strip_accents(last)).lower()
    return last or None


def build_standard_citekey(entry: dict[str, Any], gemini_api_key: str | None = None) -> str | None:
    """Build a citation key such as "Smith2024:MachineLearning" from the first
    author's name, the year, and key title words.

    Uses BIBTEX_KEY_MAX_WORDS (default 4) title words so similar titles like
    "Dairy DigiD: keypoint..." vs "Dairy DigiD: Edge-Cloud..." get distinct keys.
    """
    fields = entry.get("fields") or {}
    title = (fields.get("title") or "").strip()
    if not title:
        return None
    year = fields.get("year")
    y_int = extract_year_from_any(year, fallback=None)
    y = str(y_int) if y_int else "0000"
    author = fields.get("author") or ""
    last = _first_author_lastname(author) or "anon"
    last_cap = last[:1].upper() + last[1:]
    short = _short_title_for_key(title, max_words=BIBTEX_KEY_MAX_WORDS, gemini_api_key=gemini_api_key) or "Title"
    return f"{last_cap}{y}:{short}"


def short_filename_for_entry(
    entry: dict[str, Any], gemini_api_key: str | None = None, existing_files: set[str] | None = None, max_words: int = 2
) -> str:
    """Construct a concise .bib filename from the first author's name, the
    year, and a shortened title.

    When existing_files is provided, appends more title words to resolve
    filename collisions.
    """
    fields = entry.get("fields") or {}
    author = fields.get("author") or ""
    last = _first_author_lastname(author) or "anon"
    last_cap = last[:1].upper() + last[1:]
    year = fields.get("year")
    y_int = extract_year_from_any(year, fallback=None)
    y = str(y_int) if y_int else "0000"
    title = fields.get("title") or ""

    def _build_filename(num_words: int) -> str:
        short = _short_title_for_key(title, max_words=num_words, gemini_api_key=gemini_api_key) or "Title"
        base = _FILENAME_SANITIZE_RE.sub("", f"{last_cap}{y}-{short}")[:BIBTEX_FILENAME_MAX_LENGTH]
        return f"{base}.bib"

    for num_words in range(max_words, 11):
        filename = _build_filename(num_words)
        if existing_files is None or filename not in existing_files:
            logger.debug(f"filename_ok | {filename}", category=LogCategory.CITEKEY)
            return filename
        logger.debug(f"filename_collision | file={filename} | attempt={num_words}", category=LogCategory.CITEKEY)

    filename = _build_filename(20)
    logger.debug(f"filename_ok | {filename}", category=LogCategory.CITEKEY)
    return filename
