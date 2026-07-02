"""Direct tests for ``finalize_run`` (the post-run finalization tail).

``citeforge.pipeline.postrun.finalize_run`` owns the irreversible on-disk data
safety guarantees of the whole pipeline: it is the only place that deletes
``.bib`` files (duplicate orphans and out-of-window files) and the place that
rewrites ``baseline.json`` / ``badges.json``. It had zero direct tests. These
exercise the real function against a hermetic ``tmp_path`` output tree with real
factory-serialized ``.bib`` files and a real io_utils summary CSV, and assert
the load-bearing guards by inspecting the filesystem after the call:

* ORPHAN SAFETY -- a tracked file survives; a dissimilar on-disk orphan is KEPT
  (with a warn), while a >= 0.95 title-duplicate orphan IS removed.
* YEAR-WINDOW -- an out-of-window file is deleted while an in-window file and
  anything under ``a2i2/`` are untouched.
* PHANTOM-WRITE -- a second identical run rewrites no ``.bib`` bytes and bumps no
  mtime (content-comparison guard holds).
* baseline.json / badges.json are created under ``out_dir`` and are valid JSON.

No network is touched. The a2i2 build is neutralized by pointing its input CSV
at a nonexistent path so build_a2i2_folder returns early without clearing the
``a2i2/`` folder, isolating the year-window contract.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from citeforge.config import get_min_year
from citeforge.io_utils import append_summary_to_csv, init_summary_csv
from citeforge.models import Record
from citeforge.pipeline import postrun
from citeforge.pipeline.postrun import finalize_run
from tests.factories import article, write_bib

# A year comfortably inside the contribution window regardless of any
# CITEFORGE_MIN_YEAR override (under the default env this is exactly 2024, so the
# filenames read Foo2024-*.bib as in the assignment).
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


# --- baseline.json / badges.json --------------------------------------------


def test_writes_valid_baseline_and_badges_json(tmp_path: Path, no_a2i2: None) -> None:
    """finalize_run writes baseline.json and badges.json under out_dir, and both
    are valid JSON with the expected shape.
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)

    f1 = write_bib(author, article(key="B1", title="First Surviving Paper"), f"Doe{_IN_WINDOW_YEAR}-First.bib")
    f2 = write_bib(author, article(key="B2", title="Second Surviving Paper"), f"Doe{_IN_WINDOW_YEAR}-Second.bib")

    csv_path = tmp_path / "summary.csv"
    _track_in_csv(csv_path, [f1, f2])

    finalize_run(str(out_dir), _records(), total_saved=2, processed=2, summary_csv_path=str(csv_path))

    baseline_path = out_dir / "baseline.json"
    badges_path = out_dir / "badges.json"
    assert baseline_path.exists(), "baseline.json must be written under out_dir"
    assert badges_path.exists(), "badges.json must be written under out_dir"

    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["total"] == 2, "baseline total must reflect the two surviving files"
    assert baseline["authors"]["Doe (abc123)"] == 2

    badges = json.loads(badges_path.read_text(encoding="utf-8"))
    for field in ("total_queries", "cache_positive_hits", "cache_negative_hits", "cache_misses", "hit_rate"):
        assert field in badges, f"badges.json missing {field}"


def test_no_summary_csv_writes_nothing(tmp_path: Path, no_a2i2: None) -> None:
    """When the summary CSV path is None or missing, finalize_run performs no
    cleanup and writes no baseline/badges files (guarded by the CSV-exists check).
    """
    out_dir = tmp_path / "out"
    author = _author_dir(out_dir)
    survivor = write_bib(author, article(key="S1", title="A Solitary Paper"), f"Doe{_IN_WINDOW_YEAR}-Solo.bib")

    finalize_run(str(out_dir), _records(), total_saved=1, processed=1, summary_csv_path=None)

    assert survivor.exists(), "no CSV means no cleanup: the file must remain"
    assert not (out_dir / "baseline.json").exists(), "baseline.json is only written when the CSV exists"
    assert not (out_dir / "badges.json").exists(), "badges.json is only written when the CSV exists"
