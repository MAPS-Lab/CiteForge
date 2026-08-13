from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import zlib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from citeforge.bibtex_utils import bibtex_from_dict, parse_strict_bibtex_document
from citeforge.id_utils import normalize_strict_arxiv_id
from citeforge.io_utils import build_a2i2_folder
from citeforge.models import Record
from citeforge.refresh.authority import publication_key_for
from citeforge.refresh.census import AuthorCensus, AuthorCensusRow, load_census
from citeforge.refresh.corpus import scan_existing_corpus
from citeforge.refresh.engine import RefreshEngine
from citeforge.refresh.inventory import InventoryPolicy, RefreshCredentials
from citeforge.refresh.ledger import (
    FaultInjectedError,
    Ledger,
    PublicationMetadata,
    StaleClaimError,
)
from citeforge.refresh.transport import LedgerTransport, SendOperation
from citeforge.refresh.types import GenerationSpec, GenerationState, TaskDisposition

_git_executable = shutil.which("git")
if _git_executable is None:
    raise RuntimeError("git is required for corpus tests")
_GIT: str = _git_executable


def test_strict_committed_bibtex_parser_requires_one_complete_entry() -> None:
    parsed = parse_strict_bibtex_document(
        b"@article{Key, title={A title}, author={Lovelace, Ada}, year={2026}, doi={HTTPS://doi.org/10.1000/X}}\n"
    )
    assert parsed == {
        "type": "article",
        "key": "Key",
        "fields": {
            "author": "Lovelace, Ada",
            "doi": "HTTPS://doi.org/10.1000/X",
            "title": "A title",
            "year": "2026",
        },
    }


def test_strict_committed_bibtex_is_in_serializer_canonical_value_domain() -> None:
    first = parse_strict_bibtex_document(b'@article{K,title={T},year={2024},publisher={f{\\"u}r}}\n')
    second = parse_strict_bibtex_document(bibtex_from_dict(first).encode())
    assert first == second


@pytest.mark.parametrize(
    "value",
    ["doi:10.1234/foo--bar", "https://doi.org/10.1234/foo--bar", "https://publisher.example/a--b"],
)
def test_strict_committed_bibtex_preserves_identifier_howpublished(value: str) -> None:
    first = parse_strict_bibtex_document(f"@misc{{K,title={{T}},year={{2024}},howpublished={{{value}}}}}\n".encode())
    second = parse_strict_bibtex_document(bibtex_from_dict(first).encode())
    assert first == second
    assert first["fields"]["howpublished"] == value


@pytest.mark.parametrize("dash", ["\u2014", "\u2013"])
def test_strict_committed_bibtex_canonicalizes_unicode_dashes_idempotently(dash: str) -> None:
    first = parse_strict_bibtex_document(f"@article{{K,title={{A {dash} B}},year={{2024}}}}\n".encode())
    assert parse_strict_bibtex_document(bibtex_from_dict(first).encode()) == first


@pytest.mark.parametrize("field", ["author", "publisher", "url", "doi", "month", "number"])
def test_strict_committed_bibtex_drops_blank_optional_fields_for_fixpoint(field: str) -> None:
    first = parse_strict_bibtex_document(f"@article{{K,title={{T}},year={{2024}},{field}={{}}}}\n".encode())
    assert field not in first["fields"]
    assert parse_strict_bibtex_document(bibtex_from_dict(first).encode()) == first


@pytest.mark.parametrize("value", ["A\u2019\u2019B", "A\u2018\u2019B"])
def test_strict_committed_bibtex_canonicalizes_paired_unicode_quotes_idempotently(value: str) -> None:
    first = parse_strict_bibtex_document(f"@article{{K,title={{{value}}},year={{2024}}}}\n".encode())
    assert parse_strict_bibtex_document(bibtex_from_dict(first).encode()) == first
    for invalid in (
        b"",
        b"junk @article{Key, title={A title}}",
        b"@article{Key, title={A title}} junk",
        b"@string{x={value}}\n@article{Key, title=x}",
        b"@article{One, title={One}}\n@article{Two, title={Two}}",
        b"@article{Key, title={}}",
        b"@article{Key, title={A title}}",
        b"@article{Key, title={A title}, year={1899}}",
        b"@article{Key, title={A title}, year={twenty twenty}}",
        b"@unknown{Key, title={A title}}",
        b"@article{Key, title={\xff}}",
        b"@article{Bad\x00Key, title={A title}, year={2026}}",
        b"@article{Key, title={A\x7ftitle}, year={2026}}",
        b"@article{Key, title={A\x01title}, year={2026}}",
        "@article{Key, title={A\u0085title}, year={2026}}".encode(),
    ):
        with pytest.raises((TypeError, ValueError)):
            parse_strict_bibtex_document(invalid)


def test_strict_committed_bibtex_strips_after_latex_normalization() -> None:
    first = parse_strict_bibtex_document(b"@article{K,title={T},year={2024},note={~K}}\n")
    assert first["fields"]["note"] == "K"
    assert parse_strict_bibtex_document(bibtex_from_dict(first).encode()) == first


