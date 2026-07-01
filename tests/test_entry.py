from __future__ import annotations

import pytest

from src.bibtex_utils import bibtex_from_dict, parse_bibtex_to_dict
from src.entry import BibEntry

# Representative BibTeX entries exercising the parse/serialize surface:
# an @article with doi/url, an @inproceedings with booktitle, an @misc, an
# entry carrying a non-preferred (unordered) field, and one with accented /
# LaTeX-formatted text to drive the ASCII normalization path.
ARTICLE_DOI_URL = """@article{Smith2020:Example,
  title = {A Study of Something Interesting},
  author = {Smith, John and Doe, Jane},
  year = {2020},
  journal = {Journal of Examples},
  volume = {12},
  number = {3},
  pages = {100--120},
  doi = {10.1234/example.2020},
  url = {https://doi.org/10.1234/example.2020}
}
"""

INPROCEEDINGS_BOOKTITLE = """@inproceedings{Doe2019:Conf,
  title = {Fast Algorithms for Hard Problems},
  author = {Doe, Jane},
  year = {2019},
  booktitle = {Proceedings of the International Conference on Examples},
  pages = {1--10}
}
"""

MISC_ENTRY = """@misc{Anon2021:Data,
  title = {A Dataset of Things},
  author = {Anonymous},
  year = {2021},
  howpublished = {Online repository}
}
"""

EXTRA_FIELD = """@article{Lee2022:Extra,
  title = {On Extra Fields},
  author = {Lee, Kim},
  year = {2022},
  journal = {Extra Journal},
  keywords = {sorting, ordering},
  abstract = {We test non-preferred fields.}
}
"""

ACCENTED_LATEX = """@article{Cafe2018:Accents,
  title = {\\'{E}tude sur le caf\\'{e}: \\textit{une analyse} d\\'{e}taill\\'{e}e},
  author = {Mu\\~{n}oz, Jos\\'{e}},
  year = {2018},
  journal = {Revista Espa\\~{n}ola}
}
"""

ALL_ENTRIES = [
    ARTICLE_DOI_URL,
    INPROCEEDINGS_BOOKTITLE,
    MISC_ENTRY,
    EXTRA_FIELD,
    ACCENTED_LATEX,
]


@pytest.mark.parametrize("text", ALL_ENTRIES)
def test_from_bibtex_to_bibtex_matches_reference(text: str) -> None:
    """to_bibtex() must be byte-identical to the direct dict serialization."""
    parsed = parse_bibtex_to_dict(text)
    assert parsed is not None
    reference = bibtex_from_dict(parsed)

    entry = BibEntry.from_bibtex(text)
    assert entry is not None
    assert entry.to_bibtex() == reference


@pytest.mark.parametrize("text", ALL_ENTRIES)
def test_from_bibtex_to_dict_equals_parsed(text: str) -> None:
    """to_dict() must reproduce the exact shape parse_bibtex_to_dict returns."""
    parsed = parse_bibtex_to_dict(text)
    assert parsed is not None

    entry = BibEntry.from_bibtex(text)
    assert entry is not None
    assert entry.to_dict() == parsed


@pytest.mark.parametrize("text", ALL_ENTRIES)
def test_roundtrip_fixpoint(text: str) -> None:
    """Re-parsing a serialized entry is an idempotent fixpoint.

    to_bibtex(from_bibtex(to_bibtex(from_bibtex(text)))) ==
    to_bibtex(from_bibtex(text)).
    """

    def _roundtrip(t: str) -> str:
        entry = BibEntry.from_bibtex(t)
        assert entry is not None
        return entry.to_bibtex()

    once = _roundtrip(text)
    twice = _roundtrip(once)
    assert twice == once


def test_from_bibtex_returns_none_on_invalid() -> None:
    assert BibEntry.from_bibtex("this is not a bibtex entry") is None


def test_from_dict_preserves_type_key_fields() -> None:
    entry = BibEntry.from_dict({"type": "article", "key": "Smith2020", "fields": {"title": "Hello", "year": "2020"}})
    assert entry.entry_type == "article"
    assert entry.key == "Smith2020"
    assert entry.fields == {"title": "Hello", "year": "2020"}
    assert entry.to_dict() == {
        "type": "article",
        "key": "Smith2020",
        "fields": {"title": "Hello", "year": "2020"},
    }


def test_from_dict_defaults() -> None:
    entry = BibEntry.from_dict({})
    assert entry.entry_type == "misc"
    assert entry.key == "entry"
    assert entry.fields == {}


def test_from_dict_lowercases_type_and_field_keys() -> None:
    entry = BibEntry.from_dict(
        {"type": "InProceedings", "key": "MixedCaseKey", "fields": {"Title": "T", "BookTitle": "B"}}
    )
    # entry_type lowercased, field keys lowercased, key case-preserved.
    assert entry.entry_type == "inproceedings"
    assert entry.key == "MixedCaseKey"
    assert entry.fields == {"title": "T", "booktitle": "B"}


def test_from_dict_defensive_copies_source_fields() -> None:
    source = {"type": "misc", "key": "K", "fields": {"title": "Original"}}
    entry = BibEntry.from_dict(source)
    # Mutating the source dict's fields must not leak into the entry.
    source["fields"]["title"] = "Changed"
    assert entry.fields["title"] == "Original"


def test_to_dict_returns_defensive_copy() -> None:
    entry = BibEntry.from_dict({"type": "misc", "key": "K", "fields": {"title": "Original"}})
    out = entry.to_dict()
    fields_out = out["fields"]
    assert isinstance(fields_out, dict)
    fields_out["title"] = "Mutated"
    # Mutating the returned dict must not change the entry.
    assert entry.fields["title"] == "Original"
