from __future__ import annotations

import contextlib
import html
import os
import re
import threading
from typing import Any

from slugify import slugify

from .cache import response_cache
from .config import (
    AUTHOR_NAME_SUFFIXES,
    BIBTEX_FILENAME_MAX_LENGTH,
    BIBTEX_KEY_MAX_WORDS,
    CACHE_TTL_GEMINI_DAYS,
    DEFAULT_DICTIONARY_FILE,
    PREPRINT_DOI_PREFIXES,
    PREPRINT_SERVERS,
    SIM_DEDUP_COMPOSITE_THRESHOLD,
    SIM_DEDUP_MULTI_SIGNAL_MIN,
    SIM_FILE_DUPLICATE_THRESHOLD,
)
from .id_utils import _norm_doi, external_ids_match, extract_arxiv_eprint
from .io_utils import safe_read_json
from .text_utils import (
    author_overlap_ratio,
    authors_overlap,
    compute_dedup_score,
    extract_year_from_any,
    has_placeholder,
    normalize_title,
    strip_accents,
    title_is_truncated_match,
    title_similarity,
)


def make_bibkey(title: str, authors: list[str], year: int, fallback: str = "entry") -> str:
    """
    Build a compact BibTeX citation key using the first author's surname, the
    publication year, and the first word of the title, falling back to a generic
    label when needed.
    """
    last = re.sub(r"[^A-Za-z0-9]", "", authors[0].split()[-1]) if authors and authors[0] else ""
    word = re.sub(r"[^A-Za-z0-9]", "", (title.split()[:1] or [""])[0])
    y = str(year) if year else ""
    parts = [p for p in [last, y, word] if p]
    base = "".join(parts) if parts else fallback
    base = re.sub(r"\W+", "", base)
    return base or fallback


def build_minimal_bibtex(title: str, authors: list[str], year: int, keyhint: str) -> str:
    """
    Create a simple BibTeX @misc entry from a title, optional authors, and optional
    year so that even sparse metadata can be stored consistently.
    """
    key = make_bibkey(title, authors, year, fallback=re.sub(r"\W+", "", keyhint) or "entry")
    lines = [f"@misc{{{key},", f"  title = {{{title}}},"]
    if authors:
        lines.append(f"  author = {{{' and '.join(authors)}}},")
    if year:
        lines.append(f"  year = {{{year}}},")
    if lines and lines[-1].endswith(","):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def _parse_bibtex_head(bibtex: str) -> dict[str, str] | None:
    """
    Read the opening line of a BibTeX entry and pull out the entry type and
    citation key if they follow the expected @type{key, pattern.
    """
    m = re.search(r"@\s*([a-zA-Z]+)\s*\{\s*([^,\s]+)\s*,", bibtex)
    if not m:
        return None
    return {"type": m.group(1).strip(), "key": m.group(2).strip()}


def _extract_balanced_braces(text: str, start: int) -> str | None:
    """
    Extract the text inside a balanced pair of braces starting at the given
    position, keeping track of nested braces so inner blocks are preserved
    correctly.
    """
    if start >= len(text) or text[start] != '{':
        return None
    depth = 0
    result = []
    i = start
    while i < len(text):
        ch = text[i]
        if ch == '{':
            depth += 1
            if depth > 1:  # Don't include the outermost braces
                result.append(ch)
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return ''.join(result)
            result.append(ch)
        else:
            result.append(ch)
        i += 1
    return None  # unbalanced


def _assign_field_value(fields: dict[str, str], field_name: str, full_value: str) -> None:
    """
    Helper to assign a parsed value to fields based on whether the value is
    brace-wrapped, quoted, or plain text. Keeps logic in one place to avoid
    duplication.
    """
    if full_value.startswith('{'):
        val = _extract_balanced_braces(full_value, 0)
        fields[field_name] = val.strip() if val is not None else full_value.strip().strip('{}')
    elif full_value.startswith('"'):
        m2 = re.match(r'^"([^"]*)"', full_value)
        fields[field_name] = m2.group(1).strip() if m2 else full_value.strip()
    else:
        fields[field_name] = full_value.strip()


