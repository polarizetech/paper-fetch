"""The library's promises, tested by counting network calls on a fake Http."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from conftest import (
    JATS,
    PDF,
    PROSE,
    FakeHttp,
    FakeProvider,
    J,
    fixture,
    good_provider,
    make_library,
    offline_openalex,
)
from paperlib import Library, LocalStore, MemoryStore, NotFound
from paperlib.http import Response
from paperlib.ids import doi_key
from paperlib.providers import Hit, Location, ProviderUnavailable

DOI = "10.5555/example.001"


def test_first_fetch_retrieves_and_records_provenance() -> None:
    lib, _ = make_library()
    rec = lib.fetch(DOI)
    assert rec["from"] == "retrieved"
    assert rec["full_text"] is True
    assert rec["route"] == "good:pdf"
    assert rec["title"] == "An example open-access article"
    assert rec["authors"] == ["Ada Example", "Ben Sample"]
    assert rec["pmid"] == "11111111"
    prov = lib.provenance("W7")
    assert prov["route"] == "good:pdf"
    assert prov["providers"] == ["good"]
    assert prov["ids"]["doi"] == DOI
    assert prov["license"] == "cc-by"
    assert prov["rights"] == "open-access copy reported by good"
    assert set(prov["sha256"]) == {"fulltext.pdf", "fulltext.txt"}
    assert prov["retry_after"] is None
    assert "unfamiliar" in re.sub(r"\s+", " ", lib.text("W7"))
    assert lib.work(DOI)["abstract"] == "An abstract in order."


def test_a_held_paper_is_never_downloaded_again() -> None:
    lib, http = make_library()
    good = lib.providers[0]
    assert isinstance(good, FakeProvider)
    lib.fetch(DOI)
    calls, downloads = http.calls, good.downloads
    for spelling in (DOI, "https://doi.org/10.5555/EXAMPLE.001", "W7", "pmid:11111111"):
        assert lib.fetch(spelling)["from"] == "library"
    assert (http.calls, good.downloads) == (calls, downloads)


def test_a_deleted_catalogue_is_repaired_without_network() -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    store = lib.store
    assert isinstance(store, MemoryStore)
    del store.objects["papers/index.jsonl"]
    lib2, http2 = make_library(store=store)
    good2 = lib2.providers[0]
    assert isinstance(good2, FakeProvider)
    assert lib2.fetch(DOI)["from"] == "library"
    assert (http2.calls, good2.downloads) == (0, 0)
    assert store.has("papers/index.jsonl")
    lib3, http3 = make_library(store=store)
    del store.objects["papers/index.jsonl"]
    assert lib3.lookup("W7") is not None
    assert http3.calls == 0


def test_rebuild_index_and_status() -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    assert lib.rebuild_index() == 1
    st = lib.status()
    assert (st["works"], st["with_full_text"], st["not_obtainable"]) == (1, 1, 0)
    assert st["store"] == "MemoryStore"
    assert lib.index(refresh=True)["W7"]["full_text"] is True


def test_not_obtainable_is_remembered_until_retry_after() -> None:
    lib, http = make_library(providers=[FakeProvider("none")])
    r = lib.fetch(DOI)
    assert r["from"] == "not-obtainable"
    assert r["refused"]
    assert lib.store.has("papers/works/W7/work.json")
    with pytest.raises(NotFound, match="no readable full text"):
        lib.text(DOI)
    calls = http.calls
    assert lib.fetch(DOI)["from"] == "library"
    assert http.calls == calls
    past = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    lib.index()["W7"]["retry_after"] = past
    lib.fetch(DOI)
    assert http.calls > calls


def test_force_asks_again() -> None:
    lib, http = make_library()
    lib.fetch(DOI)
    calls = http.calls
    assert lib.fetch(DOI, force=True)["from"] == "retrieved"
    assert http.calls > calls


def test_a_work_openalex_does_not_know_is_keyed_by_its_arrival_identifier() -> None:
    pre = FakeProvider(
        "pre", [Location("https://e.example/pre.xml", "jats-xml", "pre", "cc_by", "preprint")], JATS
    )
    lib, _ = make_library({"api.openalex.org/works/": Response(404, {}, b"", "")}, providers=[pre])
    r = lib.fetch("10.1101/2026.01.01.000123")
    assert r["work"] == f"doi-{doi_key('10.1101/2026.01.01.000123')}"
    assert r["full_text"] is True
    assert lib.fetch("10.1101/2026.01.01.000123")["from"] == "library"


def test_arxiv_ids_go_through_their_datacite_doi() -> None:
    lib, http = make_library({"api.openalex.org/works/": Response(404, {}, b"", "")})
    r = lib.fetch("arxiv:2401.12345v2")
    assert r["work"] == "arxiv-2401.12345"
    assert r["doi"] == "10.48550/arxiv.2401.12345"
    assert any("works/doi:10.48550/arxiv.2401.12345" in u for u in http.log)
    assert lib.fetch("arxiv:2401.12345")["from"] == "library"
    assert lib.lookup("10.48550/arxiv.2401.12345") is not None


IDC = J({"records": [{"pmcid": "PMC3333333", "doi": DOI, "pmid": 33333333}]})


def _pmc_routes() -> dict[str, Response]:
    return {
        "api.openalex.org/works/pmcid:": Response(404, {}, b"", ""),
        "api.openalex.org/works/": J(fixture("openalex_work.json")),
        "idconv": IDC,
    }


def test_a_pmcid_resolves_through_ncbi_to_the_openalex_work() -> None:
    lib, http = make_library(_pmc_routes())
    r = lib.fetch("PMC3333333")
    assert (r["work"], r["title"], r["year"], r["pmcid"]) == (
        "W7",
        "An example open-access article",
        2020,
        "PMC3333333",
    )
    assert not any("works/pmcid:" in u for u in http.log)
    calls = http.calls
    assert lib.fetch("PMC3333333")["from"] == "library"
    assert http.calls == calls


def test_search_library_and_titles() -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    assert [r["work"] for r in lib.search_library("example ADA")] == ["W7"]
    assert lib.search_library("unfamiliar") == []
    rows = lib.search_library("unfamiliar project", full_text=True)
    assert rows[0]["matched_in"] == "full text"
    assert len(lib.search_library("", limit=1)) == 1


def test_not_held_raises_not_found() -> None:
    lib, _ = make_library()
    for fn in (lib.text, lib.provenance, lib.work):
        with pytest.raises(NotFound):
            fn("10.5555/unknown")


def test_verify_detects_tampering() -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    assert lib.verify(DOI) == {
        "work": "W7",
        "ok": True,
        "files": {"fulltext.pdf": "ok", "fulltext.txt": "ok"},
    }
    lib.store.put("papers/works/W7/fulltext.txt", b"edited")
    lib.store.delete("papers/works/W7/fulltext.pdf")
    v = lib.verify(DOI)
    assert v["ok"] is False
    assert v["files"] == {"fulltext.pdf": "missing", "fulltext.txt": "mismatch"}


def test_local_store_end_to_end(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "lib")
    lib, _ = make_library(store=store)
    lib.fetch(DOI)
    assert (tmp_path / "lib" / "papers" / "works" / "W7" / "fulltext.pdf").read_bytes() == PDF
    fresh, http = make_library(store=LocalStore(tmp_path / "lib"))
    assert fresh.fetch(DOI)["from"] == "library"
    assert http.calls == 0
    assert fresh.verify("W7")["ok"] is True


# ---------------------------------------------------------------------------------- add_local


def test_add_local_requires_rights_and_a_pdf(tmp_path: Path) -> None:
    lib, _ = make_library()
    pdf = tmp_path / "held.pdf"
    pdf.write_bytes(PDF)
    with pytest.raises(ValueError, match="declare the rights"):
        lib.add_local(pdf, DOI, rights="  ")
    txt = tmp_path / "held.txt"
    txt.write_text("not a pdf")
    with pytest.raises(ValueError, match="not a PDF"):
        lib.add_local(txt, DOI, rights="author copy")
    rec = lib.add_local(pdf, DOI, rights="author copy")
    assert rec["route"] == "operator-supplied:pdf"
    assert rec["full_text"] is True
    assert lib.provenance("W7")["rights"] == "author copy"
    unknown, _ = make_library({"api.openalex.org/works/": Response(404, {}, b"", "")})
    with pytest.raises(NotFound, match="OpenAlex has no work"):
        unknown.add_local(pdf, DOI, rights="author copy")


# ---------------------------------------------------------------------------------- orphans


def test_adopt_orphans_moves_unique_and_drops_duplicates() -> None:
    orph = MemoryStore()
    for key, doi in (("pmcid-PMC3333333", DOI), ("pmcid-PMC1", "10.5555/dup")):
        base = f"papers/works/{key}/"
        orph.put(
            base + "work.json",
            json.dumps(
                {"id": key, "doi": doi, "ids": {}, "title": None, "authorships": []}
            ).encode(),
        )
        orph.put(base + "fulltext.jats.xml", JATS)
        orph.put(base + "fulltext.txt", PROSE.encode())
        prov = {
            "work": key,
            "full_text": True,
            "route": "pmc-s3:jats-xml",
            "format": "jats-xml",
            "sha256": {"fulltext.jats.xml": "x", "fulltext.txt": "y"},
            "ids": {"pmcid": key[6:], "doi": doi, "pmid": None, "openalex": None, "arxiv": None},
        }
        orph.put(base + "provenance.json", json.dumps(prov).encode())
        orph.put(f"papers/doi/{doi_key(doi)}.json", json.dumps({"work": key}).encode())
    w8 = {
        **fixture("openalex_work.json"),
        "id": "https://openalex.org/W8",
        "doi": "https://doi.org/10.5555/dup",
        "title": "Held already",
    }
    orph.put("papers/works/W8/work.json", json.dumps(w8).encode())
    orph.put(
        "papers/works/W8/provenance.json",
        json.dumps(
            {"work": "W8", "full_text": True, "sha256": {}, "ids": {"doi": "10.5555/dup"}}
        ).encode(),
    )
    orph.put(
        "papers/works/doi-10.5555%2Fnobody/provenance.json",
        json.dumps(
            {"work": "doi-10.5555%2Fnobody", "full_text": False, "ids": {"doi": "10.5555/nobody"}}
        ).encode(),
    )
    orph.put("papers/works/doi-10.5555%2Fnobody/work.json", json.dumps({"id": "x"}).encode())
    routes = {
        "api.openalex.org/works/doi:10.5555/nobody": Response(404, {}, b"", ""),
        "api.openalex.org/works/doi:10.5555/dup": J(w8),
        **_pmc_routes(),
    }
    lib, _ = make_library(routes, store=orph, providers=[])
    assert lib.rebuild_index() == 4
    rep = {x["orphan"]: x["action"] for x in lib.adopt_orphans()}
    assert rep == {
        "pmcid-PMC3333333": "moved",
        "pmcid-PMC1": "dropped: duplicate of a held work",
        "doi-10.5555%2Fnobody": "kept",
    }
    w7 = lib.index()["W7"]
    assert (w7["title"], w7["full_text"], w7["pmcid"]) == (
        "An example open-access article",
        True,
        "PMC3333333",
    )
    assert orph.has("papers/works/W7/fulltext.jats.xml")
    assert not orph.keys("papers/works/pmcid-")
    assert lib.rebuild_index() == 3
    assert json.loads(orph.get(f"papers/doi/{doi_key(DOI)}.json"))["work"] == "W7"
    assert json.loads(orph.get(f"papers/doi/{doi_key('10.5555/dup')}.json"))["work"] == "W8"


# ---------------------------------------------------------------------------------- search


def _search_library() -> Library:
    lib, _ = make_library()
    lib.fetch(DOI)
    return lib


def test_federated_search_merges_marks_and_reports_by_name() -> None:
    lib = _search_library()
    a = FakeProvider(
        "a",
        hits=[
            Hit("a", "Same paper", 2020, {"doi": DOI}, True),
            Hit("a", "Only in A", 2019, {"doi": "10.5555/only-a"}, True),
        ],
    )
    b = FakeProvider("b", hits=[Hit("b", "Same paper", 2020, {"doi": DOI, "pmcid": "PMC1"}, True)])
    dead = FakeProvider("dead", raise_=ProviderUnavailable("dead rate-limited us (HTTP 429)"))
    broken = FakeProvider("broken", raise_=KeyError("x"))
    gated = FakeProvider("gated", needs=("PAPERLIB_TEST_NEVER_SET",))
    lib.search_providers = [a, b, dead, broken, gated]
    res = lib.search("same")
    assert len(res["hits"]) == 2
    same = res["hits"][0]
    assert sorted(set(same["providers"])) == ["a", "b"]
    assert same["ids"]["pmcid"] == "PMC1"
    assert same["full_text_in_library"] is True
    assert same["work"] == "W7"
    st = res["providers"]
    assert (st["dead"]["status"], st["broken"]["status"], st["gated"]["status"]) == (
        "unavailable",
        "error",
        "skipped",
    )
    assert st["web"]["status"] == "not-needed"
    again = lib.search("same")["providers"]
    assert (again["a"]["status"], again["b"]["status"]) == ("cache", "cache")
    assert lib.search("same", refresh=True)["providers"]["a"]["status"] == "ok"


def test_search_by_named_providers() -> None:
    lib, http = make_library({"europepmc": J(fixture("europepmc_search.json"))})
    res = lib.search("x", providers=["europepmc"], web_fallback=False)
    assert list(res["providers"]) == ["europepmc", "web"]
    assert res["providers"]["europepmc"]["n"] == 3
    assert any("europepmc" in u for u in http.log)


def test_web_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    lib = _search_library()
    lib.http = FakeHttp({"searx.example/search": J(fixture("searxng.json"))})
    closed = FakeProvider(
        "closed", hits=[Hit("closed", "Closed", 2020, {"doi": "10.5555/c"}, False)]
    )
    lib.search_providers = [closed]
    r = lib.search("q1")
    assert r["providers"]["web"]["status"] == "ok"
    assert "open-access" in r["providers"]["web"]["why_ran"]
    assert len(r["hits"]) == 3
    assert any(u.startswith("https://lab.example.edu") for h in r["hits"] for u in h["urls"])
    assert not any("/searches/web/" in k for k in lib.store.keys("papers/searches/"))
    assert lib.search("q2", web_fallback=False)["providers"]["web"]["status"] == "skipped"
    monkeypatch.setenv("PAPERLIB_WEB_FALLBACK", "0")
    assert lib.search("q3")["providers"]["web"]["status"] == "skipped"
    monkeypatch.delenv("PAPERLIB_WEB_FALLBACK")
    lib.search_providers = []
    empty = lib.search("q4")
    assert empty["providers"]["web"]["why_ran"] == "no hits from the scholarly providers"


def test_expired_search_cache_is_ignored() -> None:
    lib = _search_library()
    lib.search_ttl_days = 0
    a = FakeProvider("a", hits=[Hit("a", "T", 2020, {"doi": "10.5555/t"}, True)])
    lib.search_providers = [a]
    lib.search("t")
    assert lib.search("t")["providers"]["a"]["status"] == "ok"


def test_default_library_uses_the_local_store(tmp_path: Path) -> None:
    lib = Library.default()
    assert isinstance(lib.store, LocalStore)
    assert lib.store.root == tmp_path / "data"
    assert [p.name for p in lib.providers][:2] == ["pmc-s3", "europepmc"]
    assert "openalex" in [p.name for p in lib.search_providers]


def test_offline_helpers_are_offline() -> None:
    lib = Library(MemoryStore(), offline_openalex(FakeHttp()), providers=[good_provider()])
    assert lib.lookup(DOI) is None
