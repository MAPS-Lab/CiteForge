"""Direct tests for ``finalize_run`` (the post-run finalization tail).

``citeforge.pipeline.postrun.finalize_run`` owns the irreversible on-disk data
safety guarantees of the whole pipeline: it is the only place that deletes
``.bib`` files (duplicate orphans and out-of-window files) and the place that
rewrites ``baseline.json``. It had zero direct tests. These
exercise the real function against a hermetic ``tmp_path`` output tree with real
factory-serialized ``.bib`` files and a real io_utils summary CSV, and assert
the load-bearing guards by inspecting the filesystem after the call:

* ORPHAN SAFETY -- a tracked file survives; a dissimilar on-disk orphan is KEPT
  (with a warn), while a >= 0.95 title-duplicate orphan IS removed.
* YEAR-WINDOW -- an out-of-window file is deleted while an in-window file and
  anything under ``a2i2/`` are untouched.
* PHANTOM-WRITE -- a second identical run rewrites no ``.bib`` bytes and bumps no
  mtime (content-comparison guard holds).
* baseline.json is created under ``out_dir`` and is valid JSON.

No network is touched. The a2i2 build is neutralized by pointing its input CSV
at a nonexistent path so build_a2i2_folder returns early without clearing the
``a2i2/`` folder, isolating the year-window contract.
"""

from __future__ import annotations

import csv
import json
import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from citeforge.config import get_min_year
from citeforge.io_utils import append_summary_to_csv, init_summary_csv
from citeforge.models import Record
from citeforge.pipeline import postrun
from citeforge.pipeline.postrun import FinalizationError, finalize_run
from tests.factories import article, misc, write_bib

# A year inside the contribution window under any CITEFORGE_MIN_YEAR override.
_IN_WINDOW_YEAR = get_min_year() + 4


def _author_dir(out_dir: Path, name: str = "Doe (abc123)") -> Path:
    """Create and return an author subdirectory under *out_dir*."""
    d = out_dir / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def _track_in_csv(csv_path: Path, tracked: list[Path]) -> None:
    """Build a summary CSV via the real io_utils helpers, tracking *tracked*.

    Paths are written absolute so os.path.abspath() in finalize_run resolves them
    identically regardless of the process CWD, keeping the test hermetic.
    """
    init_summary_csv(str(csv_path))
    for p in tracked:
        append_summary_to_csv(str(csv_path), str(p), trust_hits=1, flags={})


def _snapshot_bibs(directory: Path) -> dict[str, tuple[bytes, int]]:
    """Map each ``.bib`` filename under *directory* to (bytes, mtime_ns)."""
    return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in sorted(directory.glob("*.bib"))}


