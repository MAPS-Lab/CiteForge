from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from citeforge.io_utils import read_records
from citeforge.refresh.census import AuthorCensus, load_census
from citeforge.refresh.types import GenerationSpec, GenerationState, TaskDisposition

_SCHOLAR_URL = "https://scholar.google.com/citations?user=AbCdEfGh1234"


def _write_census(path: Path, rows: list[str]) -> Path:
    path.write_text(
        "Name,Scholar Link,DBLP Link,Enabled,Exclusion Reason\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("enabled", ["", "maybe"])
def test_rejects_unclassified_rows(tmp_path: Path, enabled: str) -> None:
    path = _write_census(tmp_path / "authors.csv", [f"Ada Lovelace,{_SCHOLAR_URL},,{enabled},"])

    with pytest.raises(ValueError, match="Enabled"):
        load_census(path)


def test_rejects_blank_physical_rows(tmp_path: Path) -> None:
    path = _write_census(tmp_path / "authors.csv", [f"Ada Lovelace,{_SCHOLAR_URL},,true,", ""])

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
    path = _write_census(tmp_path / "authors.csv", [f",{_SCHOLAR_URL},,true,"])

    with pytest.raises(ValueError, match="Name"):
        load_census(path)


def test_rejects_duplicate_normalized_identity(tmp_path: Path) -> None:
    path = _write_census(
        tmp_path / "authors.csv",
        [
            f"Ada  Lovelace,{_SCHOLAR_URL},,true,",
            f"  ADA LOVELACE  ,{_SCHOLAR_URL},,true,",
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
            f"Ada Lovelace,{_SCHOLAR_URL},,true,",
        ],
    )

    records = read_records(str(path))

    assert [(record.name, record.scholar_id, record.dblp) for record in records] == [
        ("Grace Hopper", "", "12/345"),
        ("Ada Lovelace", "AbCdEfGh1234", ""),
    ]


def test_row_keys_survive_order_and_whitespace_but_change_with_identity(tmp_path: Path) -> None:
    first = _write_census(
        tmp_path / "first.csv",
        [
            f"Ada Lovelace,{_SCHOLAR_URL},,true,",
            "Grace Hopper,,https://dblp.org/pid/12/345,true,",
        ],
    )
    reordered = _write_census(
        tmp_path / "reordered.csv",
        [
            "  Grace   Hopper  ,,https://dblp.org/pid/12/345,true,",
            f" Ada   Lovelace , {_SCHOLAR_URL} ,,true,",
        ],
    )
    changed = _write_census(
        tmp_path / "changed.csv",
        ["Ada Lovelace,https://scholar.google.com/citations?user=OtherId12345,,true,"],
    )

    first_keys = {row.normalized_name: row.row_key for row in load_census(first).rows}
    reordered_keys = {row.normalized_name: row.row_key for row in load_census(reordered).rows}

    assert first_keys == reordered_keys
    assert load_census(changed).rows[0].row_key != first_keys["ada lovelace"]


def test_generation_id_is_canonical_and_materially_sensitive(tmp_path: Path) -> None:
    first = load_census(
        _write_census(
            tmp_path / "first.csv",
            [
                f"Ada Lovelace,{_SCHOLAR_URL},,true,",
                "Grace Hopper,,https://dblp.org/pid/12/345,true,",
            ],
        )
    )
    reordered = load_census(
        _write_census(
            tmp_path / "reordered.csv",
            [
                "Grace Hopper,,https://dblp.org/pid/12/345,true,",
                f"Ada Lovelace,{_SCHOLAR_URL},,true,",
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


@pytest.mark.parametrize(
    "link",
    [
        "http://scholar.google.com/citations?user=AbCdEfGh1234",
        "https://example.test/citations?user=AbCdEfGh1234",
        "https://scholar.google.com/profile?user=AbCdEfGh1234",
        "https://scholar.google.com/citations?user=",
        "https://scholar.google.com/citations?user=bad!",
        "https://scholar.google.com/citations?user=AbCdEfGh1234&user=OtherId12345",
        "https://scholar.google.com/citations?hl=en&user=AbCdEfGh1234&user=",
        "https://scholar.google.com/citations?user=AbCdEfGh1234#profile",
        "https://scholar.google.com/citations?user=AbCdEf%20h1234",
        "https://scholar.google.com/citations?us%65r=AbCdEfGh1234",
        "https://scholar.google.com/citations?user=%41bCdEfGh1234",
        "https://scholar.google.com/citations?user=AbCdEfGh1234&us%65r=OtherId12345",
        "https://user@scholar.google.com/citations?user=AbCdEfGh1234",
        "https://scholar.google.com:444/citations?user=AbCdEfGh1234",
    ],
)
def test_rejects_malformed_or_untrusted_scholar_links(tmp_path: Path, link: str) -> None:
    path = _write_census(tmp_path / "authors.csv", [f"Ada Lovelace,{link},,true,"])

    with pytest.raises(ValueError, match="Scholar Link"):
        load_census(path)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://scholar.google.com/citations?user=AbCdEfGh1234", "AbCdEfGh1234"),
        ("https://scholar.google.ca/citations?user=_bCdEfGh-234", "_bCdEfGh-234"),
        ("https://scholar.google.com/citations?hl=en&user=AbCdEfGh1234&pagesize=100", "AbCdEfGh1234"),
        ("https://scholar.google.com/citations?pagesize=100&user=AbCdEfGh1234&hl=en", "AbCdEfGh1234"),
        ("https://scholar.google.com/citations?hl=en%2DGB&user=AbCdEfGh1234", "AbCdEfGh1234"),
        ("https://scholar.google.com/citations?user=AbCdEfGh1234&view_op=list%5Fworks", "AbCdEfGh1234"),
        ("https://scholar.google.com:443/citations?user=AbCdEfGh1234", "AbCdEfGh1234"),
    ],
)
def test_accepts_supported_scholar_profiles(tmp_path: Path, link: str, expected: str) -> None:
    census = load_census(_write_census(tmp_path / "authors.csv", [f"Ada Lovelace,{link},,true,"]))

    assert census.rows[0].scholar_id == expected


@pytest.mark.parametrize(
    "link",
    [
        "http://dblp.org/pid/12/3456",
        "https://example.test/pid/12/3456",
        "https://dblp.org/person/12/3456",
        "https://dblp.org/pid/12/3456?view=bibtex",
        "https://dblp.org/pid/12/3456#profile",
        "https://dblp.org/pid/12//3456",
        "12/34 56",
        "arbitrary-string",
        "https://user@dblp.org/pid/12/3456",
        "https://dblp.org:444/pid/12/3456",
        "https://dblp.org/pid/12/3456.json",
        "https://dblp.org/pid/12/3456.txt",
        "https://dblp.org/pid/12/3456.html.bak",
        "pid:12/3456.xml",
    ],
)
def test_rejects_malformed_or_untrusted_dblp_identifiers(tmp_path: Path, link: str) -> None:
    path = _write_census(tmp_path / "authors.csv", [f"Ada Lovelace,,{link},true,"])

    with pytest.raises(ValueError, match="DBLP Link"):
        load_census(path)


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://dblp.org/pid/12/3456", "12/3456"),
        ("https://dblp.uni-trier.de/pid/r/ARauChaplin.html", "r/ARauChaplin"),
        ("https://dblp.org:443/pid/12/3456.xml", "12/3456"),
        ("75/8719-1", "75/8719-1"),
        ("b/PBodorik", "b/PBodorik"),
        ("pid:75/8719-1", "75/8719-1"),
    ],
)
def test_accepts_supported_dblp_identifiers(tmp_path: Path, link: str, expected: str) -> None:
    census = load_census(_write_census(tmp_path / "authors.csv", [f"Ada Lovelace,,{link},true,"]))

    assert census.rows[0].dblp_id == expected


