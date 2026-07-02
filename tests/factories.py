"""Compact, rule-driven builders for CiteForge test entries and fixtures.

Every test that needs a BibTeX entry, an enricher pair, or an on-disk .bib
goes through these helpers so the suite has one canonical entry shape and the
inline-literal sprawl stays out of the individual test files. The named
fixtures encode the recurring adversarial shapes (preprint/published twins,
repository-only records, generic proceedings, venue aliases, malformed
metadata) as small readable functions rather than one-off dicts.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from citeforge.bibtex_utils import bibtex_from_dict

Entry = dict[str, Any]
Enricher = tuple[str, Entry]


def entry(*, type: str = "article", key: str = "K", **fields: str) -> Entry:
    """Build a canonical entry dict ``{"type", "key", "fields"}``.

    Sensible title/author/year defaults are supplied so a caller only states
    the fields relevant to the behaviour under test. Pass ``title=``/``author=``
    to override. The ``type`` keyword names the BibTeX entry type.
    """
    base: dict[str, str] = {
        "title": "A Study of Neural Networks",
        "author": "Doe, Jane",
        "year": "2021",
    }
    base.update(fields)
    return {"type": type, "key": key, "fields": base}


def article(**fields: str) -> Entry:
    """Build an ``@article`` entry (see :func:`entry`)."""
    return entry(type="article", **fields)


def inproceedings(**fields: str) -> Entry:
    """Build an ``@inproceedings`` entry (see :func:`entry`)."""
    return entry(type="inproceedings", **fields)


def misc(**fields: str) -> Entry:
    """Build an ``@misc`` entry (see :func:`entry`)."""
    return entry(type="misc", **fields)


def enricher(source: str, **fields: str) -> Enricher:
    """Build the ``(source_name, entry)`` pair that ``merge_with_policy`` consumes.

    ``source`` must be a real name from ``config.TRUST_ORDER`` (e.g. ``crossref``,
    ``csl``, ``datacite``, ``s2``, ``scholar_min``) so trust ranking is exercised
    against the production order, not an invented label.
    """
    return (source, entry(**fields))


def write_bib(directory: Path, e: Entry, filename: str) -> Path:
    """Serialize *e* with the production serializer and write it under *directory*.

    Returns the written path. Used by save-time and finalize-run tests that need
    a real .bib file on disk in the exact bytes the pipeline would produce.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / filename
    path.write_text(bibtex_from_dict(e), encoding="utf-8")
    return path


# --- Named adversarial fixtures (one small function each) -------------------


def arxiv_published_twin() -> tuple[Entry, Enricher]:
    """A preprint/published twin: same title and authors, arXiv side vs a
    published (CVPR/ACM DOI) side. The published record must win on DOI while
    the work stays represented once.
    """
    title = "Deep Residual Learning for Image Recognition"
    authors = "He, Kaiming and Zhang, Xiangyu and Ren, Shaoqing and Sun, Jian"
    preprint = article(title=title, author=authors, journal="arXiv", doi="10.48550/arXiv.1512.03385")
    published = enricher("crossref", title=title, author=authors, journal="CVPR", doi="10.1109/cvpr.2016.90")
    return preprint, published


def repo_only_zenodo() -> Entry:
    """A repository-only record (Zenodo DOI, no published counterpart). Must be
    retained and represented, never dropped for lacking a journal.
    """
    return article(
        title="A Reproducible Dataset for Vessel Tracking",
        author="Spadon, Gabriel",
        journal="Zenodo",
        doi="10.5281/zenodo.9000001",
    )


def generic_proceedings_lncs() -> Entry:
    """An entry whose booktitle is a generic series name (LNCS). The specific
    conference name must be preferred over the generic series when available.
    """
    return inproceedings(
        title="Graph Kernels for Structured Prediction",
        booktitle="Lecture Notes in Computer Science",
    )


def venue_alias_neurips() -> Entry:
    """An entry carrying a mistyped venue alias that must canonicalize to the
    correct spelling (``NeuriPS`` -> ``NeurIPS``).
    """
    return inproceedings(
        title="Attention Is All You Need",
        booktitle="NeuriPS 2017",
    )


def conflicting_trusted_sources() -> tuple[Entry, list[Enricher]]:
    """A low-rank primary carrying the full author list versus a higher-rank
    enricher carrying a truncated author list. The fuller list must survive
    unless the enricher is >= TRUST_DIFF_OVERRIDE_THRESHOLD ranks more trusted.
    """
    title = "Scaling Laws for Neural Language Models"
    primary = article(
        title=title,
        author="Kaplan, Jared and McCandlish, Sam and Henighan, Tom and Brown, Tom B.",
    )
    truncated = enricher("s2", title=title, author="Kaplan, Jared")
    return primary, [truncated]


def untrusted_doi_candidate() -> tuple[Entry, Enricher]:
    """A published-looking DOI arriving from a non-registry source with no
    registry echo. The DOI-trust gate must reject it rather than let it pollute
    the merged entry.
    """
    title = "On the Measure of Intelligence"
    primary = article(title=title, author="Chollet, Francois")
    polluter = enricher("scholar_page", title=title, author="Chollet, Francois", doi="10.1000/not-echoed-0001")
    return primary, polluter


def allcaps_title() -> Entry:
    """An all-caps Scholar title that must be recased to sentence/title case."""
    return article(title="DEEP LEARNING FOR SATELLITE IMAGE CLASSIFICATION")


def nonascii_author() -> Entry:
    """A non-ASCII author name that must round-trip byte-exactly through the
    serializer without mojibake.
    """
    return article(
        title="Variational Methods in Fluid Dynamics",
        author="Müller, André and Sørensen, Bjørn",
    )


def pages_edge() -> tuple[Entry, Entry]:
    """A pair distinguishing a real page range from a publisher article-id.
    The valid range is admitted; the article-id-style value is rejected.
    """
    valid = article(title="Ocean Currents and Vessel Routing", pages="331-345")
    invalid = article(title="Ocean Currents and Vessel Routing", pages="e0250001")
    return valid, invalid


def duplicate_titles_two_authors() -> tuple[Entry, Entry]:
    """The same title appearing under two different authors with distinct DOIs.
    These are distinct works and must not be collapsed into one file.
    """
    a = article(title="Machine Learning in Healthcare", author="Smith, John", doi="10.1145/3580305")
    b = article(title="Machine Learning in Healthcare", author="Doe, Jane", doi="10.1038/s41586-024-00001")
    return a, b
