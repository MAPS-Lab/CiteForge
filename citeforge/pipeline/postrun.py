"""Post-run finalization tail.

Runs the deterministic sequence that closes out a run, flushing the summary CSV,
reconciling phantom rows, removing duplicate orphan files, applying the
year-window cleanup, running the post-run fixup pass, dropping superseded
preprints, building the a2i2 folder, and rewriting `baseline.json`. The order is
load-bearing.

Only the first three steps read the summary CSV. The rest operate on `out_dir`
alone and therefore run whether or not a CSV was produced.
"""

from __future__ import annotations

import csv
import os
import re
from dataclasses import dataclass
from typing import Any

from citeforge import bibtex_utils as bt
from citeforge.bibtex_build import get_container_field
from citeforge.cache import get_cache_hit_counts
from citeforge.canonicalize import (
    _fixup_bib_entry,
)
from citeforge.config import (
    A2I2_OUTPUT_DIR,
    DEFAULT_A2I2_INPUT,
    PREPRINT_SERVERS,
    get_min_year,
)
from citeforge.fsscan import iter_author_bibs, iter_output_dirs
from citeforge.http_utils import get_api_call_counts
from citeforge.id_utils import is_secondary_doi
from citeforge.identity import IdentityContext, evaluate_identity
from citeforge.io_utils import (
    build_a2i2_folder,
    collect_orphan_files,
    flush_summary_csv,
    reconcile_summary_csv,
    retarget_summary_csv_paths,
    safe_write_file,
    safe_write_json,
)
from citeforge.log_utils import LogCategory, logger
from citeforge.models import Record
from citeforge.text_utils import (
    _is_preprint_fields,
    author_list_is_surname_initials,
    extract_year_from_any,
)

# The author prefix of a generated name: "Jin2026-AlteredNeuro.bib" and the
# citation key "Jin2026:AlteredNeuro" share the shape <Surname><Year><sep>.
_FILENAME_AUTHOR_RE = re.compile(r"^([A-Za-z]+)(\d{4}-.+\.bib)$")
_CITEKEY_AUTHOR_RE = re.compile(r"^([A-Za-z]+)(\d{4}:.+)$")


def _reconcile_author_prefix(entry: dict[str, Any], filename: str) -> tuple[str, str | None]:
    """Return the entry's citation key and filename with a stale surname fixed.

    Scoped to "Surname Initials" author lists, where the trailing token is
    initials rather than a name, so a file for a paper by Jin ends up named
    `Y2026-...`. Fixing the derivation does not fix files already on disk, which
    is what this is for.

    Do not widen it to "prefix disagrees with the first author". A file may
    legitimately hold content whose author no longer matches its name, because
    the deduplicator writes a survivor under the losing duplicate's filename;
    renaming on the author alone produces a name that is still wrong about the
    year and title while looking freshly derived.

    Only the surname is reconsidered. The title portion may come from Gemini or
    from collision resolution, and re-deriving it would churn correct names.
    """
    fields = entry.get("fields") or {}
    authors = str(fields.get("author") or "")
    names = [part.strip() for part in authors.split(" and ") if part.strip()]
    if not author_list_is_surname_initials(names):
        return str(entry.get("key") or ""), None

    surname = bt._first_author_lastname(authors)
    if not surname:
        return str(entry.get("key") or ""), None
    expected = surname[:1].upper() + surname[1:]

    key = str(entry.get("key") or "")
    if (matched := _CITEKEY_AUTHOR_RE.match(key)) and matched.group(1) != expected:
        key = f"{expected}{matched.group(2)}"

    renamed = None
    if (matched := _FILENAME_AUTHOR_RE.match(filename)) and matched.group(1) != expected:
        renamed = f"{expected}{matched.group(2)}"
    return key, renamed


class FinalizationError(RuntimeError):
    """A finalization step could not read or write a file it owns.

    Raised instead of continuing, because a partially applied cleanup leaves the
    output tree in a state no later step can distinguish from a clean one.
    """


