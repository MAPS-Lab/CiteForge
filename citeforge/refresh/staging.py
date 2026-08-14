"""Staged materialization of one refresh generation's candidate corpus.

Committed ``output/`` is what every reader of the repository sees, so nothing
on the refresh path may touch it while a generation is still incomplete. Every
byte produced here lands under one stage root, and promotion is a separate
decision the caller makes after the ledger proves completeness.

Three properties are load-bearing.

The stage is built from committed Git objects, never from the working tree. A
dirty checkout, a half-finished legacy run, or a concurrent editor cannot leak
into a candidate, because :func:`prepare_stage` reads blobs at an explicit
commit rather than walking the filesystem.

The candidate is a function of the stage and the intents alone.
:class:`CorpusManifest` hashes every staged file under a sorted walk, so two
runs over equal inputs produce equal digests and one changed byte changes the
digest. That is what makes "two byte-identical materializations" checkable
instead of hopeful.

Nothing here records anything. :func:`materialize_candidate` returns a
:class:`~citeforge.refresh.ledger.MaterializationEvidence` and the engine
records it, because the ledger writer opens ``BEGIN IMMEDIATE`` and holding
that transaction open across a tree copy is the deadlock shape.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from ..fsscan import GitTreeEntry, read_committed_blobs, read_committed_tree
from .authority import IntentKind, MaterializationIntent
from .ledger import EvidenceState, MaterializationEvidence

CORPUS_MANIFEST_SCHEMA_VERSION = "1"

# The corpus a refresh rewrites. `data/a2i2.csv` is derived rather than
# reduced, so it is staged only when a caller asks for it.
DEFAULT_STAGE_ROOTS: tuple[str, ...] = ("output",)

# Committed corpus blobs are text. An executable bit or a symlink in `output/`
# is either a mistake or an attack, and neither belongs in a candidate.
_ALLOWED_BLOB_MODE = "100644"


class StagingError(RuntimeError):
    """A stage could not be prepared, validated, or materialized."""


@dataclass(frozen=True)
class CorpusManifest:
    """Deterministic path-to-digest census of one staged tree.

    Bound to a generation, so the same bytes staged under two generations
    produce two digests and a candidate cannot be presented as another
    generation's work.
    """

    schema_version: str
    generation_id: str
    entries: tuple[tuple[str, str], ...]

    def canonical_content(self) -> dict[str, Any]:
        return {
            "entries": [{"digest": digest, "path": path} for path, digest in self.entries],
            "generation_id": self.generation_id,
            "schema_version": self.schema_version,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.canonical_content(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    @classmethod
    def from_stage(cls, stage_root: Path, *, generation_id: str) -> CorpusManifest:
        """Hash every regular file under *stage_root* in sorted path order."""
        if not stage_root.is_dir():
            raise StagingError(f"stage root does not exist: {stage_root}")
        entries: list[tuple[str, str]] = []
        for path in sorted(stage_root.rglob("*")):
            relative = path.relative_to(stage_root).as_posix()
            if path.is_symlink():
                raise StagingError(f"staged entry is a symbolic link: {relative}")
            if path.is_dir():
                continue
            if not path.is_file():
                raise StagingError(f"staged entry is not a regular file: {relative}")
            entries.append((relative, _digest_file(path)))
        return cls(CORPUS_MANIFEST_SCHEMA_VERSION, generation_id, tuple(entries))


def prepare_stage(
    repo_root: Path,
    stage_root: Path,
    *,
    base_commit: str,
    roots: Sequence[str] = DEFAULT_STAGE_ROOTS,
) -> tuple[str, ...]:
    """Copy the committed bytes of *roots* at *base_commit* into *stage_root*.

    Refuses a stage root that already holds anything. A reused root would mix
    two generations' bytes into one candidate, and the resulting digest would
    describe a tree nobody reduced.
    """
    if stage_root.exists() and any(stage_root.iterdir()):
        raise StagingError(f"stage root is not empty: {stage_root}")

    blobs: list[GitTreeEntry] = []
    for root in roots:
        try:
            entries = read_committed_tree(repo_root, base_commit, root)
        except ValueError as exc:
            raise StagingError(f"committed tree {root} is unusable: {exc}") from exc
        blobs.extend(entry for entry in entries if entry.object_type == "blob")

    paths = [entry.path for entry in blobs]
    if len(set(paths)) != len(paths):
        raise StagingError("committed roots overlap on one path")
    for entry in blobs:
        if entry.mode != _ALLOWED_BLOB_MODE:
            raise StagingError(f"committed corpus blob mode is unsupported: {entry.path}")

    try:
        bodies = read_committed_blobs(repo_root, tuple(dict.fromkeys(entry.object_id for entry in blobs)))
    except ValueError as exc:
        raise StagingError(f"committed corpus blobs are unreadable: {exc}") from exc

    stage_root.mkdir(parents=True, exist_ok=True)
    staged: list[str] = []
    for entry in sorted(blobs, key=lambda item: item.path):
        target = _stage_path(stage_root, entry.path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(bodies[entry.object_id])
        staged.append(entry.path)
    return tuple(staged)


def validate_stage(
    stage_root: Path,
    intents: Sequence[MaterializationIntent],
    *,
    generation_id: str,
) -> CorpusManifest:
    """Prove the staged tree is exactly what *intents* describe.

    Every kept and upserted entry has to hash to its intent digest and every
    removed entry has to be gone. Files no intent mentions are left alone,
    because an intent set covers the publications a generation reduced, not the
    whole corpus.
    """
    if not stage_root.is_dir():
        raise StagingError(f"stage root does not exist: {stage_root}")
    _reject_conflicts(intents, generation_id)

    for intent in _ordered(intents):
        path = _stage_path(stage_root, intent.target_path)
        if intent.kind is IntentKind.REMOVE:
            if path.exists():
                raise StagingError(f"removed entry is still staged: {intent.target_path}")
            continue
        # KEEP carries identical before and after digests, so one comparison
        # covers both kinds.
        _require_digest(path, intent.after_digest, intent.target_path)
    return CorpusManifest.from_stage(stage_root, generation_id=generation_id)


def materialize_candidate(
    stage_root: Path,
    intents: Sequence[MaterializationIntent],
    contents: Mapping[str, bytes],
    *,
    generation_id: str,
) -> MaterializationEvidence:
    """Apply *intents* to the stage and return evidence for the caller to record.

    *contents* supplies the bytes for every UPSERT target, keyed by target
    path. Bytes that disagree with the intent's digest are refused rather than
    written, because the intent is what the ledger reduced and the bytes are
    what a reducer happened to hand over.

    Not idempotent in place. A REMOVE whose target is already absent is an
    error, since it means the stage does not match the baseline the intents
    were reduced against. Retry by discarding the stage and calling
    :func:`prepare_stage` again, which is cheap and cannot inherit a partial
    write.
    """
    if not stage_root.is_dir():
        raise StagingError(f"stage root does not exist: {stage_root}")
    _reject_conflicts(intents, generation_id)

    upserts = {intent.target_path for intent in intents if intent.kind is IntentKind.UPSERT}
    unclaimed = sorted(set(contents) - upserts)
    if unclaimed:
        raise StagingError(f"staged content has no intent: {unclaimed[0]}")

    counts = {"kept": 0, "upserted": 0, "removed": 0}
    for intent in _ordered(intents):
        path = _stage_path(stage_root, intent.target_path)
        if intent.kind is IntentKind.KEEP:
            _require_digest(path, intent.before_digest, intent.target_path)
            counts["kept"] += 1
        elif intent.kind is IntentKind.UPSERT:
            body = contents.get(intent.target_path)
            if body is None:
                raise StagingError(f"no staged content for {intent.target_path}")
            if hashlib.sha256(body).hexdigest() != intent.after_digest:
                raise StagingError(f"staged content does not match its intent digest: {intent.target_path}")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
            counts["upserted"] += 1
        else:
            _require_digest(path, intent.before_digest, intent.target_path)
            path.unlink()
            counts["removed"] += 1

    manifest = validate_stage(stage_root, intents, generation_id=generation_id)
    return MaterializationEvidence(
        staged_path=str(stage_root),
        manifest_digest=manifest.digest,
        corpus_counts={"staged_files": len(manifest.entries), **counts},
        validation_state=EvidenceState.VALIDATED,
    )


def _ordered(intents: Sequence[MaterializationIntent]) -> list[MaterializationIntent]:
    """Apply intents in path order so a failure reports the same entry twice."""
    return sorted(intents, key=lambda intent: intent.target_path)


def _reject_conflicts(intents: Sequence[MaterializationIntent], generation_id: str) -> None:
    seen: set[str] = set()
    for intent in intents:
        if intent.generation_id != generation_id:
            raise StagingError(f"intent belongs to another generation: {intent.generation_id}")
        # Case-folded, matching how the corpus scanner and the intent's own
        # REMOVE guard compare paths, so a case-only collision cannot stage two
        # entries onto one file.
        folded = intent.target_path.casefold()
        if folded in seen:
            raise StagingError(f"more than one intent targets {intent.target_path}")
        seen.add(folded)


def _require_digest(path: Path, expected: str | None, relative: str) -> None:
    if not path.is_file():
        raise StagingError(f"staged entry is missing: {relative}")
    if _digest_file(path) != expected:
        raise StagingError(f"staged entry does not match its intent digest: {relative}")


def _digest_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _stage_path(stage_root: Path, relative: str) -> Path:
    """Resolve *relative* under *stage_root*, refusing anything that escapes it.

    The committed-tree reader and the intent constructor both reject traversal
    already. This is the last gate before a write, and it is the one guarantee
    the rest of the refresh depends on, so it is checked here too.
    """
    pure = PurePosixPath(relative)
    if pure.is_absolute() or not pure.parts or any(part in {"", ".", ".."} for part in pure.parts):
        raise StagingError(f"staged path escapes the stage root: {relative}")
    candidate = (stage_root / pure).resolve()
    if not candidate.is_relative_to(stage_root.resolve()):
        raise StagingError(f"staged path escapes the stage root: {relative}")
    return candidate
