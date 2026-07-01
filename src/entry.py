from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict


@dataclass
class BibEntry:
    """Typed wrapper over the dict shape used throughout the pipeline.

    This is a thin, byte-neutral facade over the existing
    ``parse_bibtex_to_dict`` / ``bibtex_from_dict`` functions in
    :mod:`src.bibtex_utils`. It introduces a domain type without changing any
    parse or serialize behaviour; call sites are migrated onto it in later
    steps.

    Invariants mirror ``parse_bibtex_to_dict`` semantics:

    - ``entry_type`` is stored lowercased (``"@Article"`` -> ``"article"``).
    - ``fields`` keys are stored lowercased; field *values* are kept verbatim.
    - ``key`` is stored case-preserved.

    Normalization happens in ``__post_init__`` so direct construction,
    :meth:`from_dict`, and :meth:`from_bibtex` all yield the same invariants.
    """

    entry_type: str
    key: str
    fields: dict[str, str]

    def __post_init__(self) -> None:
        # Normalize to match parse_bibtex_to_dict semantics. Rebuilding the
        # fields mapping also yields a defensive copy of the caller's dict.
        self.entry_type = (self.entry_type or "misc").lower()
        self.key = self.key or "entry"
        self.fields = {str(k).lower(): v for k, v in (self.fields or {}).items()}

    @classmethod
    def from_bibtex(cls, text: str) -> BibEntry | None:
        """Parse a BibTeX string into a ``BibEntry``.

        Delegates to ``parse_bibtex_to_dict``; returns ``None`` when the input
        has no parseable entry header. The resulting entry is exactly
        equivalent to the parsed dict (see :meth:`to_dict`).
        """
        parsed = parse_bibtex_to_dict(text)
        if parsed is None:
            return None
        return cls(entry_type=parsed["type"], key=parsed["key"], fields=parsed["fields"])

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> BibEntry:
        """Wrap an existing ``{"type", "key", "fields"}`` dict.

        Missing pieces default to ``type="misc"``, ``key="entry"``,
        ``fields={}``. The ``fields`` mapping is defensively copied so later
        mutation of the source dict does not leak into the entry.
        """
        raw_fields = entry.get("fields") or {}
        fields = {str(k): str(v) for k, v in dict(raw_fields).items()}
        return cls(
            entry_type=entry.get("type") or "misc",
            key=entry.get("key") or "entry",
            fields=fields,
        )

    def to_dict(self) -> dict[str, str | dict[str, str]]:
        """Return the ``{"type", "key", "fields"}`` dict the pipeline expects.

        ``fields`` is a fresh copy, so mutating the returned mapping does not
        affect this entry.
        """
        return {"type": self.entry_type, "key": self.key, "fields": dict(self.fields)}

    def to_bibtex(self) -> str:
        """Serialize back to BibTeX text via ``bibtex_from_dict``.

        Delegates for now; owning the serialization contract on this method is
        a later migration step.
        """
        return bibtex_from_dict(self.to_dict())
