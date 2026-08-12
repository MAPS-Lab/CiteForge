from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from citeforge.refresh.census import AuthorCensusRow
from citeforge.refresh.inventory import (
    InventoryPolicy,
    InventorySnapshot,
    SnapshotContribution,
    build_inventory_task,
    capability_for,
    decode_dblp_inventory,
    decode_scholar_inventory,
    reduce_author_inventory,
)
from citeforge.refresh.types import TaskDisposition


def _row() -> AuthorCensusRow:
    return AuthorCensusRow(
        2,
        "author-ada",
        "Ada Lovelace",
        "ada lovelace",
        "Scholar123",
        "12/345",
        True,
        "",
        TaskDisposition.PENDING,
    )


def test_capability_separates_logical_scholar_from_wire_serpapi() -> None:
    capability = capability_for("scholar", "inventory", "1")
    assert capability.logical_source == "scholar"
    assert capability.wire_provider == "serpapi"
    task = build_inventory_task(_row(), capability, "2026-08", InventoryPolicy(2020, 1000, 10))
    assert task.provider == "scholar"
    assert task.request is not None
    assert task.request.provider == "scholar"
    assert dict(task.request.normalized_payload) == {
        "author_key": "author-ada",
        "min_year": 2020,
        "num": 100,
        "profile_id": "Scholar123",
        "sort": "pubdate",
        "start": 0,
    }
    with pytest.raises(ValueError, match="capability"):
        capability_for("scholar", "inventory", "999")


def test_scholar_decoder_is_strict_and_derives_trusted_next_offset() -> None:
    body = json.dumps(
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
            "articles": [
                {
                    "title": "Analytical Engine",
                    "authors": "Ada Lovelace, Charles Babbage",
                    "year": "2024",
                    "citation_id": "Scholar123:paper1",
                    "publication": "Proceedings, 2024",
                    "link": "https://scholar.google.com/citations?view_op=view_citation&citation_for_view=paper1",
                }
            ],
            "serpapi_pagination": {
                "next": "https://serpapi.com/search.json?engine=google_scholar_author&cstart=100"
                "&author_id=Scholar123&hl=en"
            },
        }
    ).encode()
    normalized, empty = decode_scholar_inventory(body, "Scholar123", 0, 100, 2020)
    assert not empty
    assert normalized["next_offset"] == 100
    assert normalized["articles"][0]["year"] == 2024
    duplicate = b'{"articles":[],"articles":[]}'
    with pytest.raises(ValueError, match="duplicate"):
        decode_scholar_inventory(duplicate, "Scholar123", 0, 100, 2020)
    with pytest.raises(ValueError, match="provider error"):
        decode_scholar_inventory(b'{"error":"quota"}', "Scholar123", 0, 100, 2020)
    malformed = json.loads(body)
    malformed["search_parameters"]["cstart"] = True
    with pytest.raises(ValueError, match="offset"):
        decode_scholar_inventory(json.dumps(malformed).encode(), "Scholar123", 0, 100, 2020)
    forked = json.loads(body)
    forked["serpapi_pagination"]["next"] += "&cstart=200"
    with pytest.raises(ValueError, match="identity"):
        decode_scholar_inventory(json.dumps(forked).encode(), "Scholar123", 0, 100, 2020)


