from pathlib import Path

import pytest

from citeforge.curated import CuratedError, load
from citeforge.model import Kind, Source, Verdict
from tests.factories import member

SAMPLE = """
[lab]
ror = ["https://ror.org/01e6qks80"]
[[accept]]
member = "Spadon (bfdGsGUAAAAJ)"
id = "doi:10.1109/OJVT.2021.3097560"
[[reject]]
member = "*"
id = "doi:10.2172/1991087"
reason = "namesake"
[[reject]]
member = "Spadon (bfdGsGUAAAAJ)"
id = "arxiv:2101.00001v2"
[[link]]
preprint = "arxiv:2310.18948"
published = "doi:10.1016/j.oceaneng.2024.117234"
[[override]]
id = "doi:10.1234/x"
field = "booktitle"
value = "Proceedings of Something"
[[record]]
id = "manual:spadon-2021-thesis"
kind = "phdthesis"
title = "A thesis"
authors = ["Gabriel Spadon"]
year = 2021
school = "Universidade de São Paulo"
members = ["Spadon (bfdGsGUAAAAJ)"]
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "curated.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_sample(tmp_path: Path) -> None:
    c = load(write(tmp_path, SAMPLE))
    spadon = "Spadon (bfdGsGUAAAAJ)"
    assert c.lab_rors == {"https://ror.org/01e6qks80"}
    accepted = c.decision(spadon, frozenset({"doi:10.1109/ojvt.2021.3097560"}))
    assert accepted is not None and accepted.verdict is Verdict.ACCEPT
    rejected = c.decision(spadon, frozenset({"arxiv:2101.00001"}))
    assert rejected is not None and rejected.verdict is Verdict.REJECT
    everyone = c.decision("Ye (4OQaVGUAAAAJ)", frozenset({"doi:10.2172/1991087"}))
    assert everyone is not None and everyone.evidence == {"reject:namesake"}
    assert c.excluded(frozenset({"doi:10.2172/1991087"})) == "curated:namesake"
    assert c.decision("Ye (4OQaVGUAAAAJ)", frozenset({"doi:10.9999/none"})) is None
    assert c.links == (("arxiv:2310.18948", "doi:10.1016/j.oceaneng.2024.117234"),)
    assert c.overrides["doi:10.1234/x"] == (("booktitle", "Proceedings of Something"),)
    (thesis,) = c.records
    assert thesis.kind is Kind.PHDTHESIS and thesis.source is Source.CURATED
    assert thesis.authors[0].family == "Spadon"
    assert c.decision(spadon, thesis.ids) is not None
    c.check_members([member()])
    with pytest.raises(CuratedError, match="unknown members"):
        c.check_members([member(dirname="Other (x)")])


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("[[accept]]\nmember = 'x'\nid = 'title:foo'\n", "unknown id kind"),
        ("[[accept]]\nmember = 'x'\nid = 'doi:nope'\n", "not a valid id"),
        ("[[accept]]\nmember = 'x'\n", "missing keys"),
        ("[[accept]]\nmember = 'x'\nid = 'doi:10.1234/x'\ntitle = 'y'\n", "unknown keys"),
        ("[[override]]\nid = 'doi:10.1234/x'\nfield = 'doi'\nvalue = 'z'\n", "cannot be overridden"),
        ("[lab]\nror = ['01e6qks80']\n", "ror.org"),
        ("[lab]\nror = ['https://ror.org/not-a-ror']\n", "ror.org"),
        ("[stuff]\n", "unknown sections"),
        ("[[accept]]\nmember='m'\nid='doi:10.1234/x'\n[[reject]]\nmember='m'\nid='doi:10.1234/X'\n", "both accepts"),
        (
            "[[record]]\nid='manual:a'\nkind='dataset'\ntitle='t'\nauthors=['A B']\nyear=2021\nmembers=['m']\n",
            "not a publishable kind",
        ),
        (
            "[[record]]\nid='manual:a'\nkind='article'\ntitle='t'\nauthors=[]\nyear=2021\nmembers=['m']\n",
            "authors must be",
        ),
        ("not toml = = =", "curated.toml"),
    ],
)
def test_bad_curated_aborts(tmp_path: Path, text: str, message: str) -> None:
    with pytest.raises(CuratedError, match=message):
        load(write(tmp_path, text))


def test_lab_rors_share_the_registry_form(tmp_path: Path) -> None:
    c = load(write(tmp_path, "[lab]\nror = ['https://ror.org/01E6QKS80/']\n"))
    assert c.lab_rors == {"https://ror.org/01e6qks80"}
