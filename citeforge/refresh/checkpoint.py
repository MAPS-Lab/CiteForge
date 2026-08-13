"""Authenticated checkpoints for a refresh generation.

A six-hour Actions segment can end while a generation is still running, so the
durable state has to survive the runner. This module seals that state into one
authenticated blob and restores it on the next segment.

Two properties are load-bearing.

Authentication, not just encryption. The blob lands on a branch of a public
repository. AES-GCM binds the ciphertext to a cleartext manifest through its
associated data, so an edited manifest, a rotated key, and a truncated payload
all fail closed with :class:`CheckpointError` rather than restoring partial
state. The retired cache path used unauthenticated AES-CBC, where a flipped
byte decrypted to garbage instead of raising.

Never a blank restart. Two sequences are retained. If the newest fails to
verify, the previous one is used; if both fail, this raises. A checkpoint that
cannot be read is an error to escalate, never a reason to start the corpus from
zero, which is what the fifty-pass loop did every month.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import tarfile
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

CHECKPOINT_SCHEMA_VERSION = "1"

# AES-GCM with a 96-bit nonce, the size NIST recommends and the only one the
# AESGCM recipe treats as the fast path.
_NONCE_BYTES = 12
_VALID_KEY_BYTES = frozenset({16, 24, 32})

# Current plus previous, so a corrupt newest sequence has somewhere to fall
# back to. A third adds storage without adding recoverability: two independent
# failures already mean the branch is not trustworthy.
_RETAINED_SEQUENCES = 2

_MANIFEST_SUFFIX = ".manifest.json"
_CIPHERTEXT_SUFFIX = ".bin"


class CheckpointError(RuntimeError):
    """A checkpoint could not be written, verified, or restored."""


@dataclass(frozen=True)
class CheckpointManifest:
    """Non-secret description of one sealed checkpoint.

    Every field is safe to commit in cleartext. It carries digests and
    identifiers, never a credential, a provider response, or a key.
    """

    schema_version: str
    generation_id: str
    input_digest: str
    policy_digest: str
    sequence: int
    created_at: datetime
    ciphertext_digest: str
    key_id: str

    def canonical_content(self) -> dict[str, Any]:
        """Deterministic mapping used both as the on-disk manifest and as AAD."""
        return {
            "ciphertext_digest": self.ciphertext_digest,
            "created_at": self.created_at.astimezone(timezone.utc).isoformat(),
            "generation_id": self.generation_id,
            "input_digest": self.input_digest,
            "key_id": self.key_id,
            "policy_digest": self.policy_digest,
            "schema_version": self.schema_version,
            "sequence": self.sequence,
        }

    def to_bytes(self) -> bytes:
        return json.dumps(self.canonical_content(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()

    def binding_bytes(self) -> bytes:
        """The AAD: every manifest field except the digest of the ciphertext.

        ciphertext_digest is excluded because it is derived from the sealed
        bytes, and the sealed bytes depend on the AAD. Including it would make
        the manifest unable to authenticate itself. It is still covered, by the
        explicit digest comparison in the restore path, which also gives a
        truncated payload its own error rather than a generic auth failure.
        """
        binding = {k: v for k, v in self.canonical_content().items() if k != "ciphertext_digest"}
        return json.dumps(binding, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> CheckpointManifest:
        try:
            content = json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise CheckpointError("checkpoint manifest is not valid JSON") from exc
        if not isinstance(content, dict):
            raise CheckpointError("checkpoint manifest is not a JSON object")
        try:
            return cls(
                schema_version=str(content["schema_version"]),
                generation_id=str(content["generation_id"]),
                input_digest=str(content["input_digest"]),
                policy_digest=str(content["policy_digest"]),
                sequence=int(content["sequence"]),
                created_at=datetime.fromisoformat(str(content["created_at"])),
                ciphertext_digest=str(content["ciphertext_digest"]),
                key_id=str(content["key_id"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"checkpoint manifest is missing or malformed: {exc}") from exc


def _seal_directory(source: Path) -> bytes:
    """Pack *source* into a deterministic gzip tar.

    Every timestamp, owner, and mode is normalized and entries are sorted, so
    identical state produces identical plaintext. That is what makes the
    round-trip assertions in the tests meaningful; the ciphertext still differs
    per save because the nonce is fresh.
    """
    buffer = io.BytesIO()
    # gzip is applied explicitly with mtime=0. tarfile's own "w:gz" stamps the
    # gzip header with the wall clock, which would make the plaintext differ
    # between two saves of identical state.
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0, compresslevel=6) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
            for path in sorted(source.rglob("*")):
                if not path.is_file():
                    continue
                info = archive.gettarinfo(str(path), arcname=str(path.relative_to(source)))
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mode = 0o600
                with path.open("rb") as handle:
                    archive.addfile(info, handle)
    return buffer.getvalue()


def _unseal_directory(payload: bytes, destination: Path) -> None:
    """Extract a sealed archive into *destination*.

    ``filter="data"`` refuses absolute paths, parent traversal, symlinks, and
    device nodes. The payload is authenticated by this point, but a checkpoint
    is still restored into a live checkout, so the extraction stays fenced.
    """
    destination.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
            archive.extractall(path=destination, filter="data")
    except (tarfile.TarError, OSError, ValueError) as exc:
        raise CheckpointError(f"checkpoint payload could not be extracted: {exc}") from exc


class CheckpointStore:
    """Reads and writes authenticated checkpoints under one directory."""

    def __init__(self, root: Path, key: bytes, key_id: str) -> None:
        if len(key) not in _VALID_KEY_BYTES:
            raise CheckpointError(f"checkpoint key must be 16, 24, or 32 bytes, got {len(key)}")
        if not key_id or not key_id.strip():
            raise CheckpointError("checkpoint key identifier must not be empty")
        self._root = root
        self._key = key
        self._key_id = key_id.strip()
        self._aesgcm = AESGCM(key)

    @property
    def root(self) -> Path:
        return self._root

    def save(
        self,
        *,
        generation_id: str,
        input_digest: str,
        policy_digest: str,
        sequence: int,
        created_at: datetime,
        state_dir: Path,
    ) -> CheckpointManifest:
        """Seal *state_dir* as checkpoint *sequence* and prune old sequences."""
        if sequence < 1:
            raise CheckpointError("checkpoint sequence must be positive")
        if not state_dir.is_dir():
            raise CheckpointError(f"checkpoint state directory does not exist: {state_dir}")

        plaintext = _seal_directory(state_dir)
        nonce = os.urandom(_NONCE_BYTES)

        # Built with an empty digest purely so binding_bytes() can be taken;
        # binding_bytes() excludes that field, so the value here is never read.
        binding = CheckpointManifest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            generation_id=generation_id,
            input_digest=input_digest,
            policy_digest=policy_digest,
            sequence=sequence,
            created_at=created_at,
            ciphertext_digest="",
            key_id=self._key_id,
        )
        sealed = nonce + self._aesgcm.encrypt(nonce, plaintext, binding.binding_bytes())
        manifest = replace(binding, ciphertext_digest=hashlib.sha256(sealed).hexdigest())

        self._root.mkdir(parents=True, exist_ok=True)
        self._write_atomic(self._ciphertext_path(sequence), sealed)
        self._write_atomic(self._manifest_path(sequence), manifest.to_bytes())
        self._prune(keep_through=sequence)
        return manifest

    def load_latest_valid(
        self,
        *,
        generation_id: str,
        input_digest: str,
        policy_digest: str,
        destination: Path,
    ) -> CheckpointManifest:
        """Restore the newest verifiable checkpoint into *destination*.

        Falls back to the previous sequence when the newest fails. Raises when
        no retained sequence verifies, because silently restarting a generation
        from zero is the failure this whole mechanism exists to prevent.
        """
        sequences = self.available_sequences()
        if not sequences:
            raise CheckpointError(f"no checkpoint found under {self._root}")

        failures: list[str] = []
        for sequence in sequences:
            try:
                manifest = self._restore_one(
                    sequence=sequence,
                    generation_id=generation_id,
                    input_digest=input_digest,
                    policy_digest=policy_digest,
                    destination=destination,
                )
            except CheckpointError as exc:
                failures.append(f"sequence {sequence}: {exc}")
                continue
            return manifest

        raise CheckpointError(
            "no retained checkpoint verified, refusing to restart the generation from zero: " + "; ".join(failures)
        )

    def available_sequences(self) -> list[int]:
        """Retained sequences, newest first."""
        if not self._root.is_dir():
            return []
        found: list[int] = []
        for path in self._root.glob(f"*{_CIPHERTEXT_SUFFIX}"):
            try:
                found.append(int(path.name[: -len(_CIPHERTEXT_SUFFIX)]))
            except ValueError:
                continue
        return sorted(found, reverse=True)

    def _restore_one(
        self,
        *,
        sequence: int,
        generation_id: str,
        input_digest: str,
        policy_digest: str,
        destination: Path,
    ) -> CheckpointManifest:
        manifest_path = self._manifest_path(sequence)
        ciphertext_path = self._ciphertext_path(sequence)
        if not manifest_path.is_file() or not ciphertext_path.is_file():
            raise CheckpointError("manifest or ciphertext missing")

        manifest = CheckpointManifest.from_bytes(manifest_path.read_bytes())
        sealed = ciphertext_path.read_bytes()

        if manifest.schema_version != CHECKPOINT_SCHEMA_VERSION:
            raise CheckpointError(f"schema version {manifest.schema_version} is not {CHECKPOINT_SCHEMA_VERSION}")
        if manifest.sequence != sequence:
            raise CheckpointError(f"manifest sequence {manifest.sequence} does not match file {sequence}")
        if manifest.key_id != self._key_id:
            raise CheckpointError(f"manifest key identifier {manifest.key_id} is not {self._key_id}")
        # Checked before decrypting so a truncated payload reports as itself
        # rather than as a generic authentication failure.
        if hashlib.sha256(sealed).hexdigest() != manifest.ciphertext_digest:
            raise CheckpointError("ciphertext digest does not match the manifest")
        if manifest.generation_id != generation_id:
            raise CheckpointError(f"checkpoint belongs to generation {manifest.generation_id}")
        if manifest.input_digest != input_digest or manifest.policy_digest != policy_digest:
            raise CheckpointError("checkpoint was taken under a different input census or policy")
        if len(sealed) <= _NONCE_BYTES:
            raise CheckpointError("ciphertext is too short to carry a nonce")

        nonce, body = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
        try:
            plaintext = self._aesgcm.decrypt(nonce, body, manifest.binding_bytes())
        except InvalidTag as exc:
            raise CheckpointError("checkpoint failed authentication (tampered, or wrong key)") from exc

        _unseal_directory(plaintext, destination)
        return manifest

    def _manifest_path(self, sequence: int) -> Path:
        return self._root / f"{sequence:012d}{_MANIFEST_SUFFIX}"

    def _ciphertext_path(self, sequence: int) -> Path:
        return self._root / f"{sequence:012d}{_CIPHERTEXT_SUFFIX}"

    def _prune(self, *, keep_through: int) -> None:
        """Drop every sequence older than the retained window."""
        retained = set(self.available_sequences()[:_RETAINED_SEQUENCES]) | {keep_through}
        for sequence in self.available_sequences():
            if sequence in retained:
                continue
            self._manifest_path(sequence).unlink(missing_ok=True)
            self._ciphertext_path(sequence).unlink(missing_ok=True)

    @staticmethod
    def _write_atomic(path: Path, payload: bytes) -> None:
        """Write through a sibling temp file so a killed runner cannot half-write."""
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_bytes(payload)
            os.replace(tmp, path)
        except OSError as exc:
            tmp.unlink(missing_ok=True)
            raise CheckpointError(f"could not write {path.name}: {exc}") from exc