def test_dblp_decoder_rejects_unsafe_or_wrong_pid_and_normalizes_records() -> None:
    xml = b"""<dblpperson key="homepages/12/345"><person key="homepages/12/345"><author>Ada Lovelace</author></person>
    <r><article key="journals/x/1"><author>Ada Lovelace 0001</author>
    <title>Computing Machinery</title><year>2024</year><journal>Science</journal>
    <ee>https://doi.org/10.1000/example</ee></article></r><coauthors/></dblpperson>"""
    normalized, empty = decode_dblp_inventory(xml, "12/345")
    assert not empty
    assert normalized["articles"][0]["doi"] == "10.1000/example"
    assert normalized["articles"][0]["authors"] == ["Ada Lovelace"]
    assert normalized["articles"][0]["record_key"] == "journals/x/1"
    with pytest.raises(ValueError, match="PID"):
        decode_dblp_inventory(xml, "99/wrong")
    with pytest.raises(ValueError, match="PID"):
        decode_dblp_inventory(b'<dblpperson pid="99/999" key="homepages/12/345"/>', "12/345")
    with pytest.raises(ValueError):
        decode_dblp_inventory(b'<!DOCTYPE x [<!ENTITY y SYSTEM "file:///etc/passwd">]><dblpperson/>', "12/345")
    with pytest.raises(ValueError, match="forbidden"):
        decode_dblp_inventory(
            b'<!DOCTYPE dblpperson [<!ENTITY xxe SYSTEM "https://attacker.invalid/x">]>'
            b'<dblpperson pid="12/345">&xxe;</dblpperson>',
            "12/345",
        )
    with pytest.raises(ValueError, match="count"):
        decode_dblp_inventory(b'<dblpperson pid="12/345" n="1"/>', "12/345")
    relative = b"""<dblpperson key="homepages/12/345"><r><article key="conf/x/one">
    <title>Relative URL</title><year>2024</year><url>db/conf/x/one.html</url></article></r></dblpperson>"""
    relative_normalized, _ = decode_dblp_inventory(relative, "12/345")
    assert relative_normalized["articles"][0]["url"] == "https://dblp.org/rec/conf/x/one"
    data_xml = b"""<dblpperson key="homepages/35/5521"><r><data key="data/11/AdjeiZHSN25">
    <author>Malcolm Heywood</author><title>Benchmark Dataset</title><year>2025</year>
    <publisher>IEEE DataPort</publisher><url>db/data/11/AdjeiZHSN25.html</url></data></r></dblpperson>"""
    data_normalized, _ = decode_dblp_inventory(data_xml, "35/5521")
    assert data_normalized["articles"][0]["record_type"] == "data"
    assert data_normalized["articles"][0]["publication"] == "IEEE DataPort"
    assert data_normalized["articles"][0]["url"] == "https://dblp.org/rec/data/11/AdjeiZHSN25"


def test_pure_union_is_order_independent_and_seeds_every_publication() -> None:
    scholar = SnapshotContribution(
        "scholar-task",
        "scholar",
        TaskDisposition.SUCCEEDED,
        "schema-1",
        "a" * 64,
        (
            {
                "title": "Computing Machinery",
                "authors": ("Ada Lovelace",),
                "year": 2024,
                "citation_id": "Scholar123:paper1",
                "publication": "Science",
                "url": "https://scholar.google.com/paper1",
            },
        ),
    )
    dblp = SnapshotContribution(
        "dblp-task",
        "dblp",
        TaskDisposition.SUCCEEDED,
        "schema-1",
        "b" * 64,
        (
            {
                "title": "Computing Machinery",
                "authors": ("Ada Lovelace", "Charles Babbage"),
                "year": 2024,
                "doi": "10.1000/example",
                "publication": "Science",
                "record_key": "journals/x/1",
            },
        ),
    )
    policy = InventoryPolicy(2020, 1000, 10)
    first = reduce_author_inventory(_row(), InventorySnapshot("author-ada", (scholar, dblp)), policy)
    second = reduce_author_inventory(_row(), InventorySnapshot("author-ada", (dblp, scholar)), policy)
    assert first == second
    assert len(first.publications) == len(first.seed_tasks) == 1
    assert first.publications[0].exact_identifiers["doi"] == "10.1000/example"
    assert first.seed_tasks[0].provider == "doi_csl"


def test_pure_union_has_no_socket_or_filesystem_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("socket")))
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("filesystem")))
    snapshot = InventorySnapshot(
        "author-ada",
        (
            SnapshotContribution(
                "task",
                "dblp",
                TaskDisposition.CONFIRMED_EMPTY,
                "dblpperson-v1",
                "a" * 64,
                (),
            ),
        ),
    )
    assert reduce_author_inventory(_row(), snapshot, InventoryPolicy(2020, 1000, 10)).publications == ()


def test_pure_union_preserves_preprint_and_published_versions() -> None:
    articles = (
        {
            "title": "A Unified Result",
            "authors": ("Ada Lovelace",),
            "year": 2024,
            "doi": "10.48550/arXiv.2401.00001",
            "publication": "arXiv",
        },
        {
            "title": "A Unified Result",
            "authors": ("Ada Lovelace",),
            "year": 2024,
            "doi": "10.1000/journal",
            "publication": "Science",
        },
    )
    snapshot = InventorySnapshot(
        "author-ada",
        (
            SnapshotContribution(
                "task",
                "dblp",
                TaskDisposition.SUCCEEDED,
                "dblpperson-v1",
                "a" * 64,
                articles,
            ),
        ),
    )
    reduction = reduce_author_inventory(_row(), snapshot, InventoryPolicy(2020, 1000, 10))
    assert len(reduction.publications) == len(reduction.seed_tasks) == 2