def parse_bibtex_to_dict(bibtex: str) -> dict[str, Any] | None:
    """
    Turn a BibTeX string into a dictionary that separates the entry type, key,
    and field values while handling nested braces and multi-line fields.
    Also handles single-line BibTeX entries common in API responses.
    """
    head = _parse_bibtex_head(bibtex)
    if not head:
        return None
    fields: dict[str, str] = {}

    # Check if this is a single-line entry by looking for the pattern
    # where fields are comma-separated all on one line after the entry key
    # Example: @type{key, field1={val}, field2={val}, ...}
    single_line_pattern = re.search(
        r'@\s*[a-zA-Z]+\s*\{\s*[^,\s]+\s*,\s*(.+)\s*\}\s*$',
        bibtex,
        re.DOTALL
    )

    if single_line_pattern and '\n' not in bibtex.strip():
        # Parse single-line format by splitting on commas outside of braces AND quotes
        fields_text = single_line_pattern.group(1).strip()

        # Split by commas while respecting brace nesting and quotes
        brace_depth = 0
        in_quote = False
        field_start = 0
        field_parts = []

        for i, char in enumerate(fields_text):
            if char == '{' and not in_quote:
                brace_depth += 1
            elif char == '}' and not in_quote:
                brace_depth -= 1
            elif char == '"' and brace_depth == 0:
                in_quote = not in_quote
            elif char == ',' and brace_depth == 0 and not in_quote:
                # Found a field separator
                field_parts.append(fields_text[field_start:i].strip())
                field_start = i + 1

        # Don't forget the last field
        if field_start < len(fields_text):
            last_part = fields_text[field_start:].strip()
            if last_part:
                field_parts.append(last_part)

        # Now parse each field
        for part in field_parts:
            m = re.match(r'^\s*([a-zA-Z][a-zA-Z0-9_\-]*)\s*=\s*(.*)$', part)
            if m:
                field_name = m.group(1).lower()
                field_value = m.group(2).strip()
                _assign_field_value(fields, field_name, field_value)

        return {"type": head["type"].lower(), "key": head["key"], "fields": fields}

    # Multi-line format parsing (original logic)
    # state machine for multi-line values
    current_field = None
    accumulator = []

    lines = bibtex.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]

        # check if this line starts a new field
        m = re.match(r'^\s*([a-zA-Z][a-zA-Z0-9_\-]*)\s*=\s*(.*)$', line)

        if m:
            # save previous field
            if current_field and accumulator:
                full_value = ' '.join(accumulator)
                _assign_field_value(fields, current_field, full_value)

            current_field = m.group(1).lower()
            rest = m.group(2).strip()
            accumulator = [rest]

            # check if value is complete on this line
            if rest.startswith('{'):
                # try to extract balanced braces
                val = _extract_balanced_braces(rest, 0)
                if val is not None:
                    fields[current_field] = val.strip()
                    current_field = None
                    accumulator = []
            elif rest.startswith('"'):
                # complete quoted value on one line
                m2 = re.match(r'^"([^"]*)"', rest)
                if m2:
                    fields[current_field] = m2.group(1).strip()
                    current_field = None
                    accumulator = []
        elif current_field:
            # continuation of field value
            stripped = line.strip()
            if stripped:
                accumulator.append(stripped)
                # try to parse accumulated value
                full_value = ' '.join(accumulator)
                if full_value.startswith('{'):
                    val = _extract_balanced_braces(full_value, 0)
                    if val is not None:
                        fields[current_field] = val.strip()
                        current_field = None
                        accumulator = []

        i += 1

    # save last field
    if current_field and accumulator:
        full_value = ' '.join(accumulator)
        _assign_field_value(fields, current_field, full_value)

    return {"type": head["type"].lower(), "key": head["key"], "fields": fields}


