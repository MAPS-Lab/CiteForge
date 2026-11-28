import dataclasses
import functools
import json
import re
from pathlib import Path

import httpx
import pytest

from citeforge import cli
from citeforge.bibtex import read_entry
from citeforge.http import Http, Result
from citeforge.model import Claim, Fetch, Kind, Record, Source, Status
from citeforge.sources import crossref, openalex, orcid
from tests.conftest import FakeClock
from tests.world import (
    INPUT,
    MATWIN,
    MW,
    SP,
    SP_OA,
    SPADON,
    SPADON_ORCID,
    YE,
    YE_ORCID,
    cr,
    default_world,
    install,
    repo,
    tree,
)


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = repo(tmp_path)
    monkeypatch.chdir(root)
    monkeypatch.setenv("OPENALEX_API_KEY", "test-key")
    monkeypatch.delenv("CITEFORGE_MIN_YEAR", raising=False)
    return root


def outputs(root: Path) -> dict[str, bytes]:
    return {
        **{f"output/{k}": v for k, v in tree(root / "output").items()},
        **{f"state/{k}": v for k, v in tree(root / "state").items()},
    }


def test_first_run_end_to_end(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    files = sorted(p.relative_to(workdir / "output").as_posix() for p in (workdir / "output").rglob("*.bib"))
    spadon = [f for f in files if f.startswith(SPADON)]
    assert f"{SPADON}/Spadon2024-PhysicsInformed.bib" in spadon
    assert f"{SPADON}/Spadon2023-MaritimeTraffic.bib" in spadon
    assert len(spadon) == 8
    assert not any("Editorial" in f or "Soil" in f or "Old" in f for f in files)
    assert [f for f in files if f.startswith(YE)] == [f"{YE}/Ye2024-WirelessChannel.bib"]
    gnn = (workdir / "output" / SPADON / "Spadon2024-PhysicsInformed.bib").read_text(encoding="utf-8")
    assert gnn == (workdir / "output" / MATWIN / "Spadon2024-PhysicsInformed.bib").read_text(encoding="utf-8")
    assert gnn == (workdir / "output/a2i2/Spadon2024-PhysicsInformed.bib").read_text(encoding="utf-8")
    entry = read_entry(gnn)
    assert entry["eprint"] == "2310.18948" and entry["doi"] == "10.1016/j.oceaneng.2024.117234"
    assert entry["pages"] == "1--10"
    assert "{AIS}" in entry["title"]
    baseline = json.loads((workdir / "output/baseline.json").read_text())
    assert baseline["authors"] == {SPADON: 8, MATWIN: 2, YE: 1, "a2i2": 8}
    assert baseline["total"] == 19
    summary = (workdir / "output/summary.csv").read_text().splitlines()
    assert summary[0] == "file_path,trust_hits,openalex,orcid,dblp,crossref,datacite,csl,arxiv,curated"
    assert len(summary) == 12
    report = json.loads((workdir / "state/report.json").read_text())
    assert "timing" not in report and "timing" in json.loads((workdir / "run/report.json").read_text())
    assert report["members"][SPADON]["status"] == "complete"
    assert report["coverage"]["unexplained"] == 0
    reasons = {i["id"]: i["reason"] for i in report["coverage"]["items"]}
    assert reasons["doi:10.1234/editorial"] == "kind:editorial"
    assert "doi:10.1234/old" not in reasons
    assert reasons["doi:10.1234/wrong"] == "doi_mismatch"
    assert any(d["id"] == "doi:10.1234/wrong" for d in report["links"]["doi_detached"])
    assert (workdir / "run/report.md").read_text().startswith("## CiteForge refresh")
    rows = [json.loads(line) for line in (workdir / "state/works.jsonl").read_text().splitlines()]
    namesake = next(r for r in rows if r["work"]["key"] == "doi:10.1234/namesake")
    assert namesake["members"][SPADON]["rule"] == "orcid_contra"
    old = next(r for r in rows if r["work"]["key"] == "doi:10.1234/old")
    assert old["members"][SPADON]["rule"] == "out_of_window"


def test_rerun_and_shuffled_roster_are_byte_identical(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    assert cli.main([]) == 0
    assert cli.main([]) == 0
    first = outputs(workdir)
    assert cli.main([]) == 0
    assert outputs(workdir) == first
    header, *rows = INPUT.strip().split("\n")
    (workdir / "data/input.csv").write_text("\n".join([header, *reversed(rows)]) + "\n", encoding="utf-8")
    assert cli.main([]) == 0
    assert outputs(workdir) == first


def _assert_outage_kept_everything(workdir: Path, before: dict[str, bytes]) -> dict:
    assert tree(workdir / "output") == before
    report = json.loads((workdir / "state/report.json").read_text())
    assert all(m["removed"] == [] for m in report["members"].values())
    assert "shrink_guard" not in report["events"]["counts"]
    assert report["members"][SPADON]["status"] == "partial:registry"
    assert report["run"]["exit_code"] == 3
    return report


def test_crossref_outage_removes_nothing(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    before = tree(workdir / "output")
    world.crossref_down = True
    assert cli.main([]) == 3
    assert world.calls["crossref.by_doi"] == 2
    report = _assert_outage_kept_everything(workdir, before)
    assert report["events"]["counts"]["pending"] >= 1


def test_crossref_503_marks_the_host_down_and_exits_3(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real_by_doi = crossref.by_doi
    install(monkeypatch, default_world())
    assert cli.main([]) == 0
    before = tree(workdir / "output")
    monkeypatch.setattr(crossref, "by_doi", real_by_doi)
    clock = FakeClock()
    unavailable = httpx.MockTransport(lambda r: httpx.Response(503, headers={"retry-after": "600"}))
    monkeypatch.setattr(cli, "Http", functools.partial(Http, transport=unavailable, clock=clock, sleep=clock.sleep))
    assert cli.main([]) == 3
    report = _assert_outage_kept_everything(workdir, before)
    assert report["sources"]["api.crossref.org"]["down"] == "retry_after 600s"
    assert report["run"]["exit_code"] == 3


def test_search_failure_keeps_the_linked_doi_and_ships_one_file(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    linked = cr("doi:10.1145/3580305.1", "Maritime traffic graphs at scale", (SP, MW), year=2023)
    monkeypatch.setattr(crossref, "search", lambda *a: Result(Fetch.OK, (linked,)))
    assert cli.main([]) == 0

    def maritime() -> list[str]:
        folder = workdir / "output" / SPADON
        return sorted(p.name for p in folder.glob("*.bib") if "Maritime traffic" in p.read_text(encoding="utf-8"))

    assert maritime() == ["Spadon2023-MaritimeTraffic.bib"]
    assert "10.1145/3580305.1" in (workdir / "output" / SPADON / "Spadon2023-MaritimeTraffic.bib").read_text()
    first = tree(workdir / "output")
    monkeypatch.setattr(crossref, "search", lambda *a: Result(Fetch.FAILED, None, "down"))
    assert cli.main([]) == 0
    assert tree(workdir / "output") == first
    rows = [json.loads(line) for line in (workdir / "state/works.jsonl").read_text().splitlines()]
    assert "dblp:conf/kdd/SpadonM23" not in {r["work"]["key"] for r in rows}


def test_search_outage_on_a_new_group_marks_the_member_partial(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    monkeypatch.setattr(crossref, "search", lambda *a: Result(Fetch.FAILED, None, "down"))
    assert cli.main([]) == 3
    report = json.loads((workdir / "state/report.json").read_text())
    assert report["members"][SPADON]["status"] == "partial:crossref"


def test_dblp_challenge_keeps_every_entry_and_exits_3(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    before = set(tree(workdir / "output"))
    world.dblp_challenge = True
    world.orcid_works.clear()
    world.orcid_works.update({k: [] for k in ("0000-0002-1825-0097", "0000-0001-5109-3700")})
    assert cli.main([]) == 3
    assert set(tree(workdir / "output")) == before
    report = json.loads((workdir / "state/report.json").read_text())
    assert report["members"][SPADON]["status"].startswith("partial")


def test_missing_counter_and_shrink_guard(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    spadon_dir = workdir / "output" / SPADON
    world.orcid_works["0000-0002-1825-0097"] = [
        c for c in world.orcid_works["0000-0002-1825-0097"] if "bulk0" not in min(c.ids)
    ]
    assert cli.main([]) == 0
    assert (spadon_dir / "Spadon2022-OceanAnalogStudyNumber0.bib").exists()
    assert cli.main([]) == 0
    assert not (spadon_dir / "Spadon2022-OceanAnalogStudyNumber0.bib").exists()
    world.orcid_works["0000-0002-1825-0097"] = world.orcid_works["0000-0002-1825-0097"][:1]
    assert cli.main([]) == 0
    assert cli.main([]) == 3
    assert len(list(spadon_dir.glob("*.bib"))) == 7
    report = json.loads((workdir / "state/report.json").read_text())
    assert report["events"]["counts"]["shrink_guard"] == 1


def test_over_cap_carries_previous_decisions(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    before = tree(workdir / "output")
    monkeypatch.setattr("citeforge.run.PER_YEAR_CAP", 0)
    assert cli.main([]) == 3
    assert tree(workdir / "output") == before
    assert json.loads((workdir / "state/report.json").read_text())["members"][SPADON]["status"] == "over_cap"


@pytest.mark.parametrize(
    ("env", "setup", "message"),
    [
        ({"OPENALEX_API_KEY": ""}, None, "OPENALEX_API_KEY"),
        ({"CITEFORGE_MIN_YEAR": "twenty"}, None, "CITEFORGE_MIN_YEAR"),
        ({}, ("data/input.csv", INPUT.replace("true,\n", "maybe,\n", 1)), "Enabled must be"),
        ({}, ("data/curated.toml", "[[accept]]\nmember = 'Nobody (x)'\nid = 'doi:10.1234/x'\n"), "unknown members"),
        ({}, ("data/a2i2.csv", "Name\nA\nB\nC\n"), "group members"),
    ],
)
def test_config_errors_exit_2_before_any_request(
    workdir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], env, setup, message
) -> None:
    world = default_world()
    install(monkeypatch, world)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    if setup:
        (workdir / setup[0]).write_text(setup[1], encoding="utf-8")
    assert cli.main([]) == 2
    assert message in capsys.readouterr().err
    assert sum(world.calls.values()) == 0
    assert not (workdir / "output").exists()


def test_rolling_window_and_key_file(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    monkeypatch.delenv("OPENALEX_API_KEY")
    (workdir / "keys").mkdir()
    (workdir / "keys/OpenAlex.key").write_text("file-key\n")
    monkeypatch.setenv("CITEFORGE_MIN_YEAR", "")
    assert cli.main([]) == 0
    assert json.loads((workdir / "state/report.json").read_text())["run"]["min_year"] >= 2020


def test_unhandled_error_exits_1(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())

    def boom(*args, **kwargs):
        raise RuntimeError("adapter bug")

    monkeypatch.setattr("citeforge.sources.crossref.by_doi", boom)
    assert cli.main([]) == 1
    assert not (workdir / "state/works.jsonl").exists()


def removed(root: Path, member: str) -> dict[str, str]:
    report = json.loads((root / "state/report.json").read_text())
    return dict(report["members"][member]["removed"])


def status(root: Path, member: str) -> str:
    return json.loads((root / "state/report.json").read_text())["members"][member]["status"]


def test_dblp_challenge_keeps_every_entry_past_the_missing_runs(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    before = {f for f in tree(workdir / "output") if f.startswith((SPADON, MATWIN))}
    world.dblp_challenge = True
    for _ in range(3):
        assert cli.main([]) == 3
    assert before <= set(tree(workdir / "output"))
    assert status(workdir, MATWIN) == "partial:dblp"


def test_curated_reject_after_publication_removes_the_file(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    assert cli.main([]) == 0
    path = workdir / "output" / YE / "Ye2024-WirelessChannel.bib"
    assert path.exists()
    with (workdir / "data/curated.toml").open("a", encoding="utf-8") as f:
        f.write(f"\n[[reject]]\nmember = '{YE}'\nid = 'doi:10.1234/ye'\n")
    assert cli.main([]) == 0
    assert not path.exists()
    assert removed(workdir, YE) == {"doi:10.1234/ye": "curated"}


def test_work_absorbed_into_a_doi_is_removed_as_merged(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    old = "dblp:conf/kdd/SpadonM23"
    conf = world.dblp_claims["192/1659"][0]
    assert conf.ids == {old} and conf.record is not None
    doi = "doi:10.1234/kdd23"
    world.crossref[doi] = cr(doi, conf.title, (SP, MW), year=2023, container=("Proceedings of KDD",))
    rec = dataclasses.replace(conf.record, ids=conf.ids | {doi})
    world.dblp_claims["192/1659"][0] = world.dblp_claims["m/StanMatwin"][0] = dataclasses.replace(
        conf, ids=rec.ids, record=rec
    )
    assert cli.main([]) == 0
    assert removed(workdir, SPADON) == {old: f"merged_into:{doi}"}


def test_held_doi_review_keeps_the_earlier_file(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    ye = world.crossref["doi:10.1234/ye"]
    world.crossref["doi:10.1234/ye"] = dataclasses.replace(
        ye, authors=(dataclasses.replace(ye.authors[0], orcid=None),)
    )
    install(monkeypatch, world)
    assert cli.main([]) == 0
    path = workdir / "output" / YE / "Ye2024-WirelessChannel.bib"
    assert path.exists()
    claim = world.orcid_works[YE_ORCID][0]
    world.orcid_works[YE_ORCID] = [
        dataclasses.replace(claim, title="Channel estimation for wireless links with attention models")
    ]
    for _ in range(3):
        assert cli.main([]) == 0
        assert path.exists()
    report = json.loads((workdir / "state/report.json").read_text())
    assert report["events"]["counts"]["doi_review"] == 1


def test_retraction_after_publication_removes_the_file(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    world.crossref["doi:10.1234/ye"] = dataclasses.replace(world.crossref["doi:10.1234/ye"], status=Status.RETRACTED)
    assert cli.main([]) == 0
    assert not (workdir / "output" / YE / "Ye2024-WirelessChannel.bib").exists()
    assert removed(workdir, YE) == {"doi:10.1234/ye": "status:retracted"}


def test_missing_and_future_years_are_held_for_review(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    undated = dataclasses.replace(cr("doi:10.1234/undated", "An undated note on sea ice", (SP,)), year=None, dates=())
    world.crossref["doi:10.1234/undated"] = undated
    world.orcid_works[SPADON_ORCID].append(
        Claim(Source.ORCID, frozenset({"doi:10.1234/undated"}), "An undated note on sea ice", None)
    )
    future = Record(
        Source.DBLP,
        frozenset({"dblp:conf/x/Matwin99"}),
        Kind.INPROCEEDINGS,
        "inproceedings",
        "Vessel routing in a later decade",
        authors=(MW,),
        year=2999,
        dates=(2999,),
        container=("X",),
    )
    world.dblp_claims["m/StanMatwin"] = [
        *world.dblp_claims["m/StanMatwin"],
        Claim(Source.DBLP, future.ids, future.title, 2999, record=future),
    ]
    install(monkeypatch, world)
    assert cli.main([]) == 0
    rows = {r["work"]["key"]: r for r in map(json.loads, (workdir / "state/works.jsonl").read_text().splitlines())}
    assert rows["doi:10.1234/undated"]["members"][SPADON]["verdict"] == "review"
    assert rows["doi:10.1234/undated"]["members"][SPADON]["rule"] == "no_year"
    assert rows["dblp:conf/x/Matwin99"]["members"][MATWIN]["rule"] == "future_year"
    files = tree(workdir / "output")
    assert not any("Undated" in f or "Later" in f for f in files)


def test_registry_outage_holds_new_works_as_pending(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    install(monkeypatch, world)
    assert cli.main([]) == 0
    before = tree(workdir / "output")
    world.crossref_down = True
    doi = "doi:10.1234/late"
    world.crossref[doi] = cr(doi, "A late paper on sea ice", (SP,))
    late = Record(Source.DBLP, frozenset({doi, "dblp:journals/x/Spadon24"}), Kind.ARTICLE, "article",
                  "A late paper on sea ice", authors=(SP,), year=2024, dates=(2024,), container=("X",))  # fmt: skip
    world.dblp_claims["192/1659"] = [
        *world.dblp_claims["192/1659"],
        Claim(Source.DBLP, late.ids, late.title, 2024, record=late),
    ]
    for _ in range(3):
        assert cli.main([]) == 3
        assert tree(workdir / "output") == before
    report = json.loads((workdir / "state/report.json").read_text())
    assert ["pending", SPADON, "doi:10.1234/late", "pending:registry"] in report["events"]["items"]
    world.crossref_down = False
    assert cli.main([]) == 0
    assert (workdir / "output" / SPADON / "Spadon2024-LatePaper.bib").exists()


def test_unknown_or_failed_profiles_hold_removals_and_exit_3(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, default_world())
    assert cli.main([]) == 0
    path = workdir / "output" / YE / "Ye2024-WirelessChannel.bib"
    real = orcid.record
    monkeypatch.setattr(
        orcid, "record", lambda http, o: Result(Fetch.NOT_FOUND, None, "not_found") if o == YE_ORCID else real(http, o)
    )
    for _ in range(3):
        assert cli.main([]) == 3
        assert path.exists()
    assert status(workdir, YE) == "no_profile"
    world_down = default_world()
    world_down.dblp_challenge = True
    install(monkeypatch, world_down)
    monkeypatch.setattr(orcid, "record", lambda http, o: Result(Fetch.FAILED, None, "timeout"))
    before = set(tree(workdir / "output"))
    for _ in range(3):
        assert cli.main([]) == 3
    assert set(tree(workdir / "output")) == before
    assert status(workdir, SPADON) == "partial:dblp,orcid"


def test_openalex_lookup_failure_keeps_openalex_only_works(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (workdir / "data/input.csv").write_text(INPUT.replace(",A5023888391,", ",,"), encoding="utf-8")
    world = default_world()
    doi = "doi:10.1234/oaonly"
    world.crossref[doi] = cr(doi, "Sea ice drift from vessel tracks", (SP_OA,), year=2023)
    oa = Record(Source.OPENALEX, frozenset({doi, "openalex:W9"}), Kind.ARTICLE, "article",
                "Sea ice drift from vessel tracks", authors=(SP_OA,), year=2023, dates=(2023,))  # fmt: skip
    world.openalex_claims.append(Claim(Source.OPENALEX, oa.ids, oa.title, 2023, record=oa))
    install(monkeypatch, world)
    assert cli.main([]) == 0
    path = workdir / "output" / SPADON / "Spadon2023-SeaIce.bib"
    assert path.exists()
    monkeypatch.setattr(openalex, "authors_by_orcid", lambda http, o: Result(Fetch.FAILED, None, "down"))
    for _ in range(3):
        assert cli.main([]) == 3
        assert path.exists()
    assert status(workdir, SPADON).startswith("partial") and "openalex" in status(workdir, SPADON)


def test_warm_run_reuses_search_links_and_writes_the_same_bytes(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    doi = "doi:10.1145/kdd23"
    found = cr(doi, "Maritime traffic graphs at scale", (SP, MW), year=2023, container=("Proceedings of KDD",))
    install(monkeypatch, world)
    world.crossref[doi] = found
    searches = []
    monkeypatch.setattr(crossref, "search", lambda http, t, f, y: searches.append(t) or Result(Fetch.OK, (found,)))
    assert cli.main([]) == 0
    cold = outputs(workdir)
    assert len(searches) == 1
    rows = {r["work"]["key"]: r for r in map(json.loads, (workdir / "state/works.jsonl").read_text().splitlines())}
    assert "dblp:conf/kdd/SpadonM23" in rows[doi]["work"]["aliases"]
    assert cli.main([]) == 0
    warm = outputs(workdir)
    assert cli.main([]) == 0
    assert len(searches) == 1 and outputs(workdir) == warm
    assert {k: v for k, v in warm.items() if k != "state/report.json"} == {
        k: v for k, v in cold.items() if k != "state/report.json"
    }


def test_a_search_linked_doi_is_fetched_again_and_its_retraction_applies(
    workdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    world = default_world()
    doi = "doi:10.1145/3580305.1"
    early = cr(doi, "Maritime traffic graphs at scale", (SP, MW), year=2023, volume=None, pages=None)
    install(monkeypatch, world)
    world.crossref[doi] = early
    asked: list[str] = []
    real = crossref.by_doi
    monkeypatch.setattr(crossref, "by_doi", lambda http, ids: (asked.extend(ids), real(http, ids))[1])
    monkeypatch.setattr(crossref, "search", lambda *a: Result(Fetch.OK, (early,)))
    assert cli.main([]) == 0
    path = workdir / "output" / SPADON / "Spadon2023-MaritimeTraffic.bib"
    assert path.exists()
    world.crossref[doi] = dataclasses.replace(early, volume="17", pages="1-9")
    asked.clear()
    assert cli.main([]) == 0
    assert doi in asked and "17" in read_entry(path.read_text())["volume"]
    world.crossref[doi] = dataclasses.replace(world.crossref[doi], status=Status.RETRACTED)
    assert cli.main([]) == 0
    assert not path.exists()


def test_affiliation_emails_never_reach_state_report_or_output(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    gnn = "doi:10.1016/j.oceaneng.2024.117234"
    home = "Dalhousie University, Halifax, Canada. Electronic address: someone@example.org"
    raw = {
        "DOI": gnn.removeprefix("doi:"),
        "type": "journal-article",
        "title": [world.crossref[gnn].title],
        "author": [
            {"given": "Gabriel", "family": "Spadon", "ORCID": f"https://orcid.org/{SPADON_ORCID}",
             "affiliation": [{"name": home}]},
            {"given": "Stan", "family": "Matwin", "affiliation": [{"name": "E-mail: other.person@example.org"}]},
        ],
        "issued": {"date-parts": [[2024]]},
    }  # fmt: skip
    world.crossref[gnn] = dataclasses.replace(world.crossref[gnn], authors=crossref.parse_work(raw).authors)
    install(monkeypatch, world)
    assert cli.main([]) == 0
    written = {**outputs(workdir), **{f"run/{k}": v for k, v in tree(workdir / "run").items()}}
    email = re.compile(rb"[\w.+-]+@[\w-]+\.\w")
    assert [k for k, v in written.items() if email.search(v)] == []
    assert b"Dalhousie University, Halifax, Canada." in written["state/works.jsonl"]


def test_orcid_work_without_an_identifier_is_counted(workdir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world = default_world()
    title = "Soft-sensing approach for predicting bovine respiratory disease"
    world.orcid_works[SPADON_ORCID].append(Claim(Source.ORCID, frozenset(), title, 2024, by_self=True))
    install(monkeypatch, world)
    assert cli.main([]) == 0
    coverage = json.loads((workdir / "state/report.json").read_text())["coverage"]
    assert {"member": SPADON, "id": "", "title": title, "reason": "no_identifier"} in coverage["items"]
    assert coverage["unexplained"] == 0
    assert coverage["claimed"] == coverage["emitted"] + len(coverage["items"])
    assert f"{SPADON}: {title} (no_identifier)" in (workdir / "run/report.md").read_text()