@dataclass(frozen=True)
class FinalizationReport:
    """What the post-run tail actually did, as counts rather than log text.

    Every field records an irreversible or observable effect, so a caller can
    validate finalization directly instead of re-deriving it from the tree.
    """

    summary_csv_path: str | None
    summary_csv_present: bool
    phantom_rows_removed: int
    orphans_removed: int
    orphans_kept: int
    out_of_window_removed: int
    files_fixed: int
    files_renamed: int
    renames_blocked: int
    superseded_preprints_removed: int
    a2i2_files: int
    baseline_total: int
    baseline_authors: dict[str, int]


def finalize_run(
    out_dir: str,
    records: list[Record],
    total_saved: int,
    processed: int,
    summary_csv_path: str | None,
) -> FinalizationReport:
    """Run the strict-ordered post-run finalization tail and report what it did.

    Logs run stats, then, when the summary CSV exists, flushes it, reconciles
    phantom rows and removes duplicate orphans. The remaining steps read only
    *out_dir* and *records*, so they run regardless of the CSV: year-window
    cleanup, post-run fixup, superseded-preprint removal, the a2i2 build and the
    baseline.json rewrite. Order is load-bearing.

    Raises :class:`FinalizationError` when a step cannot read or write a file it
    is responsible for.
    """
    counts = get_api_call_counts()
    logger.step("Run complete", category=LogCategory.PLAN)
    logger.info(f"Records processed: {processed}", category=LogCategory.PLAN)
    logger.info(f"BibTeX files saved: {total_saved}", category=LogCategory.PLAN)
    if counts:
        logger.info(f"API calls: {counts}", category=LogCategory.PLAN)
    logger.info(f"Total API calls: {sum(counts.values()) if counts else 0}", category=LogCategory.PLAN)
    cache_counts = get_cache_hit_counts()
    logger.info(
        f"Cache: {cache_counts['positive']} positive, {cache_counts['negative']} negative, {cache_counts['miss']} miss",
        category=LogCategory.PLAN,
    )
    logger.info(f"Log file: {logger.log_file_path or 'n/a'}", category=LogCategory.PLAN)

    csv_path = summary_csv_path if summary_csv_path and os.path.exists(summary_csv_path) else None
    phantoms = 0
    removed = 0
    kept = 0

    if csv_path is not None:
        flush_summary_csv(csv_path)

        phantoms = reconcile_summary_csv(csv_path)
        if phantoms:
            logger.info(f"Reconciled summary CSV: removed {phantoms} phantom entries", category=LogCategory.CLEANUP)

        # Safe orphan removal (duplicates only)
        orphans = collect_orphan_files(csv_path, out_dir)
        if orphans:
            csv_entries = _load_csv_entries(csv_path)
            for orphan in orphans:
                try:
                    with open(orphan, encoding="utf-8") as of:
                        orphan_entry = bt.parse_bibtex_to_dict(of.read())
                except (OSError, ValueError) as exc:
                    raise FinalizationError(f"cannot read orphan candidate {orphan}") from exc

                author_dir_path = os.path.dirname(orphan)
                tracked = csv_entries.get(author_dir_path, [])
                # Full identity, never a title score. Near-identical titles are
                # routine in this corpus (successive genome-wide studies differ
                # by a word) and conflicting DOIs settle it. DISK_SURVIVOR is
                # what save_entry_to_file requires to merge two files, so
                # deleting on anything weaker would be inconsistent with it.
                is_dup = orphan_entry is not None and any(
                    evaluate_identity(entry, orphan_entry, context=IdentityContext.DISK_SURVIVOR).verdict
                    for entry in tracked
                )

                if is_dup:
                    os.remove(orphan)
                    removed += 1
                    logger.info(
                        f"Removed duplicate orphan: {os.path.basename(orphan)}",
                        category=LogCategory.CLEANUP,
                    )
                else:
                    kept += 1
                    logger.warn(
                        f"Orphan kept (no duplicate found): {os.path.basename(orphan)}",
                        category=LogCategory.CLEANUP,
                    )
            if removed:
                logger.info(
                    f"Removed {removed}/{len(orphans)} orphan .bib files (duplicates only)",
                    category=LogCategory.CLEANUP,
                )

    # Remove .bib files outside the contribution window
    window_min = get_min_year()
    window_removed = 0
    for entry in iter_output_dirs(out_dir):
        if entry == A2I2_OUTPUT_DIR:
            continue
        d = os.path.join(out_dir, entry)
        for fname in iter_author_bibs(d):
            fpath = os.path.join(d, fname)
            # The entry's own year decides, not the filename's. A filename is a
            # derived label and it does go stale: the deduplicator writes a
            # surviving entry under the duplicate's existing filename, so 13
            # committed files are named for a year their contents do not carry
            # (Saha2020-EnergyAware.bib holds a 2023 paper). None of those
            # straddles the window today, which is luck rather than a guarantee,
            # and this is a deletion. Reading the label would eventually delete
            # an in-window paper because its name says otherwise.
            try:
                with open(fpath, encoding="utf-8") as bf:
                    parsed = bt.parse_bibtex_to_dict(bf.read())
            except (OSError, ValueError) as exc:
                raise FinalizationError(f"cannot read {fpath} for the year-window check") from exc
            year = extract_year_from_any((parsed or {}).get("fields", {}).get("year"), fallback=0) or 0
            # The entry's own year decides, with no fallback to the filename.
            # "in press", "n.d." and an unparseable year all arrive here, and a
            # filename can be inherited from another record, so falling back
            # would delete an in-window paper on no evidence about its contents.
            if not year:
                logger.debug(
                    f"YEAR_WINDOW | keeping {fname}: the entry states no usable year",
                    category=LogCategory.CLEANUP,
                )
                continue
            if year < window_min:
                logger.debug(
                    f"YEAR_WINDOW | removing {fname} (bib_year={year} < {window_min})",
                    category=LogCategory.CLEANUP,
                )
                os.remove(fpath)
                window_removed += 1
    if window_removed:
        logger.info(
            f"Removed {window_removed} out-of-window files (year < {window_min})",
            category=LogCategory.CLEANUP,
        )

    # Post-run fixup applies entry type and field corrections to ALL .bib
    # files. This catches orphans (files not processed during enrichment) and
    # entries where Phase 4 corrections were undone by Tier 2 filling.
    postrun_fixed = 0
    renamed_count = 0
    renames_blocked = 0
    applied_renames: dict[str, str] = {}
    for pr_entry_name in iter_output_dirs(out_dir):
        if pr_entry_name == A2I2_OUTPUT_DIR:
            continue
        pr_dir = os.path.join(out_dir, pr_entry_name)
        for pr_fname in iter_author_bibs(pr_dir):
            pr_fpath = os.path.join(pr_dir, pr_fname)
            try:
                with open(pr_fpath, encoding="utf-8") as prf:
                    pr_content = prf.read()
                pr_parsed = bt.parse_bibtex_to_dict(pr_content)
            except (OSError, ValueError) as exc:
                raise FinalizationError(f"cannot read {pr_fpath} for the post-run fixup") from exc
            if not pr_parsed:
                continue
            _fixup_bib_entry(pr_parsed)
            # After the field rules, because author casing runs among them and
            # the surname is read from the corrected author string.
            reconciled_key, renamed = _reconcile_author_prefix(pr_parsed, pr_fname)
            if reconciled_key and reconciled_key != pr_parsed.get("key"):
                pr_parsed["key"] = reconciled_key
            # The serialized form decides, not whether a rule reported a change.
            # A file can differ from its own re-serialization with no rule
            # firing at all, and gating on the rules left those files rewritten
            # on every run and never settled, so the corpus digest never
            # repeated and the refresh loop could not converge.
            bib_str = bt.bibtex_from_dict(pr_parsed)
            if bib_str != pr_content:
                if not safe_write_file(pr_fpath, bib_str):
                    raise FinalizationError(f"post-run fixup could not write {pr_fpath}")
                postrun_fixed += 1
            if renamed:
                target = os.path.join(pr_dir, renamed)
                # A collision means another entry already owns the corrected
                # name. Renaming onto it would destroy that entry, so the stale
                # name is kept and reported instead.
                if os.path.exists(target):
                    renames_blocked += 1
                    logger.warn(
                        f"Author prefix stale on {pr_fname}, but {renamed} exists; left as is",
                        category=LogCategory.CLEANUP,
                    )
                else:
                    os.rename(pr_fpath, target)
                    renamed_count += 1
                    applied_renames[os.path.abspath(pr_fpath)] = os.path.abspath(target)
                    logger.info(f"Renamed {pr_fname} -> {renamed}", category=LogCategory.CLEANUP)
    if postrun_fixed:
        logger.info(
            f"Post-run fixup: corrected {postrun_fixed} .bib files",
            category=LogCategory.CLEANUP,
        )
    if renamed_count:
        logger.info(
            f"Post-run fixup: renamed {renamed_count} .bib files to their corrected author prefix",
            category=LogCategory.CLEANUP,
        )

    if renames_blocked:
        logger.warn(
            f"Post-run fixup: {renames_blocked} rename(s) blocked by an existing target",
            category=LogCategory.CLEANUP,
        )
    # The CSV was reconciled before these renames happened, so it now names the
    # old paths. Repoint it here rather than leaving the next run to discover
    # the drift as phantom rows plus untracked orphans.
    retargeted = retarget_summary_csv_paths(csv_path, applied_renames) if csv_path else 0
    if retargeted:
        logger.info(
            f"Repointed {retargeted} summary CSV row(s) at their renamed file",
            category=LogCategory.CLEANUP,
        )

    superseded = _remove_superseded_preprints(out_dir)
    if superseded:
        logger.info(
            f"Removed {superseded} superseded preprint .bib files (published twin exists)",
            category=LogCategory.CLEANUP,
        )

    # Everything that removes a .bib runs after the CSV was reconciled, so a
    # deletion leaves a row naming a file that no longer exists and the run
    # returns asserting a consistency it does not have. The rename step repoints
    # its own rows; deletions cannot, so the CSV is reconciled once more here.
    # The invariant this restores is the one the report implies: at return, the
    # CSV names exactly the files on disk.
    if csv_path is not None and (window_removed or superseded):
        trailing = reconcile_summary_csv(csv_path)
        if trailing:
            phantoms += trailing
            logger.info(
                f"Reconciled summary CSV: removed {trailing} row(s) for files deleted after the first pass",
                category=LogCategory.CLEANUP,
            )

    a2i2_count = build_a2i2_folder(DEFAULT_A2I2_INPUT, records, out_dir)
    if a2i2_count:
        logger.info(
            f"Built a2i2 folder: {a2i2_count} deduplicated files",
            category=LogCategory.CLEANUP,
        )

    # Write per-author baseline counts (a2i2 included by design; the
    # baseline total must equal the on-disk .bib count)
    # An author directory holding no BibTeX file is left out rather than
    # recorded as zero. Git cannot commit an empty directory, so a reader
    # deriving counts from a committed tree never sees such an author, and a
    # zero recorded here made the two memberships unequal by construction. The
    # 2026-08 refresh wrote seven of them and its own corpus check rejected the
    # result as stale. The total is unaffected, since a zero adds nothing.
    baseline: dict[str, int] = {}
    for entry in iter_output_dirs(out_dir):
        count = len(iter_author_bibs(os.path.join(out_dir, entry)))
        if count:
            baseline[entry] = count
    baseline_path = os.path.join(out_dir, "baseline.json")
    if not safe_write_json(baseline_path, {"total": sum(baseline.values()), "authors": baseline}):
        raise FinalizationError(f"could not write {baseline_path}")

    if csv_path is not None:
        logger.info(f"Summary CSV: {csv_path}", category=LogCategory.PLAN)

    return FinalizationReport(
        summary_csv_path=summary_csv_path,
        summary_csv_present=csv_path is not None,
        phantom_rows_removed=phantoms,
        orphans_removed=removed,
        orphans_kept=kept,
        out_of_window_removed=window_removed,
        files_fixed=postrun_fixed,
        files_renamed=renamed_count,
        renames_blocked=renames_blocked,
        superseded_preprints_removed=superseded,
        a2i2_files=a2i2_count,
        baseline_total=sum(baseline.values()),
        baseline_authors=baseline,
    )


