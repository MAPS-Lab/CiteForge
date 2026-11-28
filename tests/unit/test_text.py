import unicodedata

import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.model import Author
from citeforge.text import (
    ascii_family,
    clean,
    family_keys,
    fold,
    initials,
    is_affiliation,
    is_group,
    name_key,
    name_matches,
    ratio,
    recase_title,
    slug_words,
    token_set,
    word_prefix,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Deep &amp;amp; &lt;i&gt;Wide&lt;/i&gt; nets", "Deep & Wide nets"),
        ("CO<sub>2</sub> capture", "CO2 capture"),
        ("<jats:title>A <mml:math><mml:mi>k</mml:mi></mml:math>-NN study</jats:title>", "A k-NN study"),
        (r"{SAR} images at 100% and \$5", "SAR images at 100% and $5"),
        (r"M\'{a}rquez and G\"odel", "Márquez and Gödel"),
        ("a < b and c > d", "a < b and c > d"),
        ("Q&A with {AIS}", "Q&A with AIS"),
        (r"R&D for \emph{x}", "R&D for x"),
        (r"{a_b} X", "a_b X"),
        (r"C# in {GPU}", "C# in GPU"),
        (r"Tom \& {Jerry}", "Tom & Jerry"),
        ("line\nbreak\ttab​zero", "line break tabzero"),
        ("Café", "Café"),
    ],
)
def test_clean(raw: str, expected: str) -> None:
    assert clean(raw) == expected


@given(st.text(max_size=80))
def test_clean_idempotent_and_safe(raw: str) -> None:
    once = clean(raw)
    assert clean(once) == once
    assert "\n" not in once
    assert not any(unicodedata.category(c) == "Cc" for c in once)
    assert unicodedata.is_normalized("NFC", once)


def test_fold_and_similarity() -> None:
    assert fold("Müller-Lüdenscheidt, Ç.") == "muller ludenscheidt c"
    assert ratio("Vessel  Trajectory", "vessel trajectory") == 1.0
    assert token_set("Deep learning for AIS", "AIS deep learning") == 1.0
    assert word_prefix("Graph neural networks", "Graph neural networks for maritime traffic")
    assert not word_prefix("Graph neural", "Graph neural networks")


def test_names() -> None:
    assert family_keys("de Souza") == {"desouza", "souza"}
    assert "chaplin" in family_keys("Rau-Chaplin")
    assert initials("J.-P.") == "jp"
    assert initials("Md Mahbub") == "mm"
    spadon = [name_key("Gabriel", "Spadon")]
    assert name_matches(spadon, Author("G.", "Spadon"))
    assert name_matches(spadon, Author("", "Spadon"))
    assert not name_matches(spadon, Author("Marcos", "Spadon"))
    assert name_matches(spadon, Author("", "", literal="Gabriel Spadon"))
    assert not name_matches(spadon, Author("", "", literal="Spadon Research Group"))
    assert name_matches([name_key("Anna", "de Souza")], Author("A.", "Souza"))


def test_group_and_affiliation() -> None:
    assert is_group("The ATLAS Collaboration")
    assert is_group("COVID-19 Genomics UK Consortium")
    assert is_affiliation("Dalhousie University")
    assert is_affiliation("Acme Inc.")
    assert not is_affiliation("University Hospital Study Group")
    assert not is_affiliation("Gabriel Spadon")


def test_recase_title() -> None:
    assert recase_title("DEEP LEARNING FOR IOT AND AI") == "Deep Learning for IoT and AI"
    assert recase_title("Normal Title with SAR") == "Normal Title with SAR"
    assert recase_title("ABC") == "ABC"


@given(st.text(max_size=60))
def test_slug_words_ascii(title: str) -> None:
    for w in slug_words(title):
        assert w.isascii() and w.isalnum()


def test_slug_and_family() -> None:
    expected = ["Enhancing", "Short", "Term", "Vessel", "Trajectory"]
    assert slug_words("Enhancing short-term vessel trajectory") == expected
    assert ascii_family("Rau-Chaplin") == "RauChaplin"
    assert ascii_family("Müller") == "Muller"
    assert ascii_family("de la Fuente") == "DeLaFuente"
