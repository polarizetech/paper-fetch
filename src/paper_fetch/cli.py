"""Command line.

    paper-fetch fetch <doi|W123|pmid:N|PMCN|arxiv:ID> [...] library first, then OpenAlex + OA routes
    paper-fetch search "<query>" [--providers a,b]       every search provider, merged
                [--scite]                                also ask scite (rationed, so opt-in)
                [--profile P] [--collection C]           in a discipline / for a project
    paper-fetch profiles [slug]                          discipline profiles, or one in full
    paper-fetch collection [name] [--create] [--profile P] [--description D]
    paper-fetch collect <name> <id> [...] [--remove]     list papers in a collection
    paper-fetch recall ["<query>"] [--profile P] [--collection C] [--work ID]
    paper-fetch index [id ...] [--collection C] [--refresh | --rebuild]   the passage index
    paper-fetch retrieve "<query>" [--collection C] [--work ID ...] [-n N] passages, best first
    paper-fetch providers                                what is enabled, what each needs, its terms
    paper-fetch library ["<query>"] [--full-text]        search only what is already held
    paper-fetch text <id>                                print stored full text
    paper-fetch provenance <id>                          where a copy came from, every route tried
    paper-fetch verify <id>                              re-hash stored files against provenance
    paper-fetch add <file.pdf> <id> --rights "..."       a copy you legitimately hold
    paper-fetch citations <id> [--references] [-n N]     who cites it (or what it cites)
    paper-fetch scite-login | scite-logout               sign this machine in to scite (browser)
    paper-fetch leads <url> [...]                        identifiers in URLs you found on the web
    paper-fetch problems [-n N] [--trace]                provider defects logged on this machine
    paper-fetch status | rebuild-index | adopt-orphans

`python -m paper_fetch ...` is the same program.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from . import problems, scite_auth
from .citations import CitationsUnavailable
from .config import load_env
from .library import Library, NotFound
from .providers import describe

__all__ = ["build_parser", "main"]

LEGEND = "  ■ full text in library   □ in library, no full text"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="paper-fetch", description="A local library of legal open-access papers."
    )
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("fetch", help="get papers into the library")
    p.add_argument("ids", nargs="+")
    p.add_argument("--force", action="store_true", help="ask the providers even if held")
    p.add_argument("--collection", help="also list the papers in this collection")

    p = sub.add_parser("search", help="federated search across providers")
    p.add_argument("query")
    p.add_argument("--providers", help="comma-separated provider names")
    p.add_argument("--scite", action="store_true", help="also ask scite (250 calls a month)")
    p.add_argument("--include-closed", action="store_true")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--refresh", action="store_true", help="ignore cached answers")
    p.add_argument("--profile", help="a discipline profile (see `profiles`)")
    p.add_argument("--collection", help="search for this collection (its profile applies)")
    p.add_argument("--no-expand", action="store_true", help="do not add synonym variants")

    sub.add_parser("providers", help="list providers (no network)")

    p = sub.add_parser("profiles", help="discipline profiles")
    p.add_argument("slug", nargs="?", default="")

    p = sub.add_parser("collection", help="list collections, or show / create one")
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--create", action="store_true")
    p.add_argument("--profile")
    p.add_argument("--description")

    p = sub.add_parser("collect", help="list papers in a collection (no download)")
    p.add_argument("name")
    p.add_argument("ids", nargs="+")
    p.add_argument("--remove", action="store_true")

    p = sub.add_parser("recall", help="past searches (no network)")
    p.add_argument("query", nargs="?", default="")
    p.add_argument("--profile")
    p.add_argument("--collection")
    p.add_argument("--work", help="searches that found this paper")
    p.add_argument("-n", type=int, default=10)

    p = sub.add_parser("index", help="index held full texts into passages")
    p.add_argument("ids", nargs="*")
    p.add_argument("--collection")
    p.add_argument("--refresh", action="store_true", help="re-index texts that changed")
    p.add_argument("--rebuild", action="store_true", help="delete the index and index again")

    p = sub.add_parser("retrieve", help="passages of held papers that answer a query")
    p.add_argument("query")
    p.add_argument("--collection")
    p.add_argument("--work", action="append", help="limit to this paper (repeatable)")
    p.add_argument("-n", type=int, default=8)
    p.add_argument("--per-paper", type=int, default=0)

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

    sub.add_parser(
        "scite-login", help="sign in to scite in the browser (enables the scite provider)"
    )
    sub.add_parser("scite-logout", help="forget this machine's scite sign-in")
    p = sub.add_parser("leads", help="identifiers in URLs you found on the web (no network)")
    p.add_argument("found", nargs="+")
    p = sub.add_parser("problems", help="provider defects logged on this machine")
    p.add_argument("-n", type=int, default=10)
    p.add_argument("--trace", action="store_true", help="print each traceback")
    sub.add_parser("status")
    sub.add_parser("rebuild-index")
    sub.add_parser("adopt-orphans")
    return ap


def _flag(item: dict[str, Any]) -> str:
    return "■" if item["full_text_in_library"] else "□" if item["in_library"] else " "


def _fetch(lib: Library, a: argparse.Namespace) -> None:
    for i in a.ids:
        r = lib.fetch(i, force=a.force, collection=a.collection)
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
        a.query,
        providers=names,
        also=["scite"] if a.scite else None,
        oa_only=not a.include_closed,
        limit=a.n,
        refresh=a.refresh,
        profile=a.profile,
        collection=a.collection,
        expand=not a.no_expand,
    )
    for v in res["variants"]:
        print(f"  also searched: {v}")
    for m in res["memory"]:
        print(f"  searched before ({m['at'][:10]}, {m['n_hits']} hits): {m['query']}")
    for name, st in res["providers"].items():
        if st["status"] in ("ok", "cache"):
            extra = f"{st.get('n', 0)} hits, ${st.get('cost_usd', 0)}"
        else:
            extra = st.get("why")
        print(f"  {name:16s} {st['status']:12s} {extra}")
    if res.get("ask_the_web"):
        print(
            f"  no open copy found ({res['ask_the_web']['why']}) and no web search is set up "
            "here.\n  Search the web yourself, then: paper-fetch leads <url> [<url> ...]"
        )
    for b in res.get("broken", []):
        print(
            f"! BROKEN: {b['provider']} failed because of a defect, not an outage: {b['error']}\n"
            f"  Its results are missing from this search. Logged with a traceback in {b['log']}"
            " (`paper-fetch problems`).",
            file=sys.stderr,
        )
    print(f"\n{len(res['hits'])} distinct papers")
    for h in res["hits"]:
        ident = h["ids"].get("doi") or h["ids"].get("pmcid") or h["ids"].get("arxiv") or ""
        provs = ",".join(sorted(set(h["providers"])))
        marks = ("*" if h["profile_match"] else " ") + ("↺" if h["seen_before"] else " ")
        print(f" {_flag(h)}{marks}{h['year'] or '':4} [{provs}] {(h['title'] or '')[:70]}  {ident}")
    print(LEGEND + "   * names the profile's terms   ↺ found by an earlier search")


def _profiles(lib: Library, a: argparse.Namespace) -> None:
    if a.slug:
        print(json.dumps(lib.profile(a.slug).guidance(), indent=1, ensure_ascii=False))
        return
    for p in lib.profiles().values():
        print(f"  {p.slug:16s} {p.label}  ({len(p.anchors)} anchors; {p.origin})")


def _collection(lib: Library, a: argparse.Namespace) -> None:
    if a.create or a.profile is not None or a.description is not None:
        lib.create_collection(a.name, profile=a.profile, description=a.description)
    if not a.name:
        for c in lib.list_collections():
            print(f"  {c['name']:24s} {c['members']:5d} papers  {c['profile'] or ''}")
        return
    c = lib.collection(a.name)
    print(f"{c['name']}  profile={c['profile']}  {c['n']} papers, {c['with_full_text']} readable")
    if c["description"]:
        print(f"  {c['description']}")
    for m in c["members"]:
        mark = "■" if m["full_text"] else "□" if m["in_library"] else " "
        print(f" {mark} {m['work'] or m['key']:24s} {m.get('year') or '':4} {m.get('title') or ''}")
    for s in c["searches"]:
        print(f"  searched {s['at'][:10]}: {s['query']}  ({s['n_hits']} hits)")


def _recall(lib: Library, a: argparse.Namespace) -> None:
    rows = lib.recall(a.query, profile=a.profile, collection=a.collection, work=a.work, limit=a.n)
    for r in rows:
        where = " ".join(f"{k}={r[k]}" for k in ("profile", "collection") if r.get(k))
        print(f"{r['at'][:16]}  {r['query']}  ({r['n_hits']} hits; score {r['score']}) {where}")
        for h in r["hits"][:5]:
            mark = "■" if h["full_text_in_library"] else "□" if h["in_library"] else " "
            print(f"   {mark} {h.get('year') or '':4} {(h.get('title') or '')[:80]}")
    print(f"  {len(rows)} past search(es)")


def _index(lib: Library, a: argparse.Namespace) -> None:
    if a.rebuild:
        path = lib.passages.path
        lib.passages.close()
        for suffix in ("", "-wal", "-shm"):
            path.with_name(path.name + suffix).unlink(missing_ok=True)
        lib.passages = None
    r = lib.index_works(a.ids or None, collection=a.collection, refresh=a.refresh)
    print(
        f"indexed {len(r['indexed'])} paper(s), {r['passages_added']} passages; "
        f"{r['unchanged']} unchanged; {len(r['no_full_text'])} without full text"
    )
    for key in r["not_held"]:
        print(f"  not held: {key}")
    for sk in r["skipped"]:
        print(f"  skipped: {sk['work']}: {sk['why']}")
    if r["skipped"]:
        print(f"{len(r['skipped'])} record(s) skipped; the rest were indexed")
    print(json.dumps(lib.passages.stats()))


def _retrieve(lib: Library, a: argparse.Namespace) -> None:
    res = lib.retrieve(
        a.query,
        identifiers=a.work,
        collection=a.collection,
        limit=a.n,
        per_paper=a.per_paper or None,
    )
    for p in res["results"][0]["passages"]:
        paper = p["paper"]
        print(f"{p['id']}  [{p['start']}:{p['end']}]  {paper.get('year')}  {paper.get('title')}")
        print("    " + " ".join(p["text"].split())[:300])
    scope = "whole index" if res["scope"] is None else f"{res['scope']} paper(s)"
    print(f"  scope: {scope}; reranker: {res['reranker'] or 'none'}; index: {res['index']}")


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
    elif a.cmd == "profiles":
        _profiles(lib, a)
    elif a.cmd == "collection":
        _collection(lib, a)
    elif a.cmd == "collect":
        res = (lib.uncollect if a.remove else lib.collect)(a.name, a.ids)
        for bad in res.get("rejected", []):
            print(f"  rejected: {bad['id']}: {bad['why']}")
        _collection(
            lib, argparse.Namespace(name=a.name, create=False, profile=None, description=None)
        )
    elif a.cmd == "recall":
        _recall(lib, a)
    elif a.cmd == "index":
        _index(lib, a)
    elif a.cmd == "retrieve":
        _retrieve(lib, a)
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
    elif a.cmd == "scite-login":
        path = scite_auth.login(lib.oa.http)
        print(f"signed in to scite; sign-in stored at {path} (0600)")
    elif a.cmd == "scite-logout":
        print("scite sign-in removed" if scite_auth.logout() else "no scite sign-in on file")
    elif a.cmd == "leads":
        res = lib.leads(a.found)
        for ld in res["leads"]:
            held = "■" if ld["full_text_in_library"] else "□" if ld["in_library"] else " "
            print(f" {held} {ld['fetch']:40s} from {ld['from'][:60]}")
        for item in res["no_identifier"]:
            print(f"   no identifier in: {item[:80]}")
        print(LEGEND + "   then: paper-fetch fetch <id>")
    elif a.cmd == "problems":
        rows = problems.recent(a.n)
        print(f"{len(rows)} shown, from {problems.log_path()}")
        for r in rows:
            print(f"  {r['at']}  {r['provider']:12s} {r['operation']:7s} {r['error']}")
            if a.trace:
                print("    " + r["traceback"].rstrip().replace("\n", "\n    "))
    elif a.cmd == "status":
        print(json.dumps(lib.status(), indent=1))
    elif a.cmd == "rebuild-index":
        print(f"catalogue rebuilt: {lib.rebuild_index()} works")
    elif a.cmd == "adopt-orphans":
        print(json.dumps(lib.adopt_orphans(), indent=1))
    return 0


def main(argv: Sequence[str] | None = None, *, library: Library | None = None) -> int:
    """Entry point for `paper-fetch`. `library` lets tests supply an offline Library."""
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
