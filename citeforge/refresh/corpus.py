"""Read-only committed-corpus authority for Task 5C discovery."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from urllib.parse import parse_qsl, unquote, urlsplit

from ..bibtex_utils import parse_bibtex_to_dict, parse_strict_bibtex_document
from ..config import SIM_MERGE_DUPLICATE_THRESHOLD
from ..fsscan import GitTreeEntry, read_committed_blobs, read_committed_tree
from ..id_utils import (
    doi_bases_match,
    extract_arxiv_eprint,
    find_doi_in_text,
    find_dois_in_text,
    is_secondary_doi,
    normalize_doi,
    normalize_strict_arxiv_id,
)
from ..text_utils import (
    extract_year_from_any,
    format_author_dirname,
    normalize_person_name,
    normalize_title,
    title_similarity,
)
from .authority import (
    CorpusItemEvidence,
    CorpusSnapshot,
    EvidenceKind,
    PublicationSeedEvidence,
    evidence_digest,
    publication_key_for,
)
from .census import AuthorCensus, AuthorCensusRow, _dblp_id, _scholar_id
from .ledger import PublicationMetadata
from .privacy import ensure_public_https_url, ensure_safe_durable_text

_publication_key_authority = publication_key_for

_CORPUS_AUTHORITY = (
    "citeforge.committed-corpus",
    "1",
    "citeforge.strict-bibtex",
    "1",
)
SCANNER_ID, SCANNER_VERSION, PARSER_ID, PARSER_VERSION = _CORPUS_AUTHORITY
FRESHNESS_POLICY = "monthly"
A2I2_POLICY_VERSION = "1"
_A2I2_POLICY = ("1", 2020, 2026)
_DIGIT_ID = re.compile(r"[0-9]+")
_ARXIV_ID = re.compile(r"(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})", re.I)
_ARXIV_URL_ID = r"(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})"
_S2_ID = re.compile(r"[0-9a-f]{40}", re.I)
_OPENALEX_ID = re.compile(r"(?:https://openalex\.org/)?(W\d+)", re.I)
_SECRET_KEY = re.compile(r"(?:^|[_-])(?:authorization|api[_-]?key|cookie|token|secret|password)(?:$|[_-])", re.I)


def _valid_arxiv_id(value: str) -> bool:
    return normalize_strict_arxiv_id(value) is not None


def _canonical_arxiv_id(value: str) -> str:
    normalized = normalize_strict_arxiv_id(value)
    if normalized is None:
        raise ValueError("committed corpus entry contains invalid arXiv identifier")
    return normalized


@dataclass(frozen=True)
class ExistingCorpusEvidence:
    snapshot: CorpusSnapshot
    items: tuple[CorpusItemEvidence, ...]
    publications: tuple[PublicationMetadata, ...]
    seeds: tuple[PublicationSeedEvidence, ...]
    derived_a2i2_count: int
    baseline_total: int
    git_proof: GitProof


@dataclass(frozen=True)
class GitProof:
    base_commit: str
    output_entries: tuple[GitTreeEntry, ...]
    a2i2_entries: tuple[GitTreeEntry, ...]
    blob_digests: tuple[tuple[str, str], ...]


def attest_existing_corpus(repo_root: Path, proof: GitProof) -> None:
    """Re-attest immutable Git bytes without reparsing or reducing the corpus."""
    output_entries = read_committed_tree(repo_root, proof.base_commit, "output")
    a2i2_entries = read_committed_tree(repo_root, proof.base_commit, "data/a2i2.csv")
    if output_entries != proof.output_entries or a2i2_entries != proof.a2i2_entries:
        raise ValueError("trusted Git tree changed from cached corpus proof")
    object_ids = tuple(
        dict.fromkeys(entry.object_id for entry in (*output_entries, *a2i2_entries) if entry.object_type == "blob")
    )
    blobs = read_committed_blobs(repo_root, object_ids)
    digests = tuple(sorted((object_id, hashlib.sha256(body).hexdigest()) for object_id, body in blobs.items()))
    if digests != proof.blob_digests:
        raise ValueError("trusted Git blobs changed from cached corpus proof")


@dataclass(frozen=True)
class _ParsedSource:
    path: str
    author: AuthorCensusRow
    content: bytes
    entry: Mapping[str, object]
    legacy_entry: Mapping[str, object]
    publication: PublicationMetadata


def corpus_author_set_digest(rows: Sequence[AuthorCensusRow]) -> str:
    """Derive the scanner's enabled-author directory authority."""
    return evidence_digest(
        [
            {
                "author_key": row.row_key,
                "directory": format_author_dirname(row.name, row.scholar_id or row.dblp_id),
            }
            for row in sorted((row for row in rows if row.enabled), key=lambda item: item.row_key)
        ]
    )


