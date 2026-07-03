"""Tests for the stage-parameterized canonicalize() dispatch.

Covers per-rule behavior at CanonicalStage.POST_MERGE (Site C), LOAD_REPAIR
(Site B), and COMPLETE_SKIP_FINALIZE (complete-entry skip path), and proves each
rule set is a data fixpoint on the committed corpus (no oscillation).
"""

from __future__ import annotations

import copy
import glob
from typing import Any

import pytest

from citeforge.bibtex_utils import parse_bibtex_to_dict
from citeforge.canonicalize import (
    CanonicalStage,
    _rule_article_preprint_doi,
    canonicalize,
)
from citeforge.id_utils import is_secondary_doi
from tests.corpus import PREPRINT_SERVER_JOURNALS

# DOI fixtures drawn from tests.corpus.SECONDARY_DOI_CASES. Their classification is
# load-bearing for the preprint-journal contracts below, so it is asserted in
# test_doi_fixtures_are_classified_as_expected rather than assumed.
_PUBLISHED_DOI = "10.1145/3580305"  # SECONDARY_DOI_CASES: is_secondary=False
_SECONDARY_DOI = "10.48550/arxiv.2401.00001"  # SECONDARY_DOI_CASES: is_secondary=True


def _load_repair(entry: dict[str, Any]) -> dict[str, Any]:
    """Run LOAD_REPAIR canonicalize on a copy and return the mutated copy."""
    e = copy.deepcopy(entry)
    canonicalize(e, stage=CanonicalStage.LOAD_REPAIR)
    return e


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


def _complete_finalize(entry: dict[str, Any]) -> dict[str, Any]:
    """Run COMPLETE_SKIP_FINALIZE canonicalize on a copy and return the mutated copy."""
    e = copy.deepcopy(entry)
    canonicalize(e, stage=CanonicalStage.COMPLETE_SKIP_FINALIZE)
    return e