def bibtex_from_dict(entry: dict[str, Any]) -> str:
    """
    Format a dictionary-based BibTeX entry back into text, listing common
    citation fields first and writing remaining fields in a stable order.
    """

    def _strip_latex_formatting(val: str) -> str:
        r"""
        Remove LaTeX formatting commands while preserving the content inside.
        Handles commands like:
        - \textit{content} -> content
        - \textbf{content} -> content
        - \emph{content} -> content
        - \textsc{content} -> content
        - \texttt{content} -> content
        - \textrm{content} -> content
        - \textsf{content} -> content
        - \underline{content} -> content
        - \uppercase{content} -> content
        - \lowercase{content} -> content
        - \mbox{content} -> content
        - \hbox{content} -> content
        - {\it content} -> content (old-style)
        - {\bf content} -> content (old-style)
        - {\em content} -> content (old-style)
        - {\sc content} -> content (old-style)
        - {\tt content} -> content (old-style)
        - {\rm content} -> content (old-style)
        - {\sf content} -> content (old-style)
        Also handles:
        - \& -> &
        - \% -> %
        - \$ -> $
        - \# -> #
        - \_ -> _
        - \{ -> {
        - \} -> }
        - ~ (non-breaking space) -> space
        - -- (en dash) -> -
        - --- (em dash) -> --
        """
        # List of LaTeX formatting commands to strip (command name without backslash)
        formatting_commands = [
            'textit', 'textbf', 'emph', 'textsc', 'texttt', 'textrm', 'textsf',
            'underline', 'uppercase', 'lowercase', 'mbox', 'hbox', 'text'
        ]

        # Process \command{content} style - iterate until no more changes
        # (handles nested commands)
        prev_val = None
        while prev_val != val:
            prev_val = val
            for cmd in formatting_commands:
                # Match \command{...} with balanced braces
                pattern = r'\\' + cmd + r'\s*\{'
                while True:
                    match = re.search(pattern, val)
                    if not match:
                        break
                    # Find the matching closing brace
                    start = match.end() - 1  # Position of opening brace
                    depth = 0
                    end = start
                    for i in range(start, len(val)):
                        if val[i] == '{':
                            depth += 1
                        elif val[i] == '}':
                            depth -= 1
                            if depth == 0:
                                end = i
                                break
                    if depth == 0:
                        # Extract content and replace
                        content = val[start + 1:end]
                        val = val[:match.start()] + content + val[end + 1:]
                    else:
                        # Unbalanced braces, skip this match
                        break

        # Process old-style {\xx content} commands
        old_style_commands = ['it', 'bf', 'em', 'sc', 'tt', 'rm', 'sf', 'sl']
        for cmd in old_style_commands:
            # Match {\xx content} or {\xx{content}}
            pattern = r'\{\\' + cmd + r'\s+([^}]+)\}'
            val = re.sub(pattern, r'\1', val)
            # Also handle {\xx{content}}
            pattern2 = r'\{\\' + cmd + r'\s*\{([^}]+)\}\}'
            val = re.sub(pattern2, r'\1', val)

        # Handle escaped special characters
        special_chars = {
            r'\&': '&',
            r'\%': '%',
            r'\$': '$',
            r'\#': '#',
            r'\_': '_',
            r'\{': '{',
            r'\}': '}',
        }
        for latex_char, plain_char in special_chars.items():
            val = val.replace(latex_char, plain_char)

        # Handle tilde (non-breaking space in LaTeX) → regular space
        # But be careful not to replace tildes that are part of URLs
        # Only replace standalone ~ not preceded by http/ftp/etc
        val = re.sub(r'(?<![:/])~', ' ', val)

        # Handle dashes: --- → --, -- → - (for plain text, not BibTeX)
        # Actually, keep -- as - and --- as -- for readability
        val = val.replace('---', '--')
        val = val.replace('--', '-')

        # Clean up any double spaces that might result
        val = re.sub(r'  +', ' ', val)

        return val

    def _normalize_to_ascii(val: str) -> str:
        """
        Normalize Unicode characters to ASCII equivalents for BibTeX compatibility.
        Fixes issues like:
        - ' (U+2019 right single quote) → ' (ASCII apostrophe)
        - " " (U+201C/U+201D curly quotes) → " (ASCII quote)
        - " '21" (space before apostrophe) → "'21"
        - Accented characters: æ → ae, ø → o, é → e, etc.
        - LaTeX formatting commands: \textit{}, \textbf{}, etc.
        """
        # First, decode HTML entities (from CSL/Crossref metadata)
        val = html.unescape(val)

        # Strip LaTeX formatting commands (preserving content)
        val = _strip_latex_formatting(val)

        # Use strip_accents (unidecode) for comprehensive Unicode to ASCII conversion
        # This handles accented characters like æ → ae, ø → o, é → e, ú → u, etc.
        val = strip_accents(val)

        # Replace Unicode quotation marks with ASCII equivalents
        # (some may survive unidecode or come from different sources)
        replacements = {
            '\u2019': "'",  # Right single quotation mark → apostrophe
            '\u2018': "'",  # Left single quotation mark → apostrophe
            '\u201C': '"',  # Left double quotation mark → quote
            '\u201D': '"',  # Right double quotation mark → quote
            '\u2013': '-',  # En dash → hyphen
            '\u2014': '--', # Em dash → double hyphen
            '\u2026': '...', # Horizontal ellipsis → three dots
            '\u00A0': ' ',  # Non-breaking space → regular space
        }
        for unicode_char, ascii_char in replacements.items():
            val = val.replace(unicode_char, ascii_char)

        # Fix space before apostrophe in year abbreviations (e.g., " '21" → "'21")
        val = re.sub(r"\s+'(\d{2})\b", r"'\1", val)

        return val

    def _sanitize_title(title_val: str | None) -> str | None:
        if title_val is None:
            return None
        t = str(title_val).strip()

        # Remove duplicated suffix after colon
        if ':' in t:
            parts = t.split(':')
            if len(parts) >= 3:  # Has at least 2 colons
                # Check if last two parts are the same (after stripping whitespace)
                last_part = parts[-1].strip()
                second_last_part = parts[-2].strip()
                if last_part and last_part == second_last_part and len(last_part) > 15:
                    # Remove the duplicated last part
                    t = ':'.join(parts[:-1]).strip()

        # trim trailing periods unless it's an ellipsis
        if t.endswith("...") or t.endswith("…"):
            return t
        if t.endswith('.'):
            return t[:-1].rstrip()
        return t

    etype = (entry.get("type") or "misc").lower()
    key = entry.get("key") or "entry"
    fields: dict[str, str] = entry.get("fields") or {}
    preferred = [
        "title", "author", "year",
        "journal", "booktitle", "howpublished", "publisher",
        "volume", "number", "pages",
        "doi", "url", "eprint", "archiveprefix", "primaryclass"
    ]
    lines = [f"@{etype}{{{key},"]
    used = set()
    # write important fields first
    for k in preferred:
        val = fields.get(k)
        if val is not None and str(val).strip():
            used.add(k)
            val = _normalize_to_ascii(str(val))
            if k == "title":
                val = _sanitize_title(val)
            lines.append(f"  {k} = {{{val}}},")
    # then write everything else in sorted order for deterministic output
    for k in sorted(fields.keys()):
        if k in used:
            continue
        val = fields[k]
        if val is not None and str(val).strip():
            val = _normalize_to_ascii(str(val))
            if k == "title":
                val = _sanitize_title(val)
            lines.append(f"  {k} = {{{val}}},")
    # remove trailing comma from last field
    if len(lines) > 1 and lines[-1].endswith(','):
        lines[-1] = lines[-1][:-1]
    lines.append("}")
    return "\n".join(lines) + "\n"