def _blob_map(repo_root: Path, entries: Sequence[GitTreeEntry]) -> dict[str, bytes]:
    blob_entries = []
    for entry in entries:
        if entry.object_type != "blob":
            continue
        if entry.mode != "100644":
            raise ValueError("committed corpus contains executable or symlink blob mode")
        blob_entries.append(entry)
    by_id = read_committed_blobs(repo_root, tuple(dict.fromkeys(entry.object_id for entry in blob_entries)))
    return {entry.path: by_id[entry.object_id] for entry in blob_entries}


def _tree_digest(entries: Sequence[GitTreeEntry], blobs: Mapping[str, bytes]) -> str:
    content = []
    for entry in entries:
        content.append(
            {
                "blob_digest": hashlib.sha256(blobs[entry.path]).hexdigest() if entry.object_type == "blob" else None,
                "mode": entry.mode,
                "object_id": entry.object_id,
                "object_type": entry.object_type,
                "path": entry.path,
            }
        )
    return evidence_digest(content)


def _contains_secret(value: object, *, key: str = "") -> bool:
    if key and _SECRET_KEY.search(key):
        return True
    if isinstance(value, Mapping):
        return any(_contains_secret(item, key=str(item_key)) for item_key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_secret(item) for item in value)
    return False


def _baseline(content: bytes, counts: Mapping[str, int]) -> str:
    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("committed baseline.json is malformed")
            result[key] = value
        return result

    def reject_constant(_value: str) -> object:
        raise ValueError("committed baseline.json is malformed")

    try:
        value = json.loads(content.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("committed baseline.json is malformed") from exc
    if not isinstance(value, dict) or set(value) != {"authors", "total"} or _contains_secret(value):
        raise ValueError("committed baseline.json schema is unsupported or secret-bearing")
    authors = value.get("authors")
    total = value.get("total")
    if (
        not isinstance(authors, dict)
        or any(
            not isinstance(key, str) or isinstance(count, bool) or not isinstance(count, int) or count < 0
            for key, count in authors.items()
        )
        or isinstance(total, bool)
        or not isinstance(total, int)
        or dict(sorted(authors.items())) != dict(sorted(counts.items()))
        or total != sum(counts.values())
    ):
        raise ValueError("committed baseline.json membership or counts are stale")
    return evidence_digest(
        {
            "blob_digest": hashlib.sha256(content).hexdigest(),
            "content": {"authors": dict(sorted(authors.items())), "total": total},
            "schema_version": "1",
        }
    )


def _identifiers(entry: Mapping[str, object]) -> Mapping[str, str]:
    fields = entry.get("fields")
    if not isinstance(fields, Mapping):
        raise ValueError("parsed corpus fields are absent")
    raw_doi = str(fields.get("doi", ""))
    explicit_doi = normalize_doi(raw_doi)
    if raw_doi.strip() and (explicit_doi is None or normalize_doi(find_doi_in_text(explicit_doi)) != explicit_doi):
        raise ValueError("committed corpus entry contains invalid explicit DOI")
    referenced_dois = {
        value
        for value in (
            explicit_doi,
            *(
                doi
                for field in ("url", "howpublished")
                for doi in find_dois_in_text(unquote(str(fields.get(field, ""))))
            ),
        )
        if value
    }
    primary_dois = {value for value in referenced_dois if not is_secondary_doi(value)}
    secondary_dois = referenced_dois - primary_dois
    if len(primary_dois) > 1:
        raise ValueError("committed corpus entry contains conflicting primary DOI representations")
    if len(secondary_dois) > 1:
        raise ValueError("committed corpus entry contains conflicting secondary DOI representations")
    result: dict[str, str] = {}
    canonical_doi = next(iter(primary_dois or secondary_dois), None)
    if canonical_doi:
        result["doi"] = canonical_doi
    if primary_dois and secondary_dois:
        result["secondary_doi"] = next(iter(secondary_dois))
    arxiv_candidates = set()
    if canonical_doi:
        doi_arxiv = re.fullmatch(r"10\.48550/arxiv\.(.+)", canonical_doi, re.I)
        if doi_arxiv:
            arxiv_candidates.add(_canonical_arxiv_id(doi_arxiv.group(1)))
    for candidate_fields in (
        {"archiveprefix": fields.get("archiveprefix"), "eprint": fields.get("eprint")},
        {"doi": fields.get("doi")},
        {"journal": fields.get("journal")},
        {"journal": fields.get("howpublished")},
    ):
        if arxiv := extract_arxiv_eprint({"fields": candidate_fields}):
            arxiv_candidates.add(_canonical_arxiv_id(arxiv))
    for field in ("url", "howpublished"):
        for match in re.finditer(rf"(?i)arxiv\.org/(?:abs|pdf)/{_ARXIV_URL_ID}(?:v\d+)?", str(fields.get(field, ""))):
            if arxiv := extract_arxiv_eprint({"fields": {"archiveprefix": "arxiv", "eprint": match[1]}}):
                arxiv_candidates.add(_canonical_arxiv_id(arxiv))
    if len(arxiv_candidates) > 1:
        raise ValueError("committed corpus entry contains conflicting arXiv representations")
    if arxiv_candidates:
        arxiv = next(iter(arxiv_candidates))
        if not _valid_arxiv_id(arxiv):
            raise ValueError("committed corpus entry contains invalid explicit arXiv evidence")
        result["arxiv"] = arxiv
    pmids = {str(fields[key]).strip() for key in ("pmid", "x_pmid") if key in fields and str(fields[key]).strip()}
    if len(pmids) > 1 or (pmids and not _DIGIT_ID.fullmatch(next(iter(pmids)))):
        raise ValueError("committed corpus entry contains conflicting or invalid PMID")
    if pmids:
        result["pmid"] = next(iter(pmids))
    s2 = str(fields.get("x_s2_paper_id", "")).strip()
    if s2:
        if _S2_ID.fullmatch(s2) is None:
            raise ValueError("committed corpus entry contains invalid Semantic Scholar paper ID")
        result["s2"] = s2.lower()
    openalex = str(fields.get("x_openalex_id", "")).strip()
    if openalex:
        openalex_match = _OPENALEX_ID.fullmatch(openalex)
        if openalex_match is None:
            raise ValueError("committed corpus entry contains invalid OpenAlex work ID")
        result["openalex"] = openalex_match[1].upper()
    return MappingProxyType(dict(sorted(result.items())))


def _publication(author: AuthorCensusRow, path: str, entry: Mapping[str, object]) -> PublicationMetadata:
    fields = entry["fields"]
    if not isinstance(fields, Mapping):
        raise ValueError("parsed corpus fields are absent")
    title = normalize_title(str(fields["title"]))
    if not title:
        raise ValueError("committed corpus normalized title is blank")
    year = extract_year_from_any(fields.get("year"), fallback=0) or None
    if year is None:
        raise ValueError("committed corpus year is absent or invalid")
    identifiers = _identifiers(entry)
    ensure_safe_durable_text(str(entry.get("key", "")))
    for value in fields.values():
        ensure_safe_durable_text(str(value))
    for field in ("url", "howpublished"):
        raw_url = str(fields.get(field, "")).strip()
        if not raw_url:
            continue
        if field == "howpublished" and re.match(r"(?i)^doi\s*:", raw_url):
            decoded = unquote(raw_url)
            if "%" in decoded:
                raise ValueError("committed corpus contains an invalid direct DOI")
            direct_doi = normalize_doi(decoded)
            if direct_doi is None or normalize_doi(find_doi_in_text(direct_doi)) != direct_doi:
                raise ValueError("committed corpus contains an invalid direct DOI")
            continue
        if field == "howpublished" and re.match(r"(?i)^arxiv\s*:", raw_url):
            direct_arxiv = re.fullmatch(r"(?i)arxiv\s*:\s*(\S+)", raw_url)
            if direct_arxiv is None or not _valid_arxiv_id(direct_arxiv.group(1)):
                raise ValueError("committed corpus contains an invalid direct arXiv identifier")
            continue
        if (
            field == "howpublished"
            and not raw_url.startswith("//")
            and not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw_url)
        ):
            continue
        try:
            parsed_url = urlsplit(raw_url)
            port = parsed_url.port
        except ValueError as exc:
            raise ValueError("committed corpus contains an unsafe URL") from exc
        ensure_public_https_url(raw_url)
        if port not in {None, 443}:
            raise ValueError("committed corpus contains an unsafe URL")
        resolver_host = (parsed_url.hostname or "").rstrip(".").casefold()
        if resolver_host in {"doi.org", "dx.doi.org"}:
            decoded_path = unquote(parsed_url.path)
            if "%" in decoded_path:
                raise ValueError("committed corpus contains an invalid DOI resolver URL")
            resolver_doi = normalize_doi(decoded_path.lstrip("/"))
            if normalize_doi(find_doi_in_text(resolver_doi or "")) != resolver_doi:
                raise ValueError("committed corpus contains an invalid DOI resolver URL")
        if resolver_host == "arxiv.org" and not re.fullmatch(
            r"/(?:abs|pdf)/(?:\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?(?:\.pdf)?",
            parsed_url.path,
            re.I,
        ):
            raise ValueError("committed corpus contains an invalid arXiv resolver URL")
    publication_key = _publication_key_authority(author.row_key, title, year, str(identifiers.get("doi", "")) or None)
    return PublicationMetadata(
        author.row_key,
        publication_key,
        "corpus",
        title,
        year,
        identifiers,
        path,
        FRESHNESS_POLICY,
    )