@pytest.fixture
def no_a2i2(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Neutralize the a2i2 build so it never clears/creates ``out_dir/a2i2``.

    build_a2i2_folder returns 0 early when its input CSV is missing, so pointing
    DEFAULT_A2I2_INPUT (as imported into postrun) at a nonexistent path isolates
    the year-window and phantom-write contracts from a2i2 side effects.
    """
    monkeypatch.setattr(postrun, "DEFAULT_A2I2_INPUT", str(tmp_path / "no_such_a2i2.csv"))


@pytest.fixture
def cf_caplog(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    """Capture records from the non-propagating ``CiteForge`` logger.

    The project logger sets ``propagate = False``, so pytest's root-attached
    capture handler never sees its records. Attaching caplog's handler directly
    to the ``CiteForge`` logger for the duration of the test fixes that.
    """
    cf_logger = logging.getLogger("CiteForge")
    caplog.set_level(logging.DEBUG, logger="CiteForge")
    cf_logger.addHandler(caplog.handler)
    try:
        yield caplog
    finally:
        cf_logger.removeHandler(caplog.handler)


def _records() -> list[Record]:
    """Return the single real Record whose id matches the author dir suffix."""
    return [Record(name="Doe, Jane", scholar_id="abc123")]


# --- ORPHAN SAFETY ----------------------------------------------------------


def test_dissimilar_orphan_survives_and_is_warned(
    tmp_path: Path, no_a2i2: None, cf_caplog: pytest.LogCaptureFixture
) -> None:
    """An on-disk orphan whose title is < 0.95 similar to any tracked title is
    KEPT (never deleted) and a keep/warn is logged. This is the load-bearing
    data-loss guard.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    tracked = write_bib(
        author,
        article(key="DoeAlpha", title="Alpha Study of Vessel Tracking Systems"),
        f"Doe{_IN_WINDOW_YEAR}-Alpha.bib",
    )
    orphan = write_bib(
        author,
        article(key="DoeBeta", title="Weather Prediction Using Orbital Satellites"),
        f"Doe{_IN_WINDOW_YEAR}-Beta.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [tracked])

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert tracked.exists(), "tracked .bib must never be removed"
    assert orphan.exists(), "dissimilar orphan must survive finalize_run (data-loss guard)"
    assert any(
        "Orphan kept" in rec.getMessage() and "Doe" in rec.getMessage() and "Beta" in rec.getMessage()
        for rec in cf_caplog.records
    ), "a keep/warn must be logged for the surviving orphan"


def test_duplicate_orphan_is_removed(tmp_path: Path, no_a2i2: None, cf_caplog: pytest.LogCaptureFixture) -> None:
    """An on-disk orphan whose title is a >= 0.95 duplicate of a tracked title in
    the same author directory IS removed, and the removal is logged.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    dup_title = "Alpha Study of Vessel Tracking Systems"
    tracked = write_bib(author, article(key="DoeAlpha", title=dup_title), f"Doe{_IN_WINDOW_YEAR}-Alpha.bib")
    # Identical normalized title => similarity 1.0, well above the 0.95 band.
    dup_orphan = write_bib(author, article(key="DoeGamma", title=dup_title), f"Doe{_IN_WINDOW_YEAR}-Gamma.bib")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [tracked])

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert tracked.exists(), "tracked .bib must survive"
    assert not dup_orphan.exists(), "duplicate orphan (>= 0.95 title match) must be removed"
    assert any(
        "Removed duplicate orphan" in rec.getMessage() and "Gamma" in rec.getMessage() for rec in cf_caplog.records
    ), "duplicate-orphan removal must be logged"


def test_both_orphan_branches_in_one_run(tmp_path: Path, no_a2i2: None) -> None:
    """A single finalize_run keeps the dissimilar orphan and removes the
    duplicate orphan, exercising both branches of the orphan guard together.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    alpha_title = "Alpha Study of Vessel Tracking Systems"
    tracked = write_bib(author, article(key="DoeAlpha", title=alpha_title), f"Doe{_IN_WINDOW_YEAR}-Alpha.bib")
    keep = write_bib(
        author,
        article(key="DoeBeta", title="Weather Prediction Using Orbital Satellites"),
        f"Doe{_IN_WINDOW_YEAR}-Beta.bib",
    )
    remove = write_bib(author, article(key="DoeGamma", title=alpha_title), f"Doe{_IN_WINDOW_YEAR}-Gamma.bib")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [tracked])

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert tracked.exists()
    assert keep.exists(), "dissimilar orphan kept"
    assert not remove.exists(), "duplicate orphan removed"


# --- PREPRINT SUPERSEDED BY PUBLISHED ---------------------------------------


def test_preprint_superseded_by_published_is_removed(
    tmp_path: Path, no_a2i2: None, cf_caplog: pytest.LogCaptureFixture
) -> None:
    """When an author has a preprint (arXiv DOI) AND a published record of the
    same work (same title, real journal DOI), the preprint file is deleted and
    the published one is kept. This is the "published outranks preprint" pair
    guard, applied generally across authors.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    same_title = "Detecting Ongoing Events Using Contextual Word and Sentence Embeddings"

    preprint = write_bib(
        author,
        misc(
            key="PreA",
            title=same_title,
            year=str(_IN_WINDOW_YEAR - 2),
            howpublished="arXiv",
            doi="10.48550/arxiv.2007.01379",
        ),
        f"Doe{_IN_WINDOW_YEAR - 2}-Detecting.bib",
    )
    published = write_bib(
        author,
        article(
            key="PubA",
            title=same_title,
            year=str(_IN_WINDOW_YEAR),
            journal="Expert Systems with Applications",
            doi="10.1016/j.eswa.2022.118257",
        ),
        f"Doe{_IN_WINDOW_YEAR}-Detecting.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [preprint, published])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    assert published.exists(), "published record must be kept"
    assert not preprint.exists(), "preprint superseded by a published twin must be removed"
    assert any(
        "Removed superseded preprint" in rec.getMessage() and "Detecting" in rec.getMessage()
        for rec in cf_caplog.records
    ), "superseded-preprint removal must be logged"


def test_standalone_preprint_is_retained(tmp_path: Path, no_a2i2: None) -> None:
    """A preprint with NO published counterpart in the author dir is retained
    (the goal keeps arXiv/repository entries when no published record exists).
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    lone = write_bib(
        author,
        misc(
            key="Lone",
            title="Succinct Euler Tour Trees for Sparse Graphs",
            year=str(_IN_WINDOW_YEAR),
            howpublished="arXiv",
            doi="10.48550/arxiv.2105.04965",
        ),
        f"Doe{_IN_WINDOW_YEAR}-Euler.bib",
    )
    unrelated = write_bib(
        author,
        article(
            key="Other",
            title="A Completely Different Published Paper on Weather",
            year=str(_IN_WINDOW_YEAR),
            journal="Journal of Climate",
            doi="10.1175/jcli-d-20-0001.1",
        ),
        f"Doe{_IN_WINDOW_YEAR}-Weather.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [lone, unrelated])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    assert lone.exists(), "standalone preprint (no published twin) must be retained"
    assert unrelated.exists(), "unrelated published paper must be kept"


def test_two_preprints_no_published_both_retained(tmp_path: Path, no_a2i2: None) -> None:
    """Two preprints with no published record are both kept: supersede only fires
    against a genuinely published twin, never preprint-vs-preprint.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    title = "On the Convergence of Variable Metric Methods"

    p1 = write_bib(
        author,
        misc(key="P1", title=title, year=str(_IN_WINDOW_YEAR), howpublished="arXiv", doi="10.48550/arxiv.2401.00001"),
        f"Doe{_IN_WINDOW_YEAR}-One.bib",
    )
    p2 = write_bib(
        author,
        misc(key="P2", title=title, year=str(_IN_WINDOW_YEAR), howpublished="arXiv", doi="10.48550/arxiv.2401.00002"),
        f"Doe{_IN_WINDOW_YEAR}-Two.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [p1, p2])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    assert p1.exists() and p2.exists(), "two preprints with no published twin must both be retained"


# --- YEAR-WINDOW ------------------------------------------------------------


def test_year_window_removes_only_out_of_window_files(tmp_path: Path, no_a2i2: None) -> None:
    """Out-of-window files are deleted; in-window files and anything under
    ``a2i2/`` are untouched by the year-window cleanup.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()

    in_window = write_bib(
        author,
        article(key="InW", title="Vessel Routing in the North Atlantic", year=str(window_min)),
        f"Doe{window_min}-InWindow.bib",
    )
    out_of_window = write_bib(
        author,
        article(key="OutW", title="Historical Weather Records Analysis", year=str(window_min - 5)),
        f"Doe{window_min - 5}-OutWindow.bib",
    )

    a2i2_dir = out_dir / "a2i2"
    a2i2_dir.mkdir(parents=True)
    a2i2_old = write_bib(
        a2i2_dir,
        article(key="Anc", title="An Ancient Joint Publication", year=str(window_min - 10)),
        f"Zzz{window_min - 10}-Ancient.bib",
    )

    csv_path = tmp_path / "summary.csv"
    # Track both author files so the orphan pass is a no-op and only the
    # year-window logic decides their fate.
    _track_in_csv(csv_path, [in_window, out_of_window])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    assert in_window.exists(), "in-window file must be kept"
    assert not out_of_window.exists(), "out-of-window file must be removed"
    assert a2i2_old.exists(), "a2i2/ files must be untouched by the year-window cleanup"


def test_year_window_keeps_boundary_year(tmp_path: Path, no_a2i2: None) -> None:
    """A file exactly at the window minimum year is kept (strict ``< window_min``
    removal, not ``<=``).
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()

    boundary = write_bib(
        author,
        article(key="Bnd", title="A Boundary Year Paper", year=str(window_min)),
        f"Doe{window_min}-Boundary.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [boundary])

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert boundary.exists(), "file at the boundary year (== window_min) must be kept"


def test_year_window_trusts_the_entry_year_over_a_stale_filename(tmp_path: Path, no_a2i2: None) -> None:
    """A filename is a derived label and can go stale; the entry's year is data.

    The deduplicator writes a surviving entry under the duplicate's existing
    filename, so a file can be named for a year its contents do not carry. This
    is a deletion path, so reading the label would eventually destroy an
    in-window paper because its name says otherwise, and keep an out-of-window
    one for the same reason. Both directions are asserted.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()

    # Named out-of-window, actually in-window. Must survive.
    keep = write_bib(
        author,
        article(key="Keep", title="Actually A Recent Paper", year=str(window_min + 2)),
        f"Doe{window_min - 3}-StaleName.bib",
    )
    # Named in-window, actually out-of-window. Must be removed.
    drop = write_bib(
        author,
        article(key="Drop", title="Actually An Old Paper", year=str(window_min - 3)),
        f"Doe{window_min + 2}-StaleName.bib",
    )

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=None)

    assert keep.exists(), "an in-window entry must not be deleted because its filename says otherwise"
    assert not drop.exists(), "an out-of-window entry must not survive because its filename says otherwise"


def test_year_window_keeps_a_file_with_no_usable_year_anywhere(tmp_path: Path, no_a2i2: None) -> None:
    """No evidence is not evidence of being out of window.

    The permissive path is unchanged by the precedence swap: when neither the
    entry nor the filename yields a valid year, the file stays. Deletion
    requires a year, never the absence of one.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    undated = write_bib(
        author,
        article(key="NoYear", title="An Undated Paper", year=""),
        "undated-paper.bib",
    )

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert undated.exists(), "a file with no usable year must be kept, not deleted"


@pytest.mark.parametrize("year_field", ["", "in press", "n.d.", "MMXXIV", "forthcoming"])
def test_year_window_never_deletes_on_the_filename_year(tmp_path: Path, no_a2i2: None, year_field: str) -> None:
    """A stale filename must not delete a file its entry says nothing about.

    A filename can be inherited from another record, since the deduplicator
    writes a survivor under the losing duplicate's name, so an unparseable year
    field must never fall back to it. Deletion needs the entry's own year.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()

    undated = write_bib(
        author,
        article(key="Undated", title="A Paper With No Parseable Year", year=year_field),
        f"Doe{window_min - 5}-StaleOldName.bib",
    )

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert undated.exists(), "no entry year is no evidence, and no evidence must not delete"


# --- RENAME vs ORPHAN SWEEP -------------------------------------------------


def test_a_renamed_file_survives_the_next_run(tmp_path: Path, no_a2i2: None) -> None:
    """A file the author-prefix repair renamed is not deleted by the next run.

    Three steps have to agree for this to hold, and they run in this order:
    phantom reconciliation, orphan removal, then the fixup pass that renames.
    The rename therefore lands AFTER the CSV has been read, so on the next run
    the file is untracked. The summary CSV persists across runs
    (``preserve_existing=True``), so the stale row naming the old path is still
    in it, and an untracked ``.bib`` whose title matches a tracked title is
    exactly what the orphan sweep deletes.

    What saves it is that reconciliation strips the stale row first, taking the
    matching title with it, so the renamed file is kept and warned about instead
    of removed. That is a real guarantee resting on step order rather than on
    anything local, which is why it is pinned here: reordering finalize_run or
    making reconciliation conditional would silently delete corrected files.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    pubmed = "Jin Y and Guo Y and Koller Jm and Grossen Sc and Uhlmann A and Forde Nj"
    stale = write_bib(
        author,
        misc(key="Y2026:Neuro", title="Altered Neurodevelopmental Trajectories", author=pubmed, year="2026"),
        "Y2026-AlteredNeuro.bib",
    )
    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [stale])

    first = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    renamed = author / "Jin2026-AlteredNeuro.bib"
    assert first.files_renamed == 1
    assert renamed.exists()

    # The same CSV, still naming the path the rename invalidated.
    second = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert renamed.exists(), "the corrected file must not be swept as an orphan of its own former name"
    # The rename now repoints the CSV row inside the same run, so the second run
    # finds nothing stale and nothing untracked. Previously the file survived
    # only because reconciliation happened to strip the stale row before the
    # orphan sweep read it, which was an ordering accident rather than a
    # guarantee; now there is no stale row to strip and no orphan to judge.
    assert second.phantom_rows_removed == 0, "the CSV must already name the renamed file"
    assert second.orphans_removed == 0
    assert second.orphans_kept == 0, "the renamed file must be tracked, not merely spared"


def test_csv_names_exactly_the_files_on_disk_at_return(tmp_path: Path, no_a2i2: None) -> None:
    """The CSV must be consistent when finalize_run returns, not one run later.

    Every step that removes a .bib runs after reconciliation, so a deletion left
    a row naming a file that no longer exists while the report said
    phantom_rows_removed=0. The rename step repoints its own rows; deletions
    cannot, so a trailing reconcile restores the invariant the report implies.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()

    keep = write_bib(
        author,
        article(key="Keep", title="An In Window Paper", year=str(window_min + 1)),
        f"Doe{window_min + 1}-Keep.bib",
    )
    # Deleted by the year window, which runs after the CSV was reconciled.
    doomed = write_bib(
        author,
        article(key="Old", title="An Out Of Window Paper", year=str(window_min - 4)),
        f"Doe{window_min - 4}-Old.bib",
    )
    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [keep, doomed])

    report = finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    assert not doomed.exists()
    assert report.out_of_window_removed == 1
    assert report.phantom_rows_removed == 1, "the row for the file this run deleted must be gone at return"

    rows = [row["file_path"] for row in csv.DictReader(csv_path.open(newline="", encoding="utf-8"))]
    on_disk = {str(p) for p in author.glob("*.bib")}
    assert {os.path.abspath(r) for r in rows} == {os.path.abspath(p) for p in on_disk}


