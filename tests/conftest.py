"""Shared offline test doubles. Nothing in the suite opens a socket.

`FakeHttp` replaces `paper_fetch.http.Http`: it serves canned `Response`s by URL substring and
counts calls, which is how "a held paper is never downloaded again" is tested.
"""

from __future__ import annotations

import json
import zlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

from paper_fetch import Library, MemoryStore, OpenAlex
from paper_fetch.citations import OpenCitations
from paper_fetch.http import Response
from paper_fetch.providers import REGISTRY, Hit, Location, Provider

FIXTURES = Path(__file__).parent / "fixtures"

PROSE = (
    "Someone unfamiliar with your project should be able to look at your computer files and "
    "understand in detail what you did and why. Everything you do, you will probably have to "
    "do over again. "
) * 20
FONT_TABLE = (
    "354 781 604 927 750 822 562 822 729 541 697 770 729 947 770 677 0 343 0 343 0 0 0 "
    "468 520 427 520 437 270 468 531 250 250 458 239 802 531 500 520 520 364 333 291 "
) * 60


def make_pdf(text: str) -> bytes:
    """A minimal, valid one-page PDF whose compressed content stream draws `text`."""
    lines = [text[i : i + 90] for i in range(0, len(text), 90)]
    escaped = (ln.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for ln in lines)
    ops = "BT /F1 9 Tf 40 800 Td 11 TL " + " ".join(f"({ln}) '" for ln in escaped) + " ET"
    stream = zlib.compress(ops.encode("latin-1"))
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 5000] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offs)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


PDF = make_pdf(PROSE)
JATS = (
    "<article><body>"
    + "".join(f"<sec><p>{PROSE[:400]}</p></sec>" for _ in range(8))
    + "</body></article>"
).encode()
TEI = (
    "<TEI xmlns='http://www.tei-c.org/ns/1.0'><text><body>"
    + "".join(f"<div><p>{PROSE[:400]}</p></div>" for _ in range(8))
    + "</body></text></TEI>"
).encode()


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


def J(obj: Any, status: int = 200, headers: dict[str, str] | None = None) -> Response:
    return Response(status, headers or {}, json.dumps(obj).encode(), "")


Route = Response | Callable[[str], Response]


class FakeHttp:
    """Serves the first route whose key is a substring of the URL; 404 otherwise."""

    def __init__(self, routes: Mapping[str, Route] | None = None) -> None:
        self.routes: Mapping[str, Route] = routes or {}
        self.calls = 0
        self.log: list[str] = []
        self.headers: list[dict[str, str]] = []
        self.posts: list[tuple[str, bytes]] = []

    def get(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> Response:
        self.calls += 1
        self.log.append(url)
        self.headers.append(dict(headers or {}))
        for pat, resp in self.routes.items():
            if pat in url:
                return resp(url) if callable(resp) else resp
        return Response(404, {}, b"", url)

    def post(
        self,
        url: str,
        body: bytes,
        headers: dict[str, str] | None = None,
        *,
        timeout: float | None = None,
    ) -> Response:
        """Served from the same routes as `get`; the body is kept in `posts`."""
        self.posts.append((url, body))
        return self.get(url, headers)


class DeadHttp:
    calls = 0

    def get(self, *_a: Any, **_k: Any) -> Response:
        raise ConnectionError("timed out")

    def post(self, *_a: Any, **_k: Any) -> Response:
        raise ConnectionError("timed out")


class FakeProvider(Provider):
    """A provider whose answers are given, not fetched."""

    can_locate = True
    can_search = True

    def __init__(
        self,
        name: str,
        locs: list[Location] | None = None,
        body: bytes | None = None,
        *,
        raise_: Exception | None = None,
        needs: tuple[str, ...] = (),
        hits: list[Hit] | None = None,
    ) -> None:
        super().__init__(FakeHttp())
        self.name = name
        self.needs = needs
        self._locs = locs or []
        self._body = body
        self._raise = raise_
        self._hits = hits or []
        self.downloads = 0

    def locate(self, ids: dict[str, Any]) -> list[Location]:
        if self._raise:
            raise self._raise
        return self._locs

    def download(self, loc: Location) -> bytes | None:
        self.downloads += 1
        return self._body

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        if self._raise:
            raise self._raise
        return self._hits


def good_provider(name: str = "good") -> FakeProvider:
    return FakeProvider(name, [Location("https://e.example/p.pdf", "pdf", name, "cc-by")], PDF)


def offline_openalex(http: FakeHttp | DeadHttp) -> OpenAlex:
    return OpenAlex(http=http, api_key="", email="")


def make_library(
    routes: Mapping[str, Route] | None = None,
    *,
    store: Any = None,
    providers: list[Provider] | None = None,
) -> tuple[Library, FakeHttp]:
    """A Library over FakeHttp that knows the example work W7 (10.5555/example.001)."""
    # Specific routes first: the first matching substring wins.
    table: dict[str, Route] = dict(routes or {})
    table.setdefault("api.openalex.org/works/", J(fixture("openalex_work.json")))
    table.setdefault("idconv", J({"records": []}))
    http = FakeHttp(table)
    lib = Library(
        store if store is not None else MemoryStore(),
        offline_openalex(http),
        providers=providers if providers is not None else [good_provider()],
        search_providers=[],
    )
    return lib, http


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No real configuration, no polite sleeps, and every default directory under tmp_path."""
    import os  # noqa: PLC0415

    for key in list(os.environ):
        if key.startswith("PAPER_FETCH_") or key in (
            "OPENALEX_API_KEY",
            "OPENCITATIONS_ACCESS_TOKEN",
            "NCBI_API_KEY",
            "CORE_API_KEY",
            "SEARXNG_URL",
            "XDG_DATA_HOME",
            "XDG_CACHE_HOME",
            "XDG_CONFIG_HOME",
        ):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("PAPER_FETCH_ENV_DIR", str(tmp_path / "no-env"))
    monkeypatch.setenv("PAPER_FETCH_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PAPER_FETCH_CACHE", str(tmp_path / "cache"))
    for cls in REGISTRY.values():
        monkeypatch.setattr(cls, "min_interval_s", 0.0)
    monkeypatch.setattr(OpenCitations, "min_interval_s", 0.0)