# ---------------------------------------------------------------------------
# Per-rule tests at POST_MERGE
# ---------------------------------------------------------------------------
def test_r11_conference_journal_to_inproceedings() -> None:
    """@article with a conference-proceedings journal -> @inproceedings."""
    result = _canon(_article(journal="Proceedings of the International Conference on Machine Learning"))
    assert result["type"] == "inproceedings"
    assert result["fields"]["booktitle"] == "Proceedings of the International Conference on Machine Learning"
    assert "journal" not in result["fields"]
    # Second POST_MERGE pass is a fixpoint (no further change).
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r14_patent_to_misc() -> None:
    """@article with a US patent number as journal -> @misc (journal -> note)."""
    result = _canon(_article(journal="US Patent 10,123,456"))
    assert result["type"] == "misc"
    assert result["fields"]["note"] == "US Patent 10,123,456"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r15_unpublished_to_misc() -> None:
    """@article with "Unpublished" journal -> @misc."""
    result = _canon(_article(journal="Unpublished"))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r16_preprint_journal_to_misc() -> None:
    """@article with a preprint server as journal -> @misc (journal -> howpublished)."""
    result = _canon(_article(journal="arXiv"))
    assert result["type"] == "misc"
    assert result["fields"]["howpublished"] == "arXiv"
    assert "journal" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_r19_misc_downgrade_branch() -> None:
    """An @article with a preprint DOI and no volume/pages is downgraded to @misc
    (the misc branch of the preprint-DOI rule).

    This exercises the rule helper directly. At full POST_MERGE the manufactured
    howpublished is a plain venue name that the misc->inproceedings upgrade would
    then promote, so the misc-downgrade branch is asserted in isolation here.
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
    """Keep branch: real journal + volume/pages keeps @article, strips preprint DOI/URL."""
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
    """@misc with a conference/workshop howpublished -> @inproceedings."""
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


# ---------------------------------------------------------------------------
# Per-rule tests at LOAD_REPAIR (Site B) + proof C-only rules are ABSENT
# ---------------------------------------------------------------------------
def test_load_repair_unpublished_to_misc_drops_publisher() -> None:
    """LOAD_REPAIR "Unpublished" journal -> @misc, dropping BOTH journal and publisher.

    This diverges from POST_MERGE (which keeps publisher), so it must be its own rule.
    """
    result = _load_repair(_article(journal="Unpublished", publisher="Some Press"))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert "publisher" not in result["fields"]


def test_load_repair_patent_to_misc() -> None:
    """LOAD_REPAIR @article with a US patent number as journal -> @misc (journal -> note)."""
    result = _load_repair(_article(journal="US Patent 10,123,456"))
    assert result["type"] == "misc"
    assert result["fields"]["note"] == "US Patent 10,123,456"
    assert "journal" not in result["fields"]


def test_load_repair_strips_email_from_author() -> None:
    """LOAD_REPAIR strips an email address (and a dangling separator) from the author field."""
    result = _load_repair(_article(author="Jane Doe jane@x.org and John Roe"))
    assert result["fields"]["author"] == "Jane Doe and John Roe"


def test_load_repair_strips_bracket_j_title() -> None:
    """LOAD_REPAIR strips a trailing "[J]" bracket artifact from the title."""
    result = _load_repair(_article(title="A Study of Neural Networks [J]"))
    assert result["fields"]["title"] == "A Study of Neural Networks"


def test_load_repair_strip_secondary_doi_keeps_article() -> None:
    """LOAD_REPAIR strips the preprint DOI/URL when journal + volume/pages exist,
    keeping the entry as @article (mirrors the POST_MERGE keep-branch)."""
    result = _load_repair(
        _article(
            journal="Real Journal",
            doi="10.48550/arXiv.2101.00002",
            volume="12",
            pages="1-10",
            url="https://arxiv.org/abs/2101.00002",
        )
    )
    assert result["type"] == "article"
    assert result["fields"]["journal"] == "Real Journal"
    assert "doi" not in result["fields"]
    assert "url" not in result["fields"]


def test_load_repair_secondary_doi_misc_branch_absent() -> None:
    """The POST_MERGE-only misc-downgrade is absent at LOAD_REPAIR. An @article with
    a preprint DOI but no volume/pages is left unchanged (still @article, DOI kept).

    POST_MERGE would downgrade this to @misc; LOAD_REPAIR must not.
    """
    entry = _article(journal="Journal of Foo", doi="10.48550/arXiv.2101.00001")
    result = _load_repair(entry)
    assert result["type"] == "article"
    assert result["fields"]["journal"] == "Journal of Foo"
    assert result["fields"]["doi"] == "10.48550/arXiv.2101.00001"
    # Contrast: POST_MERGE moves it out of @article (downgrade then misc->inproceedings upgrade),
    # so LOAD_REPAIR's keep-as-article behavior is genuinely distinct.
    post = _canon(entry)
    assert post["type"] != "article"
    assert "journal" not in post["fields"]


def test_load_repair_r20_misc_howpublished_absent() -> None:
    """The POST_MERGE-only misc-howpublished-to-@inproceedings upgrade is absent at
    LOAD_REPAIR. A @misc with a conference howpublished stays @misc (POST_MERGE
    would upgrade it)."""
    entry = _article(type="misc", howpublished="International Conference on Learning Representations")
    result = _load_repair(entry)
    assert result["type"] == "misc"
    assert result["fields"]["howpublished"] == "International Conference on Learning Representations"
    assert "booktitle" not in result["fields"]
    # Contrast: POST_MERGE upgrades the same entry to @inproceedings.
    assert _canon(entry)["type"] == "inproceedings"


def test_load_repair_r13_url_booktitle_absent() -> None:
    """The POST_MERGE-only url-booktitle-to-@misc downgrade is absent at LOAD_REPAIR.
    An @inproceedings with a URL booktitle stays @inproceedings (POST_MERGE -> @misc)."""
    entry = _article(type="inproceedings", booktitle="https://foo.example/paper")
    result = _load_repair(entry)
    assert result["type"] == "inproceedings"
    assert "booktitle" in result["fields"]
    # Contrast: POST_MERGE downgrades the same entry to @misc.
    assert _canon(entry)["type"] == "misc"


def test_load_repair_article_no_journal_stays_article() -> None:
    """C-only terminal (article-with-no-journal -> @misc) is ABSENT at LOAD_REPAIR:
    a bare @article without a journal is left as @article (POST_MERGE -> @misc)."""
    entry = _article()  # title/author/year only, no journal
    result = _load_repair(entry)
    assert result["type"] == "article"
    # Contrast: POST_MERGE downgrades a journal-less @article to @misc.
    assert _canon(entry)["type"] == "misc"


# ---------------------------------------------------------------------------
# Corpus idempotency: LOAD_REPAIR is a data fixpoint (second pass = no change)
# ---------------------------------------------------------------------------
def test_load_repair_is_fixpoint_on_corpus() -> None:
    """Applying LOAD_REPAIR twice yields identical type + fields for every committed
    output/**/*.bib (a second pass is a strict data fixpoint)."""
    files = sorted(glob.glob("output/**/*.bib", recursive=True))
    if not files:
        pytest.skip("no committed output corpus present")
    non_fixpoint: list[str] = []
    for bib_path in files:
        with open(bib_path, encoding="utf-8") as fh:
            entry = parse_bibtex_to_dict(fh.read())
        if entry is None:
            continue
        canonicalize(entry, stage=CanonicalStage.LOAD_REPAIR)
        snapshot = copy.deepcopy(entry)
        canonicalize(entry, stage=CanonicalStage.LOAD_REPAIR)
        if entry["type"] != snapshot["type"] or entry["fields"] != snapshot["fields"]:
            non_fixpoint.append(bib_path)
    assert not non_fixpoint, f"LOAD_REPAIR not a data fixpoint for: {non_fixpoint[:10]}"


# ---------------------------------------------------------------------------
# Per-rule tests at COMPLETE_SKIP_FINALIZE (complete entry, enrichment skipped)
# ---------------------------------------------------------------------------
def test_complete_finalize_strips_preprint_only_publisher() -> None:
    """The single live rule: strip a preprint-only publisher off a real-venue entry."""
    result = _complete_finalize(_article(journal="Real Journal", publisher="Cold Spring Harbor Laboratory"))
    assert "publisher" not in result["fields"]
    assert result["fields"]["journal"] == "Real Journal"


def test_complete_finalize_keeps_publisher_when_journal_is_preprint() -> None:
    """Guard: do NOT strip when the journal is itself a preprint server."""
    result = _complete_finalize(_article(journal="arXiv", publisher="Cold Spring Harbor Laboratory"))
    assert result["fields"]["publisher"] == "Cold Spring Harbor Laboratory"


def test_complete_finalize_keeps_normal_publisher() -> None:
    """A publisher that is not preprint-exclusive is left untouched."""
    result = _complete_finalize(_article(journal="Real Journal", publisher="Springer"))
    assert result["fields"]["publisher"] == "Springer"


def test_complete_finalize_dead_preprint_doi_rule_removed() -> None:
    """The removed dead rule (@article + secondary DOI -> @misc) is gone.

    _entry_is_complete() only admits NON-preprint DOIs, so that quick-fixup could
    never fire on this path. Prove its removal is inert: a complete-shaped entry
    carrying a preprint DOI (and no preprint-only publisher) passes through
    COMPLETE_SKIP_FINALIZE completely unchanged (no @misc downgrade, DOI kept,
    no howpublished manufactured).
    """
    entry = _article(journal="Real Journal", doi="10.48550/arXiv.2101.00003")
    assert canonicalize(copy.deepcopy(entry), stage=CanonicalStage.COMPLETE_SKIP_FINALIZE) is False
    result = _complete_finalize(entry)
    assert result["type"] == "article"
    assert result["fields"]["doi"] == "10.48550/arXiv.2101.00003"
    assert result["fields"]["journal"] == "Real Journal"
    assert "howpublished" not in result["fields"]


def test_complete_finalize_is_fixpoint_on_corpus() -> None:
    """Applying COMPLETE_SKIP_FINALIZE twice yields identical type + fields for every
    committed output/**/*.bib (a second pass is a strict data fixpoint)."""
    files = sorted(glob.glob("output/**/*.bib", recursive=True))
    if not files:
        pytest.skip("no committed output corpus present")
    non_fixpoint: list[str] = []
    for bib_path in files:
        with open(bib_path, encoding="utf-8") as fh:
            entry = parse_bibtex_to_dict(fh.read())
        if entry is None:
            continue
        canonicalize(entry, stage=CanonicalStage.COMPLETE_SKIP_FINALIZE)
        snapshot = copy.deepcopy(entry)
        canonicalize(entry, stage=CanonicalStage.COMPLETE_SKIP_FINALIZE)
        if entry["type"] != snapshot["type"] or entry["fields"] != snapshot["fields"]:
            non_fixpoint.append(bib_path)
    assert not non_fixpoint, f"COMPLETE_SKIP_FINALIZE not a data fixpoint for: {non_fixpoint[:10]}"


# ---------------------------------------------------------------------------
# Real-dispatch venueless / preprint downgrades (Site C, POST_MERGE)
#
# These drive the REAL canonicalize(entry, stage=POST_MERGE) dispatch and assert
# the production rule performs the reclassification. They are the genuine home
# for the contracts that tests/test_regression.py's TestVenuelessTypeDowngrade,
# TestArticlePreprintDoiDowngrade, and TestPreprintJournalDowngrade only *claimed*
# to test: those regression tests call merge_with_policy, observe that the
# downgrade did NOT happen there, then re-implement it in the test body
# (result["type"] = "misc") and assert on their own mutation -- so they would
# stay green even if canonicalize.py deleted every rule. The tests below have no
# such escape hatch; they mutate nothing and read only what canonicalize does.
# ---------------------------------------------------------------------------
def _load_then_post_twice(entry: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Apply the three fix sites in order (LOAD_REPAIR -> POST_MERGE -> POST_MERGE).

    Returns (settled_after_first_post_merge, after_repeat) so a caller can assert
    the second POST_MERGE pass is a strict data fixpoint (the anti-oscillation
    contract that keeps entry types from flipping between consecutive runs).
    """
    e = copy.deepcopy(entry)
    canonicalize(e, stage=CanonicalStage.LOAD_REPAIR)
    canonicalize(e, stage=CanonicalStage.POST_MERGE)
    settled = copy.deepcopy(e)
    canonicalize(e, stage=CanonicalStage.POST_MERGE)
    return settled, e


