"""Structural contracts for GitHub Actions workflows."""

from __future__ import annotations

import re
import shutil
import sqlite3
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from ruamel.yaml import YAML

from citeforge.refresh.census import AuthorCensus, AuthorCensusRow
from citeforge.refresh.ledger import Ledger
from citeforge.refresh.types import GenerationSpec, TaskDisposition

# Resolved rather than spelled, so the path is absolute on any host.
_BASH = shutil.which("bash") or "/bin/bash"

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOW_DIRECTORY = _REPOSITORY_ROOT / ".github/workflows"
# Both suffixes, so a document added as `.yaml` cannot sit outside every
# contract below while GitHub still runs it.
_WORKFLOW_PATHS = sorted(path for path in _WORKFLOW_DIRECTORY.iterdir() if path.suffix in {".yml", ".yaml"})
_MONTHLY_REFRESH_WORKFLOW = _WORKFLOW_DIRECTORY / "monthly-refresh.yml"
_MERGED_REFRESH_WORKFLOW = _WORKFLOW_DIRECTORY / "citeforge-refresh-merged.yml"
# Located by shape rather than by name. A shadow is whichever workflow calls
# itself one, so a sensible rename keeps its contracts instead of silently
# dropping them.
_SHADOW_WORKFLOWS = [path for path in _WORKFLOW_PATHS if "shadow" in path.stem]

# The one workflow allowed to hold a write token and mint the App credential.
# An allowlist by name fails loudly on a rename, which is the correct outcome
# for a privilege grant.
_PUBLISHING_WORKFLOWS = {_MONTHLY_REFRESH_WORKFLOW.name}

# What marks the one step that leases a generation segment. The invocation is
# built as an argument array in one workflow and written out in another, so the
# required flag identifies it where the command name does not.
_SEGMENT_FLAG = "--state-dir"

# A refspec naming the default branch, in any of the forms a push can take:
# `HEAD:main`, `origin main`, `HEAD:refs/heads/master`.
_DEFAULT_BRANCH = re.compile(r"\b(?:main|master)\b")


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load a GitHub Actions document with a YAML 1.2 parser."""
    loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _workflow_steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    """Return every step declared by every job."""
    steps: list[dict[str, Any]] = []
    for job in workflow["jobs"].values():
        steps.extend(job.get("steps", []))
    return steps


def _workflow_run_commands(workflow: dict[str, Any]) -> list[str]:
    """Return every shell command declared in workflow job steps."""
    commands: list[str] = []
    for step in _workflow_steps(workflow):
        if command := step.get("run"):
            commands.append(command)
    return commands


def _shell_statements(commands: Iterable[str]) -> list[str]:
    """Return the executable shell lines, continuations joined and comments dropped.

    A push or a pipeline split across continuation lines is one statement to
    the shell, so a contract asserted line by line would read only its head.
    """
    statements: list[str] = []
    for command in commands:
        joined = command.replace("\\\n", " ")
        for line in joined.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                statements.append(stripped)
    return statements


def _shadow_workflow() -> Path:
    """Return the one shadow workflow, failing plainly when none was written."""
    assert len(_SHADOW_WORKFLOWS) == 1, f"expected one *shadow* workflow, found {[p.name for p in _SHADOW_WORKFLOWS]}"
    return _SHADOW_WORKFLOWS[0]


def _declared_permissions(workflow: dict[str, Any]) -> list[Any]:
    """Return the top-level permission block and every job's own block."""
    blocks = [workflow.get("permissions")]
    blocks.extend(job.get("permissions") for job in workflow["jobs"].values())
    return [block for block in blocks if block is not None]