def sanitize_bibtex_remove_placeholders(bibtex: str) -> str:
    """
    Remove BibTeX fields that still contain obvious placeholder values while keeping the rest of the entry unchanged.
    """
    entry = parse_bibtex_to_dict(bibtex)
    if not entry:
        return bibtex
    fields = entry["fields"]
    clean: dict[str, str] = {}
    for k, v in fields.items():
        if not has_placeholder(v):
            clean[k] = v
    entry2 = {"type": entry["type"], "key": entry["key"], "fields": clean}
    return bibtex_from_dict(entry2)


def _slugify(text: str) -> str:
    """
    Convert free-form text into a lowercase, URL-friendly slug by replacing non-alphanumeric runs with single dashes.

    Uses python-slugify library for robust Unicode handling and edge case coverage.
    """
    return slugify(text, lowercase=True)


def _migrate_legacy_cache() -> None:
    """One-time migration: import data/cache.json entries into ResponseCache, then delete the file."""
    dict_path = DEFAULT_DICTIONARY_FILE
    if not os.path.isabs(dict_path):
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        dict_path = os.path.join(project_root, dict_path)
    if not os.path.exists(dict_path):
        return
    legacy = safe_read_json(dict_path, default={})
    if not isinstance(legacy, dict) or not legacy:
        return
    for title_key, short_title in legacy.items():
        if title_key and short_title:
            response_cache.put("gemini", title_key, {"short_title": str(short_title)}, ttl_days=CACHE_TTL_GEMINI_DAYS)
    with contextlib.suppress(OSError):
        os.remove(dict_path)


