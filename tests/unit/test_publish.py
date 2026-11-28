import dataclasses
import io
from pathlib import Path

import pytest

from citeforge import identify, publish
from citeforge.http import Result
from citeforge.model import Claim, Decision, Fetch, Names, Source, Verdict
from citeforge.report import MARKDOWN_MAX, markdown
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import Affiliation, OrcidRecord
from citeforge.state import StateRow
from tests.factories import member, record, work

SP, MW = member(), member("Stan Matwin", "Matwin (rCoJeuYAAAAJ)", dblp=None)
OK = Decision(Verdict.ACCEPT, "authorship_orcid")


def row(doi: str, stem: str, **members: Decision) -> StateRow:
    w = dataclasses.replace(work(record(doi)), sources=frozenset({Source.CROSSREF, Source.ORCID}))
    return StateRow(w, Names(stem, stem.replace("-", ":")), {k.replace("_", " "): v for k, v in members.items()})


def state() -> dict[str, StateRow]:
    a = row("doi:10.1234/a", "Spadon2023-A", **{"Spadon (bfdGsGUAAAAJ)": OK, "Matwin (rCoJeuYAAAAJ)": OK})
    b = row("doi:10.1234/b", "Spadon2023-B", **{"Spadon (bfdGsGUAAAAJ)": Decision(Verdict.REVIEW, "openalex_only")})
    return {a.work.key: a, b.work.key: b}


def test_plan_and_check() -> None:
    files = publish.plan(state(), [SP, MW], [MW])
    assert set(files) == {
        Path("Spadon (bfdGsGUAAAAJ)/Spadon2023-A.bib"),
        Path("Matwin (rCoJeuYAAAAJ)/Spadon2023-A.bib"),
        Path("a2i2/Spadon2023-A.bib"),
        publish.SUMMARY,
        publish.BASELINE,
    }
    assert files[publish.SUMMARY].splitlines()[1] == "output/Matwin (rCoJeuYAAAAJ)/Spadon2023-A.bib,2,0,1,0,1,0,0,0,0"
    assert '"total": 3' in files[publish.BASELINE]
    assert publish.check(files) == []


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda f, p: f.__setitem__(p, f[p].replace("  year", "  note = {x},\n  year")), "forbidden fields"),
        (lambda f, p: f.__setitem__(p, f[p].replace("Graph", "{Graph")), "unbalanced"),
        (lambda f, p: f.__setitem__(p, f[p].replace("Graph", "Gr\x07aph")), "control character"),
        (lambda f, p: f.__setitem__(Path("a2i2/Spadon2023-A.bib"), f[p].replace("2023}", "2024}")), "differs"),
        (lambda f, p: f.__setitem__(p.with_name("Spadon2023-C.bib"), f[p]), "doi repeats"),
        (
            lambda f, p: f.__setitem__(p.with_name("Spadon2023-C.bib"), f[p].replace("10.1234/a", "10.1234/c")),
            "ID repeats",
        ),
        (
            lambda f, p: f.__setitem__(p.with_name("spadon2023-a.bib"), f[p].replace("10.1234/a", "10.1234/z")),
            "collides",
        ),
        (lambda f, p: f.__setitem__(p, f[p][:-2]), "not a CiteForge"),
        (lambda f, p: f.pop(publish.BASELINE), "baseline.json"),
    ],
)
def test_check_catches_violations(mutate, message: str) -> None:
    files = publish.plan(state(), [SP, MW], [MW])
    mutate(files, Path("Spadon (bfdGsGUAAAAJ)/Spadon2023-A.bib"))
    assert any(message in p for p in publish.check(files))


def test_write_replaces_and_prunes_only_member_folders(tmp_path: Path) -> None:
    out = tmp_path / "output"
    (out / "Spadon (bfdGsGUAAAAJ)").mkdir(parents=True)
    (out / "Spadon (bfdGsGUAAAAJ)/Stale2019-X.bib").write_text("old")
    (out / "Gone (x)").mkdir()
    (out / "Gone (x)/Keep.bib").write_text("not ours")
    files = publish.plan(state(), [SP, MW], [MW])
    publish.write(out, files, ["Spadon (bfdGsGUAAAAJ)", "Matwin (rCoJeuYAAAAJ)", "a2i2"])
    assert not (out / "Spadon (bfdGsGUAAAAJ)/Stale2019-X.bib").exists()
    assert (out / "Gone (x)/Keep.bib").exists()
    written = {path: path.stat().st_mtime_ns for path in out.rglob("*.bib")}
    publish.write(out, files, ["Spadon (bfdGsGUAAAAJ)"])
    assert {path: path.stat().st_mtime_ns for path in out.rglob("*.bib")} == written
    empty = publish.plan({}, [SP], [])
    publish.write(out, empty, ["Spadon (bfdGsGUAAAAJ)", "Matwin (rCoJeuYAAAAJ)", "a2i2"])
    assert not (out / "Spadon (bfdGsGUAAAAJ)").exists()


def test_markdown_is_capped() -> None:
    members = {f"M{i} (x)": {"status": "complete", "added": [["doi:10.1/" + "x" * 200, "S"]] * 50, "removed": [],
                             "renamed": []} for i in range(100)}  # fmt: skip
    report = {
        "run": {"exit_code": 0, "min_year": 2020, "registry_failed": []},
        "sources": {},
        "members": members,
        "review": [],
        "excluded": {},
        "links": {},
        "coverage": {"claimed": 0, "emitted": 0, "unexplained": 0, "items": []},
    }
    assert len(markdown(report)) <= MARKDOWN_MAX


def test_identify_proposals(monkeypatch: pytest.MonkeyPatch) -> None:
    lab = "https://ror.org/01e6qks80"
    candidate = "0000-0002-1825-0097"

    def claims(n: int, source: Source) -> tuple[Claim, ...]:
        return tuple(Claim(source, frozenset({f"doi:10.1234/{k}"}), "t", 2023) for k in range(n))

    monkeypatch.setattr(
        identify.dblp, "person", lambda h, p: Result(Fetch.OK, DblpPerson((), (candidate,), claims(4, Source.DBLP)))
    )
    monkeypatch.setattr(
        identify.orcid,
        "record",
        lambda h, o: Result(
            Fetch.OK,
            OrcidRecord("G", "S", None, (), (Affiliation(lab, "Dalhousie", "employment"),), claims(3, Source.ORCID)),
        ),
    )
    monkeypatch.setattr(
        identify.openalex, "authors_by_orcid", lambda h, o: Result(Fetch.OK, {candidate: ("A1234567",)})
    )
    (p,) = identify.propose([member()], None, frozenset({lab}))  # type: ignore[arg-type]
    assert p.accept and p.overlap == 3 and p.dblp_lists and p.openalex == ("A1234567",)
    out = io.StringIO()
    identify.print_table([p], out)
    assert out.getvalue().splitlines()[1].startswith("Spadon (bfdGsGUAAAAJ)\t0000-0002-1825-0097\tA1234567\ttrue\t3")
    (none,) = identify.propose([member(dblp=None)], None, frozenset({lab}))  # type: ignore[arg-type]
    assert not none.accept and none.note == "no ORCID candidate"
