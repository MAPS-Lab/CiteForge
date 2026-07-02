"""Deterministic end-to-end tests for the article pipeline and post-run tail.

``process_article`` (the 4-phase per-article flow) and ``finalize_run`` (the
ordered post-run tail) are the functions that most directly own the PRIME
DIRECTIVE and the on-disk data-safety guarantees, yet neither had a direct
test. These run the real functions with every network client stubbed to an
empty result, so only the deterministic baseline -> merge -> canonicalize ->
serialize -> save path executes, and assert byte-identical .bib output across a
cold re-run and a cache-hit re-run. The post-run finalization safety guarantees
(orphan-only deletion, contamination guard, year-window, phantom-write) are
covered in test_finalize_run.py.

No socket is ever opened (asserted), so a leaked real call would fail loudly
rather than make the oracle flaky.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest

from citeforge.models import Record
from citeforge.pipeline import article as article_mod
from tests.fakes import install_block_network

# Every article-module name that would otherwise reach the network. Each is
# replaced with a stub returning the empty result its caller expects, so the
# pipeline runs fully offline and deterministically.
_EMPTY_LIST_STUBS = [
    "crossref_search_multiple",
    "openreview_search_papers_multiple",
    "arxiv_search",
    "openalex_search_multiple",
    "pubmed_search_papers_multiple",
    "europepmc_search_papers_multiple",
    "s2_search_papers_multiple",
    "crossref_search_by_venue",
    "openalex_search_by_venue",
]


def _stub_all_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every enrichment entry point to an offline empty result."""
    for name in _EMPTY_LIST_STUBS:
        monkeypatch.setattr(article_mod, name, lambda *a, **k: [], raising=True)
    # DOI validation and Scholar citation detail: no candidate, no detail.
    monkeypatch.setattr(article_mod, "process_validated_doi", lambda *a, **k: False, raising=True)
    if hasattr(article_mod, "fetch_scholar_citation"):
        monkeypatch.setattr(article_mod, "fetch_scholar_citation", lambda *a, **k: None, raising=True)


def _art() -> dict[str, Any]:
    """A minimal Scholar-shaped article record (no network needed to seed it)."""
    return {
        "title": "A Deterministic Study of Vessel Trajectories",
        "authors": "Ada Lovelace and Charles Babbage",
        "year": "2021",
        "publication": "Journal of Maritime Analytics",
        "source": "scholar",
        "citation_id": "cid-deterministic-1",
    }


def _run_once(out_dir: Path, monkeypatch: pytest.MonkeyPatch) -> int:
    _stub_all_network(monkeypatch)
    install_block_network(monkeypatch)  # any real connection attempt now fails loudly
    rec = Record(name="Ada Lovelace", scholar_id="ABC1234567")
    return article_mod.process_article(
        rec,
        _art(),
        serply_key=None,
        out_dir=str(out_dir),
        s2_api_key=None,
        or_creds=None,
        gemini_api_key=None,
        summary_csv_path=None,
        min_year=0,
    )


def _bib_files(out_dir: Path) -> list[Path]:
    return sorted(out_dir.rglob("*.bib"))


def _digest(paths: list[Path]) -> dict[str, str]:
    return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def test_cold_runs_are_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two fresh runs of the same input produce byte-identical .bib files."""
    a, b = tmp_path / "a", tmp_path / "b"
    assert _run_once(a, monkeypatch) == 1
    assert _run_once(b, monkeypatch) == 1
    da, db = _digest(_bib_files(a)), _digest(_bib_files(b))
    assert da and da == db, f"cold-run drift: {da} vs {db}"


def test_cache_hit_rerun_is_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Re-running into the same directory (the existing .bib becomes the seed)
    leaves the bytes unchanged: the cache-hit PRIME DIRECTIVE."""
    out = tmp_path / "out"
    assert _run_once(out, monkeypatch) == 1
    first = _digest(_bib_files(out))
    _run_once(out, monkeypatch)
    second = _digest(_bib_files(out))
    assert first == second, f"cache-hit drift: {first} vs {second}"
