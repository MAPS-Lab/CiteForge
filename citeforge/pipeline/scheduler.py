"""Author-level scheduling.

Fetches each author's publications from Google Scholar and DBLP, merges and
deduplicates the two lists, prioritizes authors with pending work, and drives
`process_article` across a bounded pool of worker threads with an aggregate
completion warning threshold.
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from typing import Any

from tenacity import Retrying, retry_if_result, stop_after_attempt, wait_exponential

from citeforge.clients.helpers import get_article_year, strip_html_tags
from citeforge.clients.scholar import (
    fetch_author_publications,
    merge_publication_lists,
    sort_articles_by_year_current_first,
)
from citeforge.clients.search_apis import (
    dblp_fetch_for_author,
)
from citeforge.config import (
    ARTICLE_WORKERS,
    MAX_PUBLICATIONS_PER_AUTHOR,
    MAX_WORKERS,
    SCHOLAR_FETCH_BACKOFF_INITIAL,
    SCHOLAR_FETCH_BACKOFF_MAX,
    SCHOLAR_FETCH_MAX_ATTEMPTS,
    SIM_MERGE_DUPLICATE_THRESHOLD,
    get_min_year,
)
from citeforge.exceptions import (
    FULL_OPERATION_ERRORS,
)
from citeforge.fsscan import iter_author_bibs, iter_parsed_author_bibs
from citeforge.log_utils import LogCategory, LogSource, logger
from citeforge.models import Record
from citeforge.pipeline.article import process_article
from citeforge.text_utils import (
    extract_year_from_any,
    format_author_dirname,
    trim_title_default,
)

# One process-wide article pool, kept separate from the author pool so an
# author thread blocked on its own articles can never starve the workers it is
# waiting on. ThreadPoolExecutor spawns threads on first submit, so an import
# that never runs the pipeline costs nothing.
_ARTICLE_POOL = ThreadPoolExecutor(max_workers=ARTICLE_WORKERS, thread_name_prefix="article")


def _author_dirname(rec: Record) -> str:
    """Return the output directory name for *rec*, keyed by its Scholar or DBLP id."""
    return format_author_dirname(rec.name, rec.scholar_id or rec.dblp or "")


def _existing_corpus_articles(rec: Record, out_dir: str, min_year: int) -> list[dict[str, Any]]:
    """Project committed BibTeX into the article planner's provider shape.

    Current Scholar and DBLP inventories can omit a record they returned in an
    earlier month. Keeping those files only as post-run orphans prevents every
    enrichment source from ever reconsidering them.
    """
    author_dir = os.path.join(out_dir, _author_dirname(rec))
    if not os.path.isdir(author_dir):
        return []
    articles: list[dict[str, Any]] = []
    for _filename, _path, entry in iter_parsed_author_bibs(author_dir):
        fields = entry.get("fields") or {}
        title = str(fields.get("title") or "").strip()
        year = extract_year_from_any(fields.get("year"), fallback=0) or 0
        if not title or year < min_year:
            continue
        publication = (
            fields.get("journal") or fields.get("booktitle") or fields.get("howpublished") or fields.get("note")
        )
        article: dict[str, Any] = {
            "title": title,
            "authors": fields.get("author") or "",
            "year": year,
            "source": "existing_corpus",
            "citation_id": entry.get("key") or title,
        }
        if publication:
            article["publication"] = publication
        if fields.get("url"):
            article["link"] = fields["url"]
        elif fields.get("doi"):
            article["link"] = f"https://doi.org/{fields['doi']}"
        articles.append(article)
    return articles


def process_record(
    serpapi_key: str,
    serply_key: str | None,
    rec: Record,
    out_dir: str,
    max_pubs: int | None = 1,
    s2_api_key: str | None = None,
    or_creds: tuple[str, str] | None = None,
    gemini_api_key: str | None = None,
    summary_csv_path: str | None = None,
    force_enrich: bool = False,
) -> int:
    """Fetch, deduplicate, and enrich recent publications for one author.

    Returns the number of BibTeX files successfully written.
    """
    # Set up thread-local logging for this author
    author_log_path = os.path.join(out_dir, _author_dirname(rec), "author.log")
    logger.set_log_file(author_log_path)

    try:
        logger.step(
            f"Author: {rec.name} (Scholar={rec.scholar_id or 'N/A'}, DBLP={rec.dblp or 'N/A'})",
            category=LogCategory.AUTHOR,
            source=LogSource.SYSTEM,
        )

        min_year = get_min_year()

        scholar_windowed = []
        if rec.scholar_id:
            logger.info("Request author publications", category=LogCategory.FETCH, source=LogSource.SCHOLAR)

            scholar_articles: list[dict[str, Any]] = []
            # SerpAPI call; pagination handled internally by serpapi_scholar
            data: dict[str, Any] = Retrying(
                sleep=time.sleep,
                stop=stop_after_attempt(SCHOLAR_FETCH_MAX_ATTEMPTS),
                wait=wait_exponential(
                    multiplier=SCHOLAR_FETCH_BACKOFF_INITIAL,
                    min=SCHOLAR_FETCH_BACKOFF_INITIAL,
                    max=SCHOLAR_FETCH_BACKOFF_MAX,
                ),
                retry=retry_if_result(lambda result: not result.get("articles")),
                before_sleep=lambda state: logger.warn(
                    f"Scholar API returned empty "
                    f"(attempt {state.attempt_number}/{SCHOLAR_FETCH_MAX_ATTEMPTS}), retrying...",
                    category=LogCategory.FETCH,
                    source=LogSource.SCHOLAR,
                ),
                retry_error_callback=lambda state: state.outcome.result() if state.outcome is not None else {},
            )(
                fetch_author_publications,
                serpapi_key,
                rec.scholar_id,
                rec.name,
                num=MAX_PUBLICATIONS_PER_AUTHOR,
                min_year=min_year,
            )

            if not data.get("articles"):
                logger.warn(
                    f"Scholar API failed after {SCHOLAR_FETCH_MAX_ATTEMPTS} attempts; continuing with DBLP only",
                    category=LogCategory.ERROR,
                    source=LogSource.SCHOLAR,
                )
            else:
                status = (data.get("search_metadata") or {}).get("status", "")
                if status.lower() == "error":
                    raise RuntimeError(
                        f"CiteForge error for author {rec.scholar_id}: {data.get('error') or 'Unknown error'}"
                    )

                scholar_articles = data.get("articles", [])
                logger.debug(
                    f"SCHOLAR_FETCH | articles={len(scholar_articles)}",
                    category=LogCategory.AUDIT,
                )

            if not scholar_articles:
                logger.warn("No articles returned from Scholar", category=LogCategory.SKIP, source=LogSource.SCHOLAR)
            else:
                # Pre-clean titles to handle trailing periods consistently
                for a in scholar_articles:
                    try:
                        if a.get("title"):
                            a["title"] = trim_title_default(strip_html_tags(a["title"]))
                    except (TypeError, AttributeError):
                        pass
                logger.info(
                    f"{len(scholar_articles)} article(s) fetched",
                    category=LogCategory.FETCH,
                    source=LogSource.SCHOLAR,
                )

            scholar_windowed = [a for a in scholar_articles if (get_article_year(a) or 0) >= min_year]
            logger.debug(
                f"YEAR_WINDOW | total={len(scholar_articles)} | windowed={len(scholar_windowed)} | min_year={min_year}",
                category=LogCategory.AUDIT,
            )
            logger.info(
                f"{len(scholar_windowed)}/{len(scholar_articles)} within year window (>= {min_year})",
                category=LogCategory.FETCH,
                source=LogSource.SCHOLAR,
            )
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.SCHOLAR)

        dblp_items = []
        if rec.dblp:
            try:
                dblp_items = dblp_fetch_for_author(rec.name, rec.dblp, min_year)
                logger.info(
                    f"{len(dblp_items)} item(s) fetched within window",
                    category=LogCategory.FETCH,
                    source=LogSource.DBLP,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.warn(f"Fetch failed: {e}", category=LogCategory.ERROR, source=LogSource.DBLP)
        else:
            logger.info("Skipped (no ID)", category=LogCategory.SKIP, source=LogSource.DBLP)

        existing_items = _existing_corpus_articles(rec, out_dir, min_year)

        if not scholar_windowed and not dblp_items and not existing_items:
            logger.info(f"No articles within year window (>= {min_year})", category=LogCategory.SKIP)
            return 0

        # Merge live inventories first, then add committed records that neither
        # provider returned this month. A duplicate live item stays first and
        # still loads the matching committed BibTeX as its enrichment baseline.
        live_list = merge_publication_lists(scholar_windowed, dblp_items, target_author=rec.name)
        merged_list = merge_publication_lists(live_list, existing_items, target_author=rec.name)
        dedup_removed = len(scholar_windowed) + len(dblp_items) + len(existing_items) - len(merged_list)
        logger.debug(
            f"PUB_MERGE | scholar={len(scholar_windowed)} | dblp={len(dblp_items)} "
            f"existing={len(existing_items)} "
            f"| merged={len(merged_list)} | dedup_removed={dedup_removed}",
            category=LogCategory.AUDIT,
        )
        logger.info(
            f"Union: Scholar={len(scholar_windowed)}, DBLP={len(dblp_items)}, Existing={len(existing_items)} "
            f"→ {len(merged_list)} unique publications (threshold={SIM_MERGE_DUPLICATE_THRESHOLD})",
            category=LogCategory.PLAN,
        )

        articles_sorted = sort_articles_by_year_current_first(merged_list)
        total_entries = len(articles_sorted) if max_pubs is None else min(len(articles_sorted), max_pubs)
        logger.info(
            f"Plan: process {total_entries}/{len(articles_sorted)} item(s) "
            f"(limit={'all' if max_pubs is None else max_pubs})",
            category=LogCategory.PLAN,
        )

        planned = articles_sorted if max_pubs is None else articles_sorted[:max_pubs]

        def _one_article(numbered: tuple[int, dict[str, Any]]) -> int:
            idx, art = numbered
            # Each article runs on an article-pool thread, so it binds the
            # author's log itself. The handler is shared and reference counted.
            logger.set_log_file(author_log_path)
            try:
                return process_article(
                    rec,
                    art,
                    serply_key,
                    out_dir,
                    s2_api_key,
                    or_creds,
                    idx=idx + 1,
                    total=total_entries,
                    gemini_api_key=gemini_api_key,
                    summary_csv_path=summary_csv_path,
                    min_year=min_year,
                    force_enrich=force_enrich,
                )
            except FULL_OPERATION_ERRORS as e:
                logger.error(f"Article error: {e}", category=LogCategory.ERROR)
                return 0
            finally:
                logger.close()

        # Articles are the unit of parallelism, not authors. An author-level
        # pool idles once fewer authors remain than workers, and the largest
        # author then sets the wall clock on its own: in the August 2026 logs
        # the last three authors took 43 of an 82 minute iteration on at most
        # two of sixteen workers. The article pool is separate from the author
        # pool, so an author thread waiting here cannot starve the workers it
        # is waiting on. Provider pacing is unchanged, still the per-namespace
        # token buckets and the global concurrency semaphore.
        saved = sum(_ARTICLE_POOL.map(_one_article, enumerate(planned)))

        logger.info(f"Author done: saved {saved} file(s)", category=LogCategory.PLAN)
        return saved
    finally:
        # Close the thread-local log file handler
        logger.close()


def count_existing_papers(rec: Record, out_dir: str) -> int:
    """Count existing .bib files in the author's output directory."""
    try:
        return len(iter_author_bibs(os.path.join(out_dir, _author_dirname(rec))))
    except OSError:
        return 0


