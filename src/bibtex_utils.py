from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from .config import DEFAULT_DICTIONARY_FILE, BIBTEX_KEY_MAX_WORDS, BIBTEX_FILENAME_MAX_LENGTH
from .id_utils import _norm_doi, extract_arxiv_eprint
from .io_utils import safe_read_json, safe_write_json
from .text_utils import strip_accents, has_placeholder, normalize_title, authors_overlap, extract_year_from_any


def make_bibkey(title: str, authors: List[str], year: int, fallback: str = "entry") -> str:
    """
    Build a compact BibTeX citation key using the first author's surname, the
    publication year, and the first word of the title, falling back to a generic
    label when needed.
    """
    base = fallback
    if authors:
        last = re.sub(r"[^A-Za-z0-9]", "", authors[0].split()[-1]) if authors[0] else ""
    else:
        last = ""
    word = re.sub(r"[^A-Za-z0-9]", "", (title.split()[:1] or [""])[0])
    y = str(year) if year else ""
    parts = [p for p in [last, y, word] if p]
    if parts:
        base = "".join(parts)
    if not base:
        base = fallback
    base = re.sub(r"[^A-Za-z0-9_]+", "", base)
    return base or fallback


def build_minimal_bibtex(title: str, authors: List[str], year: int, keyhint: str) -> str:
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


def _parse_bibtex_head(bibtex: str) -> Optional[Dict[str, str]]:
    """
    Read the opening line of a BibTeX entry and pull out the entry type and
    citation key if they follow the expected @type{key, pattern.
    """
    m = re.search(r"(?is)@\s*([a-zA-Z]+)\s*\{\s*([^,\s]+)\s*,", bibtex)
    if not m:
        return None
    return {"type": m.group(1).strip(), "key": m.group(2).strip()}


def _extract_balanced_braces(text: str, start: int) -> Optional[str]:
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


def _assign_field_value(fields: Dict[str, str], field_name: str, full_value: str) -> None:
    """
    Helper to assign a parsed value to fields based on whether the value is
    brace-wrapped, quoted, or plain text. Keeps logic in one place to avoid
    duplication.
    """
    if full_value.startswith('{'):
        val = _extract_balanced_braces(full_value, 0)
        fields[field_name] = val.strip() if val else full_value.strip()
    elif full_value.startswith('"'):
        m2 = re.match(r'^"([^"]*)"', full_value)
        fields[field_name] = m2.group(1).strip() if m2 else full_value.strip()
    else:
        fields[field_name] = full_value.strip()


def parse_bibtex_to_dict(bibtex: str) -> Optional[Dict[str, Any]]:
    """
    Turn a BibTeX string into a dictionary that separates the entry type, key,
    and field values while handling nested braces and multi-line fields.
    Also handles single-line BibTeX entries common in API responses.
    """
    head = _parse_bibtex_head(bibtex)
    if not head:
        return None
    fields: Dict[str, str] = {}

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
        current_pos = 0
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
            elif rest.startswith('"') and rest.count('"') >= 2:
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


def bibtex_from_dict(entry: Dict[str, Any]) -> str:
    """
    Format a dictionary-based BibTeX entry back into text, listing common
    citation fields first and writing remaining fields in a stable order.
    """

    def _sanitize_title(title_val: Optional[str]) -> Optional[str]:
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
                if last_part and last_part == second_last_part:
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
    fields: Dict[str, str] = entry.get("fields") or {}
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
            if k == "title":
                val = _sanitize_title(val)
            lines.append(f"  {k} = {{{val}}},")
    # then write everything else
    for k, val in fields.items():
        if k in used:
            continue
        if val is not None and str(val).strip():
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
    clean: Dict[str, str] = {}
    for k, v in fields.items():
        if not has_placeholder(v):
            clean[k] = v
    entry2 = {"type": entry["type"], "key": entry["key"], "fields": clean}
    return bibtex_from_dict(entry2)


def _slugify(text: str) -> str:
    """
    Convert free-form text into a lowercase, URL-friendly slug by replacing non-alphanumeric runs with single dashes.
    """
    t = text.lower().strip()
    t = re.sub(r"[^a-z0-9]+", "-", t)
    t = re.sub(r"-+", "-", t).strip("-")
    return t


def _load_title_dictionary(dict_path: str = DEFAULT_DICTIONARY_FILE) -> Dict[str, str]:
    """
    Load the title dictionary from disk, returning a mapping of normalized
    full titles to their short titles. Returns an empty dict if the dictionary
    file does not exist or cannot be read.
    """
    # try relative to project root
    if not os.path.isabs(dict_path):
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        dict_path = os.path.join(project_root, dict_path)

    result = safe_read_json(dict_path, default={})
    return result if isinstance(result, dict) else {}


def _save_title_dictionary(dictionary: Dict[str, str], dict_path: str = DEFAULT_DICTIONARY_FILE) -> None:
    """
    Save the title dictionary to disk, creating the parent directory if needed.
    """
    # try relative to project root
    if not os.path.isabs(dict_path):
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        dict_path = os.path.join(project_root, dict_path)

    # safe_write_json will create parent directories and handle errors
    safe_write_json(dict_path, dictionary, makedirs=True, indent=2)


