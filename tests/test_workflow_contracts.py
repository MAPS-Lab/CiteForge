"""Structural contracts for GitHub Actions workflows."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MONTHLY_REFRESH_WORKFLOW = _REPOSITORY_ROOT / ".github/workflows/monthly-refresh.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load a GitHub Actions YAML document through the available YAML parser."""
    yq = shutil.which("yq")
    assert yq is not None, "workflow contract tests require yq"
    result = subprocess.run(  # noqa: S603
        [yq, "--output-format=json", ".", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def _workflow_run_commands(workflow: dict[str, Any]) -> list[str]:
    """Return every shell command declared in workflow job steps."""
    commands: list[str] = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if command := step.get("run"):
                commands.append(command)
    return commands


def test_legacy_monthly_refresh_is_diagnostic_only() -> None:
    """The legacy refresh may gather diagnostics but must never publish them."""
    text = _MONTHLY_REFRESH_WORKFLOW.read_text(encoding="utf-8")
    workflow = _load_workflow(_MONTHLY_REFRESH_WORKFLOW)
    commands = "\n".join(_workflow_run_commands(workflow))

    assert workflow["concurrency"] == {"group": "citeforge-refresh", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "read"}
    assert "git push" not in commands
    assert "HEAD:main" not in text
    assert "mapslab-website" not in text
    assert "/dispatches" not in commands
    assert "converged" not in text
