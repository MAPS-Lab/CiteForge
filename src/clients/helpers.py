from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..config import (
    SIM_AUTHOR_BONUS,
    SIM_BEST_ITEM_THRESHOLD,
    SIM_TITLE_WEIGHT,
    SIM_YEAR_BONUS,
    SIM_YEAR_MATCH_WINDOW,
)
from ..text_utils import extract_year_from_any


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
    s = 0.0
    s += SIM_TITLE_WEIGHT * title_sim(target_title, cand_title)
    if target_author and author_match(target_author, cand_authors):
        s += SIM_AUTHOR_BONUS

    ty = extract_year_from_any(target_year) if target_year else None
    cy = extract_year_from_any(cand_year) if cand_year else None
    if ty is not None and cy is not None:
        s += SIM_YEAR_BONUS * (1.0 if abs(ty - cy) <= SIM_YEAR_MATCH_WINDOW else 0.0)
    return s


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
    return best if best_s >= threshold else None


def extract_authors_from_article(art: dict[str, Any]) -> list[str] | None:
    """
    Extract author names from a Scholar article. When the list is truncated with
    ellipses or contains an 'et al.' token, return the partial list (excluding
    the truncation markers) instead of None so downstream code can still build a
    reasonable baseline entry.
    """
    from ..text_utils import extract_author_names

    authors = art.get("authors")
    if not authors:
        return None

    names = extract_author_names(authors, name_key="name")

    def _is_truncation_marker(name_str: str) -> bool:
        low = name_str.strip().lower()
        return low in ("...", "\u2026") or "et al" in low

    filtered_names = [n for n in names if n and not _is_truncation_marker(n)]

    return filtered_names if filtered_names else None


def get_article_year(art: dict[str, Any]) -> int:
    """Extract the publication year from an article by checking multiple fields, returning 0 if not found."""
    y = art.get("year") or art.get("publication_year")
    year = extract_year_from_any(y, fallback=None)
    if year is not None:
        return year

    pub = art.get("publication") or art.get("snippet") or art.get("publication_info")
    year = extract_year_from_any(pub, fallback=None)
    if year is not None:
        return year

    return 0


def strip_html_tags(s: str) -> str:
    """
    Remove HTML tags, convert <br> to newlines, and collapse multiple
    whitespace characters into single spaces.
    """
    s2 = re.sub(r"<\s*br\s*/?\s*>", "\n", s, flags=re.IGNORECASE)
    s2 = re.sub(r"<[^>]+>", " ", s2)
    s2 = re.sub(r"\s+", " ", s2)
    return s2.strip()


def _sanitize_dblp_author(name: str) -> str:
    """
    Clean a DBLP author name by removing trailing numeric disambiguators,
    keeping only the human-readable part of the name.
    """
    if not name:
        return name
    s = name.strip()
    s = re.sub(r"\s*\((0\d{3})\)\s*$", "", s)
    s = re.sub(r"\s+(0\d{3})\s*$", "", s)
    return s


def get_current_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year