def test_doi_fixtures_are_classified_as_expected() -> None:
    """Guard the DOI fixtures the downgrade contracts depend on.

    If is_secondary_doi's verdict for these two DOIs ever flips, the preprint
    contracts below would silently test the wrong branch. Assert the premise.
    """
    assert is_secondary_doi(_SECONDARY_DOI) is True
    assert is_secondary_doi(_PUBLISHED_DOI) is False


def test_real_dispatch_article_no_journal_becomes_misc() -> None:
    """@article with no journal -> @misc through the REAL POST_MERGE dispatch.

    Regression counterpart: TestVenuelessTypeDowngrade
    .test_article_no_journal_with_published_doi_becomes_misc, which sets
    result["type"] = "misc" itself. Here the dispatch does the downgrade.
    """
    result = _canon(_article())  # title/author/year only; no journal
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    # POST_MERGE is a data fixpoint on the downgraded entry.
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_real_dispatch_article_no_journal_downgrades_even_with_published_doi() -> None:
    """A published DOI does NOT rescue a journal-less @article: still @misc.

    This is the exact shape TestVenuelessTypeDowngrade uses (a real published
    DOI, no journal); the real rule downgrades on the missing journal alone.
    """
    result = _canon(_article(doi=_PUBLISHED_DOI, publisher="Underline Science Inc."))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert result["fields"]["doi"] == _PUBLISHED_DOI