def _looks_published(entry: dict[str, Any]) -> bool:
    """Return True when *entry* is a published record rather than a preprint.

    A record counts as published when it is not preprint-flagged (by DOI prefix or
    journal name) and carries either a DOI or a real container field (journal for
    ``@article``, booktitle for ``@inproceedings``/``@incollection``). Used to
    decide whether a preprint twin has been superseded.
    """
    fields = entry.get("fields", {}) or {}
    if _is_preprint_fields(fields):
        return False
    # A DOI alone is not publication. `_is_preprint_fields` reads the DOI prefix
    # and the journal, never howpublished, so a record whose only evidence is
    # `howpublished = {arXiv}` or a data-repository DOI (Zenodo, Figshare, which
    # are absent from PREPRINT_DOI_PREFIXES) reached here as "published" and
    # became a licence to delete a real preprint. The weakest record in a
    # directory must not be the one that authorises a removal.
    howpublished = str(fields.get("howpublished") or "").casefold()
    if any(server in howpublished for server in PREPRINT_SERVERS):
        return False
    doi = str(fields.get("doi") or "").strip()
    if doi and is_secondary_doi(doi):
        return False
    container = get_container_field(str(entry.get("type") or ""))
    has_container = bool(container and str(fields.get(container) or "").strip())
    # A published record names where it was published. A bare DOI with no
    # container is a stub, and a stub cannot supersede anything.
    return has_container or (bool(doi) and bool(str(fields.get("publisher") or "").strip()))