@pytest.mark.parametrize("assignment", ["api.key=value", "API.KEY: value"])
def test_committed_bibtex_rejects_punctuated_secret_assignment(tmp_path: Path, assignment: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key,title={{A title}},year={{2026}},note={{{assignment}}}}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="secret"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("filename", ["api%2ekey=value.bib", "api%252ekey=value.bib"])
def test_committed_corpus_rejects_percent_encoded_secret_filename(tmp_path: Path, filename: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    paper = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    paper.rename(paper.with_name(filename))
    commit = _commit(repo)
    with pytest.raises(ValueError, match="secret"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("filename", ["api&#46;key=value.bib", "api&#x2e;key=value.bib"])
def test_committed_corpus_rejects_html_encoded_secret_filename(tmp_path: Path, filename: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    paper = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    paper.rename(paper.with_name(filename))
    commit = _commit(repo)
    with pytest.raises(ValueError, match="secret"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("value", ["cs/9001001", "cs/9107001", "cs/0704001", "cs/0801001"])
def test_strict_arxiv_identifier_rejects_values_outside_legacy_epoch(value: str) -> None:
    assert normalize_strict_arxiv_id(value) is None


@pytest.mark.parametrize("value", ["cs.CL/9108001", "cs.cl/9912001", "cs/0001001", "cs/0703001", "0704.0001"])
def test_strict_arxiv_identifier_accepts_and_canonicalizes_epoch_boundaries(value: str) -> None:
    assert normalize_strict_arxiv_id(value) is not None


def test_strict_committed_bibtex_rejects_duplicate_fields() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_strict_bibtex_document(b"@article{Key, title={First}, title={Second}, year={2026}}\n")


def test_strict_committed_bibtex_rejects_conflicting_arxiv_ids(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_bytes(
        b"@article{Key, title={A title}, year={2026}, archiveprefix={arXiv}, "
        b"eprint={2501.00002}, url={https://arxiv.org/abs/2401.00001}}\n"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="conflicting arXiv"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("archiveprefix={arXiv}, eprint", "not-an-id", "arXiv"),
        ("x_s2_paper_id", "not an id", "Semantic Scholar"),
        ("x_openalex_id", "https://openalex.org/A123", "OpenAlex"),
    ],
)
def test_committed_corpus_rejects_invalid_exact_external_ids(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, {field}={{{value}}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=message):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        (_GIT, *args),
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str = "test: update fixture") -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)
    return _git(repo, "rev-parse", "HEAD")


def _committed_corpus(tmp_path: Path) -> tuple[Path, str, AuthorCensus]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "Test")
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    author_dir.mkdir(parents=True)
    (author_dir / "paper.bib").write_text(
        "@article{Key, title={A title}, author={Lovelace, Ada}, year={2026}, doi={10.1000/X}}\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":1,"authors":{"Lovelace (Scholar123)":1}}\n', encoding="utf-8"
    )
    (repo / "output" / "summary.csv").write_text("title\n", encoding="utf-8")
    (repo / "data").mkdir()
    (repo / "data" / "a2i2.csv").write_text("Name,Scholar Link,DBLP Link\n", encoding="utf-8")
    commit = _commit(repo, "test: add corpus fixture")
    row = AuthorCensusRow(
        2,
        "author-ada",
        "Ada Lovelace",
        "ada lovelace",
        "Scholar123",
        "",
        True,
        "",
        TaskDisposition.PENDING,
    )
    return repo, commit, AuthorCensus((row,))


def _complete_empty_inventory(
    ledger: Ledger,
    spec: GenerationSpec,
    articles: list[dict[str, object]] | None = None,
    dblp_xml: bytes | None = None,
) -> None:
    def send_once(operation: SendOperation) -> requests.Response:
        response = requests.Response()
        response.status_code = 200
        if operation.request.provider == "dblp":
            response.headers["Content-Type"] = "application/xml"
            response._content = dblp_xml or b'<dblpperson key="homepages/12/345" n="0"/>'
            return response
        response.headers["Content-Type"] = "application/json"
        response._content = json.dumps(
            {
                "search_metadata": {
                    "status": "Success",
                    "google_scholar_author_url": "https://scholar.google.com/citations?user=Scholar123",
                },
                "search_parameters": {
                    "engine": "google_scholar_author",
                    "author_id": "Scholar123",
                    "cstart": 0,
                },
                "author": {"name": "Ada Lovelace"},
                "articles": articles or [],
            }
        ).encode()
        return response

    result = RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10), LedgerTransport(ledger, send_once=send_once)).run(
        spec, RefreshCredentials(serpapi_key="secret"), lambda: False
    )
    assert result.status.value == "continuation"