def _short_title_for_key(
    title: str,
    max_words: int = BIBTEX_KEY_MAX_WORDS,
    gemini_api_key: Optional[str] = None
) -> str:
    """
    Pick a few informative words from a title, skipping common stop words, and
    join them into a compact phrase that works well in keys or filenames.

    If a Gemini API key is provided, this function will:
    1. Check the dictionary file for a previously generated short title (only for default max_words)
    2. If not found, use the Gemini API to generate a short title
    3. Fall back to the original algorithm if Gemini fails or no API key is provided
    4. Save successful Gemini responses to the dictionary for future use

    IMPORTANT: Cache is only used when max_words equals the default value (BIBTEX_KEY_MAX_WORDS).
    When max_words is greater than default, we're disambiguating filename collisions,
    so we bypass the cache and use the algorithmic approach to get more title words.
    """
    # normalize the title for cache lookup (consistent format)
    normalized_title = normalize_title(title)

    # Only use cache for default max_words requests
    # When max_words > default, we're disambiguating collisions and need fresh results
    use_cache = (max_words == BIBTEX_KEY_MAX_WORDS)

    # try to use Gemini if API key is available and we're using default max_words
    if gemini_api_key and use_cache:
        # check dictionary first
        dictionary = _load_title_dictionary()
        if normalized_title in dictionary:
            saved_short = dictionary[normalized_title]
            if saved_short:  # ensure it's not empty
                # sanitize cached value (remove newlines, tabs, etc from old cache entries)
                saved_short = saved_short.replace("\n", "").replace("\r", "").replace("\t", "")
                if saved_short:  # ensure still not empty after sanitization
                    return saved_short

        # not in dictionary, try Gemini API
        # import here to avoid circular dependency
        from .api_clients import gemini_generate_short_title
        gemini_result = gemini_generate_short_title(title, gemini_api_key, max_words)

        if gemini_result:
            # save the result to dictionary (only for default max_words)
            dictionary[normalized_title] = gemini_result
            _save_title_dictionary(dictionary)
            return gemini_result

    # fallback to original algorithm if:
    # - no API key
    # - cache miss
    # - Gemini failed
    # - max_words > default (disambiguation mode)
    stop = {"a", "an", "the", "on", "for", "of", "and", "to", "in", "with", "using", "via", "from", "by", "at", "into",
            "through"}
    words = [w for w in re.split(r"[^A-Za-z0-9]+", title) if w]
    picks: List[str] = []
    for w in words:
        if w.lower() in stop:
            continue
        picks.append(w)
        if len(picks) >= max_words:
            break
    if not picks and words:
        picks = words[:max_words]
    return "".join(w[:1].upper() + w[1:] for w in picks)


def _first_author_lastname(authors_field: Optional[str]) -> Optional[str]:
    """
    Derive the first author's last name from a BibTeX-style author field,
    handling both "First Last" and "Last, First" name formats.
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
        last = toks[-1] if toks else first
    last = re.sub(r"[^a-zA-Z0-9]", "", strip_accents(last)).lower()
    return last or None


def build_standard_citekey(entry: Dict[str, Any], gemini_api_key: Optional[str] = None) -> Optional[str]:
    """
    Build a human-readable citation key such as "Smith2024:MachineLearning" by
    combining the first author's name, the year, and key title words.
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
    short = _short_title_for_key(title, max_words=2, gemini_api_key=gemini_api_key) or "Title"
    return f"{last_cap}{y}:{short}"


def short_filename_for_entry(entry: Dict[str, Any], gemini_api_key: Optional[str] = None,
                             existing_files: Optional[set] = None, max_words: int = 2) -> str:
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


def _extract_year_int(year_str: Optional[str]) -> Optional[int]:
    """
    Search a string for a four-digit year and return it as an integer, or return None when no plausible year is present.
    """
    return extract_year_from_any(year_str, fallback=None)


def bibtex_entries_match_strict(entry_a: Dict[str, Any], entry_b: Dict[str, Any]) -> bool:
    """
    Decide whether two BibTeX records refer to the same publication by comparing
    DOI or arXiv identifiers first and then falling back to title, year, and
    authors with fuzzy matching to handle formatting variations from different sources.
    """
    if not entry_a or not entry_b:
        return False
    af = entry_a.get("fields") or {}
    bf = entry_b.get("fields") or {}
    a_doi = _norm_doi(af.get("doi"))
    b_doi = _norm_doi(bf.get("doi"))
    if a_doi and b_doi:
        return a_doi == b_doi
    a_ax = extract_arxiv_eprint(entry_a) or ""
    b_ax = extract_arxiv_eprint(entry_b) or ""
    if a_ax and b_ax:
        return a_ax == b_ax

    # Fuzzy title matching to handle formatting variations between sources
    a_title = normalize_title(af.get("title"))
    b_title = normalize_title(bf.get("title"))
    if not a_title or not b_title:
        return False

    # Use fuzzy title matching instead of exact equality
    # High threshold (0.95) ensures very similar titles while allowing minor variations
    from .text_utils import title_similarity
    title_sim = title_similarity(a_title, b_title)
    if title_sim < 0.95:
        return False

    # Extract and compare year integers for robust matching
    a_year_int = _extract_year_int(af.get("year"))
    b_year_int = _extract_year_int(bf.get("year"))

    # Allow 1 year difference to handle publication vs. online date discrepancies
    if a_year_int and b_year_int and abs(a_year_int - b_year_int) > 1:
        return False

    # if years missing or close enough, check authors
    return authors_overlap(af.get("author"), bf.get("author"))
