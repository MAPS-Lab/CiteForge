from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from citeforge.models import Record
from citeforge.pipeline import scheduler

_ARTICLES: list[dict[str, Any]] = [{"title": f"Paper {i}", "year": 2024} for i in range(8)]


def _stub_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Return the eight-article inventory without touching the network."""
    monkeypatch.setattr(
        scheduler, "fetch_author_publications", lambda *_a, **_k: {"articles": [dict(a) for a in _ARTICLES]}
    )
    monkeypatch.setattr(scheduler, "dblp_fetch_for_author", lambda *_a, **_k: [])
    monkeypatch.setattr(scheduler, "get_min_year", lambda: 2020)
    monkeypatch.setattr(scheduler, "merge_publication_lists", lambda scholar, _dblp, **_kw: scholar)
    monkeypatch.setattr(scheduler, "sort_articles_by_year_current_first", lambda articles: articles)


def test_articles_of_one_author_run_concurrently(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An author's articles are the unit of parallelism, not the author.

    Before this, an author's articles ran in a serial for-loop inside one
    worker, so the largest author set the wall clock on its own while the rest
    of the pool idled.
    """
    _stub_inventory(monkeypatch)

    lock = threading.Lock()
    live = 0
    peak = 0

    def slow_article(*_args: object, **_kwargs: object) -> int:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.05)
        with lock:
            live -= 1
        return 1

    monkeypatch.setattr(scheduler, "process_article", slow_article)

    started = time.monotonic()
    saved = scheduler.process_record("key", None, Record("Ada", scholar_id="a1"), str(tmp_path), max_pubs=None)
    elapsed = time.monotonic() - started

    assert saved == len(_ARTICLES), "every article result must still be counted"
    assert peak > 1, f"articles ran serially, peak concurrency was {peak}"
    assert elapsed < len(_ARTICLES) * 0.05, "wall clock matched the serial cost"


def test_article_failures_do_not_lose_the_rest_of_the_author(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """One article raising a handled error costs that article, not the author."""
    _stub_inventory(monkeypatch)

    def flaky(_rec: Record, art: dict[str, Any], *_args: object, **_kwargs: object) -> int:
        if art["title"] == "Paper 3":
            raise TimeoutError("provider stalled")
        return 1

    monkeypatch.setattr(scheduler, "process_article", flaky)

    saved = scheduler.process_record("key", None, Record("Ada", scholar_id="a1"), str(tmp_path), max_pubs=None)

    assert saved == len(_ARTICLES) - 1


def test_max_pubs_still_bounds_the_article_count(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The max_pubs cap survives the move off the indexed serial loop."""
    _stub_inventory(monkeypatch)
    seen: list[str] = []
    lock = threading.Lock()

    def record_article(_rec: Record, art: dict[str, Any], *_args: object, **_kwargs: object) -> int:
        with lock:
            seen.append(art["title"])
        return 1

    monkeypatch.setattr(scheduler, "process_article", record_article)

    saved = scheduler.process_record("key", None, Record("Ada", scholar_id="a1"), str(tmp_path), max_pubs=3)

    assert saved == 3
    assert sorted(seen) == ["Paper 0", "Paper 1", "Paper 2"]


def test_every_article_line_reaches_the_author_log(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Articles on pool threads still write to their own author's log file."""
    _stub_inventory(monkeypatch)

    def logging_article(_rec: Record, art: dict[str, Any], *_args: object, **_kwargs: object) -> int:
        scheduler.logger.info(f"processed {art['title']}", category="ARTICLE")
        return 1

    monkeypatch.setattr(scheduler, "process_article", logging_article)

    scheduler.process_record("key", None, Record("Ada", scholar_id="a1"), str(tmp_path), max_pubs=None)

    log_text = (tmp_path / "Ada (a1)" / "author.log").read_text(encoding="utf-8")
    for article in _ARTICLES:
        assert f"processed {article['title']}" in log_text


def test_existing_orphan_is_added_to_the_monthly_article_plan(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A committed record absent from the current provider inventories still
    re-enters enrichment instead of surviving forever as an untouched orphan."""
    rec = Record("Ada Lovelace", scholar_id="a1")
    author_dir = tmp_path / "Lovelace (a1)"
    author_dir.mkdir(parents=True)
    (author_dir / "Lovelace2026-Orphan.bib").write_text(
        "@misc{Lovelace2026:Orphan,\n"
        "  title = {An Orphaned Publication Record},\n"
        "  author = {A Lovelace},\n"
        "  year = {2026}\n"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler, "fetch_author_publications", lambda *_a, **_k: {"articles": []})
    monkeypatch.setattr(scheduler, "dblp_fetch_for_author", lambda *_a, **_k: [])
    monkeypatch.setattr(scheduler, "get_min_year", lambda: 2020)
    monkeypatch.setattr(scheduler.time, "sleep", lambda _seconds: None)
    seen: list[dict[str, Any]] = []

    def capture(_rec: Record, art: dict[str, Any], *_args: object, **_kwargs: object) -> int:
        seen.append(art)
        return 1

    monkeypatch.setattr(scheduler, "process_article", capture)

    saved = scheduler.process_record("key", None, rec, str(tmp_path), max_pubs=None)

    assert saved == 1
    assert [item["title"] for item in seen] == ["An Orphaned Publication Record"]
    assert seen[0]["source"] == "existing_corpus"
