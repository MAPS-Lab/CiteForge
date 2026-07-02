"""Module-level parametrize tables for the adversarial corpus layer.

Each table is external truth about a contract (what the input should produce),
not a mirror of a production map. Tests import a table and drive the real
function over it, so one table replaces the per-class case copies that were
scattered across the old monolith. Expected values were captured from the live
functions and are asserted, never re-derived.
"""

from __future__ import annotations

# (doi, is_secondary) -- preprint, grey-literature, and data DOIs are secondary;
# published journal DOIs are primary even under a registrant that also mints
# preprints (10.5194/egusphere is secondary, 10.5194/acp is not).
SECONDARY_DOI_CASES: list[tuple[str, bool]] = [
    ("10.48550/arxiv.2401.00001", True),
    ("10.1101/2021.01.01.400001", True),
    ("10.21203/rs.3.rs-100001", True),
    ("10.31234/osf.io/abcde", True),
    ("10.26434/chemrxiv-2024-aaaaa", True),
    ("10.2139/ssrn.4000001", True),
    ("10.5194/egusphere-2024-1000", True),
    ("10.5281/zenodo.9000001", True),
    ("10.1145/3580305", False),
    ("10.1038/s41586-024-00001", False),
    ("10.1109/tpami.2024.0000001", False),
    ("10.1016/j.patcog.2024.100001", False),
    ("10.5194/acp-24-1-2024", False),
]

# Journal strings that name a preprint server (an @article carrying one of these
# as its journal must not be treated as a published venue).
PREPRINT_SERVER_JOURNALS: list[str] = [
    "arXiv",
    "arXiv e-prints",
    "bioRxiv",
    "medRxiv",
    "Research Square",
    "SSRN",
    "ChemRxiv",
]

# (title_in, title_out) for the fused-compound repair engine.
FUSED_COMPOUND_CASES: list[tuple[str, str]] = [
    ("A Stateoftheart Endtoend Aidriven System", "A State-of-the-Art End-to-End AI-Driven System"),
    ("Knowledgedriven Reasoning", "Knowledge-Driven Reasoning"),
    # An already-correct title is a fixpoint (no spurious rewrite).
    ("A State-of-the-Art System", "A State-of-the-Art System"),
]

# (booktitle_in, booktitle_out) for the venue-alias / booktitle fixup engine.
BOOKTITLE_FIXUP_CASES: list[tuple[str, str]] = [
    ("NeuriPS 2017", "NeurIPS 2017"),
    # Correct spellings and unrelated strings pass through unchanged.
    ("Proceedings of NAACL", "Proceedings of NAACL"),
    ("Lecture Notes in Computer Science (LNCS)", "Lecture Notes in Computer Science (LNCS)"),
]

# (retry_after_header, expected_seconds) for _parse_retry_after. Numeric values
# parse directly; a non-numeric / unparseable value yields 0.0. HTTP-date cases
# are asserted separately in the test because they depend on the current clock.
RETRY_AFTER_CASES: list[tuple[str | None, float]] = [
    ("120", 120.0),
    ("0", 0.0),
    ("soon", 0.0),
    ("", 0.0),
    (None, 0.0),
]
