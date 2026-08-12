"""Structural contracts for GitHub Actions workflows."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_MONTHLY_REFRESH_WORKFLOW = _REPOSITORY_ROOT / ".github/workflows/monthly-refresh.yml"


def _load_workflow(path: Path) -> dict[str, Any]:
    """Load a GitHub Actions document with a YAML 1.2 parser."""
    loaded = YAML(typ="safe").load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


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

    assert "on" in workflow
    assert workflow["concurrency"] == {"group": "citeforge-refresh", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "read"}
    assert "git push" not in commands
    assert "HEAD:main" not in text
    assert "mapslab-website" not in text
    assert "/dispatches" not in commands
    assert "converged" not in text