def test_real_dispatch_inproceedings_no_booktitle_becomes_misc() -> None:
    """@inproceedings with no booktitle -> @misc through the REAL POST_MERGE dispatch.

    Regression counterpart: TestVenuelessTypeDowngrade
    .test_inproceedings_no_booktitle_becomes_misc (which re-implements the flip).
    """
    result = _canon(_article(type="inproceedings"))  # no booktitle
    assert result["type"] == "misc"
    assert "booktitle" not in result["fields"]
    assert canonicalize(copy.deepcopy(result), stage=CanonicalStage.POST_MERGE) is False


def test_real_dispatch_article_with_journal_stays_article() -> None:
    """Negative control: @article WITH a real journal is not falsely downgraded."""
    result = _canon(_article(journal="Nature", doi="10.1038/s41586-024-00001"))
    assert result["type"] == "article"
    assert result["fields"]["journal"] == "Nature"


@pytest.mark.parametrize("journal", PREPRINT_SERVER_JOURNALS)
def test_real_dispatch_preprint_journal_published_doi_drops_journal(journal: str) -> None:
    """@article whose journal names a preprint server + a PUBLISHED DOI.

    Real POST_MERGE settles it to @misc with the stale preprint journal DROPPED
    and the published DOI KEPT (no howpublished manufactured). Captured from the
    live function: _rule_preprint_journal_to_misc always relabels @misc, and the
    published-DOI branch pops the journal without asserting a preprint server.

    Regression counterpart: TestPreprintJournalDowngrade, which only asserts that
    PREPRINT_SERVERS *contains* the server strings and never drives a downgrade.
    """
    result = _canon(_article(journal=journal, doi=_PUBLISHED_DOI))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert result["fields"]["doi"] == _PUBLISHED_DOI
    assert "howpublished" not in result["fields"]
    # Repeat POST_MERGE is a strict data fixpoint.
    repeat = copy.deepcopy(result)
    canonicalize(repeat, stage=CanonicalStage.POST_MERGE)
    assert repeat["type"] == result["type"]
    assert repeat["fields"] == result["fields"]


