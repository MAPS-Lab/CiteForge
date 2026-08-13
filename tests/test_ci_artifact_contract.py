"""Contracts for the artifacts the required test workflow builds and consumes.

Required CI is one workflow that has to produce the distribution once, verify
it once, and gate every job behind a single check. The failures this file
guards against all pass a green run: a second wheel producer means the
distribution CI verified is not the one it smoke-tested, an editable consumer
install proves nothing about the built artifact, a lock recompiled but not
diffed lets a stale pin ride, and a job missing from the aggregate gate is
simply not required.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from tests.test_workflow_contracts import _load_workflow, _shell_statements, _workflow_run_commands, _workflow_steps

_TESTS_WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/tests.yml"

# Every interpreter the project claims to run on. A leg dropped from the matrix
# is a version nobody tests, and requires-python still advertises it.
_SUPPORTED_PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]

# The one version that carries coverage instrumentation for the whole suite.
_COVERAGE_VERSION = "3.12"


def _steps_running(workflow: dict[str, Any], fragment: str) -> list[dict[str, Any]]:
    """Return every step whose shell command contains the fragment."""
    return [step for step in _workflow_steps(workflow) if fragment in step.get("run", "")]


class TestPackageArtifact:
    """One producer builds the wheel, and the same wheel is what gets verified."""

    def test_exactly_one_step_builds_the_wheel(self) -> None:
        """Two producers verify one build and ship the other."""
        workflow = _load_workflow(_TESTS_WORKFLOW)
        producers = _steps_running(workflow, "pip wheel")

        assert len(producers) == 1

    def test_the_smoke_test_installs_the_wheel_the_producer_wrote(self) -> None:
        """A consumer reading a different directory verifies a different artifact."""
        workflow = _load_workflow(_TESTS_WORKFLOW)
        producer = _steps_running(workflow, "pip wheel")[0]
        wheel_dir = re.search(r"--wheel-dir\s+(\S+)", producer["run"])
        consumers = _steps_running(workflow, ".whl")

        assert wheel_dir is not None
        assert len(consumers) == 1
        assert wheel_dir.group(1) in consumers[0]["run"]

    def test_the_wheel_is_imported_from_outside_the_checkout(self) -> None:
        """Inside the checkout the source tree satisfies the import, so a broken
        distribution passes and only fails on install."""
        steps = _load_workflow(_TESTS_WORKFLOW)["jobs"]["lint"]["steps"]
        smoke = [step for step in steps if "smoke" in step.get("name", "").lower()]

        assert len(smoke) == 1
        command = smoke[0]["run"]
        assert "mktemp -d" in command
        assert "python -m venv" in command
        assert "cd " in command
        assert "import citeforge, citeforge.cli" in command

    def test_the_smoke_test_consumer_is_not_an_editable_install(self) -> None:
        """An editable install points back at the checkout, so packaging breaks
        (a module left out of the distribution, a missing entry point) survive it."""
        workflow = _load_workflow(_TESTS_WORKFLOW)
        consumer = _steps_running(workflow, ".whl")[0]
        installs = [line for line in _shell_statements([consumer["run"]]) if "pip" in line and " install " in line]

        assert installs
        for install in installs:
            assert " -e " not in install, install
            assert "--editable" not in install, install


class TestDependencyDigests:
    """Nothing installs unverified bytes, and no lock ships stale."""

    def test_every_locked_install_verifies_hashes(self) -> None:
        """Without --require-hashes a compromised or moved index entry installs silently."""
        statements = _shell_statements(_workflow_run_commands(_load_workflow(_TESTS_WORKFLOW)))
        locked = [line for line in statements if "pip" in line and " install " in line and ".lock" in line]

        assert locked
        for install in locked:
            assert "--require-hashes" in install, install

    def test_every_recompiled_lock_is_diffed_against_the_committed_one(self) -> None:
        """Recompiling without the diff reports nothing. A lock compiled but left
        out of the diff list is a pin that drifts with no gate on it."""
        workflow = _load_workflow(_TESTS_WORKFLOW)
        freshness = _steps_running(workflow, "uv pip compile")

        assert len(freshness) == 1
        command = freshness[0]["run"]
        compiled = re.findall(r"-o\s+(\S+)", command)
        diffed = re.search(r"git diff --exit-code --(.*)", command)

        assert compiled
        assert diffed is not None
        for lock in compiled:
            assert lock in diffed.group(1), lock

    def test_lint_cache_fallbacks_stay_inside_the_lint_prefix(self) -> None:
        """A bare `-pip-` fallback restores whatever another leg wrote."""
        steps = _load_workflow(_TESTS_WORKFLOW)["jobs"]["lint"]["steps"]
        caches = [step for step in steps if "actions/cache" in step.get("uses", "")]

        assert len(caches) == 1
        restore_keys = [line.strip() for line in caches[0]["with"]["restore-keys"].splitlines() if line.strip()]
        assert restore_keys
        for key in restore_keys:
            assert key.endswith("-pip-lint-"), key


class TestCompatibilityMatrix:
    """Independent legs run at once, and exactly one of them owns coverage."""

    def test_matrix_runs_every_supported_version_concurrently(self) -> None:
        """Serialized legs cost the sum of their wall time and hide later failures."""
        strategy = _load_workflow(_TESTS_WORKFLOW)["jobs"]["test"]["strategy"]

        assert strategy["matrix"]["python-version"] == _SUPPORTED_PYTHON_VERSIONS
        assert strategy["fail-fast"] is False
        assert "max-parallel" not in strategy

    def test_exactly_one_version_owns_coverage(self) -> None:
        """Coverage is a property of the suite; instrumenting five legs buys nothing."""
        steps = _load_workflow(_TESTS_WORKFLOW)["jobs"]["test"]["steps"]
        instrumented = [step for step in steps if "--cov=" in step.get("run", "")]
        plain = [
            step for step in steps if step.get("run", "").startswith("pytest") and "--cov=" not in step.get("run", "")
        ]

        assert len(instrumented) == 1
        assert instrumented[0]["if"] == f"matrix.python-version == '{_COVERAGE_VERSION}'"
        assert len(plain) == 1
        assert plain[0]["if"] == f"matrix.python-version != '{_COVERAGE_VERSION}'"

    def test_the_uninstrumented_legs_run_the_same_selection(self) -> None:
        """Deduplicating coverage must not deduplicate tests. Both legs run the
        whole hermetic suite, and only the instrumentation differs."""
        steps = _load_workflow(_TESTS_WORKFLOW)["jobs"]["test"]["steps"]
        selections = [
            re.sub(r"\s--cov\S*", "", step["run"]).strip() for step in steps if step.get("run", "").startswith("pytest")
        ]

        assert len(selections) == 2
        assert selections[0] == selections[1]


class TestAggregateGate:
    """Branch protection requires one check, so that check must cover everything."""

    def test_the_aggregate_gate_covers_every_job(self) -> None:
        """A job absent from `needs` is a job nothing requires."""
        workflow = _load_workflow(_TESTS_WORKFLOW)
        gate = workflow["jobs"]["CI"]

        assert gate["name"] == "Required CI"
        assert gate["if"] == "always()"
        assert set(gate["needs"]) == set(workflow["jobs"]) - {"CI"}

    def test_the_gate_reads_its_dependencies_rather_than_a_hand_written_list(self) -> None:
        """Enumerating jobs in the script means joining `needs` is not enough to be
        gated, which is the omission the single gate exists to make impossible."""
        gate = _load_workflow(_TESTS_WORKFLOW)["jobs"]["CI"]
        checks = [step for step in gate["steps"] if step.get("run")]

        assert len(checks) == 1
        assert checks[0]["env"]["NEEDS"] == "${{ toJSON(needs) }}"

    def test_the_gate_fails_on_an_empty_or_skipped_dependency(self) -> None:
        """No job here carries an `if:`, so `skipped` means a dependency never ran.
        An empty `needs` would otherwise report success having checked nothing."""
        gate = _load_workflow(_TESTS_WORKFLOW)["jobs"]["CI"]
        script = gate["steps"][0]["run"]

        assert "length" in script
        assert "skipped)" in script
        assert script.count("exit 1") >= 1
