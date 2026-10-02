"""A work the library minted itself (`pmcid-...`, `doi-...`) is accepted everywhere a work id is,
and one bad record never stops a batch."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from conftest import JATS, PROSE, J, make_library
from paper_fetch import Library, MemoryStore
from paper_fetch.http import Response
from paper_fetch.ids import normalize
from paper_fetch.passages import PassageIndex

PMC = "pmcid-PMC5519140"
SUPP = "doi-10.1214%2F12-aoas565supp"
BAD = "mystery-record"  # in the catalogue, but no id kind the library knows


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        (PMC, ("pmcid", "PMC5519140")),
        (SUPP, ("doi", "10.1214/12-aoas565supp")),
        ("DOI-10.1214%2F12-AOAS565SUPP", ("doi", "10.1214/12-aoas565supp")),
        ("10.1214%2F12-aoas565supp", ("doi", "10.1214/12-aoas565supp")),
        ("pmid-123", ("pmid", "123")),
        ("arxiv-q-bio%2F0601001", ("arxiv", "q-bio/0601001")),
        ("doi:10.1214/12-aoas565supp", ("doi", "10.1214/12-aoas565supp")),  # unchanged
        ("W7", ("openalex", "W7")),  # unchanged
    ],
)
def test_normalize_accepts_the_librarys_own_work_ids(raw: str, want: tuple[str, str]) -> None:
    assert tuple(normalize(raw)) == want


@pytest.mark.parametrize("raw", [BAD, "pmcid-nope", "doi-notadoi", "pmid-abc"])
def test_normalize_still_refuses_junk(raw: str) -> None:
    with pytest.raises(ValueError, match="identifier"):
        normalize(raw)


def _seed(store: MemoryStore, key: str, ids: dict[str, str | None], *, text: bool = True) -> None:
    base = f"papers/works/{key}/"
    store.put(
        base + "work.json",
        json.dumps({"id": key, "ids": {}, "title": None, "authorships": []}).encode(),
    )
    files = {"fulltext.jats.xml": "x"}
    store.put(base + "fulltext.jats.xml", JATS)
    if text:
        store.put(base + "fulltext.txt", PROSE.encode())
        files["fulltext.txt"] = "y"
    prov = {
        "work": key,
        "full_text": text,
        "route": "pmc-s3:jats-xml",
        "format": "jats-xml",
        "sha256": files,
        "ids": {"doi": None, "pmid": None, "pmcid": None, "openalex": None, "arxiv": None, **ids},
    }
    store.put(base + "provenance.json", json.dumps(prov).encode())


def _library(tmp_path: Path) -> Library:
    store = MemoryStore()
    _seed(store, PMC, {"pmcid": "PMC5519140"})
    _seed(store, SUPP, {})  # no doi recorded: only the work id says what it is
    _seed(store, BAD, {})
    lib, _ = make_library(store=store)
    lib.rebuild_index()
    lib.passages = PassageIndex(tmp_path / "p.sqlite")
    return lib


def test_index_with_no_arguments_indexes_odd_keys_from_their_rows(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    r = lib.index_works()
    assert sorted(r["indexed"]) == sorted([PMC, SUPP, BAD])  # the work id is read from its row
    assert r["skipped"] == []


def test_a_record_whose_text_cannot_be_read_is_skipped_not_fatal(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    lib.store.delete(f"papers/works/{PMC}/fulltext.txt")
    r = lib.index_works()
    assert [s["work"] for s in r["skipped"]] == [PMC]
    assert "NotFound" in r["skipped"][0]["why"] or "Error" in r["skipped"][0]["why"]
    assert sorted(r["indexed"]) == sorted([SUPP, BAD])  # later works were still indexed


def test_every_command_taking_a_work_id_accepts_pmcid_and_percent_encoded_doi(
    tmp_path: Path,
) -> None:
    lib = _library(tmp_path)
    for wid in (PMC, SUPP):
        assert lib.lookup(wid)["work"] == wid  # type: ignore[index]
        assert lib.text(wid)
        assert lib.index_works([wid])["indexed"] == [wid]
    assert lib.lookup("PMC5519140")["work"] == PMC  # type: ignore[index]
    lib.create_collection("c")
    c = lib.collect("c", [PMC, SUPP])
    assert c["rejected"] == []
    assert {m["work"] for m in c["members"]} == {PMC, SUPP}


def test_one_bad_id_does_not_fail_the_batch(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    lib.create_collection("c")
    c = lib.collect("c", ["not an id", PMC, "12345678"])
    assert [r["id"] for r in c["rejected"]] == ["not an id", "12345678"]
    assert [m["work"] for m in c["members"]] == [PMC]
    r = lib.index_works(["not an id", PMC])
    assert r["indexed"] == [PMC]
    assert r["not_held"] == ["not an id"]


def test_cli_index_prints_a_summary_of_skipped_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from paper_fetch import cli  # noqa: PLC0415

    lib = _library(tmp_path)
    lib.store.delete(f"papers/works/{PMC}/fulltext.txt")
    cli._index(lib, argparse.Namespace(rebuild=False, ids=[], collection=None, refresh=False))
    out = capsys.readouterr().out
    assert f"skipped: {PMC}" in out
    assert "1 record(s) skipped" in out


def test_a_pmcid_only_fetch_is_enriched_from_europe_pmc() -> None:
    epmc = {
        "resultList": {
            "result": [
                {
                    "title": "A paper OpenAlex lacks",
                    "pubYear": "2017",
                    "authorString": "Doe J, Roe R.",
                    "doi": "10.5555/EPMC.1",
                    "pmid": "28000000",
                    "pmcid": "PMC5519140",
                }
            ]
        }
    }
    lib, _ = make_library(
        {
            "api.openalex.org/works/": Response(404, {}, b"", ""),
            "europepmc/webservices/rest/search": J(epmc),
        }
    )
    r = lib.fetch("PMC5519140")
    assert (r["title"], r["year"], r["doi"]) == ("A paper OpenAlex lacks", 2017, "10.5555/epmc.1")
    assert r["work"] == "pmcid-PMC5519140"
    assert lib.lookup("10.5555/epmc.1")["work"] == "pmcid-PMC5519140"  # type: ignore[index]
    assert lib.index_works(["pmcid-PMC5519140"])["indexed"] == ["pmcid-PMC5519140"]


def test_enrichment_failure_is_not_fatal() -> None:
    lib, _ = make_library(
        {
            "api.openalex.org/works/": Response(404, {}, b"", ""),
            "europepmc/webservices/rest/search": Response(500, {}, b"", ""),
        }
    )
    r = lib.fetch("PMC5519140")
    assert r["work"] == "pmcid-PMC5519140"
    assert r["title"] is None
