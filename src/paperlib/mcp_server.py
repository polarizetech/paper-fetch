"""paperlib over MCP (stdio): the paper library as tools an LLM can call.

    paperlib-mcp                      # needs the `mcp` extra: pip install 'paper-library[mcp]'

Every tool is a thin wrapper over `paperlib.Library`; nothing here decides what counts as open,
what is stored, or what is held. Those rules stay in the library.

## The contract

Every tool returns one JSON envelope:

    {"ok": true,  "data": ...}
    {"ok": false, "code": "not_found" | "unavailable" | "tool_error", "error": "..."}

`not_found`: the identifier is not held (or has no readable text, or no DOI/PMID for citations).
`unavailable`: a remote service refused or did not answer; this is never "zero results".
`tool_error`: bad arguments or configuration (unparseable identifier, unknown provider, bad key).

`add_local` is deliberately not a tool. It stores a copy on a declared rights statement, and that
statement is the operator's to make, not a model's; use `paperlib add` for it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

try:  # mcp >= 2 renamed FastMCP to MCPServer; the decorator and stdio `run()` are unchanged
    from mcp.server.mcpserver import MCPServer as _Server
except ImportError:  # pragma: no cover -- mcp 1.x
    from mcp.server.fastmcp import FastMCP as _Server  # pyright: ignore[reportAttributeAccessIssue]

from .citations import DIRECTIONS
from .config import load_env
from .http import Http
from .library import Library, NotFound, Record
from .providers import ProviderUnavailable, describe

__all__ = [
    "citations",
    "fetch",
    "library",
    "main",
    "mcp",
    "provenance",
    "providers",
    "search",
    "status",
    "text",
]

#: Catalogue fields a `fetch` or `library` row carries (when present on the record).
ROW_KEYS = (
    "work",
    "doi",
    "pmid",
    "pmcid",
    "title",
    "year",
    "authors",
    "full_text",
    "oa_status",
    "license",
    "route",
    "format",
    "retrieved",
    "retry_after",
    "is_retracted",
    "from",
    "matched_in",
)
TEXT_PAGE_MIN, TEXT_PAGE_MAX = 1000, 100_000

mcp = _Server(
    "paper-library",
    instructions=(
        "A local library of legal open-access scientific papers. Call `library` first to see what "
        "is already held; `fetch` a DOI, OpenAlex id (W123), pmid:N or PMCID to add one (a held "
        "paper costs nothing); `search` finds papers across every provider; `text` reads a held "
        "full text in pages; `citations` walks the citation graph; `provenance` says where a copy "
        "came from and under what licence. Open access is a right to read, not to republish."
    ),
)

_library: Library | None = None


def _lib() -> Library:
    global _library  # noqa: PLW0603 -- one Library per server process, built on first use
    if _library is None:
        _library = Library.default()
    return _library


def _run(operation: Callable[[], Any]) -> dict[str, Any]:
    try:
        return {"ok": True, "data": operation()}
    except NotFound as exc:
        return {"ok": False, "code": "not_found", "error": str(exc)}
    except (ProviderUnavailable, ConnectionError) as exc:
        return {"ok": False, "code": "unavailable", "error": str(exc)}
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "code": "tool_error", "error": str(exc)}


def _row(record: Record) -> dict[str, Any]:
    return {key: record[key] for key in ROW_KEYS if key in record}


@mcp.tool()
def search(query: str, include_closed: bool = False, limit: int = 10) -> dict[str, Any]:
    """Search all configured literature providers and mark papers already held."""
    return _run(lambda: _lib().search(query, oa_only=not include_closed, limit=limit))


@mcp.tool()
def fetch(identifier: str) -> dict[str, Any]:
    """Fetch and privately store a legal open-access copy of a paper (held papers cost nothing)."""
    return _run(lambda: _row(_lib().fetch(identifier)))


@mcp.tool()
def library(query: str = "", full_text: bool = False, limit: int = 50) -> dict[str, Any]:
    """Search papers already held (titles and authors; optionally inside stored full text)."""

    def operation() -> dict[str, Any]:
        lib = _lib()
        rows = (
            lib.search_library(query, full_text=full_text, limit=limit)
            if query
            else list(lib.index().values())[:limit]
        )
        return {"works": [_row(row) for row in rows], "n": len(rows)}

    return _run(operation)


@mcp.tool()
def text(identifier: str, offset: int = 0, max_chars: int = TEXT_PAGE_MAX) -> dict[str, Any]:
    """Read a page of a held paper's machine-extracted full text."""

    def operation() -> dict[str, Any]:
        body = _lib().text(identifier)
        size = max(TEXT_PAGE_MIN, min(max_chars, TEXT_PAGE_MAX))
        start = max(0, offset)
        page = body[start : start + size]
        return {"text": page, "offset": start, "end": start + len(page), "total_chars": len(body)}

    return _run(operation)


@mcp.tool()
def provenance(identifier: str) -> dict[str, Any]:
    """Return acquisition route, licence, checksums, and every route tried for a held paper."""
    return _run(lambda: _lib().provenance(identifier))


@mcp.tool()
def citations(identifier: str, direction: str = "citations", limit: int = 25) -> dict[str, Any]:
    """Works citing a paper (direction='citations') or cited by it ('references'), via
    OpenCitations. Identifiers only, and no citation intent: a citation is not agreement."""

    def operation() -> dict[str, Any]:
        if direction not in DIRECTIONS:
            raise ValueError("direction must be 'citations' or 'references'")
        lib = _lib()
        g = (lib.citations if direction == "citations" else lib.references)(identifier)
        items = g["items"]
        if direction == "citations":
            items = sorted(items, key=lambda i: i.get("citing_date") or "", reverse=True)
        return {**{k: v for k, v in g.items() if k != "items"}, "items": items[: max(0, limit)]}

    return _run(operation)


@mcp.tool()
def providers() -> dict[str, Any]:
    """List every provider, what it can do, what it needs, and its terms. No network."""
    return _run(lambda: {"providers": describe(Http())})


@mcp.tool()
def status() -> dict[str, Any]:
    """Counts of works held, with and without full text, and OpenAlex allowance remaining."""
    return _run(lambda: _lib().status())


def main() -> int:
    """Entry point for `paperlib-mcp`: serve the tools over stdio."""
    load_env()
    mcp.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
