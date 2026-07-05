"""Shared helpers for the API client layer.

Generic candidate scoring by title, author, and year, best-item selection, and
the Scholar and DBLP field-extraction helpers (including HTML cleanup) used by
the individual client modules.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from ..cache import response_cache
from ..config import (
    SIM_AUTHOR_BONUS,
    SIM_BEST_ITEM_THRESHOLD,
    SIM_TITLE_WEIGHT,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
)
from ..log_utils import LogCategory, logger
from ..text_utils import extract_author_names, extract_year_from_any, normalize_title

_HTML_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MULTI_WS_RE = re.compile(r"\s+")
_DBLP_PAREN_SUFFIX_RE = re.compile(r"\s*\(\d{1,4}\)\s*$")
_DBLP_NUMERIC_SUFFIX_RE = re.compile(r"\s+\d{1,4}\s*$")


def _score_candidate_generic(
    target_title: str,
    target_author: str | None,
    target_year: int | None,
    cand_title: str,
    cand_authors: Any,
    cand_year: int | None,
    title_sim: Callable[[str, str], float],
    author_match: Callable[[str, Any], bool],
) -> float:
    tsim = title_sim(target_title, cand_title)
    s = SIM_TITLE_WEIGHT * tsim
    author_matched = bool(target_author and author_match(target_author, cand_authors))
    author_bonus = SIM_AUTHOR_BONUS if author_matched else 0.0
    s += author_bonus

    ty = extract_year_from_any(target_year) if target_year else None
    cy = extract_year_from_any(cand_year) if cand_year else None
    year_diff: int | None = None
    year_in_window = False
    year_bonus = 0.0
    if ty is not None and cy is not None:
        year_diff = abs(ty - cy)
        year_in_window = year_diff <= SIM_YEAR_MATCH_WINDOW
        year_bonus = SIM_YEAR_BONUS if year_in_window else 0.0
        s += year_bonus

    logger.debug(
        f"CANDIDATE | title_sim={tsim:.3f} | author_match={author_matched}"
        f" | author_bonus={author_bonus:.2f} | year_diff={year_diff}"
        f" | year_in_window={year_in_window} | year_bonus={year_bonus:.2f}"
        f" | total={s:.3f}",
        category=LogCategory.SCORE,
    )
    return s


def title_author_cache_key(title: str, author_name: str | None, prefix: str = "") -> str:
    """Build the shared search-cache key from a normalized title and lowercased author.

    This exact format is load-bearing. Existing entries under data/api_cache/
    were written with it, so any change would orphan the whole cache.
    """
    return f"{prefix}{normalize_title(title)}|{(author_name or '').strip().lower()}"


def _doi_cache_lookup(namespace: str, doi_norm: str) -> tuple[dict[str, Any] | None, bool]:
    """Check the response cache for a DOI-keyed entry, logging the outcome.

    Returns ``(entry, True)`` on a positive hit, ``(None, True)`` on a
    confirmed negative, and ``(None, False)`` on a miss.
    """
    cached = response_cache.get(namespace, doi_norm)
    if cached is not None:
        if cached.get("_negative"):
            logger.debug(f"{namespace} | NEG_HIT | doi={doi_norm}", category=LogCategory.CACHE)
            return None, True
        logger.debug(f"{namespace} | HIT | doi={doi_norm}", category=LogCategory.CACHE)
        return cached, True
    logger.debug(f"{namespace} | MISS | doi={doi_norm}", category=LogCategory.CACHE)
    return None, False


def _best_item_by_score(
    items: list[Any],
    score_fn: Callable[[Any], float],
    threshold: float = SIM_BEST_ITEM_THRESHOLD,
) -> Any | None:
    """Pick the highest-scoring item that meets the threshold."""
    best = None
    best_s = 0.0
    for it in items:
        s = score_fn(it)
        if s > best_s:
            best, best_s = it, s
    selected = best is not None and best_s >= threshold
    logger.debug(
        f"BEST_ITEM | candidates={len(items)} | best_score={best_s:.3f} | threshold={threshold} | selected={selected}",
        category=LogCategory.SCORE,
    )
    return best if selected else None


def extract_authors_from_article(art: dict[str, Any]) -> list[str] | None:
    """
    Extract author names from a Scholar article. When the list is truncated with
    ellipses or contains an 'et al.' token, return the partial list (excluding
    the truncation markers) instead of None so downstream code can still build a
    reasonable baseline entry.
    """
    authors = art.get("authors")
    if not authors:
        return None

    names = extract_author_names(authors, name_key="name")

    filtered_names = [
        n for n in names if n and n.strip().lower() not in ("...", "\u2026") and "et al" not in n.strip().lower()
    ]

    return filtered_names or None


def get_article_year(art: dict[str, Any]) -> int:
    """Extract the publication year from an article by checking multiple fields, returning 0 if not found."""
    y = art.get("year") or art.get("publication_year")
    primary = extract_year_from_any(y, fallback=None)
    if primary is not None:
        return primary

    pub = art.get("publication") or art.get("snippet") or art.get("publication_info")
    return extract_year_from_any(pub, fallback=None) or 0


def strip_html_tags(s: str) -> str:
    """
    Remove HTML tags, convert <br> to newlines, and collapse multiple
    whitespace characters into single spaces.
    """
    cleaned = _HTML_BR_RE.sub("\n", s)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    return _MULTI_WS_RE.sub(" ", cleaned).strip()


def _sanitize_dblp_author(name: str) -> str:
    """
    Clean a DBLP author name by removing trailing numeric disambiguators,
    keeping only the human-readable part of the name.

    DBLP uses suffixes like "0001", "0002" (or parenthesized "(0001)") to
    distinguish authors with identical names.
    """
    if not name:
        return name
    s = _DBLP_PAREN_SUFFIX_RE.sub("", name.strip())
    return _DBLP_NUMERIC_SUFFIX_RE.sub("", s)


def get_current_year() -> int:
    """Return the current UTC year."""
    return datetime.now(timezone.utc).year