@pytest.mark.parametrize("journal", PREPRINT_SERVER_JOURNALS)
def test_real_dispatch_preprint_journal_secondary_doi_moves_to_howpublished(journal: str) -> None:
    """@article whose journal names a preprint server + a SECONDARY DOI.

    Real POST_MERGE settles it to @misc with the journal MOVED to howpublished
    (case-normalized) and the journal field removed. The howpublished stays a
    preprint-server label, so the misc->inproceedings upgrade correctly declines.
    """
    result = _canon(_article(journal=journal, doi=_SECONDARY_DOI))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    # Journal string is carried into howpublished (normalization only touches case).
    assert result["fields"]["howpublished"].lower() == journal.lower()
    repeat = copy.deepcopy(result)
    canonicalize(repeat, stage=CanonicalStage.POST_MERGE)
    assert repeat["type"] == result["type"]
    assert repeat["fields"] == result["fields"]


@pytest.mark.parametrize("journal", PREPRINT_SERVER_JOURNALS)
def test_real_dispatch_preprint_journal_no_doi_moves_to_howpublished(journal: str) -> None:
    """@article whose journal names a preprint server + NO DOI.

    Same terminal state as the secondary-DOI case: @misc, journal moved to
    howpublished, no DOI fabricated. Exercises the else branch of
    _rule_preprint_journal_to_misc (missing DOI is treated like a secondary one).
    """
    result = _canon(_article(journal=journal))
    assert result["type"] == "misc"
    assert "journal" not in result["fields"]
    assert "doi" not in result["fields"]
    assert result["fields"]["howpublished"].lower() == journal.lower()


def test_real_dispatch_article_preprint_doi_no_pages_settles_at_misc() -> None:
    """@article with a real journal + arXiv (secondary) DOI + no volume/pages
    settles at @misc and must NOT be fabricated into a conference.

    _rule_article_preprint_doi downgrades to @misc and moves the journal into
    howpublished. _rule_howpublished_to_inproceedings must then leave it alone:
    a journal name ("Neural Computing and Applications") carries no conference
    signal, so promoting it to @inproceedings with the journal as a booktitle
    would fabricate a venue that does not exist. The DOI is kept.
    """
    result = _canon(_article(journal="Neural Computing and Applications", doi="10.48550/arxiv.2302.02792"))
    assert result["type"] == "misc"
    assert result["fields"]["howpublished"] == "Neural Computing and Applications"
    assert "booktitle" not in result["fields"]
    assert "journal" not in result["fields"]
    assert result["fields"]["doi"] == "10.48550/arxiv.2302.02792"
    # The @misc end state is a data fixpoint (no oscillation on repeat).
    repeat = copy.deepcopy(result)
    canonicalize(repeat, stage=CanonicalStage.POST_MERGE)
    assert repeat["type"] == result["type"]
    assert repeat["fields"] == result["fields"]


