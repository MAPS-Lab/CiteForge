from pathlib import Path

import pytest

from citeforge import cli, identify
from citeforge.http import Result
from citeforge.model import Claim, Fetch, Source
from citeforge.sources.dblp import DblpPerson
from citeforge.sources.orcid import Affiliation, OrcidRecord
from tests.factories import member
from tests.test_run import workdir  # noqa: F401
from tests.world import SPADON, SPADON_ORCID, default_world, install

LAB = "https://ror.org/01e6qks80"
CANDIDATE = "0000-0002-1825-0097"


def claims(n: int, source: Source) -> tuple[Claim, ...]:
    return tuple(Claim(source, frozenset({f"doi:10.1234/{k}"}), "t", 2023) for k in range(n))


def serve(monkeypatch: pytest.MonkeyPatch, dblp: Result, orcid: Result) -> None:
    monkeypatch.setattr(identify.dblp, "person", lambda h, p: dblp)
    monkeypatch.setattr(identify.orcid, "record", lambda h, o: orcid)
    monkeypatch.setattr(identify.openalex, "authors_by_orcid", lambda h, o: Result(Fetch.OK, {CANDIDATE: ("A1",)}))


def orcid_ok(overlap: int, employment: bool) -> Result:
    ror = LAB if employment else "https://ror.org/036rp1748"
    affiliations = (Affiliation(ror, "Somewhere", "employment"),)
    return Result(Fetch.OK, OrcidRecord("G", "S", None, (), affiliations, claims(overlap, Source.ORCID)))


@pytest.mark.parametrize(
    ("pinned", "listed", "overlap", "employment", "accept", "note"),
    [
        (True, False, 0, False, True, "pinned"),
        (True, True, 0, False, True, "pinned"),
        (False, True, 2, True, False, "below threshold"),
        (False, True, 2, False, False, "below threshold"),
        (False, True, 3, False, True, "accepted"),
        (False, True, 10, True, True, "accepted"),
    ],
)
def test_acceptance_truth_table(
    monkeypatch: pytest.MonkeyPatch, pinned: bool, listed: bool, overlap: int, employment: bool, accept: bool, note: str
) -> None:
    person = DblpPerson((), (CANDIDATE,) if listed else (), claims(10, Source.DBLP))
    serve(monkeypatch, Result(Fetch.OK, person), orcid_ok(overlap, employment))
    (p,) = identify.propose([member(orcids=(CANDIDATE,) if pinned else ())], None, frozenset({LAB}))  # type: ignore[arg-type]
    assert (p.orcid, p.accept, p.dblp_lists, p.overlap, p.employment, p.note) == (
        CANDIDATE, accept, listed, overlap, employment, note
    )  # fmt: skip
    assert p.openalex == (("A1",) if accept else ())


@pytest.mark.parametrize(
    ("dblp", "orcid", "orcid_id", "note"),
    [
        (Result(Fetch.FAILED, None, "challenge"), orcid_ok(5, True), None, "dblp challenge"),
        (Result(Fetch.OK, DblpPerson((), (), ())), orcid_ok(5, True), None, "no ORCID candidate"),
        (Result(Fetch.OK, DblpPerson((), (CANDIDATE,), ())), Result(Fetch.NOT_FOUND, None, "not_found"), CANDIDATE,
         "orcid not_found"),
    ],
)  # fmt: skip
def test_failures_never_accept(
    monkeypatch: pytest.MonkeyPatch, dblp: Result, orcid: Result, orcid_id: str | None, note: str
) -> None:
    serve(monkeypatch, dblp, orcid)
    (p,) = identify.propose([member()], None, frozenset({LAB}))  # type: ignore[arg-type]
    assert (p.orcid, p.accept, p.openalex, p.note) == (orcid_id, False, (), note)


def test_cli_identify_prints_one_row_per_candidate(
    workdir: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    install(monkeypatch, default_world())
    assert cli.main(["identify"]) == 0
    header, *rows = capsys.readouterr().out.splitlines()
    assert header == "member\torcid\topenalex\tdblp_lists\toverlap\temployment\taccept\tnote"
    spadon = next(r.split("\t") for r in rows if r.startswith(SPADON))
    assert spadon[1] == SPADON_ORCID and spadon[6:] == ["true", "pinned"]
    assert not (workdir / "state").exists() and not (workdir / "run").exists()
