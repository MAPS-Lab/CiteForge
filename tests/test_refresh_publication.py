"""Contract tests for pull-request publication and merge-gated dispatch."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from citeforge.refresh.publication import (
    CandidateOffer,
    CompletenessEvidence,
    GitHubPublisher,
    MergeObservation,
    PublicationError,
    PublicationPort,
    PublicationRequest,
    RecordingPublicationPort,
    candidate_block_reason,
    candidate_refspec,
    merge_block_reason,
)

_BRANCH = "bot/citeforge-refresh-2026-08"
_MANIFEST = "a" * 64
_OTHER_DIGEST = "b" * 64
_HEAD = "1" * 40
_MERGE = "2" * 40


def _evidence(
    *,
    complete: bool = True,
    unresolved: int = 0,
    completed_manifest_digest: str = _MANIFEST,
) -> CompletenessEvidence:
    return CompletenessEvidence(
        generation_complete=complete,
        unresolved_work=unresolved,
        completed_manifest_digest=completed_manifest_digest,
    )


def _request(
    tmp_path: Path,
    *,
    branch: str = _BRANCH,
    candidate_digest: str = _MANIFEST,
    manifest_digest: str = _MANIFEST,
    evidence: CompletenessEvidence | None = None,
) -> PublicationRequest:
    return PublicationRequest(
        branch=branch,
        candidate_digest=candidate_digest,
        manifest_digest=manifest_digest,
        worktree=tmp_path,
        evidence=_evidence() if evidence is None else evidence,
    )


def _offer(tmp_path: Path, head_sha: str = _HEAD) -> CandidateOffer:
    return CandidateOffer(
        branch=_BRANCH,
        head_sha=head_sha,
        pull_request=41,
        candidate_digest=_MANIFEST,
        manifest_digest=_MANIFEST,
        worktree=tmp_path,
    )


def _observation(
    *,
    merged: bool = True,
    merge_sha: str = _MERGE,
    merged_head_sha: str = _HEAD,
    checks: bool = True,
) -> MergeObservation:
    return MergeObservation(
        merged=merged,
        merge_sha=merge_sha,
        merged_head_sha=merged_head_sha,
        required_checks_passed=checks,
    )


class TestDefaultBranchIsUnreachable:
    """The hard rule. No code path may push at the default branch."""

    @pytest.mark.parametrize(
        "branch",
        ["main", "master", "HEAD", "bot/main", "bot/master", "refs/heads/main", "bot/x:main", "bot/a HEAD:main"],
    )
    def test_a_branch_that_could_resolve_to_the_default_is_refused(self, branch: str) -> None:
        with pytest.raises(PublicationError):
            candidate_refspec(branch)

    @pytest.mark.parametrize("branch", ["main", "bot/main", "bot/x:main"])
    def test_a_request_cannot_even_be_constructed_for_such_a_branch(self, tmp_path: Path, branch: str) -> None:
        with pytest.raises(PublicationError):
            _request(tmp_path, branch=branch)

    def test_the_only_refspec_is_the_candidate_branch_onto_itself(self) -> None:
        refspec = candidate_refspec(_BRANCH)

        assert refspec == f"refs/heads/{_BRANCH}:refs/heads/{_BRANCH}"
        assert "HEAD" not in refspec
        assert ":main" not in refspec


class TestCandidateGate:
    def test_a_complete_bound_generation_is_publishable(self, tmp_path: Path) -> None:
        assert candidate_block_reason(_request(tmp_path)) == ""

    def test_an_incomplete_generation_is_blocked(self, tmp_path: Path) -> None:
        blocked = _request(tmp_path, evidence=_evidence(complete=False))
        assert candidate_block_reason(blocked) == "generation is not complete"

    def test_unresolved_work_blocks(self, tmp_path: Path) -> None:
        blocked = _request(tmp_path, evidence=_evidence(unresolved=3))
        assert "3 unresolved work items" in candidate_block_reason(blocked)

    def test_a_generation_without_a_completed_manifest_digest_is_blocked(self, tmp_path: Path) -> None:
        blocked = _request(tmp_path, evidence=_evidence(completed_manifest_digest=""))
        assert candidate_block_reason(blocked) == "generation has no completed manifest digest"

    def test_a_wrong_manifest_digest_blocks(self, tmp_path: Path) -> None:
        blocked = _request(tmp_path, manifest_digest=_OTHER_DIGEST)
        assert candidate_block_reason(blocked) == "publication manifest digest is not the completed manifest digest"

    def test_a_wrong_candidate_digest_blocks(self, tmp_path: Path) -> None:
        blocked = _request(tmp_path, candidate_digest=_OTHER_DIGEST)
        assert candidate_block_reason(blocked) == "candidate digest is not the completed manifest digest"

    @pytest.mark.parametrize("value", ["", "abc", "A" * 64, "a" * 63])
    def test_a_malformed_digest_is_a_caller_defect_not_a_refusal(self, tmp_path: Path, value: str) -> None:
        with pytest.raises(PublicationError):
            _request(tmp_path, candidate_digest=value)


class TestMergeGate:
    def test_a_verified_merge_of_the_offered_head_passes(self, tmp_path: Path) -> None:
        assert merge_block_reason(_offer(tmp_path), _observation()) == ""

    def test_an_unmerged_pull_request_blocks(self, tmp_path: Path) -> None:
        assert merge_block_reason(_offer(tmp_path), _observation(merged=False)) == "pull request is not merged"

    def test_failed_required_ci_blocks(self, tmp_path: Path) -> None:
        reason = merge_block_reason(_offer(tmp_path), _observation(checks=False))
        assert reason == "required CI did not pass on the candidate"

    def test_a_missing_merge_commit_blocks(self, tmp_path: Path) -> None:
        assert merge_block_reason(_offer(tmp_path), _observation(merge_sha="")) == "no verified merge commit SHA"

    def test_a_wrong_head_blocks(self, tmp_path: Path) -> None:
        reason = merge_block_reason(_offer(tmp_path), _observation(merged_head_sha="9" * 40))
        assert "is not the offered candidate" in reason


class TestRecordingPort:
    def test_it_satisfies_the_port_protocol(self) -> None:
        port: PublicationPort = RecordingPublicationPort()
        assert port is not None

    def test_a_publishable_request_records_one_offer(self, tmp_path: Path) -> None:
        port = RecordingPublicationPort()
        offer = port.offer_candidate(_request(tmp_path))

        assert offer is not None
        assert port.offers == [offer]
        assert port.refusals == []

    @pytest.mark.parametrize(
        ("kwargs", "expected"),
        [
            ({"evidence": _evidence(complete=False)}, "generation is not complete"),
            ({"evidence": _evidence(unresolved=2)}, "generation carries 2 unresolved work items"),
            ({"manifest_digest": _OTHER_DIGEST}, "publication manifest digest is not the completed manifest digest"),
            ({"candidate_digest": _OTHER_DIGEST}, "candidate digest is not the completed manifest digest"),
        ],
    )
    def test_a_blocked_state_creates_no_offer(self, tmp_path: Path, kwargs: dict[str, object], expected: str) -> None:
        port = RecordingPublicationPort()

        assert port.offer_candidate(_request(tmp_path, **kwargs)) is None  # type: ignore[arg-type]
        assert port.offers == []
        assert port.dispatches == []
        assert port.refusals == [expected]

    @pytest.mark.parametrize(
        "observation",
        [
            _observation(merged=False),
            _observation(checks=False),
            _observation(merged_head_sha="9" * 40),
            _observation(merge_sha=""),
        ],
    )
    def test_a_blocked_merge_state_creates_no_dispatch(self, tmp_path: Path, observation: MergeObservation) -> None:
        port = RecordingPublicationPort()

        assert port.dispatch_website(_offer(tmp_path), observation) is None
        assert port.dispatches == []

    def test_a_verified_merge_dispatches_exactly_once_across_retries(self, tmp_path: Path) -> None:
        port = RecordingPublicationPort()
        offer, observation = _offer(tmp_path), _observation()

        assert port.dispatch_website(offer, observation) == _MERGE
        assert port.dispatch_website(offer, observation) == _MERGE
        assert port.dispatches == [_MERGE]


def _write_executable(path: Path, script: str) -> None:
    path.write_text(script, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Put recording ``git`` and ``gh`` shims at the front of PATH.

    ``--disable-socket`` only blocks sockets in this process, so a subprocess
    publisher has to be denied a real endpoint by the environment it runs in.
    The ``git`` shim records its argv and then execs the real git against a
    bare repository under tmp_path, and the ``gh`` shim is entirely local.
    """
    real_git = shutil.which("git")
    assert real_git is not None
    record = tmp_path / "record"
    record.mkdir()
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    _write_executable(
        fake_bin / "git",
        f'#!/bin/sh\nprintf "%s\\n" "$*" >> "$CITEFORGE_FAKE_RECORD/git-argv"\nexec {real_git} "$@"\n',
    )
    _write_executable(
        fake_bin / "gh",
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$CITEFORGE_FAKE_RECORD/gh-argv"\n'
        'case "$1 $2" in\n'
        '  "pr view")\n'
        '    if [ -f "$CITEFORGE_FAKE_RECORD/pr" ]; then cat "$CITEFORGE_FAKE_RECORD/pr"; exit 0; fi\n'
        '    echo "no pull requests found" >&2; exit 1;;\n'
        '  "pr create")\n'
        '    echo 41 > "$CITEFORGE_FAKE_RECORD/pr"; echo "https://example.test/pull/41"; exit 0;;\n'
        "  api*)\n"
        '    echo "$*" >> "$CITEFORGE_FAKE_RECORD/dispatch"; exit 0;;\n'
        "esac\n"
        "exit 2\n",
    )
    monkeypatch.setenv("CITEFORGE_FAKE_RECORD", str(record))
    monkeypatch.setenv("PATH", f"{fake_bin}{os.pathsep}{os.environ['PATH']}")
    return record