def _a2i2_expected(
    a2i2_csv: bytes,
    census: AuthorCensus,
    sources: Sequence[_ParsedSource],
    year_window: tuple[int, int],
) -> tuple[dict[str, bytes], bool]:
    try:
        reader = csv.DictReader(io.StringIO(a2i2_csv.decode("utf-8")), strict=True)
        rows = tuple(reader)
    except (UnicodeDecodeError, csv.Error) as exc:
        raise ValueError("committed data/a2i2.csv is malformed") from exc
    if reader.fieldnames != ["Name", "Scholar Link", "DBLP Link"]:
        raise ValueError("committed data/a2i2.csv schema is unsupported")
    if any(None in row or set(row) != {"Name", "Scholar Link", "DBLP Link"} for row in rows):
        raise ValueError("committed data/a2i2.csv row width is malformed")
    member_names = [normalize_person_name(str(row.get("Name", ""))) for row in rows if str(row.get("Name", "")).strip()]
    if len(member_names) != len(set(member_names)):
        raise ValueError("committed a2i2 member name is duplicated")
    members = set(member_names)
    census_name_counts: dict[str, int] = {}
    for row in census.rows:
        name = normalize_person_name(row.name)
        census_name_counts[name] = census_name_counts.get(name, 0) + 1
    census_names = set(census_name_counts)
    if not members <= census_names:
        raise ValueError("committed a2i2 member is absent from census")
    if any(census_name_counts[name] != 1 for name in members):
        raise ValueError("committed a2i2 member name is ambiguous in census")
    census_by_name = {normalize_person_name(row.name): row for row in census.rows}
    for physical_row, raw in enumerate(rows, start=2):
        name = normalize_person_name(str(raw.get("Name", "")))
        if not name:
            if any(str(raw.get(field, "")).strip() for field in ("Scholar Link", "DBLP Link")):
                raise ValueError("committed a2i2 member identity has no name")
            continue
        member = census_by_name[name]
        try:
            scholar_id = _scholar_id(str(raw.get("Scholar Link", "")).strip(), physical_row)
            dblp_id = _dblp_id(str(raw.get("DBLP Link", "")).strip(), physical_row)
        except ValueError as exc:
            raise ValueError("committed a2i2 member identity is malformed") from exc
        scholar_link = str(raw.get("Scholar Link", "")).strip()
        dblp_link = str(raw.get("DBLP Link", "")).strip()
        if scholar_link:
            ensure_public_https_url(scholar_link)
            parsed_scholar = urlsplit(scholar_link)
            if parse_qsl(parsed_scholar.query, keep_blank_values=True, strict_parsing=True) != [("user", scholar_id)]:
                raise ValueError("committed a2i2 member identity is not canonical")
        if dblp_link:
            ensure_public_https_url(dblp_link)
        if scholar_id != member.scholar_id or dblp_id != member.dblp_id:
            raise ValueError("committed a2i2 member identity does not match census")
    member_order = {name: index for index, name in enumerate(member_names)}
    selected = []
    for source in sorted(
        sources,
        key=lambda item: (member_order.get(normalize_person_name(item.author.name), len(member_order)), item.path),
    ):
        if normalize_person_name(source.author.name) not in members:
            continue
        fields = source.legacy_entry["fields"]
        assert isinstance(fields, Mapping)
        year = extract_year_from_any(fields.get("year"), fallback=0)
        if year is not None and year_window[0] <= year <= year_window[1]:
            selected.append(source)

    def richness(source: _ParsedSource) -> tuple[int, str]:
        fields = source.legacy_entry["fields"]
        assert isinstance(fields, Mapping)
        return sum(bool(str(value).strip()) for value in fields.values()), source.path

    def richer(first: _ParsedSource, second: _ParsedSource) -> _ParsedSource:
        first_score, first_path = richness(first)
        second_score, second_path = richness(second)
        if first_score != second_score:
            return first if first_score > second_score else second
        return first if first_path <= second_path else second

    def legacy_title(source: _ParsedSource) -> str:
        fields = source.legacy_entry["fields"]
        if not isinstance(fields, Mapping):
            raise ValueError("legacy a2i2 parser fields are malformed")
        return str(fields.get("title", ""))

    kept: list[_ParsedSource] = []
    doi_to_index: dict[str, int] = {}
    seen: set[int] = set()
    for index, source in enumerate(selected):
        fields = source.legacy_entry["fields"]
        assert isinstance(fields, Mapping)
        doi = normalize_doi(fields.get("doi")) or ""
        if not doi:
            continue
        matched = (
            doi if doi in doi_to_index else next((item for item in doi_to_index if doi_bases_match(doi, item)), None)
        )
        if matched is None:
            doi_to_index[doi] = len(kept)
            kept.append(source)
        else:
            kept[doi_to_index[matched]] = richer(kept[doi_to_index[matched]], source)
        seen.add(index)
    for index, source in enumerate(selected):
        if index in seen:
            continue
        title = legacy_title(source)
        match = next(
            (
                position
                for position, item in enumerate(kept)
                if title_similarity(title, legacy_title(item)) >= SIM_MERGE_DUPLICATE_THRESHOLD
            ),
            None,
        )
        if match is None:
            kept.append(source)
        else:
            kept[match] = richer(kept[match], source)
    result: dict[str, bytes] = {}
    for source in sorted(kept, key=lambda item: PurePosixPath(item.path).name):
        filename = PurePosixPath(source.path).name
        if filename in result:
            stem, suffix = filename.rsplit(".", 1)
            counter = 2
            while f"{stem}_{counter}.{suffix}" in result:
                counter += 1
            filename = f"{stem}_{counter}.{suffix}"
        result[filename] = source.content.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n").encode()
    return result, bool(members)


