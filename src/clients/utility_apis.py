from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..cache import response_cache
from ..config import CACHE_TTL_DOI_DAYS, CACHE_TTL_SEARCH_DAYS, DATACITE_BASE, GEMINI_BASE, ORCID_BASE
from ..exceptions import FIELD_ACCESS_ERRORS, NUMERIC_ERRORS
from ..http_utils import DEFAULT_JSON_HEADERS, handle_api_errors, http_get_json
from ..id_utils import _norm_doi
from ..log_utils import LogCategory, LogSource, logger
from ..text_utils import normalize_title
from .helpers import _best_item_by_score

# ============================================================================================
# Gemini API Integration
# ============================================================================================

def gemini_generate_short_title(
    full_title: str, api_key: str, max_words: int | None = None
) -> str | None:
    """
    Call the Gemini API to generate a short CamelCase title for a publication,
    suitable for BibTeX keys and filenames.
    """
    from ..config import BIBTEX_KEY_MAX_WORDS
    if max_words is None:
        max_words = BIBTEX_KEY_MAX_WORDS

    if not api_key or not full_title:
        return None

    prompt = (
        f"Create a smart, concise CamelCase title (1 to {max_words} words) "
        f"for this publication: \"{full_title}\". "
        f"Extract the most important keywords. "
        f"Skip stop words (a, an, the, for, of, and, to, in, with, from, by, at). "
        f"Use exactly {max_words} words or fewer if shorter captures the essence better. "
        f"IMPORTANT: Write as ONE word in CamelCase format with NO spaces between words "
        f"(e.g., 'AttentionMechanism' not 'Attention Mechanism'). "
        f"Return ONLY the CamelCase title with no quotes, explanation, spaces, or punctuation."
    )

    url = f"{GEMINI_BASE}?key={api_key}"
    payload = {
        "contents": [{
            "parts": [{
                "text": prompt
            }]
        }],
        "generationConfig": {
            "maxOutputTokens": 50,
            "temperature": 0.3,
            "topP": 0.8,
            "topK": 20,
        }
    }

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
            }
        )

        with urllib.request.urlopen(req, timeout=15.0) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("candidates"):
            candidate = data["candidates"][0]
            if "content" in candidate and "parts" in candidate["content"]:
                parts = candidate["content"]["parts"]
                if parts and "text" in parts[0]:
                    short_title = parts[0]["text"].strip()
                    short_title = short_title.strip('"\'').strip()
                    short_title = re.sub(r"\s+", "", short_title)

                    if short_title:
                        word_count = sum(1 for c in short_title if c.isupper())
                        if word_count > max_words:
                            logger.warn(
                                f"Gemini returned {word_count} words (expected max {max_words}): '{short_title}'. "
                                f"Falling back to default algorithm.",
                                category=LogCategory.DEBUG,
                                source=LogSource.SYSTEM
                            )
                            return None

                    if short_title and len(short_title) <= 100:
                        logger.info(
                            f"Generated title: {short_title}",
                            category=LogCategory.DEBUG, source=LogSource.SYSTEM,
                        )
                        return str(short_title)

        logger.warn("Returned no valid candidates in response", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None

    except urllib.error.HTTPError as e:
        try:
            error_body = json.loads(e.read().decode("utf-8"))
            error_msg = error_body.get("error", {}).get("message", str(e.reason))
            if e.code == 503:
                logger.warn(
                    "API overloaded (503), falling back to default algorithm",
                    category=LogCategory.ERROR, source=LogSource.SYSTEM,
                )
            elif e.code == 429:
                logger.warn(
                    "API quota exceeded (429), falling back to default algorithm",
                    category=LogCategory.ERROR, source=LogSource.SYSTEM,
                )
            else:
                logger.warn(f"API error {e.code}: {error_msg}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        except FIELD_ACCESS_ERRORS:
            logger.warn(f"API HTTP {e.code}: {e.reason}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None
    except (urllib.error.URLError, TimeoutError, ValueError, json.JSONDecodeError) as e:
        logger.warn(f"API call failed: {type(e).__name__}: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None


# ============================================================================================
# DataCite API Integration
# ============================================================================================

@handle_api_errors(default_return=None)
def datacite_search_doi(doi: str) -> dict[str, Any] | None:
    """Look up a DOI in DataCite to get dataset or software metadata."""
    if not doi:
        return None

    doi_norm = _norm_doi(doi)
    if not doi_norm:
        return None

    cached = response_cache.get("datacite", doi_norm)
    if cached is not None:
        return cached if cached else None

    encoded_doi = urllib.parse.quote(doi_norm, safe="")
    url = f"{DATACITE_BASE}/{encoded_doi}"

    data = http_get_json(url, timeout=15.0)

    result = data.get("data") or None
    if result is not None:
        response_cache.put("datacite", doi_norm, result, ttl_days=CACHE_TTL_DOI_DAYS)
    return result


def build_bibtex_from_datacite(record: dict[str, Any], keyhint: str) -> str | None:
    """Build a BibTeX entry from a DataCite record (typically for datasets/software)."""
    from ..bibtex_build import build_bibtex_entry, determine_entry_type
    from ..text_utils import safe_get_field

    attributes = record.get("attributes") or {}

    titles = attributes.get("titles") or []
    if not titles:
        return None
    title = safe_get_field(titles[0], "title")

    if not title:
        return None

    authors: list[str] = []
    for creator in attributes.get("creators") or []:
        name = safe_get_field(creator, "name")
        if name:
            authors.append(name)

    year = 0
    pub_year = attributes.get("publicationYear")
    if pub_year:
        try:
            year = int(pub_year)
        except NUMERIC_ERRORS:
            year = 0

    venue = safe_get_field(attributes, "publisher")

    resource_type = attributes.get("types") or {}
    resource_type_general = (safe_get_field(resource_type, "resourceTypeGeneral") or "").lower()
    entry_type = determine_entry_type(resource_type_general or attributes)

    doi = safe_get_field(attributes, "doi")
    url = safe_get_field(attributes, "url")

    extra_fields = {}
    if resource_type_general:
        extra_fields["note"] = f"Type: {resource_type_general}"
    if attributes.get("version"):
        version_note = f"Version: {attributes['version']}"
        if "note" in extra_fields:
            extra_fields["note"] += f", {version_note}"
        else:
            extra_fields["note"] = version_note

    if venue:
        extra_fields["howpublished"] = venue

    return build_bibtex_entry(
        entry_type=entry_type,
        title=title,
        authors=authors,
        year=year,
        keyhint=keyhint,
        venue="",
        doi=doi,
        url=url,
        arxiv_id=None,
        extra_fields=extra_fields
    )


# ============================================================================================
# ORCID API Integration
# ============================================================================================

@handle_api_errors(default_return=[])
def orcid_fetch_works(orcid_id: str) -> list[dict[str, Any]]:
    """Fetch a list of works for an ORCID author."""
    if not orcid_id:
        return []

    orcid_id = orcid_id.replace("https://orcid.org/", "")

    cache_key = f"orcid_works|{orcid_id}"
    cached = response_cache.get("orcid", cache_key)
    if cached is not None:
        return list(cached.get("works", []))

    url = f"{ORCID_BASE}/{orcid_id}/works"

    headers = DEFAULT_JSON_HEADERS.copy()
    headers["User-Agent"] = "CiteForge/1.0"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=15.0) as response:
        data = json.loads(response.read().decode("utf-8"))

    works = []
    for work_group in (data.get("group") or []):
        work_summary = work_group.get("work-summary") or []
        if work_summary:
            work = work_summary[0]

            title_obj = work.get("title") or {}
            title = (title_obj.get("title") or {}).get("value") or ""

            pub_date = work.get("publication-date") or {}
            year = pub_date.get("year") or {}
            year_val = year.get("value") if isinstance(year, dict) else None

            work_record = {
                "title": title,
                "year": year_val,
                "type": work.get("type"),
                "external-ids": work.get("external-ids") or {},
                "url": work.get("url") or {},
            }

            if title:
                works.append(work_record)

    if works:
        response_cache.put("orcid", cache_key, {"works": works}, ttl_days=CACHE_TTL_SEARCH_DAYS)
    return works


def orcid_search_work_by_title(orcid_id: str, title: str, _author_name: str | None = None) -> dict[str, Any] | None:
    """Search ORCID works for a specific paper by title to validate authorship."""
    works = orcid_fetch_works(orcid_id)
    if not works:
        return None

    target_norm = normalize_title(title)

    for work in works:
        work_title = work.get("title") or ""
        if normalize_title(work_title) == target_norm:
            return dict(work)

    def get_orcid_title(w: dict[str, Any]) -> str:
        return w.get("title") or ""

    def get_orcid_year(w: dict[str, Any]) -> int | None:
        year = w.get("year")
        if year:
            try:
                return int(year)
            except NUMERIC_ERRORS:
                return None
        return None

    def match_fn(_name: str, _work_item: dict[str, Any]) -> bool:
        return True

    from ..bibtex_build import create_scoring_function
    score_fn = create_scoring_function(
        title=title,
        author_name=None,
        year_hint=None,
        title_getter=get_orcid_title,
        authors_getter=lambda w: [],
        year_getter=get_orcid_year,
        author_match_fn=match_fn
    )

    return _best_item_by_score(works, score_fn)