def test_a_file_unequal_to_its_own_serialization_is_repaired(tmp_path: Path, no_a2i2: None) -> None:
    """The post-run fixup must settle a file no rule wants to change.

    A title written with a newline in it parses back with the whitespace
    collapsed, so the entry differs from its own re-serialization while every
    canonicalization rule reports no change. Gating the write on the rules left
    such a file rewritten on every run and never settled, which kept the corpus
    digest moving and stopped the monthly refresh converging.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    damaged = author / f"Doe{_IN_WINDOW_YEAR}-Multiline.bib"
    damaged.write_text(
        "@article{Doe" + str(_IN_WINDOW_YEAR) + ":Multi,\n"
        "  title = {CD16a\nhigh NK cell infiltration},\n"
        '  author = {Doe, Jane},\n'
        f"  year = {{{_IN_WINDOW_YEAR}}},\n"
        "  journal = {Some Journal}\n}\n",
        encoding="utf-8",
    )

    first = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert first.files_fixed == 1, "a file unequal to its own serialization must be repaired"
    assert "CD16a high NK cell" in damaged.read_text(encoding="utf-8")

    second = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert second.files_fixed == 0, "the repair must reach a fixpoint, not rewrite every run"


# --- PHANTOM-WRITE GUARD ----------------------------------------------------


def test_second_run_is_a_no_op_on_bib_files(tmp_path: Path, no_a2i2: None) -> None:
    """Running finalize_run twice leaves every ``.bib``'s bytes AND mtime
    unchanged on the second run (content-comparison guard prevents rewrite churn).
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    a = write_bib(
        author,
        article(key="P1", title="Deterministic Metadata Aggregation at Scale"),
        f"Doe{_IN_WINDOW_YEAR}-One.bib",
    )
    b = write_bib(
        author,
        article(key="P2", title="Trust Based Merging of Bibliographic Records"),
        f"Doe{_IN_WINDOW_YEAR}-Two.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [a, b])

    # First run stabilizes any serializer normalization.
    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))
    before = _snapshot_bibs(author)
    assert set(before) == {a.name, b.name}, "both .bib files must survive the first run"

    # Second identical run must not touch any .bib bytes or mtimes.
    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))
    after = _snapshot_bibs(author)

    assert after == before, "second finalize_run rewrote .bib files (phantom-write churn)"


