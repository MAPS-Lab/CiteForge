"""BibTeX parsing, serialization, and matching helpers.

Parses BibTeX into field dictionaries and serializes them back with a stable
field order, and provides citation-key and filename helpers. The serializer is
deterministic so cache-hit runs produce
byte-identical `.bib` files.
"""

from __future__ import annotations

import html
import re
import threading
import unicodedata
from functools import lru_cache
from typing import Any, TypeAlias

import bibtexparser
from bibtexparser.bibdatabase import BibDatabase, UndefinedString
from bibtexparser.bparser import BibTexParser

from .cache import response_cache
from .config import (
    AUTHOR_NAME_SUFFIXES,
    BIBTEX_FILENAME_MAX_LENGTH,
    BIBTEX_KEY_MAX_WORDS,
    BIBTEX_PARSE_CACHE_SIZE,
    CACHE_TTL_GEMINI_DAYS,
    VALID_YEAR_MAX,
    VALID_YEAR_MIN,
)
from .latex_utils import latex_to_ascii
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
_FILENAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_\-]+")
_TITLE_WORD_SPLIT_RE = re.compile(r"[^A-Za-z0-9]+")
_CONTROL_CHARS_RE = re.compile(r"[\n\r\t]")

_PARSER_LOCAL = threading.local()

_ParsedBibtex: TypeAlias = tuple[str, str, tuple[tuple[str, str], ...]]
_CORPUS_ENTRY_TYPES = frozenset({"article", "book", "incollection", "inproceedings", "misc", "phdthesis"})
_CORPUS_FIELDS = frozenset(
    {
        "archiveprefix",
        "author",
        "booktitle",
        "chapter",
        "doi",
        "editor",
        "eprint",
        "howpublished",
        "issn",
        "journal",
        "month",
        "note",
        "number",
        "pages",
        "pmid",
        "primaryclass",
        "publisher",
        "school",
        "series",
        "title",
        "url",
        "volume",
        "x_openalex_id",
        "x_pmid",
        "x_s2_paper_id",
        "year",
    }
)


class _StrictCorpusParser(BibTexParser):
    """Use bibtexparser's grammar while preserving duplicate-field rejection."""

    def _init_expressions(self) -> None:
        super()._init_expressions()

        def reject_duplicate_fields(_source: str, _location: int, tokens: Any) -> dict[str, Any]:
            pairs = list(tokens.get("Fields"))
            names = [str(name).casefold() for name, _value in pairs]
            if len(names) != len(set(names)):
                raise ValueError("committed BibTeX contains a duplicate field")
            return dict(reversed(pairs))

        seen: set[int] = set()
        pending = [self._expr.entry]
        while pending:
            expression = pending.pop()
            if id(expression) in seen:
                continue
            seen.add(id(expression))
            if getattr(expression, "resultsName", None) == "Fields":
                expression.set_parse_action(reject_duplicate_fields)
            pending.extend(getattr(expression, "exprs", ()))
            child = getattr(expression, "expr", None)
            if child is not None:
                pending.append(child)


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


def _parser_for_thread() -> BibTexParser:
    """Return one prepared parser per worker thread."""
    parser = getattr(_PARSER_LOCAL, "parser", None)
    if parser is None:
        parser = BibTexParser()
        parser.expect_multiple_parse = True
        _PARSER_LOCAL.parser = parser
    return parser


@lru_cache(maxsize=BIBTEX_PARSE_CACHE_SIZE)
def _parse_bibtex_immutable(bibtex: str) -> _ParsedBibtex | None:
    """Parse into an immutable value safe to share through the LRU cache."""
    parser = _parser_for_thread()
    parser.bib_database = BibDatabase()
    if parser.common_strings:
        parser.bib_database.load_common_strings()
    try:
        entries = bibtexparser.loads(bibtex, parser=parser).entries
    except (TypeError, UndefinedString, ValueError):
        entries = []
    if not entries:
        return None
    raw = dict(entries[0])
    entry_type = str(raw.pop("ENTRYTYPE", "")).lower()
    key = str(raw.pop("ID", ""))
    if not entry_type or not key:
        return None
    fields = tuple((str(name).lower(), str(value).strip()) for name, value in raw.items())
    return entry_type, key, fields


def parse_bibtex_to_dict(bibtex: str) -> dict[str, Any] | None:
    """Parse the first BibTeX entry into CiteForge's stable entry shape."""
    parsed = _parse_bibtex_immutable(bibtex)
    if parsed is None:
        logger.debug(f"header_fail | input={bibtex[:60]}", category=LogCategory.PARSE)
        return None
    entry_type, key, fields = parsed
    return {"type": entry_type, "key": key, "fields": dict(fields)}


