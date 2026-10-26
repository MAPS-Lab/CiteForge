from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monthly-refresh.yml"


def test_refresh_pr_uses_rest_creation_with_explicit_app_permissions() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "permission-contents: write" in workflow
    assert "permission-pull-requests: write" in workflow
    assert "repos/${{ github.repository }}/pulls" in workflow
    assert "--method POST" in workflow
    assert "gh pr create" not in workflow


def test_refresh_pr_waits_for_monthly_citation_audit() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "gh pr merge" not in workflow
    assert "monthly citation audit" in workflow.lower()


def test_refresh_manifest_uses_reproducible_author_counts() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'echo "enabled_authors=' in workflow
    assert 'echo "authors_with_bib=' in workflow
    assert 'echo "authors=$(find output -mindepth 1' not in workflow
