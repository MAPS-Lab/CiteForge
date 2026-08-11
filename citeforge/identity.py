"""Context-specific publication identity evidence without caller-owned actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any

from . import config as cfg
from . import text_utils as txt
from .id_utils import doi_bases_match, external_ids_match, extract_arxiv_eprint, is_secondary_doi, normalize_doi


class IdentityContext(Enum):
    IMPORT_LIST = "import_list"
    ENRICHMENT = "enrichment"
    CANDIDATE_DOI_NET = "candidate_doi_net"
    DISK_SURVIVOR = "disk_survivor"


class IdentityReason(Enum):
    NO_MATCH = "NO_MATCH"
    IMPORT_EXACT_TITLE = "IMPORT_EXACT_TITLE"
    IMPORT_WEIGHTED = "IMPORT_WEIGHTED"
    AUTHOR_CONFLICT = "AUTHOR_CONFLICT"
    YEAR_CONFLICT = "YEAR_CONFLICT"
    DOI_EXACT = "DOI_EXACT"
    DOI_VERSION = "DOI_VERSION"
    IDENTIFIER_TITLE_CONFLICT = "IDENTIFIER_TITLE_CONFLICT"
    DOI_CONFLICT = "DOI_CONFLICT"
    ARXIV_EXACT = "ARXIV_EXACT"
    ARXIV_CONFLICT = "ARXIV_CONFLICT"
    EXTERNAL_ID = "EXTERNAL_ID"
    MISSING_TITLE = "MISSING_TITLE"
    HIGH_TITLE = "HIGH_TITLE_SIM"
    TRUNCATED_TITLE = "TRUNCATED_TITLE"
    BELOW_MIN_SIM = "BELOW_MIN_SIM"
    GATE_CLOSED = "GATE_CLOSED"
    COMPOSITE = "COMPOSITE"
    KEY_TITLE = "KEY_TITLE"
    KEY_AUTHOR = "KEY_AUTHOR_OVERLAP"
    PREPRINT_PAIR = "PREPRINT_PAIR"
    PREPRINT_RELAXED = "PREPRINT_RELAXED"


@dataclass(frozen=True)
class IdentityEvidence:
    verdict: bool
    reason: IdentityReason
    title_similarity: float | None = None
    author_overlap_ratio: float | None = None
    year_gap: int | None = None
    composite_score: float | None = None
    matched_candidate_doi: str | None = None


@dataclass(frozen=True)
class _RecordView:
    fields: dict[str, Any]
    title: str
    normalized_title: str
    authors: str
    year: int | None
    doi: str | None
    arxiv_id: str | None
    key: str


def _view(record: dict[str, Any]) -> _RecordView:
    nested = record.get("fields")
    fields = nested if isinstance(nested, dict) else record
    title = str(fields.get("title") or "")
    authors = txt.to_text(fields.get("author") if "author" in fields else fields.get("authors"))
    year = txt.extract_year_from_any(fields.get("year"), fallback=None)
    return _RecordView(
        fields=fields,
        title=title,
        normalized_title=txt.normalize_title(title),
        authors=authors,
        year=year,
        doi=normalize_doi(fields.get("doi")),
        arxiv_id=extract_arxiv_eprint(record if nested is not None else {"fields": fields}),
        key=str(record.get("key") or "").strip(),
    )


def _evidence(
    left: _RecordView,
    right: _RecordView,
    verdict: bool,
    reason: IdentityReason,
    *,
    title_score: float | None = None,
    composite: float | None = None,
    candidate_doi: str | None = None,
) -> IdentityEvidence:
    overlap = txt.author_overlap_ratio(left.authors, right.authors) if left.authors and right.authors else None
    gap = abs(left.year - right.year) if left.year is not None and right.year is not None else None
    return IdentityEvidence(verdict, reason, title_score, overlap, gap, composite, candidate_doi)


def _title_score(left: _RecordView, right: _RecordView) -> float:
    return txt.title_similarity(left.normalized_title, right.normalized_title)


def _identifier_title_rejection(
    left: _RecordView,
    right: _RecordView,
    score: float,
) -> IdentityReason | None:
    if not left.normalized_title or not right.normalized_title:
        return IdentityReason.MISSING_TITLE
    if score < cfg.SIM_IDENTIFIER_TITLE_MIN:
        return IdentityReason.IDENTIFIER_TITLE_CONFLICT
    return None


def _import_identity(
    left: _RecordView,
    right: _RecordView,
    target_author: str | None,
) -> IdentityEvidence:
    score = _title_score(left, right)
    gap = abs(left.year - right.year) if left.year is not None and right.year is not None else None
    if target_author is None and left.normalized_title and left.normalized_title == right.normalized_title:
        if left.authors and right.authors and not txt.authors_overlap(left.authors, right.authors):
            return _evidence(left, right, False, IdentityReason.AUTHOR_CONFLICT, title_score=score)
        if gap is not None and gap > 3:
            return _evidence(left, right, False, IdentityReason.YEAR_CONFLICT, title_score=score)
        return _evidence(left, right, True, IdentityReason.IMPORT_EXACT_TITLE, title_score=score)
    if score < cfg.SIM_TITLE_SIM_MIN:
        return _evidence(left, right, False, IdentityReason.BELOW_MIN_SIM, title_score=score)
    author_matches = (
        txt.authors_overlap(target_author, right.authors)
        if target_author
        else txt.authors_overlap(left.authors, right.authors)
    )
    weighted = cfg.SIM_TITLE_WEIGHT * score + (cfg.SIM_AUTHOR_BONUS if author_matches else 0.0)
    if gap is not None and gap <= cfg.SIM_YEAR_MATCH_WINDOW:
        weighted += cfg.SIM_YEAR_BONUS
    return _evidence(
        left,
        right,
        weighted >= cfg.SIM_MERGE_DUPLICATE_THRESHOLD,
        IdentityReason.IMPORT_WEIGHTED,
        title_score=score,
        composite=weighted,
    )


def _enrichment_identity(left: _RecordView, right: _RecordView) -> IdentityEvidence:
    score = _title_score(left, right)
    if left.doi and right.doi:
        if left.doi == right.doi:
            reason = _identifier_title_rejection(left, right, score) or IdentityReason.DOI_EXACT
            return _evidence(left, right, reason is IdentityReason.DOI_EXACT, reason, title_score=score)
        left_preprint = any(left.doi.startswith(prefix) for prefix in cfg.PREPRINT_DOI_PREFIXES)
        right_preprint = any(right.doi.startswith(prefix) for prefix in cfg.PREPRINT_DOI_PREFIXES)
        if left_preprint == right_preprint:
            return _evidence(left, right, False, IdentityReason.DOI_CONFLICT, title_score=score)
    if left.arxiv_id and right.arxiv_id:
        if left.arxiv_id != right.arxiv_id:
            return _evidence(left, right, False, IdentityReason.ARXIV_CONFLICT, title_score=score)
        reason = _identifier_title_rejection(left, right, score) or IdentityReason.ARXIV_EXACT
        return _evidence(left, right, reason is IdentityReason.ARXIV_EXACT, reason, title_score=score)
    if not left.normalized_title or not right.normalized_title:
        return _evidence(left, right, False, IdentityReason.MISSING_TITLE, title_score=score)
    if external_ids_match(left.fields, right.fields) and score >= cfg.SIM_DEDUP_MULTI_SIGNAL_MIN:
        return _evidence(left, right, True, IdentityReason.EXTERNAL_ID, title_score=score)
    gap = abs(left.year - right.year) if left.year is not None and right.year is not None else None
    if score >= cfg.SIM_FILE_DUPLICATE_THRESHOLD:
        if gap is not None and gap > 3:
            return _evidence(left, right, False, IdentityReason.YEAR_CONFLICT, title_score=score)
        author_match = txt.authors_overlap(left.authors, right.authors)
        reason = IdentityReason.HIGH_TITLE if author_match else IdentityReason.AUTHOR_CONFLICT
        return _evidence(left, right, author_match, reason, title_score=score)
    if txt.title_is_truncated_match(left.title, right.title):
        if gap is not None and gap > 3:
            return _evidence(left, right, False, IdentityReason.YEAR_CONFLICT, title_score=score)
        if txt.authors_overlap(left.authors, right.authors):
            return _evidence(left, right, True, IdentityReason.TRUNCATED_TITLE, title_score=score)
    if score < cfg.SIM_DEDUP_MULTI_SIGNAL_MIN:
        return _evidence(left, right, False, IdentityReason.BELOW_MIN_SIM, title_score=score)
    left_preprint = txt._is_preprint_fields(left.fields)
    right_preprint = txt._is_preprint_fields(right.fields)
    preprint_pair = left_preprint != right_preprint
    overlap_ratio = txt.author_overlap_ratio(left.authors, right.authors)
    strong_authors = (
        overlap_ratio >= 0.9
        and score >= 0.6
        and len(txt.parse_authors_any(left.authors)) >= 2
        and len(txt.parse_authors_any(right.authors)) >= 2
    )
    if not preprint_pair and not external_ids_match(left.fields, right.fields) and not strong_authors:
        return _evidence(left, right, False, IdentityReason.GATE_CLOSED, title_score=score)
    composite = txt.compute_dedup_score(left.fields, right.fields, count_preprint_xor=not preprint_pair)
    return _evidence(
        left,
        right,
        composite >= cfg.SIM_DEDUP_COMPOSITE_THRESHOLD,
        IdentityReason.COMPOSITE,
        title_score=score,
        composite=composite,
    )


def _candidate_identity(left: _RecordView, right: _RecordView, candidate_doi: str | None) -> IdentityEvidence:
    candidate = normalize_doi(candidate_doi)
    score = _title_score(left, right)
    if not left.doi or not candidate:
        return _evidence(left, right, False, IdentityReason.NO_MATCH, title_score=score)
    reason = IdentityReason.DOI_EXACT if left.doi == candidate else IdentityReason.DOI_VERSION
    if reason is IdentityReason.DOI_VERSION and not doi_bases_match(left.doi, candidate):
        return _evidence(left, right, False, IdentityReason.NO_MATCH, title_score=score)
    rejection = _identifier_title_rejection(left, right, score)
    if rejection is not None:
        return _evidence(
            left,
            right,
            False,
            rejection,
            title_score=score,
            candidate_doi=candidate,
        )
    return _evidence(left, right, True, reason, title_score=score, candidate_doi=candidate)


def _disk_identity(left: _RecordView, right: _RecordView) -> IdentityEvidence:
    score = _title_score(left, right)
    if left.doi and right.doi:
        if left.doi == right.doi or doi_bases_match(left.doi, right.doi):
            reason = IdentityReason.DOI_EXACT if left.doi == right.doi else IdentityReason.DOI_VERSION
            rejection = _identifier_title_rejection(left, right, score)
            if rejection is not None:
                return _evidence(left, right, False, rejection, title_score=score)
            return _evidence(left, right, True, reason, title_score=score)
        left_preprint = is_secondary_doi(left.doi)
        right_preprint = is_secondary_doi(right.doi)
        if left_preprint != right_preprint:
            if left.arxiv_id and right.arxiv_id and left.arxiv_id != right.arxiv_id:
                return _evidence(left, right, False, IdentityReason.ARXIV_CONFLICT, title_score=score)
            composite = txt.compute_dedup_score(left.fields, right.fields, count_preprint_xor=False)
            verdict = score >= cfg.SIM_PREPRINT_TITLE_THRESHOLD and composite >= cfg.SIM_DEDUP_COMPOSITE_THRESHOLD
            return _evidence(left, right, verdict, IdentityReason.PREPRINT_PAIR, title_score=score, composite=composite)
        return _evidence(left, right, False, IdentityReason.DOI_CONFLICT, title_score=score)
    if external_ids_match(left.fields, right.fields) and score >= cfg.SIM_PREPRINT_TITLE_THRESHOLD:
        return _evidence(left, right, True, IdentityReason.EXTERNAL_ID, title_score=score)
    if left.key and left.key == right.key:
        prefix = (left.normalized_title.startswith(right.normalized_title) and len(right.normalized_title) > 20) or (
            right.normalized_title.startswith(left.normalized_title) and len(left.normalized_title) > 20
        )
        if score >= cfg.SIM_FILE_DUPLICATE_THRESHOLD or prefix:
            return _evidence(left, right, True, IdentityReason.KEY_TITLE, title_score=score)
        if txt.author_overlap_ratio(left.authors, right.authors) >= 0.8 and score >= cfg.SIM_PREPRINT_TITLE_THRESHOLD:
            return _evidence(left, right, True, IdentityReason.KEY_AUTHOR, title_score=score)
        return _evidence(left, right, False, IdentityReason.NO_MATCH, title_score=score)
    gap = abs(left.year - right.year) if left.year is not None and right.year is not None else None
    require_doiless_guards = not left.doi and not right.doi
    if score >= cfg.SIM_FILE_DUPLICATE_THRESHOLD:
        if require_doiless_guards and gap is not None and gap > 3:
            return _evidence(left, right, False, IdentityReason.YEAR_CONFLICT, title_score=score)
        if require_doiless_guards and not txt.authors_overlap(left.authors, right.authors):
            return _evidence(left, right, False, IdentityReason.AUTHOR_CONFLICT, title_score=score)
        return _evidence(left, right, True, IdentityReason.HIGH_TITLE, title_score=score)
    if txt.title_is_truncated_match(left.title, right.title):
        if require_doiless_guards and gap is not None and gap > 3:
            return _evidence(left, right, False, IdentityReason.YEAR_CONFLICT, title_score=score)
        if txt.authors_overlap(left.authors, right.authors):
            return _evidence(left, right, True, IdentityReason.TRUNCATED_TITLE, title_score=score)
    if (
        score >= 0.6
        and len(txt.parse_authors_any(left.authors)) >= 2
        and len(txt.parse_authors_any(right.authors)) >= 2
    ):
        overlap = txt.author_overlap_ratio(left.authors, right.authors)
        composite = txt.compute_dedup_score(left.fields, right.fields)
        if overlap >= 0.9 and composite >= cfg.SIM_DEDUP_COMPOSITE_THRESHOLD:
            return _evidence(left, right, True, IdentityReason.COMPOSITE, title_score=score, composite=composite)
    if score >= cfg.SIM_PREPRINT_TITLE_THRESHOLD and txt.authors_overlap(left.authors, right.authors):
        left_journal = str(left.fields.get("journal") or "").lower()
        right_journal = str(right.fields.get("journal") or "").lower()
        left_preprint = bool(left.doi and is_secondary_doi(left.doi)) or any(
            p in left_journal for p in cfg.PREPRINT_SERVERS
        )
        right_preprint = bool(right.doi and is_secondary_doi(right.doi)) or any(
            p in right_journal for p in cfg.PREPRINT_SERVERS
        )
        published_evidence = (left_preprint and bool(right.doi or right_journal)) or (
            right_preprint and bool(left.doi or left_journal)
        )
        if left_preprint != right_preprint and published_evidence:
            return _evidence(left, right, True, IdentityReason.PREPRINT_RELAXED, title_score=score)
    return _evidence(left, right, False, IdentityReason.NO_MATCH, title_score=score)


def evaluate_identity(
    left: dict[str, Any],
    right: dict[str, Any],
    *,
    context: IdentityContext,
    candidate_doi: str | None = None,
    target_author: str | None = None,
) -> IdentityEvidence:
    """Evaluate whether two records identify the same publication in one context."""
    left_view = _view(left)
    right_view = _view(right)
    if context is IdentityContext.IMPORT_LIST:
        return _import_identity(left_view, right_view, target_author)
    if context is IdentityContext.ENRICHMENT:
        return _enrichment_identity(left_view, right_view)
    if context is IdentityContext.CANDIDATE_DOI_NET:
        return _candidate_identity(left_view, right_view, candidate_doi)
    if context is IdentityContext.DISK_SURVIVOR:
        return _disk_identity(left_view, right_view)
    raise ValueError(f"unsupported identity context: {context!r}")