def _remove_superseded_preprints(out_dir: str) -> int:
    """Delete a preprint ``.bib`` when a published record of the same work exists.

    Within each author directory, a preprint file is removed only when another
    file in that directory is a genuinely published record (see
    :func:`_looks_published`) whose title matches at or above
    ``SIM_MERGE_DUPLICATE_THRESHOLD``. This generalizes the "published outranks a
    preprint" rule across every author and paper: a standalone preprint with no
    published counterpart is always retained, and published files are never
    removed. Deterministic (sorted iteration) and idempotent. Once the preprint
    is gone a later run finds no preprint+published pair and removes nothing.

    Returns the number of preprint files removed.
    """
    removed = 0
    for entry_name in iter_output_dirs(out_dir):
        if entry_name == A2I2_OUTPUT_DIR:
            continue
        author_dir = os.path.join(out_dir, entry_name)
        parsed: list[tuple[str, dict[str, Any]]] = []
        for fname in iter_author_bibs(author_dir):
            path = os.path.join(author_dir, fname)
            try:
                with open(path, encoding="utf-8") as bf:
                    parsed_entry = bt.parse_bibtex_to_dict(bf.read())
            except (OSError, ValueError):
                parsed_entry = None
            if parsed_entry:
                parsed.append((path, parsed_entry))

        published = [(p, e) for p, e in parsed if _looks_published(e)]
        if not published:
            continue

        for path, entry in parsed:
            fields = entry.get("fields", {}) or {}
            if not _is_preprint_fields(fields):
                continue
            title = str(fields.get("title") or "")
            if not title:
                continue
            # Same reasoning as the orphan sweep: a title score is not evidence
            # that two records are one work. "Machine learning for ship
            # detection: Part I" against "Part II" scores 0.988, and a preprint
            # is often the only full text on disk. DISK_SURVIVOR understands the
            # preprint-to-published relationship this sweep is built on, and
            # rejects a pair whose DOIs or arXiv ids contradict each other.
            has_published_twin = any(
                pub_path != path and evaluate_identity(pub_entry, entry, context=IdentityContext.DISK_SURVIVOR).verdict
                for pub_path, pub_entry in published
            )
            if has_published_twin:
                try:
                    os.remove(path)
                    removed += 1
                    logger.info(
                        f"Removed superseded preprint: {os.path.basename(path)}",
                        category=LogCategory.CLEANUP,
                    )
                except OSError:
                    pass
    return removed


def _load_csv_entries(csv_path: str) -> dict[str, list[dict[str, Any]]]:
    """Load CSV-tracked .bib entries, grouped by author directory.

    Whole entries rather than titles, because the orphan sweep deletes and
    `evaluate_identity` needs the DOI to tell near-identical titles apart.
    """
    result: dict[str, list[dict[str, Any]]] = {}
    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                fp = row.get("file_path", "")
                abs_fp = os.path.abspath(fp)
                author_dir_path = os.path.dirname(abs_fp)
                try:
                    with open(abs_fp, encoding="utf-8") as bf:
                        entry = bt.parse_bibtex_to_dict(bf.read())
                    if entry and (entry.get("fields") or {}).get("title"):
                        result.setdefault(author_dir_path, []).append(entry)
                except (OSError, ValueError):
                    pass
    except (OSError, ValueError):
        pass
    return result
