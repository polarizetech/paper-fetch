from __future__ import annotations

import pytest

from paper_fetch.ids import Ident, doi_key, normalize, pmcid_from_openalex


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("10.1371/journal.pcbi.1003285", ("doi", "10.1371/journal.pcbi.1003285")),
        ("https://doi.org/10.1371/JOURNAL.PCBI.1003285", ("doi", "10.1371/journal.pcbi.1003285")),
        ("doi:10.1371/journal.pcbi.1003285.", ("doi", "10.1371/journal.pcbi.1003285")),
        ("W2036318837", ("openalex", "W2036318837")),
        ("https://openalex.org/w2036318837", ("openalex", "W2036318837")),
        ("pmid:24204232", ("pmid", "24204232")),
        ("pmc3812051", ("pmcid", "PMC3812051")),
        ("pmcid:PMC3812051", ("pmcid", "PMC3812051")),
        ("arxiv:2401.12345v2", ("arxiv", "2401.12345")),
        ("arxiv:q-bio/0601001", ("arxiv", "q-bio/0601001")),
    ],
)
def test_normalize(raw: str, want: tuple[str, str]) -> None:
    assert tuple(normalize(raw)) == want


@pytest.mark.parametrize("raw", ["24204232", "", "   ", "pmid:abc", "arxiv:nope", "hello"])
def test_normalize_refuses(raw: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        normalize(raw)


def test_ident_accessors() -> None:
    i = Ident("doi", "10.5555/x")
    assert (i.kind, i.value, str(i)) == ("doi", "10.5555/x", "doi:10.5555/x")


def test_doi_key_is_case_insensitive_and_one_segment() -> None:
    assert doi_key("10.5555/ABC") == doi_key("10.5555/abc") == "10.5555%2Fabc"


@pytest.mark.parametrize(
    ("ids", "want"),
    [
        ({"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/3812051"}, "PMC3812051"),
        ({"pmcid": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3812051/"}, "PMC3812051"),
        ({"pmcid": "https://x/not-a-number"}, None),
        ({}, None),
        (None, None),
    ],
)
def test_pmcid_from_openalex(ids: dict[str, str] | None, want: str | None) -> None:
    assert pmcid_from_openalex(ids) == want
