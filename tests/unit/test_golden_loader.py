import dataclasses
import json
from pathlib import Path

import pytest

from citeforge.bibtex import render
from citeforge.model import Decision, Event, Names, Verdict
from citeforge.state import StateRow
from tests.factories import record, work
from tests.golden import Label, failures, load_labels, matches, score

SP = "Spadon (bfdGsGUAAAAJ)"


def label(**kw) -> dict:
    base = {
        "case": "c1",
        "class": "namesake",
        "member": SP,
        "id": "doi:10.1234/a",
        "expect": "reject",
        "origin": "synthetic",
    }
    return {**base, **kw}


def write(tmp_path: Path, *rows: dict) -> Path:
    path = tmp_path / "labels.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return path


def test_load_rejects_bad_labels(tmp_path: Path) -> None:
    assert load_labels(write(tmp_path, label(), label(case="c2", rule="orcid_contra")))[1].rule == "orcid_contra"
    for bad in ([label(), label()], [label(**{"class": "nope"})], [label(extra=1)]):
        with pytest.raises(ValueError, match="bad label"):
            load_labels(write(tmp_path, *bad))


def test_matches_and_thresholds() -> None:
    w = work(record("doi:10.1234/a", "arxiv:2101.00001"))
    names = Names("Spadon2023-Graph", "Spadon2023:Graph")
    row = StateRow(
        dataclasses.replace(w, aliases=w.primary.ids), names, {SP: Decision(Verdict.ACCEPT, "authorship_orcid")}
    )
    state = {row.work.key: row}
    text = render(row.work, names)
    files = {Path(SP) / "Spadon2023-Graph.bib": text, Path("a2i2/Spadon2023-Graph.bib"): text}
    events = [Event("doi_mismatch", SP, "doi:10.1234/x", "")]

    def lab(expect: str, cls: str = "namesake", wid: str = "arxiv:2101.00001", rule: str | None = None) -> Label:
        return Label("c", cls, SP, wid, expect, rule, "synthetic")

    assert matches(lab("accept", rule="authorship_orcid"), state, files, events)
    assert not matches(lab("reject"), state, files, events)
    assert matches(lab("doi_detached", "wrong_doi", "doi:10.1234/x"), state, files, events)
    assert matches(lab("field:year=2023", "year"), state, files, events)
    assert matches(lab("bytes_equal:a2i2", "dup_pair"), state, files, events)
    assert not matches(lab("exclude:editorial", "front_matter"), state, files, events)
    scores = score([lab("accept"), lab("reject", "not_namesake")], state, files, events)
    assert scores == {"namesake": (1, 1), "not_namesake": (0, 1)}
    assert failures(scores) == ["overall 1/2 below 98%", "not_namesake 0/1 below 100%"]