class TestWorkflowDocuments:
    """Contracts every workflow in the repository must satisfy."""

    def test_workflow_directory_is_not_empty(self) -> None:
        """A glob that matched nothing would make every parametrized case vacuous."""
        assert _WORKFLOW_PATHS

    @pytest.mark.parametrize("path", _WORKFLOW_PATHS, ids=[path.name for path in _WORKFLOW_PATHS])
    def test_workflow_parses_and_declares_least_privilege(self, path: Path) -> None:
        """Every document parses, runs at least one job, and starts read-only."""
        workflow = _load_workflow(path)

        assert "on" in workflow
        assert workflow["jobs"]
        # Without a top-level block the job token carries the repository
        # default, which is write on many organizations. Escalation belongs on
        # the one job that needs it.
        assert workflow["permissions"] == {"contents": "read"}

    @pytest.mark.parametrize("path", _WORKFLOW_PATHS, ids=[path.name for path in _WORKFLOW_PATHS])
    def test_no_push_resolves_to_the_default_branch(self, path: Path) -> None:
        """A push to main lands the corpus without Required CI ever running,
        which is the one outcome the whole publication design exists to prevent.
        """
        pushes = [
            line for line in _shell_statements(_workflow_run_commands(_load_workflow(path))) if "git push" in line
        ]

        for push in pushes:
            assert not _DEFAULT_BRANCH.search(push), push

    @pytest.mark.parametrize("path", _WORKFLOW_PATHS, ids=[path.name for path in _WORKFLOW_PATHS])
    def test_a_forced_push_states_its_lease_explicitly(self, path: Path) -> None:
        """These pushes name a URL, not a remote, so no remote-tracking ref exists
        and a bare --force-with-lease has nothing to compare against. It passes
        while the branch is absent and is rejected as "stale info" once it is
        not, which silently stopped every re-triggered refresh from publishing.
        """
        pushes = [
            line for line in _shell_statements(_workflow_run_commands(_load_workflow(path))) if "git push" in line
        ]

        for push in pushes:
            if "--force" not in push:
                continue
            assert "--force-with-lease=" in push, push

    @pytest.mark.parametrize("path", _WORKFLOW_PATHS, ids=[path.name for path in _WORKFLOW_PATHS])
    def test_only_the_publisher_holds_write_privilege(self, path: Path) -> None:
        """The App token can merge its own pull request, so a workflow that runs
        provider code for hours must not be able to reach it."""
        if path.name in _PUBLISHING_WORKFLOWS:
            return
        text = path.read_text(encoding="utf-8")

        for block in _declared_permissions(_load_workflow(path)):
            assert "write" not in str(block), f"{path.name}: {block}"
        assert "create-github-app-token" not in text, path.name
        assert "CI_BOT_APP_ID" not in text, path.name
        assert "CI_BOT_PRIVATE_KEY" not in text, path.name