_LEGACY_MIGRATED = False
_LEGACY_MIGRATION_LOCK = threading.Lock()


def _short_title_for_key(
    title: str,
    max_words: int = BIBTEX_KEY_MAX_WORDS,
    gemini_api_key: str | None = None
) -> str:
    """
    Pick a few informative words from a title, skipping common stop words, and
    join them into a compact phrase that works well in keys or filenames.

    If a Gemini API key is provided, this function will:
    1. Check the ResponseCache for a previously generated short title (only for default max_words)
    2. If not found, use the Gemini API to generate a short title
    3. Fall back to the original algorithm if Gemini fails or no API key is provided
    4. Save successful Gemini responses to the cache for future use

    IMPORTANT: Cache is only used when max_words equals the default value (BIBTEX_KEY_MAX_WORDS).
    When max_words is greater than default, we're disambiguating filename collisions,
    so we bypass the cache and use the algorithmic approach to get more title words.
    """
    global _LEGACY_MIGRATED
    if not _LEGACY_MIGRATED:
        with _LEGACY_MIGRATION_LOCK:
            if not _LEGACY_MIGRATED:
                _migrate_legacy_cache()
                _LEGACY_MIGRATED = True

    # normalize the title for cache lookup (consistent format)
    normalized_title = normalize_title(title)

    # Only use cache for default max_words requests
    # When max_words > default, we're disambiguating collisions and need fresh results
    use_cache = (max_words == BIBTEX_KEY_MAX_WORDS)

    # try to use Gemini if API key is available and we're using default max_words
    if gemini_api_key and use_cache:
        # check cache first
        cached = response_cache.get("gemini", normalized_title)
        if cached is not None:
            saved_short = cached.get("short_title", "")
            if saved_short:
                # sanitize cached value (remove newlines, tabs, etc from old cache entries)
                saved_short = saved_short.replace("\n", "").replace("\r", "").replace("\t", "")
                if saved_short:
                    return str(saved_short)

        # not in cache, try Gemini API
        # import here to avoid circular dependency
        from .clients.utility_apis import gemini_generate_short_title
        gemini_result = gemini_generate_short_title(title, gemini_api_key, max_words)

        if gemini_result:
            response_cache.put(
                "gemini", normalized_title,
                {"short_title": gemini_result},
                ttl_days=CACHE_TTL_GEMINI_DAYS,
            )
            return gemini_result

    # fallback to original algorithm if:
    # - no API key
    # - cache miss
    # - Gemini failed
    # - max_words > default (disambiguation mode)
    stop = {"a", "an", "the", "on", "for", "of", "and", "to", "in", "with", "using", "via", "from", "by", "at", "into",
            "through"}
    words = [w for w in re.split(r"[^A-Za-z0-9]+", title) if w]
    picks: list[str] = []
    for w in words:
        if w.lower() in stop:
            continue
        picks.append(w)
        if len(picks) >= max_words:
            break
    if not picks and words:
        picks = words[:max_words]
    return "".join(w[:1].upper() + w[1:] for w in picks)


