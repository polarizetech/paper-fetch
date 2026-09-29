from __future__ import annotations

import pytest

from conftest import FakeHttp, J, fixture
from paper_fetch.http import Response
from paper_fetch.providers import ProviderUnavailable, WebSearch
from paper_fetch.providers.web import parse_ids


@pytest.mark.parametrize(
    ("url", "text", "want"),
    [
        (
            "https://www.frontiersin.org/articles/10.5555/example.2017.00109/full",
            "",
            {"doi": "10.5555/example.2017.00109"},
        ),
        (
            "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0000001",
            "",
            {"doi": "10.1371/journal.pone.0000001"},
        ),
        ("https://pubmed.ncbi.nlm.nih.gov/24204232/", "", {"pmid": "24204232"}),
        ("https://pmc.ncbi.nlm.nih.gov/articles/PMC3812051/", "", {"pmcid": "PMC3812051"}),
        ("https://arxiv.org/pdf/2401.12345v2.pdf", "", {"arxiv": "2401.12345"}),
        (
            "https://lab.example.edu/pubs/paper.pdf",
            "Published as doi:10.5555/example.001.",
            {"doi": "10.5555/example.001"},
        ),
        ("https://blog.example.com/post", "no identifier here", {}),
    ],
)
def test_parse_ids(url: str, text: str, want: dict[str, str]) -> None:
    assert parse_ids(url, text) == want


def test_unconfigured_web_is_unavailable() -> None:
    w = WebSearch(FakeHttp())
    assert w.available() == (False, "needs SEARXNG_URL")
    with pytest.raises(ProviderUnavailable, match="not configured"):
        w.search("x")


def test_searxng_results(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example/")
    got = WebSearch(FakeHttp({"searx.example/search": J(fixture("searxng.json"))})).search("x")
    assert len(got) == 2
    assert got[0].title == "Example article"
    assert got[0].ids["doi"] == "10.1371/journal.pone.0000001"
    assert got[0].extra["url"].startswith("https://journals.plos.org")
    assert got[0].is_oa is None
    assert got[0].extra["unresponsive_engines"] == ["duckduckgo"]
    assert got[1].ids == {}
    assert got[1].extra["ids_parsed"] is False


@pytest.mark.parametrize(
    ("resp", "why"),
    [
        (J({"results": [], "unresponsive_engines": [["brave", "CAPTCHA"]]}), "unresponsive"),
        (J({}, 403), "format=json"),
        (Response(500, {}, b"", ""), "HTTP 500"),
    ],
)
def test_searxng_failures_are_unavailable(
    monkeypatch: pytest.MonkeyPatch, resp: Response, why: str
) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    with pytest.raises(ProviderUnavailable, match=why):
        WebSearch(FakeHttp({"searx.example/search": resp})).search("x")
