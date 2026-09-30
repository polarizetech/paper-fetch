"""Named collections and search memory: shared, deterministic, and safe with several writers."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeProvider, make_library
from paper_fetch import Library
from paper_fetch.collection import Collections
from paper_fetch.memory import MEMORY, SearchMemory, concepts
from paper_fetch.profiles import Anchor, Profile
from paper_fetch.providers import Hit
from paper_fetch.store import S3Store
from test_store import FakeS3

DOI = "10.5555/example.001"  # the work W7 that make_library's OpenAlex fixture knows
HRV = Anchor("free-text", "heart rate variability", synonyms=("HRV",))
CARDIO = Profile("cardio", "Cardiology", "Hearts.", anchors=(HRV,))


def _lib(hits: list[Hit] | None = None) -> Library:
    lib, _ = make_library()
    lib._profiles = {"cardio": CARDIO}
    lib.search_providers = [FakeProvider("a", hits=hits or [])]
    return lib


def _hit(title: str, doi: str, year: int = 2020) -> Hit:
    return Hit("a", title, year, {"doi": doi}, True)


# ---------------------------------------------------------------------------------- concepts


def test_concepts_drop_stopwords_and_plurals_and_map_synonyms() -> None:
    lib = _lib()
    vocab = lib.memory.vocabulary
    assert concepts("Does HRV predict outcomes in athletes?", vocab) == {
        "heart",
        "rate",
        "variability",
        "predict",
        "outcome",
        "athlete",
    }
    assert concepts("glass analysis") == {"glass", "analysis"}  # -ss and short words kept


# ---------------------------------------------------------------------------------- memory


def test_every_search_is_remembered_and_recalled_by_concept() -> None:
    lib = _lib([_hit("HRV in rowers", "10.5555/a")])
    first = lib.search("HRV in athletes", web_fallback=False)
    assert first["memory"] == []  # nothing before it
    again = lib.search("heart rate variability of athletes", web_fallback=False)
    assert [m["id"] for m in again["memory"]] == [first["search_id"]]
    assert again["memory"][0]["score"] == 1.0  # same concepts, different words
    assert again["hits"][0]["seen_before"]["query"] == "HRV in athletes"
    assert lib.search("x", remember=False, web_fallback=False).get("search_id") is None

    recalled = lib.recall("HRV athletes")
    assert [r["query"] for r in recalled] == [
        "heart rate variability of athletes",
        "HRV in athletes",
    ]
    assert recalled[0]["hits"][0]["in_library"] is False
    assert lib.recall("unrelated gardening") == []
    assert len(lib.recall()) == 2  # no query: most recent first


def test_recall_by_paper_marks_what_is_held_now() -> None:
    lib = _lib([_hit("Example", DOI)])
    lib.search("example", web_fallback=False)
    lib.fetch(DOI)
    rows = lib.recall(work="W7")  # the search found it by DOI, before it was held
    assert len(rows) == 1
    hit = rows[0]["hits"][0]
    assert (hit["in_library"], hit["full_text_in_library"], hit["work"]) == (True, True, "W7")
    assert lib.recall(work="10.5555/not-found-by-anything") == []


def test_recall_filters_by_profile_and_collection() -> None:
    lib = _lib([_hit("HRV in rowers", "10.5555/a")])
    lib.create_collection("review", profile="cardio")
    lib.search("HRV", collection="review", web_fallback=False)  # the collection's profile applies
    lib.search("HRV", web_fallback=False)
    assert [r["profile"] for r in lib.recall(profile="cardio")] == ["cardio"]
    assert [r["collection"] for r in lib.recall(collection="review")] == ["review"]


def test_concurrent_writers_keep_each_others_searches(tmp_path: Path) -> None:
    fake = FakeS3()
    a = SearchMemory(S3Store("bucket", cache=tmp_path / "a", client=fake))
    b = SearchMemory(S3Store("bucket", cache=tmp_path / "b", client=fake))
    a.entries()
    b.entries()
    b.record({"query": "one", "concepts": ["one"], "hits": []})
    a.record({"query": "two", "concepts": ["two"], "hits": []})
    fresh = SearchMemory(S3Store("bucket", cache=tmp_path / "c", client=fake))
    assert {r["query"] for r in fresh.entries()} == {"one", "two"}
    assert all(k["Key"] == MEMORY for k in fake.put_kwargs)


# ---------------------------------------------------------------------------------- collections


def test_collection_lifecycle() -> None:
    lib = _lib([_hit("Example", DOI), _hit("Other", "10.5555/other")])
    with pytest.raises(ValueError, match="create it first"):
        lib.search("x", collection="review")
    with pytest.raises(ValueError, match="unknown profile"):
        lib.create_collection("review", profile="nope")
    with pytest.raises(ValueError, match="collection name"):
        lib.create_collection("Bad Name")
    made = lib.create_collection("review", profile="cardio", description="A review")
    assert (made["profile"], made["n"]) == ("cardio", 0)

    lib.collect("review", ["10.5555/other"])  # not held: listed by its identifier
    lib.fetch(DOI, collection="review")  # held: listed by work id
    c = lib.collection("review")
    assert [(m["key"], m["in_library"], m["full_text"]) for m in c["members"]] == [
        ("doi:10.5555/other", False, False),
        ("W7", True, True),
    ]
    res = lib.search("example", collection="review", web_fallback=False)
    assert res["profile"] == "cardio"
    assert {h["ids"]["doi"]: h["in_collection"] for h in res["hits"]} == {
        DOI: True,
        "10.5555/other": True,
    }
    assert [s["query"] for s in lib.collection("review")["searches"]] == ["example"]

    lib.uncollect("review", [DOI])
    assert [m["key"] for m in lib.collection("review")["members"]] == ["doi:10.5555/other"]
    with pytest.raises(ValueError, match="not in review"):
        lib.uncollect("review", ["10.5555/never"])
    assert lib.list_collections()[0] | {"updated": None} == {
        "name": "review",
        "profile": "cardio",
        "description": "A review",
        "members": 1,
        "updated": None,
    }
    lib.collections.delete("review")
    assert lib.list_collections() == []


def test_collecting_by_doi_then_fetching_lists_the_paper_once() -> None:
    lib = _lib()
    lib.create_collection("review")
    lib.collect("review", [DOI])
    lib.fetch(DOI, collection="review")
    assert [m["key"] for m in lib.collection("review")["members"]] == ["W7"]
    lib.collect("review", [DOI])  # held now: stays one member, keeps when it was added
    assert lib.collection("review")["n"] == 1


def test_two_writers_to_one_collection_keep_both(tmp_path: Path) -> None:
    fake = FakeS3()
    a = Collections(S3Store("bucket", cache=tmp_path / "a", client=fake))
    b = Collections(S3Store("bucket", cache=tmp_path / "b", client=fake))
    a.create("review")
    a.get("review")
    b.add("review", {"doi:10.5555/b": {}})
    a.add("review", {"doi:10.5555/a": {}})
    assert set(
        Collections(S3Store("bucket", cache=tmp_path / "c", client=fake)).require("review")[
            "members"
        ]
    ) == {"doi:10.5555/a", "doi:10.5555/b"}


def test_status_counts_collections_and_searches() -> None:
    lib = _lib()
    lib.create_collection("review")
    lib.search("x", web_fallback=False)
    st = lib.status()
    assert (st["collections"], st["searches_remembered"], st["profiles"]) == (1, 1, ["cardio"])
