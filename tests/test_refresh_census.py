from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from citeforge.io_utils import read_records
from citeforge.refresh.census import load_census
from citeforge.refresh.types import GenerationSpec, TaskDisposition


def _write_census(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("enabled", ["", "maybe"])
def test_rejects_unclassified_rows(tmp_path: Path, enabled: str) -> None:
    path = _write_census(tmp_path / "authors.csv", [f"Ada Lovelace,https://example.test/?user=ada,,{enabled},"])

    with pytest.raises(ValueError, match="Enabled"):
        load_census(path)


def test_rejects_blank_physical_rows(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", ["Ada Lovelace,https://example.test/?user=ada,,true,", ""])

    with pytest.raises(ValueError, match="row 3"):
        load_census(path)


def test_rejects_disabled_row_without_reason(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", ["Ada Lovelace,,,false,"])

    with pytest.raises(ValueError, match="Exclusion Reason"):
        load_census(path)


def test_rejects_enabled_row_without_identifier(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", ["Ada Lovelace,,,true,"])

    with pytest.raises(ValueError, match="identifier"):
        load_census(path)


def test_rejects_empty_name_with_identifier(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", [",https://example.test/?user=ada,,true,"])

    with pytest.raises(ValueError, match="Name"):
        load_census(path)


def test_rejects_duplicate_normalized_identity(tmp_path: Path) -> None:
    path = _write_census(
        tmp_path / "authors.csv",
        [
            "Ada  Lovelace,https://example.test/?user=ada,,true,",
            "  ADA LOVELACE  ,https://example.test/?user=ada,,true,",
        ],
    )

    with pytest.raises(ValueError, match="duplicate normalized identity"):
        load_census(path)


def test_records_explicit_exclusion(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", ["Ada Lovelace,,,false,No public profile configured"])

    census = load_census(path)

    assert census.total_count == 1
    assert census.enabled_count == 0
    assert census.excluded_count == 1
    assert census.invalid_count == 0
    assert census.rows[0].disposition is TaskDisposition.NOT_APPLICABLE
    assert census.rows[0].exclusion_reason == "No public profile configured"


def test_legacy_records_delegate_to_census_and_preserve_enabled_order(tmp_path: Path) -> None:
    path = _write_census(
        tmp_path / "authors.csv",
        [
            "Grace Hopper,,https://dblp.org/pid/12/345,true,",
            "Excluded Author,,,false,No public profile configured",
            "Ada Lovelace,https://example.test/?user=ada,,true,",
        ],
    )

    records = read_records(str(path))

    assert [(record.name, record.scholar_id, record.dblp) for record in records] == [
        ("Grace Hopper", "", "12/345"),
        ("Ada Lovelace", "ada", ""),
    ]


def test_row_keys_survive_order_and_whitespace_but_change_with_identity(tmp_path: Path) -> None:
    first = _write_census(
        tmp_path / "first.csv",
        [
            "Ada Lovelace,https://example.test/?user=ada,,true,",
            "Grace Hopper,,https://dblp.org/pid/12/345,true,",
        ],
    )
    reordered = _write_census(
        tmp_path / "reordered.csv",
        [
            "  Grace   Hopper  ,,https://dblp.org/pid/12/345,true,",
            " Ada   Lovelace , https://example.test/?user=ada ,,true,",
        ],
    )
    changed = _write_census(tmp_path / "changed.csv", ["Ada Lovelace,https://example.test/?user=other,,true,"])

    first_keys = {row.normalized_name: row.row_key for row in load_census(first).rows}
    reordered_keys = {row.normalized_name: row.row_key for row in load_census(reordered).rows}

    assert first_keys == reordered_keys
    assert load_census(changed).rows[0].row_key != first_keys["ada lovelace"]


def test_generation_id_is_canonical_and_materially_sensitive(tmp_path: Path) -> None:
    first = load_census(
        _write_census(
            tmp_path / "first.csv",
            [
                "Ada Lovelace,https://example.test/?user=ada,,true,",
                "Grace Hopper,,https://dblp.org/pid/12/345,true,",
            ],
        )
    )
    reordered = load_census(
        _write_census(
            tmp_path / "reordered.csv",
            [
                "Grace Hopper,,https://dblp.org/pid/12/345,true,",
                "Ada Lovelace,https://example.test/?user=ada,,true,",
            ],
        )
    )

    spec = GenerationSpec(first, "policy-v1", {"scholar": "2", "dblp": "1"}, "abc123")
    equivalent = GenerationSpec(reordered, "policy-v1", {"dblp": "1", "scholar": "2"}, "abc123")

    assert spec.id == equivalent.id
    assert len(spec.id) == 64
    assert GenerationSpec(first, "policy-v2", spec.adapter_versions, "abc123").id != spec.id
    assert GenerationSpec(first, "policy-v1", {"scholar": "3", "dblp": "1"}, "abc123").id != spec.id
    assert GenerationSpec(first, "policy-v1", spec.adapter_versions, "def456").id != spec.id
    with pytest.raises((FrozenInstanceError, AttributeError)):
        spec.base_commit = "changed"  # type: ignore[misc]
    with pytest.raises(TypeError):
        spec.adapter_versions["scholar"] = "changed"  # type: ignore[index]
