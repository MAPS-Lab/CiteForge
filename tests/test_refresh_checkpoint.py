"""Contract tests for authenticated refresh checkpoints."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from citeforge.refresh.checkpoint import (
    CHECKPOINT_SCHEMA_VERSION,
    CheckpointError,
    CheckpointManifest,
    CheckpointStore,
)

_KEY = bytes(range(32))
_OTHER_KEY = bytes(range(1, 33))
_KEY_ID = "citeforge-checkpoint-1"
_GENERATION = "a" * 64
_INPUT_DIGEST = "b" * 64
_POLICY_DIGEST = "c" * 64
_WHEN = datetime(2026, 8, 13, 4, 0, tzinfo=timezone.utc)


def _state(tmp_path: Path, name: str = "state", *, marker: str = "ledger-bytes") -> Path:
    root = tmp_path / name
    (root / "nested").mkdir(parents=True)
    (root / "ledger.db").write_bytes(marker.encode())
    (root / "nested" / "staged.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")
    return root


def _store(tmp_path: Path, key: bytes = _KEY, key_id: str = _KEY_ID) -> CheckpointStore:
    return CheckpointStore(tmp_path / "checkpoints", key, key_id)


def _save(store: CheckpointStore, state: Path, sequence: int) -> CheckpointManifest:
    return store.save(
        generation_id=_GENERATION,
        input_digest=_INPUT_DIGEST,
        policy_digest=_POLICY_DIGEST,
        sequence=sequence,
        created_at=_WHEN + timedelta(minutes=sequence),
        state_dir=state,
    )


def _load(store: CheckpointStore, destination: Path) -> CheckpointManifest:
    return store.load_latest_valid(
        generation_id=_GENERATION,
        input_digest=_INPUT_DIGEST,
        policy_digest=_POLICY_DIGEST,
        destination=destination,
    )


class TestRoundTrip:
    def test_state_survives_a_seal_and_restore(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        manifest = _save(store, _state(tmp_path), 1)
        assert manifest.schema_version == CHECKPOINT_SCHEMA_VERSION
        assert manifest.sequence == 1

        restored = tmp_path / "restored"
        loaded = _load(store, restored)

        assert loaded.ciphertext_digest == manifest.ciphertext_digest
        assert (restored / "ledger.db").read_bytes() == b"ledger-bytes"
        assert json.loads((restored / "nested" / "staged.json").read_text()) == {"marker": "ledger-bytes"}

    def test_the_manifest_on_disk_carries_no_payload(self, tmp_path: Path) -> None:
        """The manifest is committed in cleartext, so it must stay non-secret."""
        store = _store(tmp_path)
        _save(store, _state(tmp_path, marker="provider-response-secret"), 1)
        manifest_text = next(store.root.glob("*.manifest.json")).read_text(encoding="utf-8")

        assert "provider-response-secret" not in manifest_text
        assert set(json.loads(manifest_text)) == {
            "ciphertext_digest",
            "created_at",
            "generation_id",
            "input_digest",
            "kdf",
            "kdf_salt",
            "key_id",
            "policy_digest",
            "schema_version",
            "sequence",
        }

    def test_identical_state_seals_to_identical_plaintext(self, tmp_path: Path) -> None:
        """Two saves of the same state differ only by the nonce, not by mtime."""
        store = _store(tmp_path)
        first = _save(store, _state(tmp_path, "one"), 1)
        second = _save(store, _state(tmp_path, "two"), 2)

        # Different nonce, so different ciphertext, but both restore identically.
        assert first.ciphertext_digest != second.ciphertext_digest
        a, b = tmp_path / "ra", tmp_path / "rb"
        _load(store, b)
        store._restore_one(
            sequence=1,
            generation_id=_GENERATION,
            input_digest=_INPUT_DIGEST,
            policy_digest=_POLICY_DIGEST,
            destination=a,
        )
        assert (a / "ledger.db").read_bytes() == (b / "ledger.db").read_bytes()


class TestRejection:
    def test_a_wrong_key_does_not_restore(self, tmp_path: Path) -> None:
        _save(_store(tmp_path), _state(tmp_path), 1)
        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            _load(_store(tmp_path, _OTHER_KEY), tmp_path / "restored")

    def test_a_rotated_key_identifier_does_not_restore(self, tmp_path: Path) -> None:
        _save(_store(tmp_path), _state(tmp_path), 1)
        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            _load(_store(tmp_path, _KEY, "citeforge-checkpoint-2"), tmp_path / "restored")

    def test_tampered_ciphertext_does_not_restore(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        blob = next(store.root.glob("*.bin"))
        raw = bytearray(blob.read_bytes())
        raw[-1] ^= 0xFF
        blob.write_bytes(bytes(raw))

        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            _load(store, tmp_path / "restored")

    def test_an_edited_manifest_does_not_restore(self, tmp_path: Path) -> None:
        """The manifest is the associated data, so editing it breaks the seal."""
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        path = next(store.root.glob("*.manifest.json"))
        content = json.loads(path.read_text(encoding="utf-8"))
        content["created_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(content), encoding="utf-8")

        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            _load(store, tmp_path / "restored")

    def test_an_edited_ciphertext_digest_does_not_restore(self, tmp_path: Path) -> None:
        """The one manifest field the AAD excludes is still covered.

        binding_bytes() leaves ciphertext_digest out, so editing it cannot break
        authentication. The explicit digest comparison is what catches it, and
        this test exists so that comparison cannot be removed unnoticed.
        """
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        path = next(store.root.glob("*.manifest.json"))
        content = json.loads(path.read_text(encoding="utf-8"))
        content["ciphertext_digest"] = "0" * 64
        path.write_text(json.dumps(content), encoding="utf-8")

        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            _load(store, tmp_path / "restored")

    def test_a_truncated_payload_reports_the_digest_rather_than_the_tag(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        blob = next(store.root.glob("*.bin"))
        blob.write_bytes(blob.read_bytes()[:-8])

        with pytest.raises(CheckpointError, match="digest does not match"):
            store._restore_one(
                sequence=1,
                generation_id=_GENERATION,
                input_digest=_INPUT_DIGEST,
                policy_digest=_POLICY_DIGEST,
                destination=tmp_path / "restored",
            )

    def test_a_different_generation_does_not_restore(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            store.load_latest_valid(
                generation_id="d" * 64,
                input_digest=_INPUT_DIGEST,
                policy_digest=_POLICY_DIGEST,
                destination=tmp_path / "restored",
            )

    def test_a_changed_census_does_not_restore(self, tmp_path: Path) -> None:
        """A checkpoint taken under a different input census is not resumable."""
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
            store.load_latest_valid(
                generation_id=_GENERATION,
                input_digest="e" * 64,
                policy_digest=_POLICY_DIGEST,
                destination=tmp_path / "restored",
            )

    def test_an_absent_store_reports_rather_than_returning_empty(self, tmp_path: Path) -> None:
        with pytest.raises(CheckpointError, match="no checkpoint found"):
            _load(_store(tmp_path), tmp_path / "restored")

    @pytest.mark.parametrize("size", [0, 8, 15])
    def test_a_truncated_secret_is_refused_at_construction(self, tmp_path: Path, size: int) -> None:
        """A floor on length only. Guessability is the KDF's problem, not this check's."""
        with pytest.raises(CheckpointError, match="at least 16 bytes"):
            CheckpointStore(tmp_path / "checkpoints", bytes(size), _KEY_ID)

    @pytest.mark.parametrize("size", [16, 31, 33, 64])
    def test_any_secret_at_or_above_the_floor_is_accepted(self, tmp_path: Path, size: int) -> None:
        """The secret is stretched, so its length no longer has to be a key length."""
        assert CheckpointStore(tmp_path / "checkpoints", bytes(range(size)), _KEY_ID)

    @pytest.mark.parametrize("key_id", ["", "   "])
    def test_an_empty_key_identifier_is_refused(self, tmp_path: Path, key_id: str) -> None:
        with pytest.raises(CheckpointError, match="key identifier must not be empty"):
            CheckpointStore(tmp_path / "checkpoints", _KEY, key_id)

    def test_a_non_positive_sequence_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(CheckpointError, match="sequence must be positive"):
            _save(_store(tmp_path), _state(tmp_path), 0)


class TestFallbackAndRetention:
    def test_a_corrupt_newest_falls_back_to_the_previous(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path, "first", marker="older"), 1)
        _save(store, _state(tmp_path, "second", marker="newer"), 2)

        newest = store.root / f"{2:012d}.bin"
        newest.write_bytes(b"corrupt")

        restored = tmp_path / "restored"
        manifest = _load(store, restored)

        assert manifest.sequence == 1
        assert (restored / "ledger.db").read_bytes() == b"older"

    def test_both_sequences_invalid_raises_rather_than_restarting(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path, "first"), 1)
        _save(store, _state(tmp_path, "second"), 2)
        for blob in store.root.glob("*.bin"):
            blob.write_bytes(b"corrupt")

        with pytest.raises(CheckpointError, match="refusing to restart the generation from zero"):
            _load(store, tmp_path / "restored")

    def test_only_the_current_and_previous_sequences_are_retained(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        for sequence in (1, 2, 3, 4):
            _save(store, _state(tmp_path, f"s{sequence}"), sequence)

        assert store.available_sequences() == [4, 3]
        assert not (store.root / f"{1:012d}.bin").exists()
        assert not (store.root / f"{1:012d}.manifest.json").exists()

    def test_a_half_written_temp_file_is_not_a_sequence(self, tmp_path: Path) -> None:
        store = _store(tmp_path)
        _save(store, _state(tmp_path), 1)
        (store.root / "000000000002.bin.tmp").write_bytes(b"partial")

        assert store.available_sequences() == [1]


class TestManifestParsing:
    def test_a_non_json_manifest_is_reported(self) -> None:
        with pytest.raises(CheckpointError, match="not valid JSON"):
            CheckpointManifest.from_bytes(b"{not json")

    @pytest.mark.parametrize("payload", [b"[]", b'"text"', b"null", b"3"])
    def test_a_non_object_manifest_is_reported(self, payload: bytes) -> None:
        with pytest.raises(CheckpointError, match="not a JSON object"):
            CheckpointManifest.from_bytes(payload)

    def test_a_missing_field_is_reported(self) -> None:
        with pytest.raises(CheckpointError, match="missing or malformed"):
            CheckpointManifest.from_bytes(json.dumps({"schema_version": "1"}).encode())

    def test_binding_bytes_excludes_only_the_ciphertext_digest(self) -> None:
        manifest = CheckpointManifest(
            schema_version=CHECKPOINT_SCHEMA_VERSION,
            generation_id=_GENERATION,
            input_digest=_INPUT_DIGEST,
            policy_digest=_POLICY_DIGEST,
            sequence=7,
            created_at=_WHEN,
            ciphertext_digest="f" * 64,
            key_id=_KEY_ID,
        )
        full = set(json.loads(manifest.to_bytes()))
        binding = set(json.loads(manifest.binding_bytes()))
        assert full - binding == {"ciphertext_digest"}

    def test_the_digest_field_does_not_affect_the_binding(self) -> None:
        """Otherwise the manifest could not authenticate itself."""

        def _with(digest: str) -> CheckpointManifest:
            return CheckpointManifest(
                schema_version=CHECKPOINT_SCHEMA_VERSION,
                generation_id=_GENERATION,
                input_digest=_INPUT_DIGEST,
                policy_digest=_POLICY_DIGEST,
                sequence=7,
                created_at=_WHEN,
                ciphertext_digest=digest,
                key_id=_KEY_ID,
            )

        assert _with("").binding_bytes() == _with("f" * 64).binding_bytes()


def test_the_salt_is_fresh_per_checkpoint_and_reveals_nothing(tmp_path: Path) -> None:
    """A salt must be public and must not repeat.

    Publishing it is what lets the restore side derive the same key. Repeating
    it would let one cracking run cover every checkpoint at once, which is most
    of what the KDF is buying.
    """
    store = _store(tmp_path)
    first = _save(store, _state(tmp_path, "one"), 1)
    second = _save(store, _state(tmp_path, "two"), 2)

    assert first.kdf == second.kdf == "scrypt-n131072-r8-p1"
    assert len(bytes.fromhex(first.kdf_salt)) == 16
    assert first.kdf_salt != second.kdf_salt, "a repeated salt shares a derived key across checkpoints"
    # Derived from os.urandom, never from the secret, so two stores holding the
    # same secret still produce unrelated salts.
    other = CheckpointStore(tmp_path / "other", _KEY, _KEY_ID)
    assert _save(other, _state(tmp_path, "three"), 1).kdf_salt != first.kdf_salt


def test_an_unsupported_kdf_is_refused(tmp_path: Path) -> None:
    """A manifest naming a different KDF must fail closed, not fall back."""
    store = _store(tmp_path)
    _save(store, _state(tmp_path), 1)
    path = next(store.root.glob("*.manifest.json"))
    content = json.loads(path.read_text(encoding="utf-8"))
    content["kdf"] = "sha256"
    path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(CheckpointError, match="no retained checkpoint verified"):
        _load(store, tmp_path / "restored")


def test_a_crafted_manifest_cannot_forge_a_log_line(tmp_path: Path) -> None:
    """Manifest strings come off a public branch and end up in two logs.

    A newline in one lets a crafted manifest write a second line of its own,
    including an Actions workflow command. The error text must stay one line.
    """
    store = _store(tmp_path)
    _save(store, _state(tmp_path), 1)
    path = next(store.root.glob("*.manifest.json"))
    content = json.loads(path.read_text(encoding="utf-8"))
    content["schema_version"] = "9\n::error::Refresh complete: generation=FAKE"
    path.write_text(json.dumps(content), encoding="utf-8")

    with pytest.raises(CheckpointError) as caught:
        _load(store, tmp_path / "restored")

    assert "\n" not in str(caught.value), "a newline survived into the error text"
    assert "::error::" not in str(caught.value).replace(" ", "") or "\n" not in str(caught.value)