class TestMonthlyRefresh:
    """The monthly refresh may publish, but only by offering a pull request."""

    def test_every_shell_block_in_every_workflow_is_balanced(self) -> None:
        """A run block must parse as a shell script.

        Deleting an `if ! save_cache ...; then` line while leaving its body took
        the `fi` that closed the enclosing failure branch. The counts still
        balanced, so nothing caught it, but the digest computation moved inside
        the failure branch and every successful run compared two unset variables
        and declared convergence after a single pass. `bash -n` sees the pairing
        that a count cannot.
        """
        for path in _WORKFLOW_PATHS:
            for job_id, job in (_load_workflow(path).get("jobs") or {}).items():
                for index, step in enumerate(job.get("steps") or []):
                    script = step.get("run")
                    if not script or script.lstrip().startswith("python"):
                        continue
                    # `${{ }}` is not shell, so it is neutralised before parsing.
                    neutral = re.sub(r"\$\{\{[^}]*\}\}", "EXPR", script)
                    # -n parses without executing, and the input is this
                    # repository's own workflow files.
                    result = subprocess.run(  # noqa: S603
                        [_BASH, "-n"], input=neutral, capture_output=True, text=True, check=False
                    )
                    assert result.returncode == 0, (
                        f"{path.name}:{job_id}[{index}] {step.get('name', '')}: {result.stderr.strip()}"
                    )

    def test_convergence_requires_a_confirming_pass(self) -> None:
        """An empty digest must never satisfy the convergence test.

        `prev_digest` starts empty, so a bare `[ "$digest" = "$prev_digest" ]`
        is true on the first iteration whenever `digest` is also empty, which
        publishes a corpus no second pass has confirmed.
        """
        loop = next(
            step["run"]
            for step in _workflow_steps(_load_workflow(_MONTHLY_REFRESH_WORKFLOW))
            if step.get("id") == "loop"
        )
        comparison = [line for line in _shell_statements([loop]) if '"$digest" = "$prev_digest"' in line]

        assert comparison, "the convergence comparison is gone"
        for line in comparison:
            assert '-n "$digest"' in line, f"convergence must reject an empty digest: {line}"

    def test_publication_retries_and_preserves_the_corpus(self) -> None:
        """An hour of provider quota must not die with a transient API error.

        A platform incident returned 403 on the push after a converged run and
        the corpus was discarded, because it lives only on the runner.
        """
        workflow = _load_workflow(_MONTHLY_REFRESH_WORKFLOW)
        statements = _shell_statements(_workflow_run_commands(workflow))

        pushes = [line for line in statements if "git push" in line]
        assert pushes and all(line.lstrip().startswith("retry ") for line in pushes), pushes

        preserving = [
            step
            for step in _workflow_steps(workflow)
            if "upload-artifact" in str(step.get("uses", "")) and "output/" in str(step.get("with", {}).get("path"))
        ]
        assert preserving, "no step preserves output/ when publication fails"
        assert "failure()" in str(preserving[0].get("if")), preserving[0]

    def test_publication_failure_does_not_end_the_run(self) -> None:
        """A converged run that could not publish must retry, not dead-end.

        The re-trigger required success and the failure branch merely exited 1,
        so a converged-but-unpublished run did neither and its corpus was lost.
        """
        verify = _load_workflow(_MONTHLY_REFRESH_WORKFLOW)["jobs"]["verify"]["steps"]
        retrigger = next(step for step in verify if "Re-trigger" in step.get("name", ""))
        failing = next(step for step in verify if step.get("name", "").startswith("Fail if"))

        assert "converged == 'true'" in str(retrigger.get("if")), retrigger.get("if")
        # Only an unconverged run is a real failure.
        assert "converged != 'true'" in str(failing.get("if")), failing.get("if")

    def test_publication_offers_a_candidate_pull_request(self) -> None:
        """The corpus reaches main through a branch Required CI has passed. The
        offer may come from the shell or from `citeforge.refresh.publication`,
        but a run that offers nothing publishes nothing."""
        workflow = _load_workflow(_MONTHLY_REFRESH_WORKFLOW)
        commands = _workflow_run_commands(workflow)
        offers = [
            step
            for step in _workflow_steps(workflow)
            if "pull request" in step.get("name", "").lower() or "gh pr create" in step.get("run", "")
        ]

        assert offers
        # An unconditional merge bypasses the check Required CI exists to enforce.
        for merge in [line for line in _shell_statements(commands) if "gh pr merge" in line]:
            assert "--auto" in merge, merge

    @pytest.mark.skip(
        reason="Cutover contract, blocked rather than pending. RunStatus.COMPLETE is "
        "unreachable by design: it needs discovery_closed = 1, which two schema triggers "
        "abort, because Task 5A may not assert that an author's publication list is "
        "complete. The monthly workflow therefore runs the legacy pipeline, and the shadow "
        "runs bounded multi-segment with ephemeral state. Unskip only if that authority "
        "invariant is deliberately lifted, never by deleting the fence to make this pass."
    )
    def test_one_generation_segment_per_run(self) -> None:
        """Asserted against the SHADOW workflow, which is where the engine runs.

        The monthly workflow runs the legacy pipeline until the shadow generation
        passes, per the architecture document's cutover rule. Pointing this at
        the monthly file would assert a cutover that has not happened.
        """
        """The retired workflow swept the whole corpus in a loop until a digest
        stopped moving, which cost the six-hour ceiling and proved nothing about
        completeness. A run now leases one bounded segment and the ledger, not
        the workflow, decides whether the generation is done."""
        statements = _shell_statements(_workflow_run_commands(_load_workflow(_shadow_workflow())))
        segments = [line for line in statements if _SEGMENT_FLAG in line]

        assert len(segments) == 1
        # The three artifacts the retired loop left behind: its pass ceiling, its
        # pass loop, and its digest convergence test. A retry around one flaky
        # push is a different thing and stays allowed.
        for line in statements:
            assert "max_runs" not in line, line
            assert "$(seq" not in line, line
            assert "prev_digest" not in line, line

    @pytest.mark.skip(
        reason="Cutover contract, blocked rather than pending. RunStatus.COMPLETE is "
        "unreachable by design: it needs discovery_closed = 1, which two schema triggers "
        "abort, because Task 5A may not assert that an author's publication list is "
        "complete. The monthly workflow therefore runs the legacy pipeline, and the shadow "
        "runs bounded multi-segment with ephemeral state. Unskip only if that authority "
        "invariant is deliberately lifted, never by deleting the fence to make this pass."
    )
    def test_the_checkpoint_is_restored_before_the_segment_and_saved_after(self) -> None:
        """Also the shadow workflow, for the same reason."""
        """A segment starting from nothing repeats work the previous run paid
        for, and one that never saves leaves the next run to start from nothing."""
        workflow = _load_workflow(_shadow_workflow())
        jobs = [
            job
            for job in workflow["jobs"].values()
            if any(_SEGMENT_FLAG in step.get("run", "") for step in job.get("steps", []))
        ]

        assert len(jobs) == 1
        steps = jobs[0]["steps"]
        segment = next(index for index, step in enumerate(steps) if _SEGMENT_FLAG in step.get("run", ""))
        # Shape-agnostic on purpose: a restore is a cache, an artifact download
        # or a fetch, and what the contract needs is one before and one after.
        handlers = [index for index, step in enumerate(steps) if index != segment and "checkpoint" in str(step).lower()]

        assert any(index < segment for index in handlers), "nothing restores the previous checkpoint"
        assert any(index > segment for index in handlers), "nothing saves the checkpoint this run wrote"

    def test_the_encrypted_data_cache_branch_is_gone(self) -> None:
        """Raw provider responses rode a public orphan branch through an AES-CBC
        monolith and a force push. The ledger and the sealed checkpoint replace
        it, and keeping it as a fallback would keep all three."""
        text = _shadow_workflow().read_text(encoding="utf-8")

        assert "data-cache" not in text
        assert "openssl enc" not in text

    def test_website_dispatch_is_not_reachable_from_the_refresh(self) -> None:
        """The corpus reaching main is the publication event, not convergence."""
        text = _shadow_workflow().read_text(encoding="utf-8")
        commands = "\n".join(_workflow_run_commands(_load_workflow(_shadow_workflow())))

        assert "mapslab-website" not in text
        assert "/dispatches" not in commands

    def test_generations_are_serialized_and_privilege_is_declared_per_job(self) -> None:
        """Two overlapping runs would race the same candidate branch. A job with
        no block of its own inherits the repository default, which is write on
        many organizations, so every job states what it needs."""
        workflow = _load_workflow(_MONTHLY_REFRESH_WORKFLOW)

        assert workflow["concurrency"] == {"group": "citeforge-refresh", "cancel-in-progress": False}
        assert workflow["permissions"] == {"contents": "read"}
        for name, job in workflow["jobs"].items():
            assert "permissions" in job, name

    def test_uploaded_artifacts_carry_no_provider_urls(self) -> None:
        """The run log records request URLs, and query-string providers key on them."""
        uploads = [
            step
            for step in _workflow_steps(_load_workflow(_MONTHLY_REFRESH_WORKFLOW))
            if "upload-artifact" in step.get("uses", "")
        ]

        assert uploads, "a refresh that publishes nothing still reports what it did"
        for step in uploads:
            path = step["with"]["path"]
            assert "run.log" not in path, path
            assert "api_cache" not in path, path
            assert "keys" not in path, path