@pytest.mark.parametrize("suffix", ["", ".html", ".xml", ".bib", ".nt", ".rdf", ".rss", ".ris"])
def test_dblp_url_canonicalization_matches_downstream_pid_extraction(tmp_path: Path, suffix: str) -> None:
    from citeforge.clients.search_apis import dblp_extract_pid

    link = f"https://dblp.org/pid/12/3456{suffix}"
    census_id = load_census(_write_census(tmp_path / "authors.csv", [f"Ada Lovelace,,{link},true,"])).rows[0].dblp_id

    assert census_id == "12/3456"
    assert dblp_extract_pid(census_id) == census_id


@pytest.mark.parametrize("provider", ["scholar", "dblp"])
def test_rejects_same_author_overlapping_provider_identity(tmp_path: Path, provider: str) -> None:
    if provider == "scholar":
        rows = [
            f"Ada Lovelace,{_SCHOLAR_URL},https://dblp.org/pid/12/3456,true,",
            f" ADA   LOVELACE ,{_SCHOLAR_URL},https://dblp.org/pid/98/7654,true,",
        ]
    else:
        rows = [
            f"Ada Lovelace,{_SCHOLAR_URL},https://dblp.org/pid/12/3456,true,",
            " ADA   LOVELACE ,https://scholar.google.com/citations?user=OtherId12345,https://dblp.org/pid/12/3456,true,",
        ]

    with pytest.raises(ValueError, match=r"duplicate.*provider identity"):
        load_census(_write_census(tmp_path / "authors.csv", rows))


