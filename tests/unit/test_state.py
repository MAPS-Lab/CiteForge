from pathlib import Path

from citeforge.model import Decision, Names, Rel, Source, Verdict
from citeforge.state import StateRow, decode, encode, load, save
from tests.factories import author, record, work


def row(doi: str) -> StateRow:
    r = record(
        f"doi:{doi}",
        "openalex:W1",
        authors=(author("José Müller", orcid="0000-0002-1825-0097", rors=("https://ror.org/01e6qks80",)),),
        relations=frozenset({(Rel.HAS_PREPRINT, "arxiv:2101.00001")}),
        container=("Ocean Engineering",),
    )
    eprint = record("arxiv:2101.00001", source=Source.ARXIV, primary_class="cs.LG")
    w = work(r, eprint=eprint, key=f"doi:{doi}")
    return StateRow(
        w,
        Names("Muller2023-GraphNeural", "Muller2023:GraphNeuralNetworksVessel"),
        {"Spadon (bfdGsGUAAAAJ)": Decision(Verdict.ACCEPT, "authorship_orcid", frozenset({"x"}))},
        {"Ye (4OQaVGUAAAAJ)": 1},
    )


def test_round_trip_and_sorted_atomic_save(tmp_path: Path) -> None:
    a, b = row("10.2/b"), row("10.1/a")
    assert decode(encode(a)) == a
    assert '"family": "Müller"' in encode(a)
    save(tmp_path, {b.work.key: b, a.work.key: a})
    lines = (tmp_path / "works.jsonl").read_text(encoding="utf-8").splitlines()
    assert [decode(line).work.key for line in lines] == ["doi:10.1/a", "doi:10.2/b"]
    assert load(tmp_path) == {"doi:10.1/a": b, "doi:10.2/b": a}
    assert not list(tmp_path.glob(".*tmp"))


def test_missing_state_is_empty(tmp_path: Path) -> None:
    assert load(tmp_path) == {}
