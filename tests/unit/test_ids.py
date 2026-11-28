import pytest
from hypothesis import given
from hypothesis import strategies as st

from citeforge.ids import (
    first_id,
    ids_of,
    is_copy_id,
    is_preprint_doi,
    is_secondary_doi,
    norm_arxiv,
    norm_doi,
    norm_openalex,
    norm_orcid,
    norm_ror,
    preprint_server_of,
    to_id,
    work_key,
)

dois = st.from_regex(r"10\.\d{4,9}/[A-Za-z0-9._;()/:-]{1,30}[A-Za-z0-9]", fullmatch=True)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://doi.org/10.1016/J.OCEANENG.2024.118303", "10.1016/j.oceaneng.2024.118303"),
        ("http://dx.doi.org/10.1109/ojvt.2021.3097560.", "10.1109/ojvt.2021.3097560"),
        ("doi: 10.1145/3589132%2F3625607", "10.1145/3589132/3625607"),
        ("  10.1093/nar/gkz935  ", "10.1093/nar/gkz935"),
        ("10.1007/978-3-319-93698-7\\_21", "10.1007/978-3-319-93698-7_21"),
        ("10.123/short", None),
        ("10.1234/ab\x1bc", None),
        ("10.1234/ab%00c", None),
        ("not a doi", None),
        ("", None),
    ],
)
def test_norm_doi(raw: str, expected: str | None) -> None:
    assert norm_doi(raw) == expected


@given(dois, st.sampled_from(["", "https://doi.org/", "http://dx.doi.org/", "doi:"]))
def test_norm_doi_idempotent_and_prefix_free(doi: str, prefix: str) -> None:
    once = norm_doi(prefix + doi)
    assert once is not None
    assert norm_doi(once) == once
    assert norm_doi(prefix + doi.upper()) == once


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2310.18948v3", "2310.18948"),
        ("arXiv:1501.00001", "1501.00001"),
        ("https://arxiv.org/abs/2101.12345v2", "2101.12345"),
        ("math.GT/0309136", "math.GT/0309136"),
        ("HEP-TH/9901001v1", "hep-th/9901001"),
        ("1234.567", None),
    ],
)
def test_norm_arxiv(raw: str, expected: str | None) -> None:
    assert norm_arxiv(raw) == expected


@given(st.from_regex(r"\d{4}\.\d{4,5}(v\d)?|[a-z-]{2,8}(\.[A-Z]{2})?/\d{7}(v\d)?", fullmatch=True))
def test_norm_arxiv_idempotent_case_insensitive(raw: str) -> None:
    once = norm_arxiv(raw)
    assert once is not None
    assert norm_arxiv(once) == once
    assert norm_arxiv(raw.upper()) == once
    assert norm_arxiv(raw.lower()) == once


def test_norm_orcid_checksum() -> None:
    assert norm_orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"
    assert norm_orcid("0000-0002-1694-233x") == "0000-0002-1694-233X"
    assert norm_orcid("0000-0002-1825-0098") is None
    assert norm_orcid("0000000218250097") is None


def test_norm_openalex() -> None:
    assert norm_openalex("https://openalex.org/A5023888391") == "A5023888391"
    assert norm_openalex("w2741809807") == "W2741809807"
    assert norm_openalex("A12") is None


def test_to_id_maps_arxiv_doi_and_rejects_garbage() -> None:
    assert to_id("doi", "10.48550/arXiv.2310.18948") == "arxiv:2310.18948"
    assert to_id("doi", "https://doi.org/10.1093/NAR/gkz935") == "doi:10.1093/nar/gkz935"
    assert to_id("pmid", "31665441") == "pmid:31665441"
    assert to_id("pmid", "0") is None
    assert to_id("dblp", "journals/corr/abs-2310-18948") == "dblp:journals/corr/abs-2310-18948"
    assert to_id("manual", "Spadon-2021-thesis") == "manual:spadon-2021-thesis"
    with pytest.raises(ValueError, match="unknown id kind"):
        to_id("isbn", "123")


def test_work_key_order_prefers_published_doi() -> None:
    assert work_key({"openalex:W1", "arxiv:2310.18948", "doi:10.1/x"}) == "doi:10.1/x"
    assert work_key({"doi:10.1101/2021.01.01.1", "doi:10.21203/rs.3.rs-1/v1", "doi:10.9999/b"}) == "doi:10.9999/b"
    assert work_key({"dblp:conf/x/Y21", "arxiv:2101.00001"}) == "arxiv:2101.00001"
    assert work_key({"doi:10.2/b", "doi:10.1/a"}) == "doi:10.1/a"


def test_preprint_and_secondary_dois() -> None:
    assert is_preprint_doi("10.21203/rs.3.rs-123/v1")
    assert is_preprint_doi("10.2172/1991087")
    assert is_preprint_doi("10.13140/rg.2.2.1")
    assert not is_preprint_doi("10.1016/j.oceaneng.2024.118303")
    assert is_secondary_doi("10.5281/zenodo.123")
    assert is_copy_id("arxiv:2101.00001")
    assert is_copy_id("doi:10.5281/zenodo.123")
    assert is_copy_id("doi:10.21203/rs.3.rs-123/v1")
    assert not is_copy_id("doi:10.1016/j.oceaneng.2024.118303")
    assert not is_copy_id("dblp:conf/x/Y21")


@pytest.mark.parametrize(
    ("doi", "copy"),
    [
        ("10.1101/2021.01.16.21249943", True),
        ("10.1101/2020.05.01.072983v2", True),
        ("10.1101/012345", True),
        ("10.1101/gr.095612.109", False),
        ("10.1101/gad.350000.122", False),
        ("10.1101/lm.053781.123", False),
        ("10.1101/cshperspect.a041234", False),
        ("10.1101/pdb.prot5678", False),
    ],
)
def test_only_biorxiv_numbering_marks_a_cold_spring_harbor_doi_a_copy(doi: str, copy: bool) -> None:
    assert is_copy_id(f"doi:{doi}") is copy
    assert (preprint_server_of(doi) == "bioRxiv") is copy


def test_ids_of_and_first_id() -> None:
    ids = {"doi:10.2/b", "arxiv:2101.00001", "doi:10.1/a", "pmid:1"}
    assert ids_of(ids, "doi") == ["doi:10.1/a", "doi:10.2/b"]
    assert ids_of(ids, "doi", "arxiv") == ["arxiv:2101.00001", "doi:10.1/a", "doi:10.2/b"]
    assert first_id(ids, "doi") == "doi:10.1/a"
    assert first_id(ids, "dblp") is None


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("https://ror.org/01e6qks80", "https://ror.org/01e6qks80"),
        ("https://ror.org/01e6qks80/", "https://ror.org/01e6qks80"),
        ("https://www.ror.org/01E6QKS80", "https://ror.org/01e6qks80"),
        ("  01e6qks80 ", "https://ror.org/01e6qks80"),
        ("http://ror.org/01e6qks80", "https://ror.org/01e6qks80"),
        ("0abcdefgh", None),
        ("01i6qks80", None),
        ("https://ror.org/01e6qks8", None),
        ("https://example.org/01e6qks80", None),
        ("", None),
    ],
)
def test_norm_ror(raw: str, want: str | None) -> None:
    assert norm_ror(raw) == want
