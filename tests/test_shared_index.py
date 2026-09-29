"""Several processes share one bucket and its catalogue. None may lose another's records.

The catalogue (papers/index.jsonl) is one object that every addition rewrites. A process that
wrote back its own in-memory copy, or trusted a copy in its disk cache, silently dropped whatever
other processes had added since it last read the bucket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from conftest import make_library
from paper_fetch.library import INDEX, Library
from paper_fetch.store import S3Store
from test_store import FakeS3


def row(work: str, **extra: Any) -> dict[str, Any]:
    return {"work": work, "title": f"Title of {work}", "full_text": False} | extra


def process(fake: FakeS3, cache: Path) -> Library:
    """A library as a separate process would build it: its own memory, maybe a shared cache."""
    lib, _ = make_library(store=S3Store("bucket", cache=cache, client=fake))
    return lib


def add(lib: Library, record: dict[str, Any]) -> None:
    lib.index()[record["work"]] = record
    lib._save_index()


def in_bucket(fake: FakeS3, tmp_path: Path) -> dict[str, Any]:
    return process(fake, tmp_path / "reader").index()


@pytest.mark.parametrize("shared_cache", [True, False])
def test_concurrent_writers_keep_each_others_records(tmp_path: Path, shared_cache: bool) -> None:
    fake = FakeS3()
    a = process(fake, tmp_path / ("cache" if shared_cache else "a"))
    b = process(fake, tmp_path / ("cache" if shared_cache else "b"))
    a.index()
    b.index()  # both have read the (empty) catalogue
    add(b, row("W2"))
    add(a, row("W1"))  # a's in-memory copy predates W2
    assert set(in_bucket(fake, tmp_path)) == {"W1", "W2"}
    assert set(a.index()) == {"W1", "W2"}  # and a now sees what b added


def test_a_new_process_does_not_trust_a_stale_disk_cache(tmp_path: Path) -> None:
    fake = FakeS3()
    cache = tmp_path / "cache"
    add(process(fake, cache), row("W1"))  # the cache now holds a catalogue with W1
    # Another machine (with its own cache) adds W2 to the bucket.
    add(process(fake, tmp_path / "elsewhere"), row("W2"))
    assert set(process(fake, cache).index()) == {"W1", "W2"}


def test_an_update_is_kept_and_a_concurrent_addition_survives_it(tmp_path: Path) -> None:
    fake = FakeS3()
    a = process(fake, tmp_path / "a")
    b = process(fake, tmp_path / "b")
    add(a, row("W1"))
    b.index()
    add(a, row("W3"))
    add(b, row("W1", full_text=True))  # b changes W1 while unaware of W3
    catalogue = in_bucket(fake, tmp_path)
    assert set(catalogue) == {"W1", "W3"}
    assert catalogue["W1"]["full_text"] is True


def test_a_removal_is_kept(tmp_path: Path) -> None:
    fake = FakeS3()
    a = process(fake, tmp_path / "a")
    add(a, row("W1"))
    add(a, row("W2"))
    b = process(fake, tmp_path / "b")
    del b.index()["W2"]
    b._save_index()
    add(a, row("W3"))  # a still has W2 in memory; it must not resurrect it
    assert set(in_bucket(fake, tmp_path)) == {"W1", "W3"}


def test_the_catalogue_is_written_once_per_save(tmp_path: Path) -> None:
    fake = FakeS3()
    a = process(fake, tmp_path / "a")
    add(a, row("W1"))
    writes = [kw for kw in fake.put_kwargs if kw["Key"] == INDEX]
    assert len(writes) == 1
    assert [json.loads(line)["work"] for line in writes[0]["Body"].decode().splitlines()] == ["W1"]


def test_rebuild_replaces_the_catalogue(tmp_path: Path) -> None:
    fake = FakeS3()
    a = process(fake, tmp_path / "a")
    add(a, row("W-stale"))  # a catalogue row with no provenance objects behind it
    assert process(fake, tmp_path / "b").rebuild_index() == 0
    assert in_bucket(fake, tmp_path) == {}