def scan_existing_corpus(
    repo_root: Path,
    census: AuthorCensus,
    *,
    generation_id: str,
    base_commit: str,
    a2i2_year_window: tuple[int, int] = (_A2I2_POLICY[1], _A2I2_POLICY[2]),
) -> ExistingCorpusEvidence:
    """Build immutable evidence from the exact committed output tree."""
    entries = read_committed_tree(repo_root, base_commit, "output")
    blobs = _blob_map(repo_root, entries)
    try:
        a2i2_entry = read_committed_tree(repo_root, base_commit, "data/a2i2.csv")
        a2i2_csv = _blob_map(repo_root, a2i2_entry)["data/a2i2.csv"]
    except (KeyError, ValueError) as exc:
        raise ValueError("committed data/a2i2.csv authority is absent") from exc
    enabled = census.enabled_rows
    expected: dict[str, AuthorCensusRow] = {}
    all_directories: dict[str, AuthorCensusRow] = {}
    for row in census.rows:
        ensure_safe_durable_text(row.name)
        ensure_safe_durable_text(row.normalized_name)
        dirname = format_author_dirname(row.name, row.scholar_id or row.dblp_id)
        ensure_safe_durable_text(dirname)
        folded = dirname.casefold()
        if folded in all_directories:
            raise ValueError("census maps multiple authors to one output directory")
        all_directories[folded] = row
        if row.enabled:
            expected[folded] = row
    tree_paths = {entry.path for entry in entries if entry.object_type == "tree"}
    root_children = {
        PurePosixPath(path).parts[1] for path in tree_paths | set(blobs) if len(PurePosixPath(path).parts) >= 2
    }
    allowed = {format_author_dirname(row.name, row.scholar_id or row.dblp_id) for row in enabled} | {
        "a2i2",
        "baseline.json",
        "summary.csv",
    }
    if root_children - allowed:
        unknown = sorted(root_children - allowed)[0]
        if unknown.casefold() in all_directories and not all_directories[unknown.casefold()].enabled:
            raise ValueError("committed output contains a disabled-author directory")
        raise ValueError("committed output contains an unexpected root entry")
    if "output/baseline.json" not in blobs or "output/summary.csv" not in blobs:
        raise ValueError("committed output metadata is incomplete")
    if "output/a2i2" in blobs:
        raise ValueError("committed a2i2 authority must be a directory")

    sources: list[_ParsedSource] = []
    provisional_items: list[CorpusItemEvidence] = []
    publications: list[PublicationMetadata] = []
    for row in enabled:
        dirname = format_author_dirname(row.name, row.scholar_id or row.dblp_id)
        prefix = f"output/{dirname}"
        if prefix in blobs:
            raise ValueError("committed author directory is a regular blob")
        author_blobs = sorted((path, content) for path, content in blobs.items() if path.startswith(f"{prefix}/"))
        nested_trees = [path for path in tree_paths if path.startswith(f"{prefix}/")]
        invalid_author_blob = any(
            len(PurePosixPath(path).parts) != 3 or not path.endswith(".bib") or PurePosixPath(path).name == ".bib"
            for path, _ in author_blobs
        )
        if nested_trees or invalid_author_blob:
            raise ValueError("committed author directory contains nested or non-BibTeX entries")
        if not author_blobs:
            absent_path = f"{prefix}/.citeforge-absent-directory"
            absent_digest = evidence_digest({"disposition": "absent", "path": prefix, "version": "1"})
            provisional_items.append(
                CorpusItemEvidence(
                    generation_id,
                    "0" * 64,
                    absent_path,
                    row.row_key,
                    absent_digest,
                    absent_digest,
                    (),
                    "absent",
                )
            )
            continue
        for path, content in author_blobs:
            ensure_safe_durable_text(path)
            entry = parse_strict_bibtex_document(content)
            legacy_entry = parse_bibtex_to_dict(content.decode("utf-8"))
            if legacy_entry is None:
                raise ValueError("committed BibTeX is absent from legacy a2i2 parser domain")
            publication = _publication(row, path, entry)
            before_digest = hashlib.sha256(content).hexdigest()
            parse_digest = evidence_digest(entry)
            provisional_items.append(
                CorpusItemEvidence(
                    generation_id,
                    "0" * 64,
                    path,
                    row.row_key,
                    before_digest,
                    parse_digest,
                    (publication.publication_key,),
                    "parsed",
                    publication.exact_identifiers,
                    entry,
                )
            )
            publications.append(publication)
            sources.append(_ParsedSource(path, row, content, entry, legacy_entry, publication))
    publication_keys = [(item.author_key, item.publication_key) for item in publications]
    if len(publication_keys) != len(set(publication_keys)):
        raise ValueError("committed corpus contains duplicate stable publication identity")
    exact_groups: dict[tuple[str, str, str], list[PublicationMetadata]] = {}
    for publication in publications:
        for kind, value in publication.exact_identifiers.items():
            identity = (publication.author_key, str(kind), str(value))
            exact_groups.setdefault(identity, []).append(publication)
    for (_author_key, kind, value), group in exact_groups.items():
        if len(group) == 1:
            continue
        dois = sorted(str(publication.exact_identifiers.get("doi", "")).casefold() for publication in group)
        if kind == "arxiv" and len(group) == 2 and dois == ["", f"10.48550/arxiv.{value}".casefold()]:
            continue
        raise ValueError("committed corpus reuses an exact identifier across same-role publications")
    grouped_identity: dict[tuple[str, str, int | None], list[PublicationMetadata]] = {}
    for publication in publications:
        grouped_identity.setdefault(
            (publication.author_key, publication.normalized_title, publication.year), []
        ).append(publication)
    for group in grouped_identity.values():
        if not (
            len(group) > 1
            and any("doi" in publication.exact_identifiers for publication in group)
            and any("doi" not in publication.exact_identifiers for publication in group)
        ):
            continue
        arxiv_values = {str(publication.exact_identifiers.get("arxiv", "")) for publication in group}
        dois = sorted(str(publication.exact_identifiers.get("doi", "")).casefold() for publication in group)
        if len(group) == 2 and len(arxiv_values) == 1 and "" not in arxiv_values:
            arxiv = next(iter(arxiv_values))
            if dois == ["", f"10.48550/arxiv.{arxiv}".casefold()]:
                continue
        raise ValueError("committed corpus contains an ambiguous late-identifier split")

    if a2i2_year_window[0] > a2i2_year_window[1]:
        raise ValueError("committed a2i2 year policy is invalid")
    expected_a2i2, has_a2i2_members = _a2i2_expected(a2i2_csv, census, sources, a2i2_year_window)
    a2i2_paths = [path for path in blobs if path.startswith("output/a2i2/")]
    if any(len(PurePosixPath(path).parts) != 3 or not path.endswith(".bib") for path in a2i2_paths):
        raise ValueError("committed a2i2 tree contains nested or non-BibTeX entries")
    if any(path.startswith("output/a2i2/") for path in tree_paths):
        raise ValueError("committed a2i2 tree contains nested directories")
    actual_a2i2 = {PurePosixPath(path).name: blobs[path] for path in a2i2_paths}
    if actual_a2i2 != expected_a2i2:
        raise ValueError("committed a2i2 tree does not match derived authority")
    derived_a2i2_digest = evidence_digest(
        {
            "files": [(name, hashlib.sha256(content).hexdigest()) for name, content in sorted(actual_a2i2.items())],
            "membership_blob_digest": hashlib.sha256(a2i2_csv).hexdigest(),
            "policy_version": _A2I2_POLICY[0],
            "year_window": a2i2_year_window,
        }
    )
    counts = {
        format_author_dirname(row.name, row.scholar_id or row.dblp_id): sum(
            item.author_key == row.row_key and item.disposition == "parsed" for item in provisional_items
        )
        for row in enabled
        if any(item.author_key == row.row_key and item.disposition == "parsed" for item in provisional_items)
    }
    if actual_a2i2 or has_a2i2_members:
        counts["a2i2"] = len(actual_a2i2)
    baseline_digest = _baseline(blobs["output/baseline.json"], counts)
    ordered_provisional = tuple(sorted(provisional_items, key=lambda item: item.source_path.casefold()))
    snapshot = CorpusSnapshot(
        generation_id,
        base_commit,
        _tree_digest(entries, blobs),
        baseline_digest,
        *_CORPUS_AUTHORITY,
        evidence_digest([item.digest for item in ordered_provisional]),
        derived_a2i2_digest,
        author_set_digest=corpus_author_set_digest(census.rows),
    )
    items = tuple(replace(item, snapshot_digest=snapshot.digest) for item in ordered_provisional)
    by_publication = {(item.author_key, item.publication_key): item for item in publications}
    seeds = []
    for item in items:
        for publication_key in item.publication_keys:
            publication = by_publication[(item.author_key, publication_key)]
            seed = PublicationSeedEvidence(
                generation_id,
                item.author_key,
                publication_key,
                EvidenceKind.CORPUS,
                item.key,
                item.digest,
                item.before_digest,
                publication.exact_identifiers,
                "0" * 64,
                item.normalized_entry,
            )
            seeds.append(replace(seed, seed_digest=seed.derived_seed_digest))
    proof_blob_ids = tuple(
        dict.fromkeys(entry.object_id for entry in (*entries, *a2i2_entry) if entry.object_type == "blob")
    )
    proof_blobs = read_committed_blobs(repo_root, proof_blob_ids)
    proof = GitProof(
        base_commit,
        entries,
        a2i2_entry,
        tuple(sorted((object_id, hashlib.sha256(body).hexdigest()) for object_id, body in proof_blobs.items())),
    )
    return ExistingCorpusEvidence(
        snapshot,
        items,
        tuple(sorted(publications, key=lambda item: (item.author_key, item.publication_key))),
        tuple(sorted(seeds, key=lambda item: (item.author_key, item.publication_key))),
        len(actual_a2i2),
        sum(counts.values()),
        proof,
    )


_scan_existing_corpus_authority = scan_existing_corpus


__all__ = [
    "A2I2_POLICY_VERSION",
    "PARSER_ID",
    "PARSER_VERSION",
    "SCANNER_ID",
    "SCANNER_VERSION",
    "ExistingCorpusEvidence",
    "corpus_author_set_digest",
    "scan_existing_corpus",
]