# --- baseline.json ----------------------------------------------------------


def test_writes_valid_baseline_json(tmp_path: Path, no_a2i2: None) -> None:
    """finalize_run writes a valid baseline.json under out_dir."""
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    f1 = write_bib(author, article(key="B1", title="First Surviving Paper"), f"Doe{_IN_WINDOW_YEAR}-First.bib")
    f2 = write_bib(author, article(key="B2", title="Second Surviving Paper"), f"Doe{_IN_WINDOW_YEAR}-Second.bib")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [f1, f2])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    baseline_path = out_dir / "baseline.json"
    assert baseline_path.exists(), "baseline.json must be written under out_dir"

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["total"] == 2, "baseline total must reflect the two surviving files"
    assert baseline["authors"]["Doe (abc123)"] == 2


def test_baseline_omits_an_author_directory_holding_no_files(tmp_path: Path, no_a2i2: None) -> None:
    """An empty author directory is absent from baseline.json, not recorded as zero.

    Git cannot commit an empty directory, so a reader deriving counts from a
    committed tree never produces a key for such an author. Recording a zero
    here made the two memberships unequal by construction, and the corpus check
    rejects that as a stale baseline. The 2026-08 refresh wrote seven of them.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    write_bib(author, article(key="K1", title="The Only Kept Paper"), f"Doe{_IN_WINDOW_YEAR}-Kept.bib")
    (out_dir / "Nobody (zzz999)").mkdir(parents=True)

    report = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    baseline = json.loads((out_dir / "baseline.json").read_text(encoding="utf-8"))
    assert "Nobody (zzz999)" not in baseline["authors"]
    assert "Nobody (zzz999)" not in report.baseline_authors
    assert baseline["authors"]["Doe (abc123)"] == 1
    assert baseline["total"] == 1, "an omitted zero cannot change the total"


def test_no_summary_csv_skips_only_the_csv_bound_steps(tmp_path: Path, no_a2i2: None) -> None:
    """Without a summary CSV the orphan pass cannot run (it has nothing to
    compare against), but the year-window cleanup and baseline rewrite do not
    read the CSV and therefore still run.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()
    survivor = write_bib(author, article(key="S1", title="A Solitary Paper"), f"Doe{_IN_WINDOW_YEAR}-Solo.bib")
    stale = write_bib(
        author,
        article(key="S2", title="An Out Of Window Paper", year=str(window_min - 5)),
        f"Doe{window_min - 5}-Stale.bib",
    )

    report = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert survivor.exists(), "an in-window file must remain"
    assert not stale.exists(), "the year-window cleanup does not need the CSV and must still run"
    assert not report.summary_csv_present
    assert report.orphans_removed == 0 and report.orphans_kept == 0, "the orphan pass needs the CSV"
    assert report.out_of_window_removed == 1
    baseline = json.loads((out_dir / "baseline.json").read_text(encoding="utf-8"))
    assert baseline["total"] == 1, "baseline.json is rewritten without a CSV"


