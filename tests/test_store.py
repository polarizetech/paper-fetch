from __future__ import annotations

import io
import os
import stat
import urllib.error
from email.message import Message
from pathlib import Path
from typing import Any

import pytest

from paper_fetch.store import (
    LocalStore,
    MemoryStore,
    NotPrivate,
    S3Store,
    Store,
    _check,
    store_from_env,
)

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


@pytest.mark.parametrize("key", ["other/x", "papers/../other/x", "papers\\x", "x/papers/y"])
def test_keys_outside_the_prefix_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="outside"):
        _check(key)


@pytest.fixture(params=["memory", "local"])
def any_store(request: pytest.FixtureRequest, tmp_path: Path) -> Store:
    return MemoryStore() if request.param == "memory" else LocalStore(tmp_path / "lib")


def test_round_trip(any_store: Store) -> None:
    s = any_store
    assert isinstance(s, Store)
    assert not s.has("papers/works/W1/work.json")
    s.put("papers/works/W1/work.json", b"{}", "application/json")
    s.put("papers/works/W1/fulltext.txt", b"text")
    s.put("papers/works/W12/work.json", b"{}")
    s.put("papers/doi/10.5555%2Fx.json", b'{"work": "W1"}')
    assert s.has("papers/works/W1/work.json")
    assert s.get("papers/works/W1/fulltext.txt") == b"text"
    assert s.keys("papers/works/W1/") == [
        "papers/works/W1/fulltext.txt",
        "papers/works/W1/work.json",
    ]
    assert s.keys("papers/works/W1") == [
        "papers/works/W1/fulltext.txt",
        "papers/works/W1/work.json",
        "papers/works/W12/work.json",
    ]
    s.put("papers/works/W1/fulltext.txt", b"replaced")
    assert s.get("papers/works/W1/fulltext.txt") == b"replaced"
    s.delete("papers/works/W1/fulltext.txt")
    s.delete("papers/works/W1/fulltext.txt")  # idempotent
    assert not s.has("papers/works/W1/fulltext.txt")
    s.invalidate("papers/works/W1/work.json")
    s.assert_private("papers/works/W1/work.json")
    with pytest.raises(KeyError):
        s.get("papers/works/nope.json")
    assert s.keys("papers/absent/") == []


@posix_only
def test_local_store_writes_private_files_and_directories(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "lib")
    s.put("papers/works/W1/fulltext.pdf", b"%PDF-1.4")
    f = tmp_path / "lib" / "papers" / "works" / "W1" / "fulltext.pdf"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    for d in (f.parent, f.parent.parent, f.parent.parent.parent, tmp_path / "lib"):
        assert stat.S_IMODE(d.stat().st_mode) == 0o700, d
    assert not [p for p in f.parent.iterdir() if p.name.startswith(".paper-fetch-tmp-")]
    s.assert_private("papers/works/W1/fulltext.pdf")


@posix_only
def test_local_store_refuses_a_readable_copy(tmp_path: Path) -> None:
    s = LocalStore(tmp_path / "lib")
    s.put("papers/works/W1/fulltext.pdf", b"%PDF-1.4")
    (tmp_path / "lib" / "papers" / "works").chmod(0o755)
    with pytest.raises(NotPrivate, match="accessible to other users"):
        s.assert_private("papers/works/W1/fulltext.pdf")


def test_local_store_default_directory(tmp_path: Path) -> None:
    s = LocalStore()
    assert s.root == tmp_path / "data"


def test_store_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    assert isinstance(store_from_env(), LocalStore)
    monkeypatch.setenv("PAPER_FETCH_STORE", "memory")
    assert isinstance(store_from_env(), MemoryStore)
    monkeypatch.setenv("PAPER_FETCH_STORE", "s3")
    with pytest.raises(RuntimeError, match="PAPER_FETCH_S3_BUCKET"):
        store_from_env()
    monkeypatch.setenv("PAPER_FETCH_STORE", "ftp")
    with pytest.raises(ValueError, match="expected"):
        store_from_env()


# ---------------------------------------------------------------------------------- S3


class _Body(io.BytesIO):
    pass


