"""Command line.

    paperlib fetch <doi|W123|pmid:N|PMCN|arxiv:ID> [...]  library first, then OpenAlex + OA routes
    paperlib search "<query>" [--providers a,b]         every search provider, merged
    paperlib providers                                   what is enabled, what each needs, its terms
    paperlib library ["<query>"] [--full-text]          search only what is already held
    paperlib text <id>                                   print stored full text
    paperlib provenance <id>                             where a copy came from, every route tried
    paperlib verify <id>                                 re-hash stored files against provenance
    paperlib add <file.pdf> <id> --rights "..."          a copy you legitimately hold
    paperlib citations <id> [--references] [-n N]        who cites it (or what it cites)
    paperlib status | rebuild-index | adopt-orphans

`python -m paperlib ...` is the same program.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from .citations import CitationsUnavailable
from .config import load_env
from .library import Library, NotFound
from .providers import describe

__all__ = ["build_parser", "main"]

LEGEND = "  ■ full text in library   □ in library, no full text"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="paperlib", description="A local library of legal open-access papers."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="get papers into the library")
    p.add_argument("ids", nargs="+")
    p.add_argument("--force", action="store_true", help="ask the providers even if held")

    p = sub.add_parser("search", help="federated search across providers")
    p.add_argument("query")
    p.add_argument("--providers", help="comma-separated provider names")
    p.add_argument("--include-closed", action="store_true")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--refresh", action="store_true", help="ignore cached answers")

    sub.add_parser("providers", help="list providers (no network)")

    p = sub.add_parser("library", help="search held papers")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--full-text", action="store_true")

    for name in ("text", "provenance", "verify"):
        sub.add_parser(name).add_argument("id")

    p = sub.add_parser("add", help="store a PDF you legitimately hold")
    p.add_argument("path")
    p.add_argument("id")
    p.add_argument("--rights", required=True)

    p = sub.add_parser("citations", help="citation graph via OpenCitations")
    p.add_argument("id")
    p.add_argument("--references", action="store_true")
    p.add_argument("-n", type=int, default=25)
    p.add_argument("--refresh", action="store_true")

    sub.add_parser("status")
    sub.add_parser("rebuild-index")
    sub.add_parser("adopt-orphans")
    return ap


def _flag(item: dict[str, Any]) -> str:
    return "■" if item["full_text_in_library"] else "□" if item["in_library"] else " "


def _fetch(lib: Library, a: argparse.Namespace) -> None:
    for i in a.ids:
        r = lib.fetch(i, force=a.force)
        mark = "✓" if r["full_text"] else "✗"
        title = (r.get("title") or "")[:70]
        print(f"{mark} {r['work']}  [{r['from']}]  {r.get('year')}  {title}")
        print(
            f"    oa={r.get('oa_status')}  route={r.get('route')}  format={r.get('format')}  "
            f"license={r.get('license')}"
        )
        if r["from"] != "library" and not r["full_text"]:
            for route, outcome in r.get("attempts", []):
                print(f"      {route:26s} {outcome}")
            if r.get("refused"):
                print(f"    {r['refused']}")


def _search(lib: Library, a: argparse.Namespace) -> None:
    names = [n.strip() for n in a.providers.split(",")] if a.providers else None
    res = lib.search(
        a.query, providers=names, oa_only=not a.include_closed, limit=a.n, refresh=a.refresh
    )
    for name, st in res["providers"].items():
        if st["status"] in ("ok", "cache"):
            extra = f"{st.get('n', 0)} hits, ${st.get('cost_usd', 0)}"
        else:
            extra = st.get("why")
        print(f"  {name:16s} {st['status']:12s} {extra}")
    print(f"\n{len(res['hits'])} distinct papers")
    for h in res["hits"]:
        ident = h["ids"].get("doi") or h["ids"].get("pmcid") or h["ids"].get("arxiv") or ""
        provs = ",".join(sorted(set(h["providers"])))
        print(f" {_flag(h)} {h['year'] or '':4} [{provs}] {(h['title'] or '')[:70]}  {ident}")
    print(LEGEND)


def _providers(lib: Library) -> None:
    for d in describe(lib.http):
        can = "+".join(x for x, y in (("search", d["search"]), ("locate", d["locate"])) if y)
        state = "ready" if d["available"] else d["why"]
        recommends: list[str] = d["recommends"]  # type: ignore[assignment]
        rec = f"  (set {', '.join(recommends)} for more)" if recommends else ""
        print(f"  {d['name']:16s} {can:14s} {state}{rec}\n  {'':16s} {d['terms']}")


def _library(lib: Library, a: argparse.Namespace) -> None:
    rows = (
        lib.search_library(a.query, full_text=a.full_text)
        if a.query
        else list(lib.index().values())
    )
    for r in rows:
        where = f"  [{r['matched_in']}]" if r.get("matched_in") else ""
        mark = "✓" if r["full_text"] else "✗"
        print(f" {mark} {r['work']:12s} {r.get('year')}  {(r.get('title') or '')[:80]}{where}")
    print(f"  {len(rows)} work(s)")


def _citations(lib: Library, a: argparse.Namespace) -> None:
    g = (lib.references if a.references else lib.citations)(a.id, refresh=a.refresh)
    cites = g["direction"] == "citations"
    of = g["of"].get("doi") or f"pmid:{g['of'].get('pmid')}"
    print(
        f"{of} {'cited by' if cites else 'cites'} {g['n']} works [{g['status']}], "
        f"{g['held']} already in the library   ({g['source']})"
    )
    rows = g["items"]
    if cites:
        rows = sorted(rows, key=lambda i: i.get("citing_date") or "", reverse=True)
    for it in rows[: a.n]:
        self_c = " self-cite" if it["author_self_citation"] else ""
        date = f"{(it.get('citing_date') or '')[:10]:10s} " if cites else ""
        ident = it["ids"].get("doi") or f"pmid:{it['ids'].get('pmid')}"
        print(f" {_flag(it)} {date}{ident}{self_c}")
    if g["n"] > a.n:
        print(f"  ... {g['n'] - a.n} more (-n)")
    print(LEGEND)


def _dispatch(lib: Library, a: argparse.Namespace) -> int:
    if a.cmd == "fetch":
        _fetch(lib, a)
    elif a.cmd == "search":
        _search(lib, a)
    elif a.cmd == "providers":
        _providers(lib)
    elif a.cmd == "library":
        _library(lib, a)
    elif a.cmd == "text":
        sys.stdout.write(lib.text(a.id))
    elif a.cmd == "provenance":
        print(json.dumps(lib.provenance(a.id), indent=1))
    elif a.cmd == "verify":
        v = lib.verify(a.id)
        print(json.dumps(v, indent=1))
        return 0 if v["ok"] else 3
    elif a.cmd == "add":
        print(json.dumps(lib.add_local(a.path, a.id, rights=a.rights), indent=1))
    elif a.cmd == "citations":
        _citations(lib, a)
    elif a.cmd == "status":
        print(json.dumps(lib.status(), indent=1))
    elif a.cmd == "rebuild-index":
        print(f"catalogue rebuilt: {lib.rebuild_index()} works")
    elif a.cmd == "adopt-orphans":
        print(json.dumps(lib.adopt_orphans(), indent=1))
    return 0


def main(argv: Sequence[str] | None = None, *, library: Library | None = None) -> int:
    """Entry point for `paperlib`. `library` lets tests supply an offline Library."""
    a = build_parser().parse_args(argv)
    load_env()
    try:
        lib = library if library is not None else Library.default()
        return _dispatch(lib, a)
    except NotFound as e:
        print(f"not found: {e}", file=sys.stderr)
        return 1
    except CitationsUnavailable as e:
        print(f"unavailable: {e}", file=sys.stderr)
        return 2
    except (ValueError, RuntimeError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