# --- STRUCTURED REPORT ------------------------------------------------------


def test_report_counts_every_step(tmp_path: Path, no_a2i2: None) -> None:
    """The returned report carries the counts a caller would otherwise have to
    re-derive from the tree or scrape out of the log.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    window_min = get_min_year()
    dup_title = "Alpha Study of Vessel Tracking Systems"

    tracked = write_bib(author, article(key="DoeAlpha", title=dup_title), f"Doe{_IN_WINDOW_YEAR}-Alpha.bib")
    write_bib(author, article(key="DoeGamma", title=dup_title), f"Doe{_IN_WINDOW_YEAR}-Gamma.bib")
    write_bib(
        author,
        article(key="DoeBeta", title="Weather Prediction Using Orbital Satellites"),
        f"Doe{_IN_WINDOW_YEAR}-Beta.bib",
    )
    write_bib(
        author,
        article(key="DoeOld", title="A Historical Record", year=str(window_min - 5)),
        f"Doe{window_min - 5}-Old.bib",
    )

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [tracked])

    report = finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert report.summary_csv_path == str(csv_path)
    assert report.summary_csv_present
    assert report.orphans_removed == 1, "the title-duplicate orphan"
    assert report.orphans_kept == 2, "the dissimilar orphan and the out-of-window orphan"
    assert report.out_of_window_removed == 1
    assert report.superseded_preprints_removed == 0
    assert report.baseline_total == 2
    assert report.baseline_authors == {"Doe (abc123)": 2}


# --- FAILURE PROPAGATION ----------------------------------------------------


def test_unreadable_orphan_raises(tmp_path: Path, no_a2i2: None) -> None:
    """An orphan candidate that cannot be decoded aborts finalization instead of
    being silently treated as title-less and kept.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    tracked = write_bib(
        author,
        article(key="DoeAlpha", title="Alpha Study of Vessel Tracking Systems"),
        f"Doe{_IN_WINDOW_YEAR}-Alpha.bib",
    )
    broken = author / f"Doe{_IN_WINDOW_YEAR}-Broken.bib"
    broken.write_bytes(b"@article{X, title = {\xff\xfe}}\n")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [tracked])

    with pytest.raises(FinalizationError, match="orphan candidate"):
        finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))


