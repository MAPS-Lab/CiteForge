"""Pull-request publication for a completed refresh generation.

Publication is the only path from a generation into the committed corpus, and
the retired one pushed straight to protected ``main``. This module removes
that possibility structurally rather than by policy. Every push refspec is
built by one helper from a branch name that helper has already refused unless
it sits under ``bot/``, so no caller and no argument can steer a push at the
default branch.

Two gates stand in front of every side effect. Before a candidate is offered,
the ledger's own completeness evidence has to say the generation is complete,
carries no unresolved work, and binds exactly the manifest being published.
Before the website is dispatched, the pull request has to be merged, Required
CI has to have passed, and the merged head has to be the exact SHA that was
offered. Any other state produces no branch, no pull request, and no dispatch.

The module knows nothing about the ledger, the staging tree, or checkpoints.
It receives a branch name, two digests, a path, and evidence, then shells out.
The caller records what it returns.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# The candidate branch shape from the architecture, bot/citeforge-refresh-YYYY-MM.
# Anchoring on the bot/ prefix is what makes the default branch unreachable.
_CANDIDATE_BRANCH_RE = re.compile(r"bot/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*")

# Abbreviated SHAs are accepted because gh and git both emit them, and the
# ledger's own publication guard uses the same 7-to-64 hex window.
_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}")

# Names a forge may treat as the default branch. A candidate whose last path
# segment is one of these is refused even though the bot/ prefix already makes
# it a distinct ref, because the cost of the second check is one comparison.
_PROTECTED_REFS = frozenset({"main", "master", "head", "trunk", "default"})

_WEBSITE_EVENT_TYPE = "citeforge-corpus-updated"


class PublicationError(RuntimeError):
    """A publication step failed, or was asked to do something forbidden."""


def _digest(value: str, label: str) -> str:
    if not _DIGEST_RE.fullmatch(value):
        raise PublicationError(f"invalid {label}: {value!r}")
    return value


def candidate_refspec(branch: str) -> str:
    """Build the only push refspec this module will ever produce.

    Source and destination are the same ``bot/`` ref, so there is no argument
    shape that turns this into ``HEAD:main`` or any other default-branch push.
    """
    if not _CANDIDATE_BRANCH_RE.fullmatch(branch):
        raise PublicationError(f"candidate branch must match bot/<name>, got {branch!r}")
    if branch.rsplit("/", 1)[-1].lower() in _PROTECTED_REFS:
        raise PublicationError(f"candidate branch resolves to a protected ref: {branch!r}")
    return f"refs/heads/{branch}:refs/heads/{branch}"


@dataclass(frozen=True)
class CompletenessEvidence:
    """What the ledger already proved about the generation.

    Publication reads these facts and never re-derives them, because the
    ledger is the sole completion authority.
    """

    generation_complete: bool
    unresolved_work: int
    completed_manifest_digest: str


@dataclass(frozen=True)
class PublicationRequest:
    """One candidate offered for publication."""

    branch: str
    candidate_digest: str
    manifest_digest: str
    worktree: Path
    evidence: CompletenessEvidence

    def __post_init__(self) -> None:
        # Malformed input is a caller defect and fails loudly here. A refused
        # generation state is a different thing and reports through
        # candidate_block_reason instead.
        candidate_refspec(self.branch)
        _digest(self.candidate_digest, "candidate digest")
        _digest(self.manifest_digest, "publication manifest digest")


@dataclass(frozen=True)
class CandidateOffer:
    """A candidate that reached a branch and a pull request."""

    branch: str
    head_sha: str
    pull_request: int
    candidate_digest: str
    manifest_digest: str
    worktree: Path


@dataclass(frozen=True)
class MergeObservation:
    """What the forge reports about an offered pull request."""

    merged: bool
    merge_sha: str
    merged_head_sha: str
    required_checks_passed: bool


def candidate_block_reason(request: PublicationRequest) -> str:
    """Why *request* must not be offered, or an empty string when it may be.

    The three digest comparisons mirror the ledger's own PUBLISHED guard,
    which requires the publication's candidate digest and manifest digest to
    both equal the generation's completed manifest digest.
    """
    evidence = request.evidence
    if not evidence.generation_complete:
        return "generation is not complete"
    if evidence.unresolved_work:
        return f"generation carries {evidence.unresolved_work} unresolved work items"
    if not _DIGEST_RE.fullmatch(evidence.completed_manifest_digest):
        return "generation has no completed manifest digest"
    if request.manifest_digest != evidence.completed_manifest_digest:
        return "publication manifest digest is not the completed manifest digest"
    if request.candidate_digest != evidence.completed_manifest_digest:
        return "candidate digest is not the completed manifest digest"
    return ""


def merge_block_reason(offer: CandidateOffer, observation: MergeObservation) -> str:
    """Why the website must not be dispatched, or an empty string when it may."""
    if not observation.merged:
        return "pull request is not merged"
    if not observation.required_checks_passed:
        return "required CI did not pass on the candidate"
    if not _SHA_RE.fullmatch(observation.merge_sha):
        return "no verified merge commit SHA"
    if observation.merged_head_sha != offer.head_sha:
        return f"merged head {observation.merged_head_sha} is not the offered candidate {offer.head_sha}"
    return ""


class PublicationPort(Protocol):
    """The publication side effects an engine is allowed to request."""

    def offer_candidate(self, request: PublicationRequest) -> CandidateOffer | None:
        """Offer *request* as a pull request, or return None when it is blocked."""

    def dispatch_website(self, offer: CandidateOffer, observation: MergeObservation) -> str | None:
        """Dispatch the website for a verified merge, or return None when blocked.

        Returns the merge SHA once it is dispatched, including on a repeat
        call for a SHA already dispatched, which sends nothing further.
        """


@dataclass
class RecordingPublicationPort:
    """In-memory port that records what a real publisher would have done.

    It runs the same two gate functions as the subprocess publisher, so a
    state refused here is refused in production for the same reason.
    """

    head_sha: str = "0" * 40
    pull_request: int = 1
    offers: list[CandidateOffer] = field(default_factory=list)
    dispatches: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)

    def offer_candidate(self, request: PublicationRequest) -> CandidateOffer | None:
        reason = candidate_block_reason(request)
        if reason:
            self.refusals.append(reason)
            return None
        offer = CandidateOffer(
            branch=request.branch,
            head_sha=self.head_sha,
            pull_request=self.pull_request,
            candidate_digest=request.candidate_digest,
            manifest_digest=request.manifest_digest,
            worktree=request.worktree,
        )
        self.offers.append(offer)
        return offer

    def dispatch_website(self, offer: CandidateOffer, observation: MergeObservation) -> str | None:
        reason = merge_block_reason(offer, observation)
        if reason:
            self.refusals.append(reason)
            return None
        if observation.merge_sha not in self.dispatches:
            self.dispatches.append(observation.merge_sha)
        return observation.merge_sha


class GitHubPublisher:
    """Publishes through ``git`` and ``gh`` subprocesses.

    Argv is always a tuple and never a shell string, and both executables are
    resolved through :func:`shutil.which` on every call so a relative name can
    never be handed to the shell.

    Website dispatch is idempotent by verified merge SHA. The SHAs already
    dispatched are appended to *dispatch_record*, which is a file rather than
    process state because a runner segment can end between the merge and the
    retry that observes it.
    """

    def __init__(
        self,
        *,
        website_repository: str,
        dispatch_record: Path,
        remote: str = "origin",
        base_branch: str = "main",
    ) -> None:
        if not website_repository:
            raise PublicationError("website repository must not be empty")
        self._website_repository = website_repository
        self._dispatch_record = dispatch_record
        self._remote = remote
        self._base_branch = base_branch

    def offer_candidate(self, request: PublicationRequest) -> CandidateOffer | None:
        if candidate_block_reason(request):
            return None
        git = _executable("git")
        branch = request.branch
        head_sha = self._run((git, "rev-parse", "--verify", f"refs/heads/{branch}"), cwd=request.worktree).strip()
        self._run(
            (git, "push", "--force-with-lease", self._remote, candidate_refspec(branch)),
            cwd=request.worktree,
        )
        return CandidateOffer(
            branch=branch,
            head_sha=head_sha,
            pull_request=self._ensure_pull_request(request),
            candidate_digest=request.candidate_digest,
            manifest_digest=request.manifest_digest,
            worktree=request.worktree,
        )

    def dispatch_website(self, offer: CandidateOffer, observation: MergeObservation) -> str | None:
        if merge_block_reason(offer, observation):
            return None
        if observation.merge_sha in self._dispatched():
            return observation.merge_sha
        self._run(
            (
                _executable("gh"),
                "api",
                "--method",
                "POST",
                f"repos/{self._website_repository}/dispatches",
                "-f",
                f"event_type={_WEBSITE_EVENT_TYPE}",
                "-f",
                f"client_payload[merge_sha]={observation.merge_sha}",
            ),
            cwd=offer.worktree,
        )
        self._record_dispatch(observation.merge_sha)
        return observation.merge_sha

    def _ensure_pull_request(self, request: PublicationRequest) -> int:
        gh = _executable("gh")
        view = (gh, "pr", "view", request.branch, "--json", "number", "--jq", ".number")
        existing = self._probe(view, cwd=request.worktree)
        if existing is None:
            self._run(
                (
                    gh,
                    "pr",
                    "create",
                    "--head",
                    request.branch,
                    "--base",
                    self._base_branch,
                    "--title",
                    f"chore: refresh corpus from {request.branch}",
                    "--body",
                    f"Candidate digest {request.candidate_digest}\nManifest digest {request.manifest_digest}",
                ),
                cwd=request.worktree,
            )
            existing = self._probe(view, cwd=request.worktree)
        if existing is None:
            raise PublicationError(f"no pull request exists for {request.branch} after creating one")
        try:
            return int(existing.strip())
        except ValueError as exc:
            raise PublicationError(f"pull request number is not an integer: {existing.strip()!r}") from exc

    def _dispatched(self) -> set[str]:
        if not self._dispatch_record.is_file():
            return set()
        return {line.strip() for line in self._dispatch_record.read_text(encoding="utf-8").splitlines() if line.strip()}

    def _record_dispatch(self, merge_sha: str) -> None:
        self._dispatch_record.parent.mkdir(parents=True, exist_ok=True)
        with self._dispatch_record.open("a", encoding="utf-8") as handle:
            handle.write(f"{merge_sha}\n")

    @staticmethod
    def _run(argv: tuple[str, ...], *, cwd: Path) -> str:
        try:
            completed = subprocess.run(argv, cwd=cwd, check=True, capture_output=True, text=True)  # noqa: S603
        except subprocess.CalledProcessError as exc:
            raise PublicationError(f"{argv[0]} {argv[1]} failed with exit {exc.returncode}") from exc
        return completed.stdout

    @staticmethod
    def _probe(argv: tuple[str, ...], *, cwd: Path) -> str | None:
        """Run *argv* where a non-zero exit is an answer rather than a failure.

        Asking whether a pull request already exists is the only such call.
        ``gh pr view`` exits non-zero when the branch has none, and that is
        what makes a repeated offer create one pull request instead of two.
        """
        completed = subprocess.run(argv, cwd=cwd, check=False, capture_output=True, text=True)  # noqa: S603
        return None if completed.returncode else completed.stdout


def _executable(name: str) -> str:
    """Resolve *name* to an absolute path so no bare command name is executed."""
    found = shutil.which(name)
    if found is None:
        raise PublicationError(f"{name} is not on PATH")
    return found
