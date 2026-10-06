"""One Library serves every tool call of an MCP server, each on its own worker thread.

These tests run the shared parts from many threads at once: the catalogue, the search memory, a
collection, and a provider's request spacing. Each fails without its lock.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from conftest import FakeHttp, J, make_library
from paper_fetch import LocalStore
from paper_fetch.collection import Collections
from paper_fetch.memory import SearchMemory
from paper_fetch.providers import Provider

THREADS = 16


def seed(lib: Any, n: int) -> list[str]:
    """Works that are in the store but not yet in the catalogue, as another process leaves them."""
    works = [f"W{1000 + i}" for i in range(n)]
    for w in works:
        base = f"papers/works/{w}/"
        record = {"id": w, "doi": f"https://doi.org/10.5555/{w.lower()}", "title": f"Title {w}"}
        lib.store.put(base + "work.json", json.dumps(record).encode())
        prov = {"work": w, "full_text": False, "ids": {"doi": f"10.5555/{w.lower()}"}}
        lib.store.put(base + "provenance.json", json.dumps(prov).encode())
    return works


def test_the_catalogue_is_read_and_written_from_many_threads(tmp_path: Path) -> None:
    lib, _ = make_library(store=LocalStore(tmp_path / "store"))
    works = seed(lib, 60)

    def writer(w: str) -> str:
        rec = lib.lookup(w)  # not in the catalogue: repaired from the store, recorded, saved
        assert rec is not None
        return rec["work"]

    def reader(i: int) -> int:
        lib.search_library("title")
        lib.lookup(f"10.5555/w{1000 + i}")
        return lib.status()["works"]

    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        written = pool.map(writer, works)
        read = pool.map(reader, range(120))
        assert sorted(written) == works
        assert all(n >= 0 for n in read)
    assert sorted(lib.index()) == works
    # Every row reached the stored catalogue: no save overwrote another's.
    fresh, _ = make_library(store=LocalStore(tmp_path / "store"))
    assert sorted(fresh.index()) == works


def test_a_save_never_empties_the_catalogue_on_the_way(tmp_path: Path) -> None:
    """A lookup from another thread during a save must not find a held paper missing, or it
    would be fetched again. So the save updates the dictionary in place without clearing it."""

    class NeverCleared(dict[str, Any]):
        def clear(self) -> None:
            raise AssertionError("the catalogue was emptied during a save")

    lib, _ = make_library(store=LocalStore(tmp_path / "store"))
    works = seed(lib, 5)
    for w in works:
        lib.lookup(w)
    lib._index = NeverCleared(lib.index())
    del lib.index()[works[0]]  # a removal, an addition and unchanged rows, all in one save
    lib.index()["W2000"] = {"work": "W2000", "full_text": False}
    lib._save_index()
    assert sorted(lib.index()) == sorted([*works[1:], "W2000"])


def test_search_memory_keeps_every_threads_row(tmp_path: Path) -> None:
    memory = SearchMemory(LocalStore(tmp_path / "store"))
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(lambda i: memory.record({"query": f"q{i}", "hits": []}), range(48)))
    assert sorted(r["query"] for r in memory.entries()) == sorted(f"q{i}" for i in range(48))


def test_a_collection_keeps_every_threads_member(tmp_path: Path) -> None:
    collections = Collections(LocalStore(tmp_path / "store"))
    collections.create("review")
    with ThreadPoolExecutor(max_workers=THREADS) as pool:
        list(pool.map(lambda i: collections.add("review", {f"doi:10.5555/{i}": {}}), range(48)))
    assert len(collections.require("review")["members"]) == 48


def test_a_providers_requests_stay_spaced_across_threads() -> None:
    sent: list[float] = []

    class Timed(FakeHttp):
        def get(self, url: str, *a: Any, **k: Any) -> Any:
            sent.append(time.monotonic())
            return super().get(url, *a, **k)

    class Slow(Provider):
        name = "slow"
        min_interval_s = 0.03

    provider = Slow(Timed({"x": J({})}))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: provider._get("https://x.example/x"), range(8)))
    gaps = [b - a for a, b in zip(sorted(sent), sorted(sent)[1:], strict=False)]
    assert min(gaps) >= 0.025, gaps
    assert provider.calls == 8