def _worktree(tmp_path: Path) -> Path:
    """A checkout on the candidate branch, wired to a bare repo as its remote."""
    git = shutil.which("git")
    assert git is not None
    remote = tmp_path / "remote.git"
    subprocess.run((git, "init", "--bare", "-q", str(remote)), check=True)  # noqa: S603
    work = tmp_path / "work"
    work.mkdir()
    (work / "corpus.bib").write_text("@misc{a, title={A}}\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"),
        ("config", "user.email", "test@example.test"),
        ("config", "user.name", "Test"),
        ("remote", "add", "origin", str(remote)),
        ("add", "-A"),
        ("commit", "-qm", "test: corpus"),
        ("checkout", "-q", "-b", _BRANCH),
    ):
        subprocess.run((git, *args), cwd=work, check=True)  # noqa: S603
    return work


def _lines(path: Path) -> list[str]:
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line] if path.is_file() else []


class TestGitHubPublisher:
    def test_it_satisfies_the_port_protocol(self, tmp_path: Path) -> None:
        port: PublicationPort = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )
        assert port is not None

    def test_it_pushes_only_the_candidate_branch_onto_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        work = _worktree(tmp_path)
        record = _fake_tools(tmp_path, monkeypatch)
        publisher = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )

        offer = publisher.offer_candidate(_request(work))

        assert offer is not None
        assert offer.pull_request == 41
        argv = "\n".join(_lines(record / "git-argv"))
        assert f"push --force-with-lease origin refs/heads/{_BRANCH}:refs/heads/{_BRANCH}" in argv
        assert "HEAD" not in argv
        assert ":main" not in argv
        assert ":master" not in argv

    def test_the_pushed_branch_carries_the_candidate_sha(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = _worktree(tmp_path)
        _fake_tools(tmp_path, monkeypatch)
        publisher = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )

        offer = publisher.offer_candidate(_request(work))

        assert offer is not None
        git = shutil.which("git")
        assert git is not None
        remote_sha = subprocess.run(  # noqa: S603
            (git, "rev-parse", _BRANCH),
            cwd=tmp_path / "remote.git",
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert remote_sha == offer.head_sha

    def test_a_repeated_offer_creates_one_pull_request(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = _worktree(tmp_path)
        record = _fake_tools(tmp_path, monkeypatch)
        publisher = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )

        first = publisher.offer_candidate(_request(work))
        second = publisher.offer_candidate(_request(work))

        assert first is not None and second is not None
        assert first.pull_request == second.pull_request
        assert len([line for line in _lines(record / "gh-argv") if line.startswith("pr create")]) == 1

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"evidence": _evidence(complete=False)},
            {"evidence": _evidence(unresolved=1)},
            {"manifest_digest": _OTHER_DIGEST},
            {"candidate_digest": _OTHER_DIGEST},
        ],
    )
    def test_a_blocked_state_runs_no_subprocess_at_all(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, object]
    ) -> None:
        work = _worktree(tmp_path)
        record = _fake_tools(tmp_path, monkeypatch)
        publisher = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )

        assert publisher.offer_candidate(_request(work, **kwargs)) is None  # type: ignore[arg-type]
        assert _lines(record / "git-argv") == []
        assert _lines(record / "gh-argv") == []
        assert _lines(record / "dispatch") == []

    @pytest.mark.parametrize(
        "observation",
        [
            _observation(merged=False),
            _observation(checks=False),
            _observation(merged_head_sha="9" * 40),
            _observation(merge_sha=""),
        ],
    )
    def test_a_blocked_merge_state_dispatches_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observation: MergeObservation
    ) -> None:
        record = _fake_tools(tmp_path, monkeypatch)
        dispatched = tmp_path / "dispatched"
        publisher = GitHubPublisher(website_repository="MAPS-Lab/mapslab-website", dispatch_record=dispatched)

        assert publisher.dispatch_website(_offer(tmp_path), observation) is None
        assert _lines(record / "dispatch") == []
        assert not dispatched.exists()

    def test_a_verified_merge_dispatches_exactly_once_across_invocations(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Idempotency is keyed on the merge SHA and survives a new process."""
        work = _worktree(tmp_path)
        record = _fake_tools(tmp_path, monkeypatch)
        dispatched = tmp_path / "dispatched"
        offer, observation = _offer(work), _observation()

        for _ in range(3):
            publisher = GitHubPublisher(website_repository="MAPS-Lab/mapslab-website", dispatch_record=dispatched)
            assert publisher.dispatch_website(offer, observation) == _MERGE

        assert len(_lines(record / "dispatch")) == 1
        assert _MERGE in _lines(record / "dispatch")[0]
        assert _lines(dispatched) == [_MERGE]

    def test_a_second_merge_sha_dispatches_again(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        work = _worktree(tmp_path)
        record = _fake_tools(tmp_path, monkeypatch)
        dispatched = tmp_path / "dispatched"
        publisher = GitHubPublisher(website_repository="MAPS-Lab/mapslab-website", dispatch_record=dispatched)

        publisher.dispatch_website(_offer(work), _observation())
        publisher.dispatch_website(_offer(work), _observation(merge_sha="3" * 40))

        assert len(_lines(record / "dispatch")) == 2

    def test_a_missing_executable_fails_closed(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty"))
        publisher = GitHubPublisher(
            website_repository="MAPS-Lab/mapslab-website", dispatch_record=tmp_path / "dispatched"
        )

        with pytest.raises(PublicationError, match="git is not on PATH"):
            publisher.offer_candidate(_request(tmp_path))

    def test_an_empty_website_repository_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(PublicationError, match="website repository"):
            GitHubPublisher(website_repository="", dispatch_record=tmp_path / "dispatched")