def _first_author_lastname(authors_field: str | None) -> str | None:
    """
    Derive the first author's last name from a BibTeX-style author field,
    handling both "First Last" and "Last, First" name formats.

    Strips academic suffixes (Jr, Sr, II, III, etc.) so that names like
    "Jose F. Rodrigues Jr" produce "rodrigues" instead of "jr".
    """
    if not authors_field:
        return None
    parts = [p.strip() for p in (authors_field.split(" and ") if " and " in authors_field else authors_field.split(";"))
             if p.strip()]
    if not parts:
        return None
    first = parts[0]
    if "," in first:
        last = first.split(",")[0].strip()
    else:
        toks = first.split()
        # Strip trailing suffixes (Jr, Sr, II, III, IV, V)
        while len(toks) > 1 and toks[-1].rstrip('.').lower() in AUTHOR_NAME_SUFFIXES:
            toks.pop()
        last = toks[-1] if toks else first
    last = re.sub(r"[^a-zA-Z0-9]", "", strip_accents(last)).lower()
    return last or None


def build_standard_citekey(entry: dict[str, Any], gemini_api_key: str | None = None) -> str | None:
    """
    Build a human-readable citation key such as "Smith2024:MachineLearning" by
    combining the first author's name, the year, and key title words.

    Uses BIBTEX_KEY_MAX_WORDS (default 4) to generate more distinctive citation keys,
    which helps avoid collisions for papers with similar titles like
    "Dairy DigiD: keypoint..." vs "Dairy DigiD: Edge-Cloud..."
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
    last_cap = last[:1].upper() + last[1:] if last else "Anon"
    # Use BIBTEX_KEY_MAX_WORDS instead of hardcoded 2 to get more distinctive keys
    # This also enables cache usage when gemini_api_key is provided
    short = _short_title_for_key(title, max_words=BIBTEX_KEY_MAX_WORDS, gemini_api_key=gemini_api_key) or "Title"
    return f"{last_cap}{y}:{short}"


def short_filename_for_entry(entry: dict[str, Any], gemini_api_key: str | None = None,
                             existing_files: set | None = None, max_words: int = 2) -> str:
    """
    Construct a concise .bib filename from the first author's name, the year,
    and a shortened title so that exported files are easy to identify.

    If existing_files is provided, will ensure filename uniqueness by using
    more title words when collisions occur.
    """
    fields = entry.get("fields") or {}
    author = fields.get("author") or ""
    last = _first_author_lastname(author) or "anon"
    last_cap = last[:1].upper() + last[1:]
    year = fields.get("year")
    y_int = extract_year_from_any(year, fallback=None)
    y = str(y_int) if y_int else "0000"
    title = fields.get("title") or ""

    # Try with increasing number of words until we get a unique filename
    # Start with max_words, try up to 10 words from the title
    for num_words in range(max_words, 11):
        short = _short_title_for_key(title, max_words=num_words, gemini_api_key=gemini_api_key) or "Title"
        base = f"{last_cap}{y}-{short}"
        base = re.sub(r"[^A-Za-z0-9_\-]+", "", base)
        if len(base) > BIBTEX_FILENAME_MAX_LENGTH:
            base = base[:BIBTEX_FILENAME_MAX_LENGTH]

        filename = f"{base}.bib"

        # If we're not checking for uniqueness, or filename is unique, use it
        if existing_files is None or filename not in existing_files:
            return filename

    # Last resort: use full title if even 10 words didn't work
    # This should be extremely rare
    short = _short_title_for_key(title, max_words=20, gemini_api_key=gemini_api_key) or "Title"
    base = f"{last_cap}{y}-{short}"
    base = re.sub(r"[^A-Za-z0-9_\-]+", "", base)
    if len(base) > BIBTEX_FILENAME_MAX_LENGTH:
        base = base[:BIBTEX_FILENAME_MAX_LENGTH]
    return f"{base}.bib"


def _is_preprint_entry(fields: dict[str, Any]) -> bool:
    """Check if a BibTeX entry looks like a preprint based on DOI prefix or journal name."""
    doi = str(fields.get("doi") or "").lower()
    if any(doi.startswith(p) for p in PREPRINT_DOI_PREFIXES):
        return True
    journal = str(fields.get("journal") or "").lower()
    return any(ps in journal for ps in PREPRINT_SERVERS)


def bibtex_entries_match_strict(entry_a: dict[str, Any], entry_b: dict[str, Any]) -> bool:
    """
    Decide whether two BibTeX records refer to the same publication by comparing
    DOI or arXiv identifiers first and then falling back to title, year, and
    authors with fuzzy matching to handle formatting variations from different sources.

    Uses a multi-signal composite score when title similarity alone is insufficient
    (e.g., preprint/published pairs with rewritten titles).
    """
    if not entry_a or not entry_b:
        return False
    af = entry_a.get("fields") or {}
    bf = entry_b.get("fields") or {}

    # Fast path 1: DOI match (exact)
    # When DOIs differ but one is a preprint, fall through to multi-signal scoring
    a_doi = _norm_doi(af.get("doi"))
    b_doi = _norm_doi(bf.get("doi"))
    if a_doi and b_doi:
        if a_doi == b_doi:
            return True
        a_preprint_doi = any(a_doi.startswith(p) for p in PREPRINT_DOI_PREFIXES)
        b_preprint_doi = any(b_doi.startswith(p) for p in PREPRINT_DOI_PREFIXES)
        if not (a_preprint_doi or b_preprint_doi):
            return False
        # One DOI is a preprint — fall through to multi-signal scoring

    # Fast path 2: arXiv eprint match (exact)
    a_ax = extract_arxiv_eprint(entry_a) or ""
    b_ax = extract_arxiv_eprint(entry_b) or ""
    if a_ax and b_ax:
        return a_ax == b_ax

    # Fast path 3: External ID match (cluster_id, S2, OpenAlex)
    a_title = normalize_title(af.get("title"))
    b_title = normalize_title(bf.get("title"))
    if not a_title or not b_title:
        return False
    title_sim = title_similarity(a_title, b_title)

    if external_ids_match(af, bf) and title_sim >= SIM_DEDUP_MULTI_SIGNAL_MIN:
        return True

    # Fast path 4: High title similarity (backward-compatible original path)
    if title_sim >= SIM_FILE_DUPLICATE_THRESHOLD:
        a_year_int = extract_year_from_any(af.get("year"), fallback=None)
        b_year_int = extract_year_from_any(bf.get("year"), fallback=None)
        if a_year_int and b_year_int and abs(a_year_int - b_year_int) > 1:
            return False
        return authors_overlap(af.get("author"), bf.get("author"))

    # Truncated title path: one title is a strict prefix of the other
    # (Scholar truncation).  Requires author overlap + year within ±1
    # to avoid matching unrelated papers with common title prefixes.
    if title_is_truncated_match(af.get("title"), bf.get("title")):
        a_year_int = extract_year_from_any(af.get("year"), fallback=None)
        b_year_int = extract_year_from_any(bf.get("year"), fallback=None)
        if a_year_int and b_year_int and abs(a_year_int - b_year_int) > 1:
            return False
        if authors_overlap(af.get("author"), bf.get("author")):
            return True

    # Multi-signal fallback for moderate title similarity
    if title_sim < SIM_DEDUP_MULTI_SIGNAL_MIN:
        return False

    a_preprint = _is_preprint_entry(af)
    b_preprint = _is_preprint_entry(bf)

    # Allow composite scoring for: preprint/published pairs, external ID matches,
    # or very strong multi-author overlap with moderate title similarity.
    # Require 2+ authors on each side to avoid single-author false positives.
    from .text_utils import parse_authors_any
    a_authors = parse_authors_any(af.get("author", ""))
    b_authors = parse_authors_any(bf.get("author", ""))
    author_overlap = author_overlap_ratio(af.get("author", ""), bf.get("author", ""))
    high_author_match = (
        author_overlap >= 0.9
        and title_sim >= 0.6
        and len(a_authors) >= 2
        and len(b_authors) >= 2
    )

    if not (a_preprint ^ b_preprint) and not external_ids_match(af, bf) and not high_author_match:
        return False

    return compute_dedup_score(af, bf) >= SIM_DEDUP_COMPOSITE_THRESHOLD