class TestMergedRefresh:
    """Website synchronization fires from the merge, not from convergence."""

    def test_trigger_is_a_closed_pull_request_on_main(self) -> None:
        workflow = _load_workflow(_MERGED_REFRESH_WORKFLOW)

        assert workflow["on"]["pull_request"]["types"] == ["closed"]
        assert workflow["on"]["pull_request"]["branches"] == ["main"]

    def test_dispatch_requires_a_merged_refresh_candidate(self) -> None:
        """A closed-unmerged pull request, or any other branch, dispatches nothing."""
        workflow = _load_workflow(_MERGED_REFRESH_WORKFLOW)
        guard = workflow["jobs"]["sync-website"]["if"]

        assert "github.event.pull_request.merged == true" in guard
        assert "startsWith(github.event.pull_request.head.ref, 'data/refresh-')" in guard

    def test_dispatch_verifies_the_merge_commit_before_sending(self) -> None:
        """merge_commit_sha is null until GitHub computes it, and null publishes nothing."""
        workflow = _load_workflow(_MERGED_REFRESH_WORKFLOW)
        commands = "\n".join(_workflow_run_commands(workflow))

        assert "[0-9a-f]{40}" in commands
        assert "client_payload[merge_sha]=${MERGE_SHA}" in commands
        assert "repos/MAPS-Lab/mapslab-website/dispatches" in commands

    def test_the_dispatcher_neither_writes_nor_pushes(self) -> None:
        workflow = _load_workflow(_MERGED_REFRESH_WORKFLOW)
        commands = "\n".join(_workflow_run_commands(workflow))

        assert workflow["permissions"] == {"contents": "read"}
        assert workflow["concurrency"]["cancel-in-progress"] is False
        assert "git push" not in commands


