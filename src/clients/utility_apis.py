from __future__ import annotations

import random
import re
import time
import urllib.parse
from typing import Any

from ..cache import response_cache
from ..config import CACHE_TTL_DOI_DAYS, CACHE_TTL_SEARCH_DAYS, DATACITE_BASE, GEMINI_BASE, ORCID_BASE
from ..exceptions import ALL_API_ERRORS, NUMERIC_ERRORS
from ..http_utils import handle_api_errors, http_get_json, http_post_json
from ..id_utils import _norm_doi
from ..log_utils import LogCategory, LogSource, logger
from ..text_utils import normalize_title
from .helpers import _best_item_by_score

# ============ Gemini ============

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
        "Extract the most important keywords. "
        "Skip stop words (a, an, the, for, of, and, to, in, with, from, by, at). "
        f"Use exactly {max_words} words or fewer if shorter captures the essence better. "
        "IMPORTANT: Write as ONE word in CamelCase format with NO spaces between words "
        "(e.g., 'AttentionMechanism' not 'Attention Mechanism'). "
        "Return ONLY the CamelCase title with no quotes, explanation, spaces, or punctuation."
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

    import requests as _requests

    data: dict[str, Any] | None = None
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        logger.debug(
            f"GEMINI_ATTEMPT | attempt={attempt} | title={full_title[:50]}",
            category=LogCategory.CITEKEY,
        )
        try:
            data = http_post_json(url, payload, timeout=15.0)
            break  # success
        except _requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 429 and attempt < max_retries:
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.debug(
                    f"GEMINI_429 | attempt={attempt} | backoff={wait:.1f}s",
                    category=LogCategory.CITEKEY,
                )
                logger.info(
                    f"Gemini 429 (attempt {attempt}/{max_retries}), retrying in {wait:.1f}s",
                    category=LogCategory.DEBUG, source=LogSource.SYSTEM,
                )
                time.sleep(wait)
                continue
            logger.debug(f"GEMINI_FAIL | error={type(e).__name__}", category=LogCategory.CITEKEY)
            logger.warn(f"API call failed: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
            return None
        except (*ALL_API_ERRORS, ValueError) as e:
            logger.debug(f"GEMINI_FAIL | error={type(e).__name__}", category=LogCategory.CITEKEY)
            logger.warn(f"API call failed: {e}", category=LogCategory.ERROR, source=LogSource.SYSTEM)
            return None

    if data is None:
        logger.debug("GEMINI_FAIL | reason=no_response", category=LogCategory.CITEKEY)
        return None

    candidates = data.get("candidates") or []
    if candidates:
        parts = candidates[0].get("content", {}).get("parts") or []
        raw_text = parts[0].get("text", "").strip() if parts else ""
    else:
        raw_text = ""
    short_title = re.sub(r"\s+", "", raw_text.strip("\"'"))

    if not short_title or len(short_title) > 100:
        logger.debug("GEMINI_FAIL | reason=invalid_length", category=LogCategory.CITEKEY)
        logger.warn("Returned no valid candidates in response", category=LogCategory.ERROR, source=LogSource.SYSTEM)
        return None

    word_count = sum(1 for c in short_title if c.isupper())
    if word_count > max_words:
        logger.debug("GEMINI_FAIL | reason=too_many_words", category=LogCategory.CITEKEY)
        logger.warn(
            f"Gemini returned {word_count} words (expected max {max_words}): '{short_title}'. "
            f"Falling back to default algorithm.",
            category=LogCategory.DEBUG,
            source=LogSource.SYSTEM,
        )
        return None

    logger.debug(
        f"GEMINI_SUCCESS | short={short_title} | word_count={word_count} | max_words={max_words} | valid=True",
        category=LogCategory.CITEKEY,
    )
    logger.info(
        f"Generated title: {short_title}",
        category=LogCategory.DEBUG, source=LogSource.SYSTEM,
    )
    return short_title


# ============ DataCite ============

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
        logger.debug(f"datacite | HIT | doi={doi_norm}", category=LogCategory.CACHE)
        return cached
    logger.debug(f"datacite | MISS | doi={doi_norm}", category=LogCategory.CACHE)

    encoded_doi = urllib.parse.quote(doi_norm, safe="")
    url = f"{DATACITE_BASE}/{encoded_doi}"

    data = http_get_json(url, timeout=15.0)

    result = data.get("data") or None
    if result is not None:
        response_cache.put("datacite", doi_norm, result, ttl_days=CACHE_TTL_DOI_DAYS)
    logger.debug(
        f"datacite | RESULT | doi={doi_norm} | found={result is not None}",
        category=LogCategory.SCORE,
    )
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

    extra_fields: dict[str, str] = {}
    note_parts = []
    if resource_type_general:
        note_parts.append(f"Type: {resource_type_general}")
    if attributes.get("version"):
        note_parts.append(f"Version: {attributes['version']}")
    if note_parts:
        extra_fields["note"] = ", ".join(note_parts)

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


# ============ ORCID ============

@handle_api_errors(default_return=[])
def orcid_fetch_works(orcid_id: str) -> list[dict[str, Any]]:
    """Fetch a list of works for an ORCID author."""
    if not orcid_id:
        return []

    orcid_id = orcid_id.replace("https://orcid.org/", "")

    cache_key = f"orcid_works|{orcid_id}"
    cached = response_cache.get("orcid", cache_key)
    if cached is not None:
        logger.debug(f"orcid | HIT | id={orcid_id}", category=LogCategory.CACHE)
        return list(cached.get("works", []))
    logger.debug(f"orcid | MISS | id={orcid_id}", category=LogCategory.CACHE)

    url = f"{ORCID_BASE}/{orcid_id}/works"

    data = http_get_json(url, timeout=15.0)

    works = []
    for work_group in (data.get("group") or []):
        work_summary = work_group.get("work-summary") or []
        if not work_summary:
            continue

        work = work_summary[0]
        title_obj = work.get("title") or {}
        title = (title_obj.get("title") or {}).get("value") or ""
        if not title:
            continue

        pub_date = work.get("publication-date") or {}
        year = pub_date.get("year") or {}
        year_val = year.get("value") if isinstance(year, dict) else None

        works.append({
            "title": title,
            "year": year_val,
            "type": work.get("type"),
            "external-ids": work.get("external-ids") or {},
            "url": work.get("url") or {},
        })

    logger.debug(
        f"orcid | WORKS | id={orcid_id} | count={len(works)}",
        category=LogCategory.SCORE,
    )
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
            logger.debug(
                f"orcid | TITLE_MATCH | title={title[:50]} | exact=True | best_score=1.000 | result=found",
                category=LogCategory.SCORE,
            )
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

    from ..bibtex_build import create_scoring_function
    # NOTE: ORCID work-summary responses do not include contributor lists,
    # so we skip author matching (author_name=None) to avoid rejecting all
    # candidates when cand_authors is always empty.
    score_fn = create_scoring_function(
        title=title,
        author_name=None,
        year_hint=None,
        title_getter=get_orcid_title,
        authors_getter=lambda w: w.get("contributors") or [],
        year_getter=get_orcid_year,
    )

    result = _best_item_by_score(works, score_fn)
    logger.debug(
        f"orcid | TITLE_MATCH | title={title[:50]} | exact=False"
        f" | result={'found' if result else 'none'}",
        category=LogCategory.SCORE,
    )
    return result
