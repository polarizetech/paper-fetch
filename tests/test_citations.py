from __future__ import annotations

import pytest

from conftest import FakeHttp, J, fixture, make_library
from paper_fetch import CitationsUnavailable, Library, NotFound
from paper_fetch.citations import OpenCitations, parse_pids
from paper_fetch.http import Response


def test_parse_pids() -> None:
    got = parse_pids("omid:br/0611 doi:10.1371/JOURNAL.PCBI.1003285 openalex:W2036318837 x junk:")
    assert got == {
        "omid": "br/0611",
        "doi": "10.1371/journal.pcbi.1003285",
        "openalex": "W2036318837",
    }
    assert parse_pids(None) == {}


def _graph_library() -> tuple[Library, FakeHttp]:
    routes = {
        "index/v2/citations/doi:10.5555/cited.001": J(fixture("opencitations_citations.json")),
        "index/v2/references/pmid:24204232": J(
            [
                {
                    "oci": "9",
                    "cited": "omid:br/9 doi:10.5555/ref.001",
                    "citing": "pmid:24204232",
                    "creation": "2013-10",
                    "journal_sc": "no",
                    "author_sc": "no",
                }
            ]
        ),
        "index/v2/citations/doi:10.5555/limited": J({}, 429),
        "api.openalex.org/works/W55": J(
            {
                "id": "https://openalex.org/W55",
                "doi": None,
                "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/24204232"},
            }
        ),
        "api.openalex.org/works/W66": J({"id": "https://openalex.org/W66", "doi": None, "ids": {}}),
    }
    lib, http = make_library(routes, providers=[])
    lib.index()["W4410000001"] = {
        "work": "W4410000001",
        "doi": "10.5555/citing.001",
        "full_text": True,
    }
    return lib, http


def test_citations_mark_held_works_and_are_cached() -> None:
    lib, http = _graph_library()
    g = lib.citations("10.5555/cited.001")
    assert (g["n"], g["held"], g["status"]) == (2, 1, "ok")
    first, second = g["items"]
    assert first["full_text_in_library"] is True
    assert first["work"] == "W4410000001"
    assert first["citing_date"] == "2025-06"
    assert first["author_self_citation"] is True
    assert "creation" not in first
    assert second["ids"] == {"omid": "br/3", "doi": "10.5555/not-held", "pmid": "111"}
    n = http.calls
    g2 = lib.citations("https://doi.org/10.5555/CITED.001")
    assert g2["status"] == "cache"
    assert http.calls == n


def test_references_resolve_an_openalex_id_to_a_pmid() -> None:
    lib, http = _graph_library()
    g = lib.references("W55")
    assert g["of"] == {"doi": None, "pmid": "24204232"}
    assert g["items"][0]["ids"]["doi"] == "10.5555/ref.001"
    assert any("references/pmid:24204232" in u for u in http.log)


def test_no_doi_or_pmid_is_not_found_and_a_429_is_unavailable() -> None:
    lib, _ = _graph_library()
    with pytest.raises(NotFound):
        lib.citations("W66")
    with pytest.raises(CitationsUnavailable, match="429"):
        lib.citations("10.5555/limited")


def test_old_cache_entries_are_upgraded() -> None:
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    lib, _ = _graph_library()
    lib.store.put(
        "papers/citations/citations/10.5555%2Fold.json",
        json.dumps(
            {"_cached_at": time.time(), "edges": [{"ids": {}, "creation": "2020"}]}
        ).encode(),
    )
    g = lib.citations("10.5555/old")
    assert g["items"][0]["citing_date"] == "2020"


def test_token_is_sent_only_when_set_and_a_wrong_one_is_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    http = FakeHttp({"index/v2": J([])})
    OpenCitations(http).edges({"doi": "10.5555/x"}, "citations")
    assert "authorization" not in http.headers[-1]
    monkeypatch.setenv("OPENCITATIONS_ACCESS_TOKEN", "t0k")
    OpenCitations(http).edges({"doi": "10.5555/x"}, "citations")
    assert http.headers[-1]["authorization"] == "t0k"
    bad = OpenCitations(FakeHttp({"index/v2": Response(403, {}, b"Invalid token.", "")}))
    with pytest.raises(CitationsUnavailable, match="rejected OPENCITATIONS_ACCESS_TOKEN"):
        bad.edges({"doi": "10.5555/x"}, "citations")


def test_counts_and_argument_errors() -> None:
    oc = OpenCitations(
        FakeHttp({"citation-count/doi:": J([{"count": "7"}]), "reference-count": J([])})
    )
    assert oc.count({"doi": "10.5555/x"}, "citations") == 7
    assert oc.count({"pmid": "1"}, "references") == 0
    with pytest.raises(ValueError, match="direction"):
        oc.edges({"doi": "10.5555/x"}, "cited-by")
    with pytest.raises(ValueError, match="direction"):
        oc.count({"doi": "10.5555/x"}, "cited-by")
    with pytest.raises(ValueError, match="DOI or a PMID"):
        oc.edges({}, "citations")


def test_unreachable_and_other_errors() -> None:
    from conftest import DeadHttp  # noqa: PLC0415

    with pytest.raises(CitationsUnavailable, match="unreachable"):
        OpenCitations(DeadHttp()).edges({"doi": "10.5555/x"}, "citations")
    with pytest.raises(CitationsUnavailable, match="HTTP 500"):
        OpenCitations(FakeHttp({"index": Response(500, {}, b"", "")})).edges(
            {"doi": "10.5555/x"}, "citations"
        )