def test_unreadable_year_window_candidate_raises(tmp_path: Path, no_a2i2: None) -> None:
    """A tracked file with no year in its name must be parsed for the year-window
    decision. When that read fails, finalization stops rather than keeping a file
    it could not classify.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    # No YYYY- segment, so the year-window step falls back to reading the entry.
    broken = author / "Doe-NoYearInName.bib"
    broken.write_bytes(b"@article{X, title = {\xff\xfe}}\n")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [broken])

    with pytest.raises(FinalizationError, match="year-window check"):
        finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert broken.exists(), "aborting must not have deleted the file it could not read"


def test_unreadable_candidate_raises(tmp_path: Path, no_a2i2: None) -> None:
    """A tracked file that cannot be read aborts the run rather than being skipped.

    The year-window step is the first reader, because it reads every entry's own
    year instead of trusting the filename. Which step notices does not matter;
    that an unreadable file stops the run rather than being skipped does.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    broken = author / f"Doe{_IN_WINDOW_YEAR}-Broken.bib"
    broken.write_bytes(b"@article{X, title = {\xff\xfe}}\n")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [broken])

    with pytest.raises(FinalizationError, match="year-window check"):
        finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))

    assert broken.exists(), "aborting must not have deleted the file it could not read"


def test_incoherent_a2i2_source_raises_finalization_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, no_a2i2: None
) -> None:
    """Conflicting member citations abort through the public finalization contract."""
    out_dir = tmp_path / "out"
    _author_dir(out_dir)

    def reject_conflict(*_args: object, **_kwargs: object) -> int:
        raise ValueError("conflicting citation metadata for DOI 10.5555/example")

    monkeypatch.setattr(postrun, "build_a2i2_folder", reject_conflict)

    with pytest.raises(FinalizationError, match="a2i2 rebuild"):
        finalize_run(str(out_dir), _records(), total_saved=0, processed=1, summary_csv_path=None)