def parse_strict_bibtex_document(content: bytes) -> dict[str, Any]:
    """Parse one complete committed-corpus BibTeX document without recovery."""
    if not isinstance(content, bytes):
        raise TypeError("committed BibTeX content must be bytes")
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("committed BibTeX must be UTF-8") from exc
    if not text.strip():
        raise ValueError("committed BibTeX must not be empty")
    if any(unicodedata.category(character) == "Cc" and character not in "\r\n\t" for character in text):
        raise ValueError("committed BibTeX contains control characters")
    parser = _StrictCorpusParser(common_strings=False)
    parser.expect_multiple_parse = True
    try:
        database = bibtexparser.loads(text, parser=parser)
    except ValueError as exc:
        if "duplicate field" in str(exc):
            raise
        raise ValueError("committed BibTeX is malformed") from exc
    except (TypeError, UndefinedString) as exc:
        raise ValueError("committed BibTeX is malformed") from exc
    if database.comments or database.preambles or database.strings:
        raise ValueError("committed BibTeX contains directives or unconsumed text")
    if len(database.entries) != 1:
        raise ValueError("committed BibTeX requires exactly one entry")
    raw = dict(database.entries[0])
    entry_type = raw.pop("ENTRYTYPE", None)
    key = raw.pop("ID", None)
    if not isinstance(entry_type, str) or entry_type.casefold() not in _CORPUS_ENTRY_TYPES:
        raise ValueError("committed BibTeX entry type is unsupported")
    if not isinstance(key, str) or not key.strip():
        raise ValueError("committed BibTeX citation key is blank")
    if any(unicodedata.category(character) == "Cc" for character in key):
        raise ValueError("committed BibTeX citation key contains control characters")
    if not all(isinstance(name, str) and isinstance(value, str) for name, value in raw.items()):
        raise ValueError("committed BibTeX fields must be strings")
    fields = {
        name.casefold(): (
            re.sub(r"[ \r\n\t]+", " ", value).strip()
            if name.casefold() in {"doi", "howpublished", "url"}
            else _normalize_to_ascii(re.sub(r"[ \r\n\t]+", " ", value).strip())
        )
        for name, value in sorted(raw.items())
    }
    if "title" in fields:
        fields["title"] = _sanitize_title(fields["title"]) or fields["title"]
    fields = {name: value for name, value in fields.items() if value or name in {"title", "year"}}
    for name, value in tuple(fields.items()):
        if name not in {"url", "doi"} and "&" in value and r"\&" not in value:
            fields[name] = value.replace("&", r"\&")
    if any(any(unicodedata.category(character) == "Cc" for character in value) for value in fields.values()):
        raise ValueError("committed BibTeX field contains control characters")
    if not set(fields) <= _CORPUS_FIELDS:
        raise ValueError("committed BibTeX contains an unsupported field")
    if not fields.get("title"):
        raise ValueError("committed BibTeX title is blank")
    year = fields.get("year", "")
    if not re.fullmatch(r"[0-9]{4}", year) or not VALID_YEAR_MIN <= int(year) <= VALID_YEAR_MAX:
        raise ValueError("committed BibTeX year is absent or outside the project range")
    return {"type": entry_type.casefold(), "key": key.strip(), "fields": fields}


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


_MULTI_SPACE_RE = re.compile(r"  +")
_APOS_YEAR_RE = re.compile(r"\s+'(\d{2})\b")
_BARE_AMP_RE = re.compile(r"(?<!\\)&")
_URL_TILDE_RE = re.compile(r"(?<=[:/])~")
_URL_TILDE_SENTINEL = "CITEFORGEURLTILDE"
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


def _normalize_to_ascii(val: str) -> str:
    """Normalize Unicode to ASCII for BibTeX compatibility.

    Decodes HTML entities, strips LaTeX formatting, converts accented
    characters via unidecode, and replaces curly quotes and dashes.
    """
    # html.unescape only changes a string containing an '&' entity.
    if "&" in val:
        val = html.unescape(val)
        # pylatexenc treats a bare ampersand as a TeX alignment marker and
        # drops it. Protect decoded HTML ampersands as ordinary text first.
        val = _BARE_AMP_RE.sub(r"\\&", val)
    val = _URL_TILDE_RE.sub(_URL_TILDE_SENTINEL, val)
    val = latex_to_ascii(val, math_mode="verbatim")
    val = val.replace(_URL_TILDE_SENTINEL, "~")
    val = val.replace("---", "--")
    val = val.replace("--", "-")
    val = val.replace("''", '"')

    val = _MULTI_SPACE_RE.sub(" ", val)

    # strip_accents and every _UNICODE_TO_ASCII key are non-ASCII, so both
    # are no-ops on an already-ASCII string; skip them in that common case.
    if not val.isascii():
        val = strip_accents(val)
        for unicode_char, ascii_char in _UNICODE_TO_ASCII.items():
            val = val.replace(unicode_char, ascii_char)

    # The apostrophe-year fixup requires a literal single quote to match.
    if "'" in val:
        val = _APOS_YEAR_RE.sub(r"'\1", val)

    return val.strip()


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
            if k not in {"doi", "howpublished", "url"}:
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
