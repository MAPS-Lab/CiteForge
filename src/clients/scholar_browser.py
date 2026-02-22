"""Browser-based Google Scholar scraping using nodriver.

All functions are async and must be invoked via ``ScholarBrowserLoop.run()``.
They return data structures identical to the SerpAPI equivalents so the rest
of the pipeline (deduplication, merge, enrichment) works unchanged.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
import urllib.parse
from typing import Any

from ..config import (
    SCHOLAR_BROWSER_MAX_DELAY,
    SCHOLAR_BROWSER_MIN_DELAY,
    SCHOLAR_BROWSER_PAGE_TIMEOUT,
)
from ..exceptions import ScholarBrowserBlockedError

_log = logging.getLogger("citeforge.browser")

_SCHOLAR_AUTHOR_URL = "https://scholar.google.com/citations"
_SCHOLAR_SEARCH_URL = "https://scholar.google.com/scholar"

_CAPTCHA_MARKERS = ("unusual traffic", "recaptcha", "gs_captcha", "sorry/index")
_CITATION_SKIP_FIELDS = frozenset({"total citations", "scholar articles"})


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


async def _random_delay() -> None:
    """Async sleep with randomized jitter for anti-detection."""
    jitter = random.random() * (SCHOLAR_BROWSER_MAX_DELAY - SCHOLAR_BROWSER_MIN_DELAY)
    await asyncio.sleep(SCHOLAR_BROWSER_MIN_DELAY + jitter)


async def _is_captcha_page(page: Any) -> bool:
    """Detect if Scholar is showing a CAPTCHA or block page.

    Returns ``True`` on detection errors as a safe default (assume blocked).
    """
    try:
        url = (getattr(page, "url", None) or "").lower()
        if any(marker in url for marker in _CAPTCHA_MARKERS):
            return True
        source = await page.get_content()
        lower = source[:3000].lower() if source else ""
        return any(marker in lower for marker in _CAPTCHA_MARKERS)
    except Exception as e:
        _log.warning("CAPTCHA detection failed (assuming blocked): %s", e)
        return True


async def _close_page(page: Any) -> None:
    """Silently close a browser page/tab."""
    with contextlib.suppress(Exception):
        await page.close()


def _extract_citation_id_from_href(href: str) -> str:
    """Extract the citation/result ID from a Scholar title link href.

    Href format: ``/citations?...&citation_for_view=XXX:YYYYYYYY``
    We want the part after the colon in ``citation_for_view``.
    """
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
    cfv = qs.get("citation_for_view", [""])[0]
    if ":" in cfv:
        return cfv.split(":", 1)[1]
    return cfv or ""


def _get_element_href(el: Any) -> str:
    """Extract href attribute from a nodriver element."""
    attrs = getattr(el, "attrs", None)
    return (attrs.get("href", "") or "") if attrs else ""


def _extract_bibtex_from_text(text: str) -> str | None:
    """Extract a BibTeX entry from raw text using brace-depth counting."""
    m = re.search(r"@\w+\s*\{", text)
    if not m:
        return None
    start = m.start()
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _parse_year_text(text: str) -> int:
    """Parse a year integer from text, returning 0 on failure."""
    text = text.strip()
    try:
        return int(text)
    except ValueError:
        m = re.search(r"\d{4}", text)
        return int(m.group(0)) if m else 0


# ------------------------------------------------------------------
# Use Case 1: Author Publications
# ------------------------------------------------------------------


async def browser_fetch_author_publications(
    browser: Any,
    author_id: str,
    num: int = 100,
    start: int = 0,
) -> dict[str, Any]:
    """Fetch author publications by scraping the Google Scholar profile page.

    Returns a dict matching the SerpAPI ``fetch_author_publications()`` format::

        {"articles": [...], "search_metadata": {"status": "Success", "source": "browser"}}
    """
    params = {
        "user": author_id,
        "hl": "en",
        "sortby": "pubdate",
        "cstart": str(start),
        "pagesize": "100",
    }
    url = f"{_SCHOLAR_AUTHOR_URL}?{urllib.parse.urlencode(params)}"

    page = await browser.get(url, new_tab=True)
    await asyncio.sleep(2)  # initial page load settle

    try:
        if await _is_captcha_page(page):
            raise ScholarBrowserBlockedError("CAPTCHA detected on author profile page")

        all_articles: list[dict[str, Any]] = []
        max_clicks = (num // 20) + 3  # safety bound on pagination clicks

        for _ in range(max_clicks):
            articles = await _parse_author_page(page)
            if len(articles) >= num or len(articles) <= len(all_articles):
                all_articles = articles
                break
            all_articles = articles

            try:
                more_btn = await page.find("Show more", timeout=5)
                if not more_btn:
                    break
                await more_btn.click()
                await _random_delay()
            except Exception as e:
                _log.debug("Pagination stopped after %d articles: %s", len(all_articles), e)
                break

        return {
            "articles": all_articles[:num],
            "search_metadata": {"status": "Success", "source": "browser"},
        }
    finally:
        await _close_page(page)


async def _parse_author_page(page: Any) -> list[dict[str, Any]]:
    """Parse article rows from the author profile page."""
    try:
        rows = await page.select_all("tr.gsc_a_tr")
    except Exception as e:
        _log.warning("Failed to select article rows from author page: %s", e)
        return []

    if not rows:
        return []

    articles: list[dict[str, Any]] = []
    failed = 0
    for row in rows:
        try:
            article = await _parse_article_row(row)
            if article:
                articles.append(article)
        except Exception as e:
            failed += 1
            _log.debug("Failed to parse article row: %s", e)
    if failed:
        _log.warning("Failed to parse %d of %d article rows", failed, len(rows))
    return articles


async def _parse_article_row(row: Any) -> dict[str, Any] | None:
    """Extract article data from a single table row."""
    title_el = await row.query_selector("a.gsc_a_at")
    if not title_el:
        return None

    title = (title_el.text or "").strip()
    if not title:
        return None

    href = _get_element_href(title_el)
    citation_id = _extract_citation_id_from_href(href) if href else ""

    # Authors and venue from gs_gray divs
    gray_divs = await row.query_selector_all("div.gs_gray") or []
    authors_text = (gray_divs[0].text or "").strip() if len(gray_divs) >= 1 else ""
    venue_text = (gray_divs[1].text or "").strip() if len(gray_divs) >= 2 else ""

    year_el = await row.query_selector("td.gsc_a_y span.gsc_a_hc")
    year_text = (year_el.text or "") if year_el else ""
    year_int = _parse_year_text(year_text)

    article: dict[str, Any] = {
        "title": title,
        "authors": authors_text,
        "year": year_int if year_int else "",
        "citation_id": citation_id,
        "result_id": citation_id,
        "source": "scholar",
    }
    if venue_text:
        article["publication_info"] = {"summary": venue_text}
        article["publication"] = venue_text

    return article


# ------------------------------------------------------------------
# Use Case 2: Citation Detail
# ------------------------------------------------------------------


async def browser_fetch_citation_detail(
    browser: Any,
    author_id: str,
    citation_id: str,
) -> dict[str, str] | None:
    """Fetch detailed citation metadata by scraping the citation view page.

    Returns a dict matching ``fetch_scholar_citation_via_serpapi()`` output format.
    """
    citation_for_view = f"{author_id}:{citation_id}" if ":" not in citation_id else citation_id
    params = {
        "view_op": "view_citation",
        "hl": "en",
        "user": author_id,
        "citation_for_view": citation_for_view,
    }
    url = f"{_SCHOLAR_AUTHOR_URL}?{urllib.parse.urlencode(params)}"

    page = await browser.get(url, new_tab=True)
    await _random_delay()

    try:
        if await _is_captcha_page(page):
            raise ScholarBrowserBlockedError("CAPTCHA detected on citation detail page")

        fields: dict[str, str] = {}

        title_el = await page.query_selector("#gsc_oci_title") or await page.query_selector("a.gsc_oci_title_link")
        if title_el:
            title_text = (title_el.text or "").strip()
            if title_text:
                fields["title"] = title_text

        field_els = await page.select_all("div.gsc_oci_field")
        value_els = await page.select_all("div.gsc_oci_value")

        for field_el, value_el in zip(field_els, value_els, strict=False):
            with contextlib.suppress(Exception):
                key = (field_el.text or "").strip().lower()
                if key in _CITATION_SKIP_FIELDS:
                    continue
                val = (value_el.text or "").strip()
                if val:
                    fields[key] = val

        return fields if fields else None
    finally:
        await _close_page(page)


# ------------------------------------------------------------------
# Use Case 3: Title Search + BibTeX Export
# ------------------------------------------------------------------


async def browser_search_scholar(
    browser: Any,
    title: str,
    author_name: str | None = None,
) -> str | None:
    """Search Google Scholar by title and return a cite link for the best match.

    Returns the cite link URL or None.
    """
    q = f'"{title}"'
    if author_name:
        q += f" {author_name}"
    params = {"q": q, "hl": "en"}
    url = f"{_SCHOLAR_SEARCH_URL}?{urllib.parse.urlencode(params)}"

    page = await browser.get(url, new_tab=True)
    await _random_delay()

    try:
        if await _is_captcha_page(page):
            raise ScholarBrowserBlockedError("CAPTCHA detected on search page")

        results = await page.select_all("div.gs_r.gs_or.gs_scl")
        if not results:
            results = await page.select_all("div.gs_ri")
        if not results:
            return None

        from ..text_utils import normalize_title, title_similarity

        target_norm = normalize_title(title)
        best_link: str | None = None
        best_sim = 0.0

        for r in results:
            with contextlib.suppress(Exception):
                title_el = await r.query_selector("h3.gs_rt a")
                if not title_el:
                    title_el = await r.query_selector("h3 a")
                if not title_el:
                    continue

                r_title = (title_el.text or "").strip()
                if not r_title:
                    continue

                sim = title_similarity(title, r_title)
                if sim > best_sim:
                    best_sim = sim
                    best_link = _get_element_href(title_el) or None

                    if normalize_title(r_title) == target_norm:
                        break

        return best_link if best_link and best_sim >= 0.9 else None
    finally:
        await _close_page(page)


async def browser_fetch_bibtex(
    browser: Any,
    scholar_url: str,
) -> str | None:
    """Navigate to a Scholar article page and extract BibTeX via the cite dialog."""
    page = await browser.get(scholar_url, new_tab=True)
    await _random_delay()

    try:
        if await _is_captcha_page(page):
            raise ScholarBrowserBlockedError("CAPTCHA detected on article page")

        cite_btn = await page.query_selector("a.gs_or_cit")
        if not cite_btn:
            with contextlib.suppress(Exception):
                cite_btn = await page.find("Cite", timeout=SCHOLAR_BROWSER_PAGE_TIMEOUT / 1000)
            if not cite_btn:
                return None

        await cite_btn.click()
        await asyncio.sleep(1.5)

        cite_links = await page.select_all("#gs_citi a")
        if not cite_links:
            cite_links = await page.select_all("div.gs_citi a")

        bibtex_url: str | None = None
        for link in cite_links:
            if "bibtex" in (link.text or "").strip().lower():
                bibtex_url = _get_element_href(link)
                break

        if not bibtex_url:
            return None

        bib_page = await browser.get(bibtex_url, new_tab=True)
        await asyncio.sleep(1)
        try:
            pre_el = await bib_page.query_selector("pre")
            if pre_el:
                return (pre_el.text or "").strip() or None
            content = await bib_page.get_content()
            if content and "@" in content:
                return _extract_bibtex_from_text(content)
            return None
        finally:
            await _close_page(bib_page)
    finally:
        await _close_page(page)