class FakeS3:
    """The five boto3 client calls S3Store makes, over a dict. Records every put's kwargs."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_kwargs: list[dict[str, Any]] = []
        self.gets = 0

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if Key not in self.objects:
            raise KeyError(Key)
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        self.gets += 1
        return {"Body": _Body(self.objects[Key])}

    def put_object(self, **kw: Any) -> None:
        self.put_kwargs.append(kw)
        self.objects[kw["Key"]] = kw["Body"]

    def list_objects_v2(self, **kw: Any) -> dict[str, Any]:
        keys = sorted(k for k in self.objects if k.startswith(kw["Prefix"]))
        start = int(kw.get("ContinuationToken") or 0)
        page = keys[start : start + 2]  # tiny pages, to exercise continuation
        more = start + 2 < len(keys)
        out: dict[str, Any] = {"Contents": [{"Key": k} for k in page], "IsTruncated": more}
        if more:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def delete_object(self, *, Bucket: str, Key: str) -> None:
        self.objects.pop(Key, None)


def _s3(tmp_path: Path, **kw: Any) -> tuple[S3Store, FakeS3]:
    fake = FakeS3()
    return S3Store("bucket", cache=tmp_path / "cache", client=fake, **kw), fake


def test_s3_round_trip_with_read_through_cache(tmp_path: Path) -> None:
    s, fake = _s3(tmp_path)
    s.put("papers/works/W1/work.json", b"{}", "application/json")
    assert "ACL" not in fake.put_kwargs[0]
    assert fake.put_kwargs[0]["ContentType"] == "application/json"
    assert s.get("papers/works/W1/work.json") == b"{}"
    assert fake.gets == 0  # served from the local cache
    s.invalidate("papers/works/W1/work.json")
    assert s.get("papers/works/W1/work.json") == b"{}"
    assert fake.gets == 1
    fake.objects["papers/works/W2/work.json"] = b"[]"
    assert s.has("papers/works/W2/work.json")
    assert not s.has("papers/works/W3/work.json")
    fake.objects["papers/works/W1/fulltext.txt"] = b"t"
    fake.objects["papers/works/W1/fulltext.pdf"] = b"p"
    assert s.keys("papers/works/W1/") == [
        "papers/works/W1/fulltext.pdf",
        "papers/works/W1/fulltext.txt",
        "papers/works/W1/work.json",
    ]
    s.delete("papers/works/W1/work.json")
    assert "papers/works/W1/work.json" not in fake.objects
    assert not (tmp_path / "cache" / "papers" / "works" / "W1" / "work.json").exists()


@pytest.mark.parametrize(
    ("kw", "want"),
    [
        (
            {"endpoint_url": "https://nyc3.digitaloceanspaces.com"},
            "https://bucket.nyc3.digitaloceanspaces.com",
        ),
        ({"region": "eu-west-1"}, "https://bucket.s3.eu-west-1.amazonaws.com"),
        ({}, "https://bucket.s3.amazonaws.com"),
        ({"public_url": "https://cdn.example/"}, "https://cdn.example"),
    ],
)
def test_s3_anonymous_url(tmp_path: Path, kw: dict[str, str], want: str) -> None:
    s, _ = _s3(tmp_path, **kw)
    assert s.base_url == want


class _Anon(io.BytesIO):
    def __init__(self, status: int) -> None:
        super().__init__(b"x")
        self.status = status

    def __enter__(self) -> _Anon:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_s3_assert_private_asks_as_a_stranger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s, _ = _s3(tmp_path)
    seen: list[str] = []

    def readable(req: Any, timeout: float) -> _Anon:
        seen.append(req.full_url)
        assert req.get_header("Authorization") is None
        return _Anon(206)

    monkeypatch.setattr("urllib.request.urlopen", readable)
    with pytest.raises(NotPrivate, match="anonymously readable"):
        s.assert_private("papers/works/W1/fulltext.pdf")
    assert seen == ["https://bucket.s3.amazonaws.com/papers/works/W1/fulltext.pdf"]

    def forbidden(req: Any, timeout: float) -> None:
        raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", forbidden)
    s.assert_private("papers/works/W1/fulltext.pdf")

    def unreachable(req: Any, timeout: float) -> None:
        raise OSError("no route")

    monkeypatch.setattr("urllib.request.urlopen", unreachable)
    s.assert_private("papers/works/W1/fulltext.pdf")


def test_s3_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_init(self: S3Store, bucket: str, **kw: Any) -> None:
        captured.update(kw, bucket=bucket)

    monkeypatch.setattr(S3Store, "__init__", fake_init)
    monkeypatch.setenv("PAPER_FETCH_S3_BUCKET", "b")
    monkeypatch.setenv("PAPER_FETCH_S3_ENDPOINT_URL", "https://s3.example")
    monkeypatch.setenv("PAPER_FETCH_S3_ACCESS_KEY_ID", "id")
    S3Store.from_env()
    assert captured["bucket"] == "b"
    assert captured["endpoint_url"] == "https://s3.example"
    assert captured["access_key_id"] == "id"
    assert captured["secret_access_key"] is None


def test_s3_builds_a_boto3_client_without_network(tmp_path: Path) -> None:
    s = S3Store(
        "bucket",
        region="us-east-1",
        endpoint_url="https://s3.example.org",
        access_key_id="id",
        secret_access_key="secret",
        cache=tmp_path,
    )
    assert s._s3.meta.endpoint_url == "https://s3.example.org"