def test_committed_corpus_scan_is_deterministic_and_ignores_worktree_drift(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    first = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text("mutated", encoding="utf-8")
    second = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert first == second
    assert len(first.items) == len(first.publications) == len(first.seeds) == 1
    assert first.items[0].source_path == "output/Lovelace (Scholar123)/paper.bib"
    assert first.publications[0].normalized_title == "a title"
    assert first.publications[0].exact_identifiers == {"doi": "10.1000/x"}
    assert first.seeds[0].baseline_entry == first.items[0].normalized_entry
    assert first.seeds[0].derived_seed_digest == first.seeds[0].seed_digest
    assert first.snapshot.base_commit == commit


def test_committed_corpus_rejects_duplicate_baseline_keys(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "baseline.json").write_text(
        '{"total":0,"total":1,"authors":{"Lovelace (Scholar123)":1}}\n',
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="malformed"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_preserves_published_and_secondary_doi_roles(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@article{Key, title={A title}, year={2026}, doi={10.1007/published}, "
        "url={https://doi.org/10.21203/rs.3.rs-123/v1}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    publication = evidence.publications[0]
    assert publication.exact_identifiers == {
        "doi": "10.1007/published",
        "secondary_doi": "10.21203/rs.3.rs-123/v1",
    }
    assert publication.publication_key == publication_key_for(
        publication.author_key, publication.normalized_title, publication.year, "10.1007/published"
    )


def test_committed_corpus_rejects_conflicting_primary_dois(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@article{Key, title={A title}, year={2026}, doi={10.1007/first}, url={https://doi.org/10.1001/second}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="conflicting primary DOI"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "invalid_doi", ["not-a-doi", "https://example.invalid/not-doi", "doi:", "10.1234/<tag>", "10.1234/é"]
)
def test_committed_corpus_rejects_invalid_explicit_doi(tmp_path: Path, invalid_doi: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, doi={{{invalid_doi}}}}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="invalid explicit DOI"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_doi_and_no_doi_identity_split(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "without-doi.bib").write_text("@article{Other, title={A title}, year={2026}}\n", encoding="utf-8")
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="ambiguous late-identifier split"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_ambiguous_a2i2_member_name(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    second = AuthorCensusRow(
        3,
        "author-ada-2",
        "Ada Lovelace",
        "ada lovelace",
        "Scholar456",
        "",
        True,
        "",
        TaskDisposition.PENDING,
    )
    ambiguous_census = AuthorCensus((*census.rows, second))
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="ambiguous"):
        scan_existing_corpus(repo, ambiguous_census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_a2i2_provider_identity_mismatch(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Other999,\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"a2i2.*identity"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "scholar_link",
    ["", "https://scholar.google.com/citations?user=Scholar123&api_key=TOPSECRET"],
)
def test_committed_corpus_rejects_missing_or_secret_a2i2_provider_identity(tmp_path: Path, scholar_link: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_text(
        f"Name,Scholar Link,DBLP Link\nAda Lovelace,{scholar_link},\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"a2i2.*identity|unsafe"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_accepts_matching_a2i2_provider_identity(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    derived = repo / "output" / "a2i2"
    derived.mkdir()
    (derived / "paper.bib").write_bytes((repo / "output" / "Lovelace (Scholar123)" / "paper.bib").read_bytes())
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":1,"a2i2":1}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.derived_a2i2_count == 1


def test_a2i2_verifier_matches_writer_universal_newline_copy(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    source = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    source.write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    derived = repo / "output" / "a2i2"
    derived.mkdir()
    (derived / "paper.bib").write_bytes(source.read_bytes().replace(b"\r\n", b"\n"))
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":1,"a2i2":1}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    assert scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit).derived_a2i2_count == 1


def test_a2i2_verifier_uses_exact_legacy_writer_richness_domain(tmp_path: Path) -> None:
    repo, _commit_id, first_census = _committed_corpus(tmp_path)
    first_dir = repo / "output" / "Lovelace (Scholar123)"
    (first_dir / "same.bib").write_text(
        "@article{Alpha,title={Same title},year={2026},doi={10.1234/x}}\n",
        encoding="utf-8",
    )
    (first_dir / "paper.bib").unlink()
    second_dir = repo / "output" / "Hopper (Scholar456)"
    second_dir.mkdir()
    richer = "@article{Zeta,title={Same title},year={2026},doi={10.1234/x},note={\\verb|x|}}\n"
    (second_dir / "same.bib").write_text(richer, encoding="utf-8")
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\n"
        "Ada Lovelace,https://scholar.google.com/citations?user=Scholar123,\n"
        "Grace Hopper,https://scholar.google.com/citations?user=Scholar456,\n",
        encoding="utf-8",
    )
    records = [Record("Ada Lovelace", "Scholar123"), Record("Grace Hopper", "Scholar456")]
    assert build_a2i2_folder(str(repo / "data" / "a2i2.csv"), records, str(repo / "output")) == 1
    assert (repo / "output" / "a2i2" / "same.bib").read_text(encoding="utf-8") == richer
    (repo / "output" / "baseline.json").write_text(
        '{"total":3,"authors":{"Hopper (Scholar456)":1,"Lovelace (Scholar123)":1,"a2i2":1}}\n',
        encoding="utf-8",
    )
    second_row = AuthorCensusRow(
        3,
        "author-grace",
        "Grace Hopper",
        "grace hopper",
        "Scholar456",
        "",
        True,
        "",
        TaskDisposition.PENDING,
    )
    census = AuthorCensus((*first_census.rows, second_row))
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.derived_a2i2_count == 1


def test_primary_corpus_rejects_duplicate_identity_before_a2i2_derivation(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    explicit = author_dir / "paper.bib"
    explicit.write_text(
        "@article{Explicit, title={Computing Machinery}, year={2026}, doi={10.1000/X}}\n", encoding="utf-8"
    )
    url_only = author_dir / "url-only.bib"
    url_only.write_text(
        "@article{Resolver, title={Computing Machinery and Intelligence}, year={2026}, "
        "url={https://doi.org/10.1000/X}}\n",
        encoding="utf-8",
    )
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    derived = repo / "output" / "a2i2"
    derived.mkdir()
    (derived / explicit.name).write_bytes(explicit.read_bytes())
    (derived / url_only.name).write_bytes(url_only.read_bytes())
    (repo / "output" / "baseline.json").write_text(
        '{"total":4,"authors":{"Lovelace (Scholar123)":2,"a2i2":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="duplicate stable publication identity"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_extracts_pmid_and_rejects_conflicting_alias(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    paper = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    paper.write_text("@article{Key, title={A title}, year={2026}, pmid={12345}}\n", encoding="utf-8")
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.items[0].exact_identifiers["pmid"] == "12345"

    paper.write_text(
        "@article{Key, title={A title}, year={2026}, pmid={12345}, x_pmid={67890}}\n",
        encoding="utf-8",
    )
    conflict = _commit(repo)
    with pytest.raises(ValueError, match="PMID"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=conflict)


@pytest.mark.parametrize("value", ["doi:10.1000/x", " DOI: 10.1000/x ", "arXiv: 2501.00002"])
def test_committed_corpus_accepts_supported_identifier_notation_in_howpublished(tmp_path: Path, value: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@misc{{Key, title={{A title}}, year={{2026}}, howpublished={{{value}}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.items[0].exact_identifiers


@pytest.mark.parametrize("value", ["doi:not-a-doi", "doi:10.1234%252Ffoo", "arXiv:not-an-id", "arXiv:2501.00002 extra"])
def test_committed_corpus_rejects_invalid_or_partially_consumed_direct_identifier(tmp_path: Path, value: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@misc{{Key, title={{A title}}, year={{2026}}, howpublished={{{value}}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"DOI|arXiv|unsafe"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("value", ["2500.00001", "2599.00001", "0000.00001", "2501.0000", "cs/9913000", "cs/0704000"])
def test_committed_corpus_rejects_invalid_modern_arxiv_month(tmp_path: Path, value: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@misc{{Key, title={{A title}}, year={{2026}}, archiveprefix={{arXiv}}, eprint={{{value}}}}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="arXiv"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_arxiv_conflict_between_eprint_and_inferred_doi(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@misc{Key, title={A title}, year={2026}, archiveprefix={arXiv}, eprint={2401.00001}, "
        "url={https://doi.org/10.48550/arxiv.2501.00002}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="arXiv"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_nested_a2i2_tree(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    source = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    nested = repo / "output" / "a2i2" / "nested"
    nested.mkdir(parents=True)
    (nested / "paper.bib").write_bytes(source.read_bytes())
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":1,"a2i2":1}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="a2i2"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_accepts_zero_derived_a2i2_member_count(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    source = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    source.write_text("@article{Key, title={Old work}, year={2019}}\n", encoding="utf-8")
    (repo / "data" / "a2i2.csv").write_text(
        "Name,Scholar Link,DBLP Link\nAda Lovelace,https://scholar.google.com/citations?user=Scholar123,\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":1,"authors":{"Lovelace (Scholar123)":1,"a2i2":0}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.derived_a2i2_count == 0


def test_committed_corpus_requires_full_base_commit_oid(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    with pytest.raises(ValueError, match="full commit object ID"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit="HEAD")


def test_committed_corpus_ignores_git_replace_substitution(tmp_path: Path) -> None:
    repo, original, census = _committed_corpus(tmp_path)
    paper = repo / "output" / "Lovelace (Scholar123)" / "paper.bib"
    paper.write_text(
        "@article{Changed, title={Substituted work}, year={2026}, doi={10.1000/Y}}\n",
        encoding="utf-8",
    )
    replacement = _commit(repo)
    _git(repo, "replace", original, replacement)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=original)
    assert evidence.publications[0].normalized_title == "a title"


def test_committed_corpus_disables_lazy_fetch_for_missing_promisor_blob(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    blob_id = _git(repo, "rev-parse", f"{commit}:output/Lovelace (Scholar123)/paper.bib")
    object_path = repo / ".git" / "objects" / blob_id[:2] / blob_id[2:]
    _git(repo, "config", "extensions.partialClone", "origin")
    _git(repo, "config", "remote.origin.promisor", "true")
    _git(repo, "config", "remote.origin.url", repo.as_uri())
    object_path.unlink()
    with pytest.raises(ValueError, match=r"Git objects|absent|unreadable"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert not object_path.exists()


def test_committed_corpus_rejects_corrupted_loose_blob(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    blob_id = _git(repo, "rev-parse", f"{commit}:output/Lovelace (Scholar123)/paper.bib")
    object_path = repo / ".git" / "objects" / blob_id[:2] / blob_id[2:]
    body = b"@article{Forgery, title={Forged work}, year={2026}}\n"
    object_path.chmod(0o600)
    object_path.write_bytes(zlib.compress(f"blob {len(body)}\0".encode() + body))
    with pytest.raises(ValueError, match="integrity"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_legacy_filesystem_scanners_import_without_git() -> None:
    environment = dict(os.environ)
    environment["PATH"] = ""
    result = subprocess.run(
        [sys.executable, "-c", "from citeforge.fsscan import iter_author_bibs; assert callable(iter_author_bibs)"],
        cwd=Path.cwd(),
        env=environment,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_committed_corpus_rejects_a2i2_regular_blob(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "a2i2").write_bytes(b"")
    commit = _commit(repo)
    with pytest.raises(ValueError, match="a2i2"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_expected_author_regular_blob(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").unlink()
    author_dir.rmdir()
    author_dir.write_bytes(b"")
    (repo / "output" / "baseline.json").write_text('{"total":0,"authors":{}}\n', encoding="utf-8")
    commit = _commit(repo)
    with pytest.raises(ValueError, match="author directory"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_empty_a2i2_csv_header(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_bytes(b"")
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"a2i2\.csv schema"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_duplicate_a2i2_csv_column(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_text("Name,Name,Scholar Link,DBLP Link\n", encoding="utf-8")
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"a2i2\.csv schema"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "content",
    [
        "Name,Scholar Link,DBLP Link\nAda Lovelace,,,overflow\n",
        'Name,Scholar Link,DBLP Link\n"Ada Lovelace,,\n',
    ],
)
def test_committed_corpus_rejects_malformed_a2i2_rows(tmp_path: Path, content: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "data" / "a2i2.csv").write_text(content, encoding="utf-8")
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"a2i2\.csv"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_del_in_path(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "bad\x7f.bib").write_text("@article{Bad, title={Bad path}, year={2026}}\n", encoding="utf-8")
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="path"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_empty_stem_bibtex_filename_before_ledger_write(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").rename(author_dir / ".bib")
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"BibTeX|filename|path"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_private_contact_in_filename(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").rename(author_dir / "person@example.test.bib")
    commit = _commit(repo)
    with pytest.raises(ValueError, match="contact"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_private_contact_in_typed_census_name(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    private_row = replace(census.rows[0], name="Ada person@example.test", normalized_name="ada person@example.test")
    with pytest.raises(ValueError, match="contact"):
        scan_existing_corpus(repo, AuthorCensus((private_row,)), generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/private",
        "//doi.org/10.1000/x",
        "javascript:alert(1)",
        "mailto:user@example.com",
        "https://user:pass@doi.org/10.1000/x",
        "https://doi.org/10.1000/x?token=secret",
    ],
)
def test_committed_corpus_rejects_unsafe_durable_url(tmp_path: Path, url: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, url={{{url}}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"unsafe|private|secret"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("howpublished", ["//doi.org/10.1000/x", "file:/private", "mailto:user@example.com"])
def test_committed_corpus_rejects_unsafe_howpublished_uri(tmp_path: Path, howpublished: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, howpublished={{{howpublished}}}}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"unsafe|private"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_c1_control_in_path(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "bad\u0085.bib").write_text("@article{Bad, title={Bad path}, year={2026}}\n", encoding="utf-8")
    commit = _commit(repo)
    with pytest.raises(ValueError, match="path"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_public_snapshot_commit_cannot_self_attest_corpus_authority(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"scholar": "1"}, commit)
    evidence = scan_existing_corpus(repo, census, generation_id=spec.id, base_commit=commit)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        ledger.create_or_resume(spec, census)
        with pytest.raises(ValueError, match="scan_and_commit_corpus"):
            ledger.commit_corpus_snapshot(evidence.snapshot, evidence.items)


def test_scan_and_commit_corpus_is_atomic_idempotent_and_nonclosing(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    ledger_path = tmp_path / "ledger.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(ledger_path) as ledger:
        RefreshEngine(ledger, InventoryPolicy(2020, 1000, 10)).run(
            spec, RefreshCredentials(serpapi_key="secret"), lambda: True
        )
        with pytest.raises(ValueError, match="inventory union"):
            ledger.scan_and_commit_corpus(repo)
        _complete_empty_inventory(ledger, spec)
        first = ledger.scan_and_commit_corpus(repo)
        second = ledger.scan_and_commit_corpus(repo)
        assert first == second
        manifest = ledger.manifest().data
        assert len(manifest["task5c_evidence"]["corpus_items"]) == 1
        assert len(manifest["task5c_evidence"]["publication_seed_evidence"]) == 1
        assert manifest["generation"]["state"] == GenerationState.RUNNING.value
        assert manifest["generation"]["discovery_closed"] == 0
        assert manifest["validations"] == []
        assert manifest["materializations"] == []
        assert not ledger.all_required_satisfied()
    with pytest.raises(ValueError, match="trusted Git repository root"):
        Ledger.open(ledger_path)
    with Ledger.open(ledger_path, corpus_repo_root=repo) as reopened:
        assert reopened.scan_and_commit_corpus(repo) == first


def test_production_planner_requires_corpus_and_revalidates_trusted_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citeforge.refresh.corpus as corpus_module

    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    calls = 0
    original = corpus_module._scan_existing_corpus_authority

    def counted(*args: object, **kwargs: object) -> object:
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(corpus_module, "_scan_existing_corpus_authority", counted)
    with Ledger.open(tmp_path / "planner.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        with pytest.raises(ValueError, match="committed-corpus authority"):
            ledger.execute_registered_pass("bind_corpus_seed")
        ledger.scan_and_commit_corpus(repo)
        assert ledger.execute_registered_pass("bind_corpus_seed").pass_id == "bind_corpus_seed"
        ledger.snapshot_for_pass("bind_corpus_seed")
        ledger.manifest()
        assert calls == 1


def test_cached_trusted_proof_fails_if_repository_disappears(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(tmp_path / "missing-repo.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        ledger.scan_and_commit_corpus(repo)
        repo.rename(tmp_path / "moved-repo")
        with pytest.raises(ValueError, match=r"Git|tree|authority"):
            ledger.manifest()


def test_scan_commit_rejects_inventory_policy_drift_during_git_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citeforge.refresh.corpus as corpus_module

    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    original = corpus_module._scan_existing_corpus_authority
    with Ledger.open(tmp_path / "drift.db") as ledger:
        _complete_empty_inventory(ledger, spec)

        def mutate_then_scan(*args: object, **kwargs: object) -> object:
            ledger._connection.execute(
                "UPDATE generations SET inventory_freshness_epoch = '2025-01' WHERE generation_id = ?",
                (spec.id,),
            )
            return original(*args, **kwargs)

        monkeypatch.setattr(corpus_module, "_scan_existing_corpus_authority", mutate_then_scan)
        with pytest.raises(StaleClaimError, match="authority changed"):
            ledger.scan_and_commit_corpus(repo)
        assert ledger._connection.execute("SELECT COUNT(*) FROM corpus_snapshots").fetchone()[0] == 0


def test_git_scan_does_not_hold_sqlite_writer_lock_and_fences_external_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citeforge.refresh.corpus as corpus_module

    repo, commit, census = _committed_corpus(tmp_path)
    path = tmp_path / "writer-race.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    entered = threading.Event()
    release = threading.Event()
    original = corpus_module._scan_existing_corpus_authority
    with Ledger.open(path) as ledger:
        _complete_empty_inventory(ledger, spec)

        def delayed(*args: object, **kwargs: object) -> object:
            entered.set()
            assert release.wait(5)
            return original(*args, **kwargs)

        monkeypatch.setattr(corpus_module, "_scan_existing_corpus_authority", delayed)
        error: list[BaseException] = []

        def scan() -> None:
            try:
                with Ledger.open(path) as worker_ledger:
                    worker_ledger.scan_and_commit_corpus(repo)
            except BaseException as exc:
                error.append(exc)

        worker = threading.Thread(target=scan)
        worker.start()
        assert entered.wait(5)
        other = sqlite3.connect(path, timeout=1, isolation_level=None)
        other.execute("UPDATE generations SET inventory_freshness_epoch = '2025-01'")
        other.close()
        release.set()
        worker.join(10)
        assert len(error) == 1 and isinstance(error[0], StaleClaimError)
        assert ledger._connection.execute("SELECT COUNT(*) FROM corpus_snapshots").fetchone()[0] == 0


def test_reopened_ledger_canonicalizes_relative_trusted_repo_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    ledger_path = tmp_path / "ledger.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(ledger_path) as ledger:
        _complete_empty_inventory(ledger, spec)
        ledger.scan_and_commit_corpus(repo)

    monkeypatch.chdir(tmp_path)
    with Ledger.open(ledger_path, corpus_repo_root=Path("repo")) as reopened:
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)
        assert reopened.manifest().data["generation"]["generation_id"] == spec.id


def test_strict_committed_bibtex_accepts_production_book_output() -> None:
    parsed = parse_strict_bibtex_document(
        b"@book{Key, title={A Book}, author={Lovelace, Ada}, year={2026}, publisher={Press}, "
        b"series={Collected Works}, chapter={Three}}\n"
    )
    assert parsed["type"] == "book"
    assert parsed["fields"]["series"] == "Collected Works"


def test_strict_committed_bibtex_normalizes_multiline_field_layout() -> None:
    parsed = parse_strict_bibtex_document(b"@article{Key, title={A\nMultiline\tTitle}, year={2026}}\n")
    assert parsed["fields"]["title"] == "A Multiline Title"


def test_corpus_commit_rederives_author_set_from_durable_census(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    ledger_path = tmp_path / "ledger.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(ledger_path) as ledger:
        _complete_empty_inventory(ledger, spec)
        evidence = scan_existing_corpus(repo, census, generation_id=spec.id, base_commit=commit)
        bad_snapshot = replace(evidence.snapshot, author_set_digest="f" * 64)
        bad_items = tuple(replace(item, snapshot_digest=bad_snapshot.digest) for item in evidence.items)
        bad_evidence = replace(evidence, snapshot=bad_snapshot, items=bad_items)
        with pytest.raises(ValueError, match="author-set"):
            ledger._commit_existing_corpus(bad_evidence)


def test_corpus_commit_rechecks_running_state_inside_transaction(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        evidence = scan_existing_corpus(repo, census, generation_id=spec.id, base_commit=commit)
        ledger.transition_generation(
            GenerationState.RUNNING,
            GenerationState.BLOCKED,
            datetime.now(timezone.utc),
            blocking_reason="concurrent lifecycle change",
        )
        with pytest.raises(ValueError, match="running generation"):
            ledger._commit_existing_corpus(evidence)
        assert ledger._connection.execute("SELECT COUNT(*) FROM corpus_snapshots").fetchone()[0] == 0


@pytest.mark.parametrize("column", ["policy_digest", "reduction_digest", "round_key"])
def test_corpus_commit_atomically_rejects_corrupt_inventory_authority(tmp_path: Path, column: str) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    path = tmp_path / f"corrupt-{column}.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(path) as ledger:
        _complete_empty_inventory(ledger, spec)
        trigger = ledger._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='inventory_authorities_append_only_update'"
        ).fetchone()[0]
        ledger._connection.commit()
        ledger._connection.execute("PRAGMA foreign_keys = OFF")
        ledger._connection.execute("DROP TRIGGER inventory_authorities_append_only_update")
        updates = {
            "policy_digest": "UPDATE inventory_authorities SET policy_digest = ?",
            "reduction_digest": "UPDATE inventory_authorities SET reduction_digest = ?",
            "round_key": "UPDATE inventory_authorities SET round_key = ?",
        }
        ledger._connection.execute(updates[column], ("f" * 64,))
        ledger._connection.execute(trigger)
        ledger._connection.commit()
        ledger._connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(ValueError, match=r"inventory authority|reduction receipt"):
            ledger.scan_and_commit_corpus(repo)
        counts = ledger._connection.execute(
            "SELECT (SELECT COUNT(*) FROM corpus_snapshots), (SELECT COUNT(*) FROM corpus_items), "
            "(SELECT COUNT(*) FROM corpus_scan_receipts)"
        ).fetchone()
        assert tuple(counts) == (0, 0, 0)


def test_trusted_corpus_cache_survives_legal_waiting_resume_lifecycle(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(tmp_path / "lifecycle.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        ledger.scan_and_commit_corpus(repo)
        now = datetime.now(timezone.utc)
        ledger.transition_generation(GenerationState.RUNNING, GenerationState.WAITING, now)
        assert ledger.manifest().data["generation"]["state"] == GenerationState.WAITING.value
        ledger.transition_generation(GenerationState.WAITING, GenerationState.RUNNING, now)
        assert ledger.snapshot_for_pass("bind_corpus_seed")["pass_id"] == "bind_corpus_seed"


def test_shared_inventory_publication_remains_exact_after_corpus_bind_and_reopen(tmp_path: Path) -> None:
    repo, _initial_commit, census = _committed_corpus(tmp_path)
    old_dir = repo / "output" / "Lovelace (Scholar123)"
    author_dir = repo / "output" / "Lovelace (12-345)"
    old_dir.rename(author_dir)
    (repo / "output" / "baseline.json").write_text('{"total":1,"authors":{"Lovelace (12-345)":1}}\n', encoding="utf-8")
    commit = _commit(repo)
    census = AuthorCensus((replace(census.rows[0], scholar_id="", dblp_id="12/345"),))
    spec = GenerationSpec(census, "policy-v1", {"dblp": "1", "doi_csl": "1", "s2": "1"}, commit)
    path = tmp_path / "shared.db"
    xml = (
        b'<dblpperson key="homepages/12/345" n="1"><r><article key="journals/x/1">'
        b"<author>Ada Lovelace</author><title>A title</title><year>2026</year>"
        b"<ee>https://doi.org/10.1000/x</ee></article></r></dblpperson>"
    )
    with Ledger.open(path) as ledger:
        _complete_empty_inventory(ledger, spec, dblp_xml=xml)
        ledger.scan_and_commit_corpus(repo)
        publication = ledger.manifest().data["publications"][0]
        assert publication["discovery_source"] == "dblp"
    with Ledger.open(path, corpus_repo_root=repo) as reopened:
        assert reopened.manifest().data["publications"][0]["discovery_source"] == "dblp"
        trigger = reopened._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='publications_append_only_update'"
        ).fetchone()[0]
        reopened._connection.execute("DROP TRIGGER publications_append_only_update")
        reopened._connection.execute(
            "UPDATE publications SET discovery_source = 'corpus', baseline_output_path = ?",
            ("output/Lovelace (12-345)/paper.bib",),
        )
        reopened._connection.execute(trigger)
        reopened._connection.commit()
        with pytest.raises(ValueError, match="publication row changed"):
            reopened.manifest()


def test_corpus_commit_rejects_same_doi_with_unrelated_inventory_title(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        corpus = scan_existing_corpus(repo, census, generation_id=spec.id, base_commit=commit)
        publication = corpus.publications[0]
        with ledger._transaction(immediate=True) as connection, ledger._authority_write():
            ledger._insert_publication(
                connection,
                spec.id,
                PublicationMetadata(
                    publication.author_key,
                    publication.publication_key,
                    "inventory",
                    "a completely unrelated work",
                    publication.year,
                    publication.exact_identifiers,
                    "",
                    "monthly",
                ),
            )
        with pytest.raises(ValueError, match="title similarity"):
            ledger._commit_existing_corpus(corpus)


def test_corpus_commit_rejects_cross_source_doi_and_no_doi_split(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    old_dir = repo / "output" / "Lovelace (Scholar123)"
    author_dir = repo / "output" / "Lovelace (12-345)"
    old_dir.rename(author_dir)
    (author_dir / "paper.bib").write_text("@article{Key, title={Computing Machinery}, year={2026}}\n", encoding="utf-8")
    (repo / "output" / "baseline.json").write_text('{"total":1,"authors":{"Lovelace (12-345)":1}}\n', encoding="utf-8")
    commit = _commit(repo)
    row = replace(census.rows[0], scholar_id="", dblp_id="12/345")
    census = AuthorCensus((row,))
    spec = GenerationSpec(census, "policy-v1", {"dblp": "1", "doi_csl": "1", "s2": "1"}, commit)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        _complete_empty_inventory(
            ledger,
            spec,
            dblp_xml=b'<dblpperson key="homepages/12/345" n="1"><r><article key="journals/x/1">'
            b"<author>Ada Lovelace</author><title>Computing Machinery</title>"
            b"<year>2026</year><ee>https://doi.org/10.1000/x</ee></article></r></dblpperson>",
        )
        corpus = scan_existing_corpus(repo, census, generation_id=spec.id, base_commit=commit)
        with pytest.raises(ValueError, match="cross-source late-identifier split"):
            ledger._commit_existing_corpus(corpus)


def test_accepted_committed_corpus_golden_membership() -> None:
    census = load_census(Path("data/input.csv"))
    base_commit = _git(Path("."), "rev-parse", "HEAD")
    evidence = scan_existing_corpus(Path("."), census, generation_id="golden", base_commit=base_commit)
    parsed = [item for item in evidence.items if item.disposition == "parsed"]
    absent = [item for item in evidence.items if item.disposition == "absent"]
    assert len(census.enabled_rows) == 64
    assert len({item.author_key for item in parsed}) == 57
    assert len(parsed) == len(evidence.publications) == len(evidence.seeds) == 2_575
    assert len(absent) == 7
    assert evidence.derived_a2i2_count == 1_094
    assert evidence.baseline_total == 3_669
    assert all(not item.publication_keys and not item.normalized_entry for item in absent)


def test_real_census_generation_identity_creates_and_resumes(tmp_path: Path) -> None:
    census = load_census(Path("data/input.csv"))
    base_commit = _git(Path("."), "rev-parse", "HEAD")
    spec = GenerationSpec(
        census,
        "policy-v1",
        {"dblp": "1", "doi_csl": "1", "s2": "1", "scholar": "1"},
        base_commit,
    )
    path = tmp_path / "real-census.db"
    with Ledger.open(path) as ledger:
        ledger.create_or_resume(spec, census)
        ledger.create_or_resume(spec, census)
    with Ledger.open(path) as reopened:
        assert reopened.manifest().data["generation"]["generation_id"] == spec.id


def test_missing_enabled_author_has_explicit_directory_absence(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    for path in author_dir.iterdir():
        path.unlink()
    author_dir.rmdir()
    (repo / "output" / "baseline.json").write_text('{"total":0,"authors":{}}\n', encoding="utf-8")
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert not evidence.publications and not evidence.seeds
    assert len(evidence.items) == 1
    assert evidence.items[0].disposition == "absent"
    assert evidence.items[0].source_path.endswith("/.citeforge-absent-directory")


@pytest.mark.parametrize("author_count", [1, 1_000])
def test_corpus_scan_scale_does_not_create_planning_rounds(tmp_path: Path, author_count: int) -> None:
    repo, _commit_id, _census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    for path in author_dir.iterdir():
        path.unlink()
    author_dir.rmdir()
    (repo / "output" / "baseline.json").write_text('{"total":0,"authors":{}}\n', encoding="utf-8")
    commit = _commit(repo)
    census = AuthorCensus(
        tuple(
            AuthorCensusRow(
                index + 2,
                f"author-{index}",
                f"Author {index}",
                f"author {index}",
                f"Scholar{index:06d}",
                "",
                True,
                "",
                TaskDisposition.PENDING,
            )
            for index in range(author_count)
        )
    )
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert len(evidence.items) == author_count
    assert all(item.disposition == "absent" for item in evidence.items)
    assert not evidence.publications and not evidence.seeds


@pytest.mark.parametrize("mutation", ["symlink", "unexpected", "bad_baseline", "duplicate_identity"])
def test_committed_corpus_scan_rejects_unsafe_or_ambiguous_tree(tmp_path: Path, mutation: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    if mutation == "symlink":
        (author_dir / "link.bib").symlink_to("paper.bib")
    elif mutation == "unexpected":
        (repo / "output" / "unexpected.txt").write_text("x", encoding="utf-8")
    elif mutation == "bad_baseline":
        (repo / "output" / "baseline.json").write_text(
            '{"total":2,"authors":{"Lovelace (Scholar123)":1}}\n', encoding="utf-8"
        )
    else:
        (author_dir / "duplicate.bib").write_text(
            "@article{Other, title={A title}, year={2026}, doi={10.1000/x}}\n", encoding="utf-8"
        )
        (repo / "output" / "baseline.json").write_text(
            '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
        )
    commit = _commit(repo)
    with pytest.raises(ValueError):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "fault",
    [
        "after_c3_corpus_snapshot",
        "after_c3_corpus_items",
        "after_c3_corpus_publications",
        "after_c3_corpus_seeds",
    ],
)
def test_composite_corpus_faults_roll_back_every_boundary(tmp_path: Path, fault: str) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    ledger_path = tmp_path / f"{fault}.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(ledger_path) as ledger:
        _complete_empty_inventory(ledger, spec)
        ledger.set_fault(fault)
        with pytest.raises(FaultInjectedError, match=fault):
            ledger.scan_and_commit_corpus(repo)
    with Ledger.open(ledger_path) as reopened:
        for table in (
            "corpus_snapshots",
            "corpus_items",
            "publication_seed_evidence",
            "corpus_scan_receipts",
        ):
            assert (
                reopened._connection.execute(
                    f"SELECT COUNT(*) FROM {table}"  # noqa: S608 - fixed table matrix
                ).fetchone()[0]
                == 0
            )
        assert reopened._connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0] == 0


def test_ledger_corpus_scan_uses_code_owned_authority_not_public_exports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citeforge.refresh.corpus as corpus_module

    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)

    def forged_scan(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("public scanner callback must not own ledger authority")

    monkeypatch.setattr(corpus_module, "scan_existing_corpus", forged_scan)
    monkeypatch.setattr(corpus_module, "publication_key_for", lambda *_args: "f" * 64)
    monkeypatch.setattr(corpus_module, "SCANNER_ID", "caller-owned-scanner")
    monkeypatch.setattr(corpus_module, "A2I2_POLICY_VERSION", "caller-owned-policy")
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        evidence = ledger.scan_and_commit_corpus(repo)
        assert evidence.snapshot.scanner_id == "citeforge.committed-corpus"
        assert evidence.snapshot.a2i2_policy_version == "1"


def test_ledger_independently_rejects_substituted_publication_derivation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import citeforge.refresh.corpus as corpus_module

    repo, commit, census = _committed_corpus(tmp_path)
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    original = corpus_module._publication

    def forged_publication(*args: object, **kwargs: object) -> PublicationMetadata:
        publication = original(*args, **kwargs)
        return replace(publication, normalized_title="forged title")

    monkeypatch.setattr(corpus_module, "_publication", forged_publication)
    with Ledger.open(tmp_path / "ledger.db") as ledger:
        _complete_empty_inventory(ledger, spec)
        with pytest.raises(ValueError, match="independently derived"):
            ledger.scan_and_commit_corpus(repo)
        assert ledger._connection.execute("SELECT COUNT(*) FROM corpus_items").fetchone()[0] == 0


def test_committed_corpus_allows_distinct_similar_doi_and_no_doi_titles(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").write_text(
        "@article{One, title={Computing Machinery and Intelligence}, year={2026}, doi={10.1000/X}}\n",
        encoding="utf-8",
    )
    (author_dir / "split.bib").write_text(
        "@article{Two, title={Computing Machinery}, year={2026}}\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert len(evidence.publications) == 2


def test_committed_corpus_rejects_multiple_dois_hidden_in_one_url(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@article{Key, title={A title}, year={2026}, url={https://doi.org/10.1000/x and https://doi.org/10.1000/y}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="conflicting primary DOI"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_rejects_multiple_arxiv_ids_hidden_in_one_field(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@article{Key, title={A title}, year={2026}, "
        "howpublished={https://arxiv.org/abs/2501.00002 and https://arxiv.org/abs/2401.00001}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="conflicting arXiv"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "url",
    [
        "https://doi.org/not-a-doi",
        "https://doi.org/not-a-doi/10.1234/foo",
        "https://arxiv.org/abs/not-an-id",
    ],
)
def test_committed_corpus_rejects_invalid_resolver_paths(tmp_path: Path, url: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, url={{{url}}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="invalid"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_committed_corpus_decodes_percent_encoded_doi_resolver_path(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@article{Key, title={A title}, year={2026}, url={https://doi.org/10.1234%2Ffoo}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.publications[0].exact_identifiers["doi"] == "10.1234/foo"


def test_committed_corpus_extracts_legacy_arxiv_url_identifier(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        "@misc{Key, title={A title}, year={2026}, url={https://arxiv.org/abs/cs/9901001}}\n",
        encoding="utf-8",
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert evidence.publications[0].exact_identifiers["arxiv"] == "cs/9901001"


@pytest.mark.parametrize("field", ["author={Ada Lovelace <ada@example.com>}", "note={See http://127.0.0.1/private}"])
def test_committed_corpus_rejects_private_text_anywhere(tmp_path: Path, field: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{Key, title={{A title}}, year={{2026}}, {field}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"unsafe|private"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("key", ["ada@example.com", "https://127.0.0.1/x"])
def test_committed_corpus_rejects_private_citation_key(tmp_path: Path, key: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    (repo / "output" / "Lovelace (Scholar123)" / "paper.bib").write_text(
        f"@article{{{key}, title={{A title}}, year={{2026}}}}\n", encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match=r"unsafe|private"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize(
    "identifier_fields",
    [
        "archiveprefix={arXiv}, eprint={2501.00002}",
        "x_s2_paper_id={0123456789abcdef0123456789abcdef01234567}",
    ],
)
def test_committed_corpus_rejects_exact_identifier_reuse(tmp_path: Path, identifier_fields: str) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    for filename, title in (("paper.bib", "First work"), ("other.bib", "Second work")):
        (author_dir / filename).write_text(
            f"@article{{Key{filename}, title={{{title}}}, year={{2026}}, {identifier_fields}}}\n",
            encoding="utf-8",
        )
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="reuses an exact identifier"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


@pytest.mark.parametrize("doi_filename", ["a-doi.bib", "z-doi.bib"])
def test_committed_corpus_accepts_exact_two_arxiv_version_representations_independent_of_path_order(
    tmp_path: Path, doi_filename: str
) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").write_text(
        "@misc{Preprint, title={Early title}, year={2026}, archiveprefix={arXiv}, eprint={2501.00002}}\n",
        encoding="utf-8",
    )
    (author_dir / doi_filename).write_text(
        "@article{Version, title={Published title}, year={2026}, doi={10.48550/arxiv.2501.00002}, "
        "archiveprefix={arXiv}, eprint={2501.00002}}\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert len(evidence.publications) == 2


def test_committed_corpus_accepts_same_title_arxiv_preprint_and_canonical_doi_pair(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").write_text(
        "@misc{Preprint, title={Same work}, year={2026}, archiveprefix={arXiv}, eprint={2501.00002}}\n",
        encoding="utf-8",
    )
    (author_dir / "published.bib").write_text(
        "@article{Published, title={Same work}, year={2026}, doi={10.48550/arxiv.2501.00002}, "
        "archiveprefix={arXiv}, eprint={2501.00002}}\n",
        encoding="utf-8",
    )
    (repo / "output" / "baseline.json").write_text(
        '{"total":2,"authors":{"Lovelace (Scholar123)":2}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    evidence = scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)
    assert len(evidence.publications) == 2


def test_committed_corpus_rejects_three_arxiv_version_representations(tmp_path: Path) -> None:
    repo, _commit_id, census = _committed_corpus(tmp_path)
    author_dir = repo / "output" / "Lovelace (Scholar123)"
    (author_dir / "paper.bib").unlink()
    for filename, title, doi in (
        ("z.bib", "First", ""),
        ("a.bib", "Second", "doi={10.48550/arxiv.2501.00002},"),
        ("m.bib", "Third", ""),
    ):
        (author_dir / filename).write_text(
            f"@misc{{{title}, title={{{title}}}, year={{2026}}, {doi} "
            "archiveprefix={arXiv}, eprint={2501.00002}}\n",
            encoding="utf-8",
        )
    (repo / "output" / "baseline.json").write_text(
        '{"total":3,"authors":{"Lovelace (Scholar123)":3}}\n', encoding="utf-8"
    )
    commit = _commit(repo)
    with pytest.raises(ValueError, match="reuses an exact identifier"):
        scan_existing_corpus(repo, census, generation_id="generation", base_commit=commit)


def test_summary_csv_bytes_are_bound_to_output_tree_digest(tmp_path: Path) -> None:
    repo, first_commit, census = _committed_corpus(tmp_path)
    first = scan_existing_corpus(repo, census, generation_id="generation", base_commit=first_commit)
    (repo / "output" / "summary.csv").write_text("title\nchanged\n", encoding="utf-8")
    second_commit = _commit(repo)
    second = scan_existing_corpus(repo, census, generation_id="generation", base_commit=second_commit)
    assert first.snapshot.output_tree_digest != second.snapshot.output_tree_digest


def test_reopen_rejects_mutated_corpus_publication_row(tmp_path: Path) -> None:
    repo, commit, census = _committed_corpus(tmp_path)
    path = tmp_path / "ledger.db"
    spec = GenerationSpec(census, "policy-v1", {"doi_csl": "1", "s2": "1", "scholar": "1"}, commit)
    with Ledger.open(path) as ledger:
        _complete_empty_inventory(ledger, spec)
        ledger.scan_and_commit_corpus(repo)
        ledger._connection.execute("DROP TRIGGER publications_append_only_update")
        ledger._connection.execute("UPDATE publications SET normalized_title = 'forged'")
        ledger._connection.execute(
            "CREATE TRIGGER publications_append_only_update BEFORE UPDATE ON publications "
            "BEGIN SELECT RAISE(ABORT, 'planning identity is append-only'); END"
        )
    with pytest.raises(ValueError, match="independently derived"):
        Ledger.open(path, corpus_repo_root=repo)