@pytest.mark.parametrize("journal", ["Nature Communications", "Scientific Reports", "IEEE Transactions on Computers"])
def test_real_dispatch_journal_preprint_never_becomes_conference(journal: str) -> None:
    """A real journal reaching howpublished via the preprint-DOI downgrade never
    becomes a fabricated @inproceedings, across several journals."""
    result = _canon(_article(journal=journal, doi="10.48550/arxiv.2401.00001"))
    assert result["type"] == "misc"
    assert result["fields"].get("howpublished") == journal
    assert "booktitle" not in result["fields"]


def test_real_dispatch_misc_conference_howpublished_still_promotes() -> None:
    """A @misc whose howpublished is a genuine conference is still upgraded to
    @inproceedings (the fix narrows the rule, it does not disable it)."""
    result = _canon(_article(type="misc", howpublished="Workshop on Machine Learning for Health"))
    assert result["type"] == "inproceedings"
    assert result["fields"]["booktitle"] == "Workshop on Machine Learning for Health"
    assert "howpublished" not in result["fields"]


def test_real_dispatch_article_preprint_doi_with_pages_keeps_article() -> None:
    """Contrast branch: real journal + secondary DOI + volume/pages stays @article.

    _rule_article_preprint_doi's keep-branch strips the preprint DOI/URL but does
    NOT downgrade, so there is no journal->howpublished move and thus no spurious
    @inproceedings promotion. This is the non-fabricating half of the same rule.
    """
    result = _canon(
        _article(
            journal="Neural Computing and Applications",
            doi="10.48550/arxiv.2302.02792",
            volume="42",
            pages="1-20",
            url="https://arxiv.org/abs/2302.02792",
        )
    )
    assert result["type"] == "article"
    assert result["fields"]["journal"] == "Neural Computing and Applications"
    assert "doi" not in result["fields"]
    assert "url" not in result["fields"]


# ---------------------------------------------------------------------------
# Anti-oscillation across the three fix sites (LOAD_REPAIR -> POST_MERGE x2)
#
# The real contract that keeps entry types + container fields from flipping
# between consecutive pipeline runs: after the load-repair pass and the first
# post-merge pass settle an entry, a second post-merge pass must be a strict
# data fixpoint (identical type and fields).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("label", "entry"),
    [
        ("article_no_journal", _article()),
        ("inproceedings_no_booktitle", _article(type="inproceedings")),
        ("preprint_journal_secondary_doi", _article(journal="bioRxiv", doi=_SECONDARY_DOI)),
        ("preprint_journal_published_doi", _article(journal="arXiv", doi=_PUBLISHED_DOI)),
        ("real_journal_preprint_doi", _article(journal="Neural Computing and Applications", doi=_SECONDARY_DOI)),
    ],
)
def test_three_fix_sites_reach_a_fixpoint(label: str, entry: dict[str, Any]) -> None:
    """Applying canonicalize at LOAD_REPAIR then POST_MERGE then POST_MERGE again
    reaches a fixpoint: the type and every field are identical after the repeat.

    This is the real anti-oscillation guarantee (the three-way fix pattern in
    CLAUDE.md), driven end to end through the dispatch rather than asserted on a
    single rule in isolation.
    """
    settled, after_repeat = _load_then_post_twice(entry)
    assert after_repeat["type"] == settled["type"], f"{label}: type oscillated"
    assert after_repeat["fields"] == settled["fields"], f"{label}: fields oscillated"


def test_three_fix_sites_container_settles_venueless_to_misc() -> None:
    """Concrete anti-oscillation end state: a journal-less @article settles at
    @misc with no journal/booktitle container and stays there on repeat."""
    settled, after_repeat = _load_then_post_twice(_article())
    assert settled["type"] == "misc"
    assert "journal" not in settled["fields"]
    assert "booktitle" not in settled["fields"]
    assert after_repeat["type"] == "misc"
    assert after_repeat["fields"] == settled["fields"]