def test_unresolved_same_doi_conflict_aborts_finalization(tmp_path: Path, no_a2i2: None) -> None:
    out_dir = tmp_path / "out"
    first_dir = _author_dir(out_dir, "Doe (abc123)")
    second_dir = _author_dir(out_dir, "Roe (def456)")
    common = {
        "title": "A Shared Network Study",
        "year": str(_IN_WINDOW_YEAR),
        "journal": "IEEE Transactions on Machine Learning in Communications and Networking",
        "doi": "10.1109/example",
    }
    write_bib(first_dir, article(key="DoeStudy", author="Jane Doe and Richard Roe", **common), "Doe-Study.bib")
    write_bib(second_dir, article(key="DoeStudy", author="Jane Doe and Alice Poe", **common), "Doe-Study.bib")

    with pytest.raises(FinalizationError, match="conflicting copies of the same DOI"):
        finalize_run(str(out_dir), _records(), total_saved=2, processed=1, summary_csv_path=None)


def test_failed_baseline_write_raises(tmp_path: Path, no_a2i2: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A baseline.json write that does not land aborts the run. The baseline is
    the run's own record of what it produced, so a discarded failure there makes
    every later comparison meaningless.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    survivor = write_bib(author, article(key="S1", title="A Solitary Paper"), f"Doe{_IN_WINDOW_YEAR}-Solo.bib")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [survivor])
    monkeypatch.setattr(postrun, "safe_write_json", lambda *a, **k: False)

    with pytest.raises(FinalizationError, match=r"baseline\.json"):
        finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=str(csv_path))
