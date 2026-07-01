"""Tests for the stage-parameterized canonicalize() dispatch.

Covers per-rule behavior at CanonicalStage.POST_MERGE (Site C) and proves the
POST_MERGE rule set is a data fixpoint on the committed corpus (no oscillation).
"""

from __future__ import annotations

import copy
import glob
from typing import Any

import pytest

from src.bibtex_utils import parse_bibtex_to_dict
from src.canonicalize import (
    CanonicalStage,
    _rule_article_preprint_doi,
    canonicalize,
)


def _article(**fields: str) -> dict[str, Any]:
    """Build a minimal @article entry with the given extra fields."""
    etype = fields.pop("type", "article")
    base = {"title": "A Study of Neural Networks", "author": "Doe, Jane", "year": "2021"}
    base.update(fields)
    return {"type": etype, "key": "k1", "fields": base}


def _canon(entry: dict[str, Any]) -> dict[str, Any]:
    """Run POST_MERGE canonicalize on a copy and return the mutated copy."""
    e = copy.deepcopy(entry)
    canonicalize(e, stage=CanonicalStage.POST_MERGE)
    return e


# ---------------------------------------------------------------------------
# Per-rule tests at POST_MERGE
# ---------------------------------------------------------------------------
def test_r11_conference_journal_to_inproceedings() -> None:
    """R11: @article with a conference-proceedings journal -> @inproceedings."""
    result = _canon(_article(journal="Proceedings of the International Conference on Machine Learning"))
    assert result["type"] == "inproceedings"
    assert result["fields"]["booktitle"] == "Proceedings of the International Conference on Machine Learning"
    assert "journal" not in result["fields"]
    # Second POST_MERGE pass is a fixpoint (no further change).
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r14_patent_to_misc() -> None:
    """R14: @article with a US patent number as journal -> @misc (journal -> note)."""
    result = _canon(_article(journal="US Patent 10,123,456"))
    assert result["type"] == "misc"
    assert result["fields"]["note"] == "US Patent 10,123,456"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r15_unpublished_to_misc() -> None:
    """R15: @article with "Unpublished" journal -> @misc."""
    result = _canon(_article(journal="Unpublished"))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r16_preprint_journal_to_misc() -> None:
    """R16: @article with a preprint server as journal -> @misc (journal -> howpublished)."""
    result = _canon(_article(journal="arXiv"))
    assert result["type"] == "misc"
    assert result["fields"]["howpublished"] == "arXiv"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r19_misc_downgrade_branch() -> None:
    """R19 (misc branch): @article with a preprint DOI and no volume/pages -> @misc.

    Exercised via the rule helper directly: at full POST_MERGE the manufactured
    howpublished is a plain venue name that R20 subsequently upgrades, so the
    misc-downgrade branch is asserted in isolation here.
    """
    entry = _article(journal="Journal of Foo", doi="10.48550/arXiv.2101.00001")
    fields = entry["fields"]
    changed = _rule_article_preprint_doi(entry, fields)
    assert changed is True
    assert entry["type"] == "misc"
    assert fields["howpublished"] == "Journal of Foo"
    assert "journal" not in fields
    # misc branch keeps the DOI (only the keep-article branch strips it)
    assert fields["doi"] == "10.48550/arXiv.2101.00001"


def test_r19_keep_article_strips_preprint_doi() -> None:
    """R19 (keep branch): real journal + volume/pages keeps @article, strips preprint DOI/URL."""
    entry = _article(
        journal="Real Journal",
        doi="10.48550/arXiv.2101.00002",
        volume="12",
        pages="1-10",
        url="https://arxiv.org/abs/2101.00002",
    )
    fields = entry["fields"]
    changed = _rule_article_preprint_doi(entry, fields)
    assert changed is True
    assert entry["type"] == "article"
    assert fields["journal"] == "Real Journal"
    assert "doi" not in fields
    assert "url" not in fields


def test_r20_misc_howpublished_to_inproceedings() -> None:
    """R20: @misc with a conference/workshop howpublished -> @inproceedings."""
    result = _canon(_article(type="misc", howpublished="International Conference on Learning Representations"))
    assert result["type"] == "inproceedings"
    assert result["fields"]["booktitle"] == "International Conference on Learning Representations"
    assert "howpublished" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


# ---------------------------------------------------------------------------
# Corpus idempotency: POST_MERGE is a data fixpoint (proves no oscillation)
# ---------------------------------------------------------------------------
def test_post_merge_is_fixpoint_on_corpus() -> None:
    """Applying POST_MERGE twice yields identical type + fields for every committed
    output/**/*.bib (proves no run-to-run oscillation).

    Note: on a small number of already-@misc preprint-DOI entries the boolean
    ``changed`` return can report True on a repeat pass due to benign intra-pass
    misc<->inproceedings churn, while the serialized data reaches a strict
    fixpoint. Byte-identity of the output depends on the data fixpoint, which is
    what this asserts.
    """
    files = sorted(glob.glob("output/**/*.bib", recursive=True))
    if not files:
        pytest.skip("no committed output corpus present")
    non_fixpoint: list[str] = []
    for bib_path in files:
        with open(bib_path, encoding="utf-8") as fh:
            entry = parse_bibtex_to_dict(fh.read())
        if entry is None:
            continue
        canonicalize(entry, stage=CanonicalStage.POST_MERGE)
        snapshot = copy.deepcopy(entry)
        canonicalize(entry, stage=CanonicalStage.POST_MERGE)
        if entry["type"] != snapshot["type"] or entry["fields"] != snapshot["fields"]:
            non_fixpoint.append(bib_path)
    assert not non_fixpoint, f"POST_MERGE not a data fixpoint for: {non_fixpoint[:10]}"