class TestRefreshShadow:
    """The shadow generation runs against live providers and publishes nothing."""

    def test_exactly_one_shadow_workflow_exists(self) -> None:
        """A live generation is the engine's one remaining unproven requirement,
        and the provider credentials for one exist only in Actions."""
        assert _SHADOW_WORKFLOWS, "no *shadow* workflow in .github/workflows"
        assert len(_SHADOW_WORKFLOWS) == 1, _SHADOW_WORKFLOWS

    def test_the_shadow_runs_a_generation(self) -> None:
        """A shadow that runs nothing satisfies every prohibition below while
        proving nothing at all."""
        commands = "\n".join(_workflow_run_commands(_load_workflow(_shadow_workflow())))

        assert "--state-dir" in commands

    def test_the_shadow_is_dispatched_by_hand(self) -> None:
        """A schedule would put an unproven generation against live providers on
        a timer, spending quota nobody is watching."""
        workflow = _load_workflow(_shadow_workflow())

        assert set(workflow["on"]) == {"workflow_dispatch"}

    def test_the_shadow_cannot_publish(self) -> None:
        """A shadow that can push, open, or merge is not a shadow. Its whole
        value is that a real run against real providers mutates nothing."""
        path = _shadow_workflow()
        workflow = _load_workflow(path)
        commands = "\n".join(_workflow_run_commands(workflow))

        for block in _declared_permissions(workflow):
            assert "write" not in str(block), block
        # The App credential is what can merge past a code-owner review, so a
        # workflow running provider code for hours must not be able to reach it.
        assert "create-github-app-token" not in path.read_text(encoding="utf-8")
        assert "git push" not in commands
        assert "gh pr create" not in commands
        assert "gh pr merge" not in commands
        assert "/dispatches" not in commands


def test_no_workflow_gates_on_a_status_the_ledger_forbids_producing() -> None:
    """No workflow may make publication wait on `complete`.

    `RunStatus.COMPLETE` requires `Ledger.all_required_satisfied()`, which
    requires `generations.discovery_closed = 1`. Two schema triggers abort any
    statement that sets it, `_assert_task5a_authority_invariant` re-checks it on
    every status read, manifest read and reopen, and the schema fingerprint
    rejects a database whose triggers were dropped. Discovery authority is the
    claim that an author's publication list is complete, and Task 5A is
    deliberately not entitled to make it.

    So a workflow gating on `complete` does not wait for a slow generation, it
    waits forever and publishes nothing. Gate on evidence the ledger can
    produce; never remove the fence to make a gate pass.
    """
    for path in _WORKFLOW_PATHS:
        for statement in _shell_statements(_workflow_run_commands(_load_workflow(path))):
            if "complete" not in statement:
                continue
            # A notice or an echo may say the word. A conditional may not branch on it.
            assert not re.search(r'(?:status|STATUS)[^\n]*(?:=|-eq)[^\n]*["\x27]?complete', statement), (
                f"{path.name} gates on a status the ledger cannot produce: {statement}"
            )


def test_the_ledger_refuses_to_close_discovery(tmp_path: Path) -> None:
    """The premise of the contract above, exercised rather than asserted.

    Pinned here as well as in the ledger suite because it is what licenses the
    monthly workflow to run the legacy pipeline. If this ever starts passing,
    that workflow's cutover comment is stale and its gate can be revisited.
    """
    row = AuthorCensusRow(
        physical_row=2,
        row_key="author-ada",
        name="Ada Lovelace",
        normalized_name="ada lovelace",
        scholar_id="Scholar123",
        dblp_id="",
        enabled=True,
        exclusion_reason="",
        disposition=TaskDisposition.PENDING,
    )
    census = AuthorCensus((row,))
    path = tmp_path / "authority.db"

    with Ledger.open(path) as ledger:
        ledger.create_or_resume(GenerationSpec(census, "policy-v1", {"scholar": "1"}, "abc123"), census)
        assert not ledger.all_required_satisfied()

    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="Task 5A"):
            connection.execute("UPDATE generations SET discovery_closed = 1")
    finally:
        connection.close()


@pytest.mark.parametrize("path", _WORKFLOW_PATHS, ids=lambda item: item.name)
def test_job_level_env_does_not_reference_the_runner_context(path: Path) -> None:
    """`runner` is unavailable at jobs.<job_id>.env and resolves to nothing there.

    GitHub grants that scope only github, needs, strategy, matrix, vars, secrets
    and inputs; `runner` arrives one level down at jobs.<job_id>.steps.env. A
    path built from `${{ runner.temp }}` in a job-level env block silently
    collapses to its suffix, so the first mkdir fails on the runner rather than
    at parse time.
    """
    document = _load_workflow(path)
    for job_id, job in (document.get("jobs") or {}).items():
        for name, value in (job.get("env") or {}).items():
            assert "runner." not in str(value), f"{path.name}:{job_id}.env.{name} reads the runner context"