def prioritize_records(records: list[Record], out_dir: str) -> list[Record]:
    """Sort authors by existing paper count (descending) so authors with more
    papers finish first; ties break on (name, id) for deterministic ordering.

    Emits the PLAN log lines and returns the count-sorted list. Kept separate
    from run_all so the caller can log/sort before initializing the summary CSV,
    matching the original run.log line ordering.
    """
    logger.info(
        "Sorting authors by existing paper count (authors with more papers will be processed first)",
        category=LogCategory.PLAN,
    )
    records_with_counts = [(rec, count_existing_papers(rec, out_dir)) for rec in records]
    records_with_counts.sort(key=lambda x: (-x[1], x[0].name.lower(), x[0].scholar_id or x[0].dblp or ""))

    # Log sorting results
    if records_with_counts:
        max_papers = records_with_counts[0][1]
        min_papers = records_with_counts[-1][1]
        logger.info(f"Author range: {max_papers} papers (max) to {min_papers} papers (min)", category=LogCategory.PLAN)

    return [rec for rec, _ in records_with_counts]


def run_all(
    serpapi_key: str,
    serply_key: str | None,
    s2_api_key: str | None,
    or_creds: tuple[str, str] | None,
    gemini_api_key: str | None,
    records: list[Record],
    out_dir: str,
    summary_csv_path: str | None,
    force_enrich: bool,
) -> tuple[int, int]:
    """Enrich every (already count-sorted) author in parallel worker threads.

    Prioritizes authors without an output directory, installs a thread
    excepthook, and drives a ThreadPoolExecutor over process_record. Expects
    ``records`` to be the count-sorted list from prioritize_records. Returns
    (total_saved, processed).
    """
    total_saved = 0
    processed = 0
    accounted: set[Future[int]] = set()

    def _account_result(future: Future[int], rec: Record) -> None:
        nonlocal processed, total_saved
        if future in accounted:
            return
        accounted.add(future)
        try:
            saved = future.result()
            total_saved += saved
            processed += 1
            logger.success(
                f"[{processed}/{len(records)}] Completed: {rec.name} ({saved} files saved)",
                category=LogCategory.AUTHOR,
            )
        except Exception as e:
            processed += 1
            logger.error(
                f"[{processed}/{len(records)}] Error processing {rec.name} ({rec.scholar_id or rec.dblp}): {e}",
                category=LogCategory.ERROR,
            )

    # Prioritize new authors (no existing output dir) so they get API resources
    # first, before cached authors consume worker slots. This intentionally
    # overrides the count-descending order from prioritize_records for new
    # authors only; the relative order within each group is preserved.
    def _has_output(r: Record) -> bool:
        return os.path.isdir(os.path.join(out_dir, _author_dirname(r)))

    records_sorted = [r for _, r in sorted(enumerate(records), key=lambda ir: (_has_output(ir[1]), ir[0]))]

    logger.step(f"Starting parallel execution with {MAX_WORKERS} workers", category=LogCategory.PLAN)

    # Install thread exception hook to log uncaught exceptions in worker threads
    _orig_excepthook = threading.excepthook

    def _thread_excepthook(args: Any) -> None:
        logger.error(
            f"Thread '{args.thread.name if args.thread else '?'}' died: {args.exc_type.__name__}: {args.exc_value}",
            category=LogCategory.ERROR,
        )
        _orig_excepthook(args)

    threading.excepthook = _thread_excepthook

    # Log once when aggregate completion exceeds 30 minutes per author. Threads
    # cannot be terminated safely, so executor shutdown still waits for them.
    completion_warning_seconds_per_author = 1800

    future_to_author: dict[Future[int], Record] = {}
    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # Submit all tasks and track them
            for idx, rec in enumerate(records_sorted, 1):
                effective_id = rec.scholar_id or rec.dblp or "N/A"
                logger.info(
                    f"[{idx}/{len(records)}] Queued: {rec.name} (ID: {effective_id})",
                    category=LogCategory.PLAN,
                )

                future = executor.submit(
                    process_record,
                    serpapi_key,
                    serply_key,
                    rec,
                    out_dir,
                    max_pubs=None,
                    s2_api_key=s2_api_key,
                    or_creds=or_creds,
                    gemini_api_key=gemini_api_key,
                    summary_csv_path=summary_csv_path,
                    force_enrich=force_enrich,
                )
                future_to_author[future] = rec

            logger.step(f"All {len(records)} authors queued for processing", category=LogCategory.PLAN)

            try:
                for future in as_completed(
                    future_to_author,
                    timeout=completion_warning_seconds_per_author * len(records),
                ):
                    _account_result(future, future_to_author[future])
            except (TimeoutError, FuturesTimeoutError):
                remaining = [r.name for f, r in future_to_author.items() if not f.done()]
                # A warning, not a kill. Cancelling here bounded the overrun but
                # discarded finished work: an author that completes just after
                # the threshold returned zero saved files despite having done
                # them. The threshold surfaces a slow run; the drain below still
                # counts every result once, and article-level parallelism is
                # what actually bounds the tail.
                logger.warn(
                    f"Pipeline completion warning threshold reached with {len(remaining)} author(s) still running: "
                    + ", ".join(remaining[:5])
                    + ". Waiting for worker threads before final accounting.",
                    category=LogCategory.PLAN,
                )
    finally:
        threading.excepthook = _orig_excepthook

    # Shutdown waits for every worker that had already started. Drain futures
    # not yielded before the deadline so every result is accounted exactly
    # once, including the ones cancel_futures left unstarted.
    for future, rec in future_to_author.items():
        _account_result(future, rec)

    return total_saved, processed
