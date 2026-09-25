"""The MCP tool contract: names, arguments, and the {"ok", "data" | "code", "error"} envelope.

Tools are plain functions, called directly here; one test also drives the real `paperlib-mcp`
entry point over stdio, as an MCP client would.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from conftest import FakeProvider, J, fixture, make_library
from paperlib import Library, NotFound
from paperlib import mcp_server as srv
from paperlib.providers import Hit, ProviderUnavailable

DOI = "10.5555/example.001"
ENVELOPE_CODES = {"not_found", "unavailable", "tool_error"}


@pytest.fixture
def lib() -> Iterator[Library]:
    library, _ = make_library(
        {
            "index/v2/citations/": J(fixture("opencitations_citations.json")),
            "index/v2/references/": J([]),
        }
    )
    srv._library = library
    yield library
    srv._library = None


def _err(env: dict[str, Any], code: str) -> None:
    assert env["ok"] is False
    assert env["code"] == code
    assert set(env) == {"ok", "code", "error"}
    assert isinstance(env["error"], str)


def test_fetch_returns_a_catalogue_row(lib: Library) -> None:
    env = srv.fetch(DOI)
    assert env["ok"] is True
    data = env["data"]
    assert set(data) <= set(srv.ROW_KEYS)
    assert data["work"] == "W7"
    assert data["doi"] == DOI
    assert data["full_text"] is True
    assert data["from"] == "retrieved"
    assert data["route"] == "good:pdf"
    assert data["license"] == "cc-by"
    assert data["is_retracted"] is False
    assert srv.fetch(DOI)["data"]["from"] == "library"
    _err(srv.fetch("12345"), "tool_error")


def test_library_lists_and_searches_held_works(lib: Library) -> None:
    assert srv.library() == {"ok": True, "data": {"works": [], "n": 0}}
    srv.fetch(DOI)
    env = srv.library()
    assert env["data"]["n"] == 1
    assert env["data"]["works"][0]["work"] == "W7"
    found = srv.library(query="example", limit=5)["data"]["works"][0]
    assert found["matched_in"] == "title/authors"
    assert srv.library(query="nothing-like-this")["data"] == {"works": [], "n": 0}


def test_text_pages(lib: Library) -> None:
    _err(srv.text(DOI), "not_found")
    srv.fetch(DOI)
    whole = lib.text(DOI)
    first = srv.text(DOI, offset=0, max_chars=10)["data"]  # clamped up to 1000
    assert set(first) == {"text", "offset", "end", "total_chars"}
    assert (first["offset"], first["end"], first["total_chars"]) == (0, 1000, len(whole))
    parts, offset = [], 0
    while True:
        page = srv.text(DOI, offset=offset, max_chars=1500)["data"]
        parts.append(page["text"])
        offset = page["end"]
        if offset >= page["total_chars"] or not page["text"]:
            break
    assert "".join(parts) == whole


def test_provenance(lib: Library) -> None:
    _err(srv.provenance(DOI), "not_found")
    srv.fetch(DOI)
    data = srv.provenance(DOI)["data"]
    assert data["work"] == "W7"
    assert set(data["sha256"]) == {"fulltext.pdf", "fulltext.txt"}


def test_search_envelope(lib: Library) -> None:
    lib.search_providers = [
        FakeProvider("a", hits=[Hit("a", "Example", 2020, {"doi": DOI}, True, ["A"])]),
        FakeProvider("down", raise_=ProviderUnavailable("down rate-limited us (HTTP 429)")),
    ]
    data = srv.search("example", include_closed=True, limit=5)["data"]
    assert set(data) == {"query", "providers", "hits"}
    hit = data["hits"][0]
    for key in (
        "ids",
        "title",
        "year",
        "authors",
        "is_oa",
        "providers",
        "work",
        "full_text_in_library",
    ):
        assert key in hit
    assert data["providers"]["down"]["status"] == "unavailable"
    assert data["providers"]["a"]["status"] == "ok"


def test_citations(lib: Library) -> None:
    data = srv.citations("10.5555/cited.001", limit=1)["data"]
    assert (data["direction"], data["n"], len(data["items"])) == ("citations", 2, 1)
    assert data["items"][0]["citing_date"] == "2025-06"  # newest first
    assert srv.citations("10.5555/cited.001", direction="references")["data"]["n"] == 0
    _err(srv.citations("10.5555/cited.001", direction="sideways"), "tool_error")


def test_error_codes(lib: Library, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(exc: Exception) -> Any:
        def f(*_a: Any, **_k: Any) -> Any:
            raise exc

        return f

    monkeypatch.setattr(lib, "citations", boom(ProviderUnavailable("refused")))
    _err(srv.citations(DOI), "unavailable")
    monkeypatch.setattr(lib, "provenance", boom(ConnectionError("down")))
    _err(srv.provenance(DOI), "unavailable")
    monkeypatch.setattr(lib, "fetch", boom(NotFound("x")))
    _err(srv.fetch(DOI), "not_found")
    monkeypatch.setattr(lib, "fetch", boom(RuntimeError("bad key")))
    _err(srv.fetch(DOI), "tool_error")


def test_providers_and_status(lib: Library) -> None:
    names = {p["name"] for p in srv.providers()["data"]["providers"]}
    assert {"openalex", "europepmc", "pmc-s3", "web"} <= names
    assert srv.status()["data"]["works"] == 0


def test_lazy_default_library(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERLIB_STORE", "memory")
    srv._library = None
    try:
        assert srv.library()["data"]["n"] == 0
        assert srv._library is not None
    finally:
        srv._library = None


# ---------------------------------------------------------------------------------- over stdio


def test_stdio_server_speaks_the_contract(tmp_path: Path) -> None:
    from mcp import ClientSession, StdioServerParameters  # noqa: PLC0415
    from mcp.client.stdio import stdio_client  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("PAPERLIB_", "OPENALEX", "OPENCITATIONS", "SEARXNG"))
    }
    env.update(PAPERLIB_STORE="memory", PAPERLIB_ENV_DIR=str(tmp_path / "none"))
    params = StdioServerParameters(
        command=sys.executable, args=["-c", "from paperlib.mcp_server import main; main()"], env=env
    )

    async def session() -> dict[str, Any]:
        async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}
            out: dict[str, Any] = {"tools": tools}
            for name, args in (
                ("library", {"limit": 5}),
                ("fetch", {"identifier": "12345"}),
                ("text", {"identifier": "W1", "offset": 0, "max_chars": 1000}),
                ("citations", {"identifier": DOI, "direction": "sideways", "limit": 1}),
            ):
                result = await s.call_tool(name, args)
                text = next(c.text for c in result.content if isinstance(c, TextContent))
                out[name] = json.loads(text)
            return out

    got = anyio.run(session)
    assert {"search", "fetch", "library", "text", "provenance", "citations"} <= got["tools"]
    assert not any("add" in t for t in got["tools"])
    assert got["library"] == {"ok": True, "data": {"works": [], "n": 0}}
    assert got["fetch"]["code"] == "tool_error"
    assert got["text"]["code"] == "not_found"
    assert got["citations"]["code"] == "tool_error"
    assert {got[k]["code"] for k in ("fetch", "text", "citations")} <= ENVELOPE_CODES