@pytest.mark.parametrize("provider", ["scholar", "dblp"])
def test_rejects_provider_identity_assigned_to_different_names(tmp_path: Path, provider: str) -> None:
    if provider == "scholar":
        rows = [f"Ada Lovelace,{_SCHOLAR_URL},,true,", f"Grace Hopper,{_SCHOLAR_URL},,true,"]
    else:
        rows = ["Ada Lovelace,,12/3456,true,", "Grace Hopper,,12/3456,true,"]

    with pytest.raises(ValueError, match=r"duplicate.*provider identity"):
        load_census(_write_census(tmp_path / "authors.csv", rows))


def test_allows_same_name_with_wholly_disjoint_provider_identities(tmp_path: Path) -> None:
    rows = [
        f"Ada Lovelace,{_SCHOLAR_URL},https://dblp.org/pid/12/3456,true,",
        " ADA   LOVELACE ,https://scholar.google.com/citations?user=OtherId12345,https://dblp.org/pid/98/7654,true,",
    ]

    assert load_census(_write_census(tmp_path / "authors.csv", rows)).total_count == 2


def test_generation_states_cover_exact_lifecycle() -> None:
    assert {state.value for state in GenerationState} == {
        "planning",
        "running",
        "waiting",
        "blocked",
        "validating",
        "complete",
        "published",
        "superseded",
    }


@pytest.mark.parametrize(
    "changed_row",
    [
        {"enabled": False, "disposition": TaskDisposition.NOT_APPLICABLE},
        {"exclusion_reason": "Changed reason"},
        {"normalized_name": "augusta ada king"},
        {"scholar_id": "OtherId12345"},
        {"dblp_id": "98/7654"},
    ],
)
def test_generation_id_changes_for_each_material_census_field(tmp_path: Path, changed_row: dict[str, object]) -> None:
    census = load_census(
        _write_census(
            tmp_path / "authors.csv",
            [f"Ada Lovelace,{_SCHOLAR_URL},https://dblp.org/pid/12/3456,true,"],
        )
    )
    spec = GenerationSpec(census, "policy-v1", {"scholar": "1"}, "abc123")
    changed_census = AuthorCensus((replace(census.rows[0], **changed_row),))

    assert GenerationSpec(changed_census, "policy-v1", {"scholar": "1"}, "abc123").id != spec.id
