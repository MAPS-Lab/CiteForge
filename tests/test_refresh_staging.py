"""Contract tests for refresh output staging."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from citeforge.refresh.authority import IntentKind, MaterializationIntent
from citeforge.refresh.ledger import EvidenceState
from citeforge.refresh.staging import (
    CorpusManifest,
    StagingError,
    materialize_candidate,
    prepare_stage,
    validate_stage,
)

_git_executable = shutil.which("git")
if _git_executable is None:
    raise RuntimeError("git is required for staging tests")
_GIT: str = _git_executable

_GENERATION = "generation-1"
_PROVENANCE = "d" * 64
_AUTHOR_DIR = "output/Lovelace (Scholar123)"
_PAPER_PATH = f"{_AUTHOR_DIR}/paper.bib"
_STALE_PATH = f"{_AUTHOR_DIR}/stale.bib"
_FRESH_PATH = f"{_AUTHOR_DIR}/fresh.bib"
_BASELINE_PATH = "output/baseline.json"
_BASELINE = '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n'
_PAPER = "@article{Key, title={A title}, author={Lovelace, Ada}, year={2026}}\n"
_STALE = "@misc{Stale, title={A preprint}, author={Lovelace, Ada}, year={2025}}\n"
_FRESH = "@inproceedings{Fresh, title={A paper}, author={Lovelace, Ada}, year={2026}}\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        (_GIT, *args), cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / _AUTHOR_DIR).mkdir(parents=True)
    (repo / "data").mkdir()
    (repo / _PAPER_PATH).write_text(_PAPER, encoding="utf-8")
    (repo / _STALE_PATH).write_text(_STALE, encoding="utf-8")
    (repo / _BASELINE_PATH).write_text(_BASELINE, encoding="utf-8")
    (repo / "output" / "summary.csv").write_text("title\n", encoding="utf-8")
    (repo / "data" / "a2i2.csv").write_text("Name,Scholar Link,DBLP Link\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "test: add corpus fixture")
    return repo, _git(repo, "rev-parse", "HEAD")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _intent(
    kind: IntentKind,
    target: str,
    *,
    before: str | None = None,
    after: str | None = None,
    generation_id: str = _GENERATION,
    publication_key: str = "publication-1",
) -> MaterializationIntent:
    content = before if kind is IntentKind.KEEP else after
    return MaterializationIntent(
        generation_id=generation_id,
        pass_key="pass-1",
        author_key="author-ada",
        publication_key=publication_key,
        source_path=target,
        target_path=target,
        kind=kind,
        before_digest=before,
        after_digest=after,
        reducer_id="citeforge.merge",
        reducer_version="1",
        provenance_set_digest=_PROVENANCE,
        final_fields=() if kind is IntentKind.REMOVE else ("title",),
        final_content_digest=content,
        removal_reason="superseded" if kind is IntentKind.REMOVE else "",
    )


def _keep(target: str, body: str) -> MaterializationIntent:
    return _intent(IntentKind.KEEP, target, before=_digest(body), after=_digest(body))


def _upsert(target: str, body: str, *, publication_key: str = "publication-2") -> MaterializationIntent:
    return _intent(IntentKind.UPSERT, target, after=_digest(body), publication_key=publication_key)


def _remove(target: str, body: str, *, publication_key: str = "publication-3") -> MaterializationIntent:
    return _intent(IntentKind.REMOVE, target, before=_digest(body), publication_key=publication_key)


def _plan() -> tuple[tuple[MaterializationIntent, ...], dict[str, bytes]]:
    """One of each intent kind, plus the bodies the UPSERT needs."""
    intents = (
        _keep(_PAPER_PATH, _PAPER),
        _upsert(_FRESH_PATH, _FRESH),
        _remove(_STALE_PATH, _STALE),
    )
    return intents, {_FRESH_PATH: _FRESH.encode()}


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root).as_posix()): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class TestPrepareStage:
    def test_stage_carries_committed_bytes_and_ignores_the_working_tree(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        (repo / _PAPER_PATH).write_text("@article{Key, title={Edited after the commit}}\n", encoding="utf-8")
        (repo / "output" / "untracked.bib").write_text("@misc{Untracked}\n", encoding="utf-8")
        stage = tmp_path / "stage"

        staged = prepare_stage(repo, stage, base_commit=commit)

        assert staged == (_PAPER_PATH, _STALE_PATH, _BASELINE_PATH, "output/summary.csv")
        assert (stage / _PAPER_PATH).read_text(encoding="utf-8") == _PAPER
        assert not (stage / "output" / "untracked.bib").exists()

    def test_extra_roots_are_staged(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)

        staged = prepare_stage(repo, tmp_path / "stage", base_commit=commit, roots=("output", "data/a2i2.csv"))

        assert "data/a2i2.csv" in staged

    def test_non_empty_stage_root_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        stage.mkdir()
        (stage / "leftover").write_text("stale", encoding="utf-8")

        with pytest.raises(StagingError, match="not empty"):
            prepare_stage(repo, stage, base_commit=commit)

    def test_unknown_commit_is_refused(self, tmp_path: Path) -> None:
        repo, _ = _repo(tmp_path)

        with pytest.raises(StagingError, match="unusable"):
            prepare_stage(repo, tmp_path / "stage", base_commit="0" * 40)

    def test_absent_root_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)

        with pytest.raises(StagingError, match="unusable"):
            prepare_stage(repo, tmp_path / "stage", base_commit=commit, roots=("docs",))


class TestCommittedOutputIsUntouched:
    def test_incomplete_work_leaves_the_repository_byte_identical(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        before = _tree(repo / "output")
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents, contents = _plan()

        materialize_candidate(stage, intents, contents, generation_id=_GENERATION)

        assert _tree(repo / "output") == before
        assert _git(repo, "status", "--porcelain") == ""
        assert not (repo / _FRESH_PATH).exists()
        assert (repo / _STALE_PATH).exists()
        assert (stage / _FRESH_PATH).read_text(encoding="utf-8") == _FRESH
        assert not (stage / _STALE_PATH).exists()


class TestManifestDeterminism:
    def test_two_materializations_agree(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        intents, contents = _plan()
        digests = []
        for name in ("first", "second"):
            stage = tmp_path / name
            prepare_stage(repo, stage, base_commit=commit)
            digests.append(materialize_candidate(stage, intents, contents, generation_id=_GENERATION).manifest_digest)

        assert digests[0] == digests[1]
        assert _tree(tmp_path / "first") == _tree(tmp_path / "second")

    def test_one_changed_byte_changes_the_digest(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents, contents = _plan()
        original = materialize_candidate(stage, intents, contents, generation_id=_GENERATION).manifest_digest

        (stage / _BASELINE_PATH).write_text(_BASELINE.replace('"total":2', '"total":3'), encoding="utf-8")

        assert CorpusManifest.from_stage(stage, generation_id=_GENERATION).digest != original

    def test_a_renamed_file_changes_the_digest(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        original = CorpusManifest.from_stage(stage, generation_id=_GENERATION).digest

        (stage / _PAPER_PATH).rename(stage / _AUTHOR_DIR / "renamed.bib")

        assert CorpusManifest.from_stage(stage, generation_id=_GENERATION).digest != original

    def test_the_manifest_is_bound_to_the_generation(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        first = CorpusManifest.from_stage(stage, generation_id=_GENERATION)
        second = CorpusManifest.from_stage(stage, generation_id="generation-2")

        assert first.entries == second.entries
        assert first.digest != second.digest

    def test_a_symlink_in_the_stage_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        (stage / "output" / "escape.bib").symlink_to(repo / _PAPER_PATH)

        with pytest.raises(StagingError, match="symbolic link"):
            CorpusManifest.from_stage(stage, generation_id=_GENERATION)


class TestValidateStage:
    def test_a_satisfied_plan_returns_the_stage_manifest(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents, contents = _plan()
        materialize_candidate(stage, intents, contents, generation_id=_GENERATION)

        manifest = validate_stage(stage, intents, generation_id=_GENERATION)

        assert dict(manifest.entries)[_FRESH_PATH] == _digest(_FRESH)
        assert _STALE_PATH not in dict(manifest.entries)

    def test_a_missing_entry_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="missing"):
            validate_stage(stage, (_upsert(_FRESH_PATH, _FRESH),), generation_id=_GENERATION)

    def test_a_digest_mismatch_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="does not match"):
            validate_stage(stage, (_keep(_PAPER_PATH, _STALE),), generation_id=_GENERATION)

    def test_a_surviving_removal_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="still staged"):
            validate_stage(stage, (_remove(_STALE_PATH, _STALE),), generation_id=_GENERATION)

    def test_a_foreign_generation_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        foreign = _intent(
            IntentKind.KEEP, _PAPER_PATH, before=_digest(_PAPER), after=_digest(_PAPER), generation_id="generation-2"
        )

        with pytest.raises(StagingError, match="another generation"):
            validate_stage(stage, (foreign,), generation_id=_GENERATION)

    def test_two_intents_for_one_path_are_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents = (_keep(_PAPER_PATH, _PAPER), _upsert(_PAPER_PATH, _FRESH))

        with pytest.raises(StagingError, match="more than one intent"):
            validate_stage(stage, intents, generation_id=_GENERATION)

    def test_an_absent_stage_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(StagingError, match="does not exist"):
            validate_stage(tmp_path / "absent", (), generation_id=_GENERATION)


class TestMaterializeCandidate:
    def test_evidence_reports_validated_counts(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents, contents = _plan()

        evidence = materialize_candidate(stage, intents, contents, generation_id=_GENERATION)

        assert evidence.validation_state is EvidenceState.VALIDATED
        assert evidence.staged_path == str(stage)
        assert evidence.manifest_digest == validate_stage(stage, intents, generation_id=_GENERATION).digest
        assert dict(evidence.corpus_counts) == {"staged_files": 4, "kept": 1, "upserted": 1, "removed": 1}

    def test_a_missing_body_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="no staged content"):
            materialize_candidate(stage, (_upsert(_FRESH_PATH, _FRESH),), {}, generation_id=_GENERATION)

    def test_a_body_that_contradicts_its_intent_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        intents = (_upsert(_FRESH_PATH, _FRESH),)

        with pytest.raises(StagingError, match="does not match"):
            materialize_candidate(stage, intents, {_FRESH_PATH: _STALE.encode()}, generation_id=_GENERATION)
        assert not (stage / _FRESH_PATH).exists()

    def test_an_unclaimed_body_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)
        contents = {_FRESH_PATH: _FRESH.encode(), "output/rogue.bib": b"@misc{Rogue}\n"}

        with pytest.raises(StagingError, match="no intent"):
            materialize_candidate(stage, (_upsert(_FRESH_PATH, _FRESH),), contents, generation_id=_GENERATION)

    def test_a_keep_over_unexpected_bytes_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="does not match"):
            materialize_candidate(stage, (_keep(_PAPER_PATH, _FRESH),), {}, generation_id=_GENERATION)

    def test_a_removal_of_an_absent_entry_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="missing"):
            materialize_candidate(stage, (_remove(_FRESH_PATH, _FRESH),), {}, generation_id=_GENERATION)

    def test_a_removal_of_unexpected_bytes_is_refused(self, tmp_path: Path) -> None:
        repo, commit = _repo(tmp_path)
        stage = tmp_path / "stage"
        prepare_stage(repo, stage, base_commit=commit)

        with pytest.raises(StagingError, match="does not match"):
            materialize_candidate(stage, (_remove(_STALE_PATH, _FRESH),), {}, generation_id=_GENERATION)
        assert (stage / _STALE_PATH).exists()
