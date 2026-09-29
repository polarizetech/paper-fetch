"""The library: look in what we already have, then (only then) ask the providers.

    lib = Library.default()                          # store + providers from the environment
    rec = lib.fetch("10.1371/journal.pcbi.1003285")
    text = lib.text(rec["work"])
    res = lib.search("reproducible computational research")   # federated: every search provider
    res["hits"], res["providers"]                    # merged hits; per-provider status

**Index-first is the whole point, and it is tested rather than asserted**: the test suite fetches
a paper, fetches it again, deletes the catalogue, fetches it a third time, and asserts the network
was touched only the first time.

## Identity

A paper is keyed by its **OpenAlex work ID** when OpenAlex knows it (single-work lookups are free),
because that one ID joins DOI, PMID and PMCID. When OpenAlex does not know it (some preprints),
it is keyed by the identifier it arrived with (`doi-...`, `arxiv-...`, `pmid-...`, `pmcid-...`),
and the record says so. Nothing is ever keyed by a provider's private ID.

## Providers are swappable

`Library(store, oa, providers=[...], search_providers=[...])`, or by name through
`PAPER_FETCH_PROVIDERS` / `PAPER_FETCH_SEARCH_PROVIDERS`. See `providers/__init__.py`.

A failed retrieval is remembered with a `retry_after`: open-access status changes (embargoes lift,
preprints get deposited), but without a delay every lookup of a closed paper re-asks every provider.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .citations import DIRECTIONS, OpenCitations
from .http import HttpClient
from .idconv import enrich
from .ids import Ident, doi_key, normalize, pmcid_from_openalex
from .openalex import OpenAlex
from .providers import FALLBACK_SEARCH, Hit, Ids, Provider, ProviderUnavailable, build
from .resolve import resolve
from .store import PREFIX, Store, store_from_env
from .text import is_pdf, looks_like_prose, pdf_text

__all__ = ["INDEX", "Library", "NotFound", "Record"]

#: A catalogue row: work, doi, pmid, pmcid, arxiv, title, year, authors, is_retracted, oa_status,
#: full_text, format, license, route, retrieved, retry_after.
Record = dict[str, Any]

INDEX = f"{PREFIX}index.jsonl"
_TS = "%Y-%m-%dT%H:%M:%SZ"
_EXT = {
    "pdf": ("fulltext.pdf", "application/pdf"),
    "tei-xml": ("fulltext.tei.xml", "application/xml"),
    "jats-xml": ("fulltext.jats.xml", "application/xml"),
    "txt": ("fulltext.provider.txt", "text/plain; charset=utf-8"),
}
_TEXT = ("fulltext.txt", "text/plain; charset=utf-8")
_CTYPE_BY_NAME = {**dict(_EXT.values()), _TEXT[0]: _TEXT[1]}
_JSON = "application/json"


class NotFound(LookupError):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime(_TS)


def _abstract(inv: dict[str, list[int]] | None) -> str | None:
    """OpenAlex ships abstracts as an inverted index {word: [positions]}; put it back in order."""
    if not inv:
        return None
    return " ".join(w for _, w in sorted((i, w) for w, idxs in inv.items() for i in idxs))


def _fallback_key(ident: Ident) -> str:
    v = doi_key(ident.value) if ident.kind == "doi" else urllib.parse.quote(ident.value, safe="")
    return f"{ident.kind}-{v}"


def _dedupe_key(ids: Ids) -> str | None:
    for k in ("doi", "pmcid", "pmid", "openalex", "arxiv"):
        if ids.get(k):
            return f"{k}:{str(ids[k]).lower()}"
    return None


def _work_id(work: dict[str, Any]) -> str:
    return work["id"].rsplit("/", 1)[-1]


def _work_doi(work: dict[str, Any]) -> str | None:
    return (work.get("doi") or "").replace("https://doi.org/", "").lower() or None


def _work_pmid(work: dict[str, Any]) -> str | None:
    return ((work.get("ids") or {}).get("pmid") or "").rstrip("/").rsplit("/", 1)[-1] or None


def _arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{arxiv_id.lower()}"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class Library:
    def __init__(
        self,
        store: Store,
        oa: OpenAlex,
        *,
        providers: Sequence[Provider] | None = None,
        search_providers: Sequence[Provider] | None = None,
        retry_after_days: int = 30,
        search_ttl_days: int = 30,
    ) -> None:
        self.store = store
        self.oa = oa
        self.http: HttpClient = oa.http
        self.providers: list[Provider] = (
            list(providers) if providers is not None else build(self.http, openalex_client=oa)
        )
        self._search_providers = list(search_providers) if search_providers is not None else None
        self.retry_after_days = retry_after_days
        self.search_ttl_days = search_ttl_days
        self._index: dict[str, Record] | None = None
        self.opencitations = OpenCitations(self.http)

    @classmethod
    def default(cls) -> Library:
        """The store named by PAPER_FETCH_STORE (local by default) and live providers."""
        return cls(store_from_env(), OpenAlex())

    @property
    def search_providers(self) -> list[Provider]:
        if self._search_providers is None:
            self._search_providers = build(self.http, openalex_client=self.oa, purpose="search")
        return self._search_providers

    @search_providers.setter
    def search_providers(self, value: Sequence[Provider]) -> None:
        self._search_providers = list(value)

    # -- catalogue ------------------------------------------------------------------------------

    def index(self, refresh: bool = False) -> dict[str, Record]:
        if self._index is None or refresh:
            self._index = {}
            if refresh:
                self.store.invalidate(INDEX)
            if self.store.has(INDEX):
                for line in self.store.get(INDEX).decode().splitlines():
                    if line.strip():
                        rec = json.loads(line)
                        self._index[rec["work"]] = rec
        return self._index

    def _save_index(self) -> None:
        rows = sorted(self.index().values(), key=lambda r: r["work"])
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
        self.store.put(INDEX, body.encode(), "application/x-ndjson")

    def _by(self, field: str, value: str | None) -> Record | None:
        if not value:
            return None
        v = str(value).lower()
        return next(
            (r for r in self.index().values() if str(r.get(field) or "").lower() == v), None
        )

    def _put_json(self, key: str, obj: Any, *, indent: int | None = None) -> bytes:
        body = json.dumps(obj, indent=indent).encode()
        self.store.put(key, body, _JSON)
        return body

    def _point_doi(self, doi: str, wid: str) -> None:
        self._put_json(f"{PREFIX}doi/{doi_key(doi)}.json", {"work": wid})

    # -- lookup: index first, then authoritative objects, never the network --------------------

    def lookup(self, identifier: str | Ident) -> Record | None:
        """The catalogue row for any identifier, or None. Never touches the network."""
        ident = identifier if isinstance(identifier, Ident) else normalize(identifier)
        idx = self.index()
        if ident.kind == "openalex" and ident.value in idx:
            return idx[ident.value]
        hit = self._by(ident.kind, ident.value)
        if hit:
            return hit
        doi = None
        if ident.kind == "doi":
            doi = ident.value
        elif ident.kind == "arxiv":
            doi = _arxiv_doi(ident.value)
            hit = self._by("doi", doi)
            if hit:
                return hit
        if doi:
            ptr = f"{PREFIX}doi/{doi_key(doi)}.json"
            if self.store.has(ptr):
                return self._repair(json.loads(self.store.get(ptr))["work"])
        candidates = [ident.value] if ident.kind == "openalex" else []
        for wid in [*candidates, _fallback_key(ident)]:
            rec = self._repair(wid)
            if rec:
                return rec
        return None

    def _repair(self, wid: str) -> Record | None:
        """Rebuild one catalogue row from the authoritative per-work objects."""
        prov_key = f"{PREFIX}works/{wid}/provenance.json"
        if not self.store.has(prov_key):
            return None
        prov = json.loads(self.store.get(prov_key))
        rec = self._row(json.loads(self.store.get(f"{PREFIX}works/{wid}/work.json")), prov)
        self.index()[wid] = rec
        self._save_index()
        return rec

    # -- fetch ---------------------------------------------------------------------------------

    def fetch(self, identifier: str, *, force: bool = False) -> Record:
        """Return the paper from the library, or obtain a legal open copy and store it.

        The result is a catalogue row plus `from`: "library" | "retrieved" | "not-obtainable";
        after a network attempt it also carries `attempts` and `refused`.
        """
        ident = normalize(identifier)
        if not force:
            rec = self.lookup(ident)
            if rec and (rec["full_text"] or not self._retry_due(rec)):
                return {**rec, "from": "library"}

        work = self._oa_work(ident)  # free; also joins DOI / PMID / PMCID
        ids: Ids
        if work is not None:
            wid = _work_id(work)
            if not force and wid != ident.value:
                rec = self.lookup(Ident("openalex", wid))
                if rec and (rec["full_text"] or not self._retry_due(rec)):
                    return {**rec, "from": "library"}
            ids = {
                "openalex": wid,
                "doi": _work_doi(work),
                "pmid": _work_pmid(work),
                "pmcid": pmcid_from_openalex(work.get("ids"))
                or (ident.value if ident.kind == "pmcid" else None),
                "arxiv": ident.value if ident.kind == "arxiv" else None,
                "_openalex_work": work,
            }
        else:
            wid = _fallback_key(ident)
            ids = {"doi": None, "pmid": None, "pmcid": None, "arxiv": None, "openalex": None}
            ids[ident.kind] = ident.value
            if ident.kind == "arxiv":
                ids["doi"] = _arxiv_doi(ident.value)
            work = {
                "id": wid,
                "doi": ids["doi"],
                "ids": {},
                "title": None,
                "publication_year": None,
                "authorships": [],
                "open_access": {},
                "_note": "OpenAlex has no record; keyed by the identifier it arrived with",
            }

        ids, ids_note = enrich(ids, self.http)
        res = resolve(ids, self.providers)
        base = f"{PREFIX}works/{wid}/"
        files: dict[str, str] = {}
        ft = res.fulltext
        if ft:
            name, ctype = _EXT[ft.fmt]
            self.store.put(base + name, ft.data, ctype)
            files[name] = _sha256(ft.data)
            if ft.text_ok and ft.text:
                tb = ft.text.encode()
                self.store.put(base + _TEXT[0], tb, _TEXT[1])
                files[_TEXT[0]] = _sha256(tb)

        ok = bool(ft and ft.text_ok)
        retry_after = (datetime.now(UTC) + timedelta(days=self.retry_after_days)).strftime(_TS)
        prov = {
            "work": wid,
            "retrieved": _now(),
            "full_text": ok,
            "file_stored": bool(ft),
            "route": ft.route if ft else None,
            "source_url": ft.url if ft else None,
            "format": ft.fmt if ft else None,
            "license": ft.license if ft else None,
            "version": ft.version if ft else None,
            "md5_verified": ft.md5_verified if ft else None,
            "text_method": ft.text_method if ft else None,
            "text_gate": ft.text_reason if ft else None,
            "oa_status": (work.get("open_access") or {}).get("oa_status"),
            "rights": f"open-access copy reported by {ft.route.split(':')[0]}" if ft else None,
            "refused": res.refused,
            "providers": [p.name for p in self.providers],
            "attempts": [list(a) for a in res.attempts],
            "sha256": files,
            "ids": {k: v for k, v in ids.items() if not k.startswith("_")},
            "ids_note": ids_note,
            "retry_after": None if ok else retry_after,
        }
        self._put_json(base + "work.json", work)
        self._put_json(base + "provenance.json", prov, indent=1)
        if ids.get("doi"):
            self._point_doi(ids["doi"], wid)
        if ft:
            self.store.assert_private(base + _EXT[ft.fmt][0])

        rec = self._row(work, prov)
        self.index()[wid] = rec
        self._save_index()
        return {
            **rec,
            "from": "retrieved" if ok else "not-obtainable",
            "attempts": res.attempts,
            "refused": res.refused,
        }

    def _oa_work(self, ident: Ident) -> dict[str, Any] | None:
        """The OpenAlex record for any identifier kind.

        OpenAlex cannot be asked by PMCID: `/works/pmcid:PMC...` is a 404 in both spellings and
        `filter=ids.pmcid:` matches nothing. So a PMCID goes through NCBI's ID converter to a DOI
        or PMID first; otherwise a paper requested by PMCID alone would be stored with no title,
        year or authors, under a `pmcid-...` key.
        """
        if ident.kind == "arxiv":
            return self.oa.work(Ident("doi", _arxiv_doi(ident.value)))
        if ident.kind != "pmcid":
            return self.oa.work(ident)
        ids, _ = enrich({"pmcid": ident.value, "pmid": None, "doi": None}, self.http)
        for kind in ("doi", "pmid"):
            if ids.get(kind):
                work = self.oa.work(Ident(kind, ids[kind]))
                if work is not None:
                    return work
        return None

    @staticmethod
    def _retry_due(rec: Record) -> bool:
        ra = rec.get("retry_after")
        return bool(ra) and datetime.now(UTC) >= datetime.strptime(ra, _TS).replace(tzinfo=UTC)

    @staticmethod
    def _row(work: dict[str, Any], prov: dict[str, Any]) -> Record:
        ids = prov.get("ids") or {}
        authors = [
            (a.get("author") or {}).get("display_name") for a in (work.get("authorships") or [])[:3]
        ]
        return {
            "work": prov["work"],
            "doi": ids.get("doi") or _work_doi(work),
            "pmid": ids.get("pmid") or _work_pmid(work),
            "pmcid": ids.get("pmcid") or pmcid_from_openalex(work.get("ids")),
            "arxiv": ids.get("arxiv"),
            "title": work.get("title") or work.get("display_name"),
            "year": work.get("publication_year"),
            "authors": [a for a in authors if a],
            "is_retracted": work.get("is_retracted"),
            "oa_status": prov.get("oa_status"),
            "full_text": prov.get("full_text"),
            "format": prov.get("format"),
            "license": prov.get("license"),
            "route": prov.get("route"),
            "retrieved": prov.get("retrieved"),
            "retry_after": prov.get("retry_after"),
        }

    # -- reading -------------------------------------------------------------------------------

    def _held(self, identifier: str) -> Record:
        rec = self.lookup(identifier)
        if not rec:
            raise NotFound(f"{identifier} is not in the library -- fetch() it first")
        return rec

    def text(self, identifier: str) -> str:
        """The stored, gate-passed full text. Raises NotFound if there is none."""
        rec = self._held(identifier)
        if not rec["full_text"]:
            raise NotFound(f"{rec['work']} has no readable full text (see its provenance)")
        return self.store.get(f"{PREFIX}works/{rec['work']}/{_TEXT[0]}").decode()

    def provenance(self, identifier: str) -> dict[str, Any]:
        """Where a held copy came from: route, source URL, licence, sha256s, every attempt."""
        rec = self._held(identifier)
        return json.loads(self.store.get(f"{PREFIX}works/{rec['work']}/provenance.json"))

    def work(self, identifier: str) -> dict[str, Any]:
        """The stored OpenAlex record, with its abstract re-assembled."""
        rec = self._held(identifier)
        w = json.loads(self.store.get(f"{PREFIX}works/{rec['work']}/work.json"))
        w["abstract"] = _abstract(w.get("abstract_inverted_index"))
        return w

    def verify(self, identifier: str) -> dict[str, Any]:
        """Re-hash every stored file of a work against the sha256s in its provenance."""
        prov = self.provenance(identifier)
        base = f"{PREFIX}works/{prov['work']}/"
        files: dict[str, str] = {}
        for name, want in (prov.get("sha256") or {}).items():
            if not self.store.has(base + name):
                files[name] = "missing"
            else:
                files[name] = "ok" if _sha256(self.store.get(base + name)) == want else "mismatch"
        return {"work": prov["work"], "ok": all(v == "ok" for v in files.values()), "files": files}

    # -- federated search ----------------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        providers: Sequence[str] | None = None,
        oa_only: bool = True,
        limit: int = 10,
        refresh: bool = False,
        web_fallback: bool | None = None,
    ) -> dict[str, Any]:
        """Ask every search provider, merge by identifier, and mark what we already hold.

        Each provider's answer is cached in the store separately, so a provider that failed can be
        retried without re-spending the ones that succeeded, and a provider that refused us is
        reported by name, never folded into "no results".

        **Web fallback.** When the providers above return no open-access hit, the `web` provider
        (SearXNG, opt-in) is asked too, and its report says why it ran. It is skipped, and
        reported `not-needed`, when an open hit already exists. Naming `web` in `providers` runs
        it unconditionally; `web_fallback=False` or PAPER_FETCH_WEB_FALLBACK=0 disables the
        fallback.
        """
        provs = (
            build(self.http, openalex_client=self.oa, names=providers, purpose="search")
            if providers is not None
            else self.search_providers
        )
        report: dict[str, dict[str, Any]] = {}
        merged: dict[str, dict[str, Any]] = {}
        for p in provs:
            self._ask(
                p, query, oa_only=oa_only, limit=limit, refresh=refresh, into=(report, merged)
            )

        if web_fallback is None:
            web_fallback = os.environ.get("PAPER_FETCH_WEB_FALLBACK", "1") != "0"
        if FALLBACK_SEARCH not in report:
            if not web_fallback:
                report[FALLBACK_SEARCH] = {"status": "skipped", "why": "web fallback disabled"}
            elif any(m["is_oa"] for m in merged.values()):
                report[FALLBACK_SEARCH] = {
                    "status": "not-needed",
                    "why": "the scholarly providers found an open-access hit",
                }
            else:
                why_ran = (
                    "no hits from the scholarly providers"
                    if not merged
                    else "no open-access hit from the scholarly providers"
                )
                web = build(self.http, names=[FALLBACK_SEARCH], purpose="search")[0]
                self._ask(
                    web, query, oa_only=oa_only, limit=limit, refresh=refresh, into=(report, merged)
                )
                report[FALLBACK_SEARCH]["why_ran"] = why_ran

        hits = []
        for m in merged.values():
            row = next(
                (
                    r
                    for f in ("openalex", "doi", "pmcid", "pmid", "arxiv")
                    for r in [self._by(f, m["ids"].get(f))]
                    if r
                ),
                None,
            )
            if row is None and m["ids"].get("openalex"):
                row = self.index().get(m["ids"]["openalex"])
            hits.append(
                {
                    **m,
                    "in_library": bool(row),
                    "full_text_in_library": bool(row and row["full_text"]),
                    "work": row["work"] if row else None,
                }
            )
        hits.sort(key=lambda h: (-len(set(h["providers"])), -(h["year"] or 0)))
        return {"query": query, "providers": report, "hits": hits}

    def _ask(
        self,
        p: Provider,
        query: str,
        *,
        oa_only: bool,
        limit: int,
        refresh: bool,
        into: tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]],
    ) -> None:
        """Ask one provider (or its cached answer); record its status and merge its hits."""
        report, merged = into
        ok, why = p.available()
        if not ok:
            report[p.name] = {"status": "skipped", "why": why}
            return
        src = json.dumps({"q": query, "oa": oa_only, "n": limit}, sort_keys=True)
        key = f"{PREFIX}searches/{p.name}/{_sha256(src.encode())}.json"
        cached = None
        if p.cacheable and not refresh and self.store.has(key):
            cached = json.loads(self.store.get(key))
            if time.time() - cached.get("_cached_at", 0) > self.search_ttl_days * 86400:
                cached = None
        if cached is not None:
            hits = [Hit.from_json(h) for h in cached["hits"]]
            report[p.name] = {"status": "cache", "n": len(hits), "cost_usd": 0.0}
        else:
            spent = self.oa.spent_usd
            try:
                hits = p.search(query, oa_only=oa_only, limit=limit)
            except ProviderUnavailable as e:
                report[p.name] = {"status": "unavailable", "why": str(e)}
                return
            except Exception as e:  # noqa: BLE001 -- reported by name; the search goes on
                report[p.name] = {"status": "error", "why": f"{type(e).__name__}: {str(e)[:120]}"}
                return
            if p.cacheable:
                entry = {
                    "_cached_at": time.time(),
                    "_query": query,
                    "hits": [h.to_json() for h in hits],
                }
                self._put_json(key, entry)
            report[p.name] = {
                "status": "ok",
                "n": len(hits),
                "cost_usd": round(self.oa.spent_usd - spent, 4),
            }
        for h in hits:
            k = _dedupe_key(h.ids) or f"{p.name}:{h.title}"
            m = merged.setdefault(
                k,
                {
                    "title": h.title,
                    "year": h.year,
                    "ids": {},
                    "authors": h.authors,
                    "providers": [],
                    "is_oa": False,
                    "locations": [],
                    "urls": [],
                },
            )
            m["ids"].update({kk: vv for kk, vv in h.ids.items() if vv and not m["ids"].get(kk)})
            m["providers"].append(p.name)
            m["is_oa"] = m["is_oa"] or bool(h.is_oa)
            m["locations"] += [f"{loc.provider}:{loc.fmt}" for loc in h.locations]
            if h.extra.get("url"):
                m["urls"].append(h.extra["url"])
            m["title"] = m["title"] or h.title
            m["year"] = m["year"] or h.year

    # -- the citation graph (OpenCitations) -----------------------------------------------------

    def citations(self, identifier: str, *, refresh: bool = False) -> dict[str, Any]:
        """Works that cite this one, from OpenCitations, each marked if the library holds it."""
        return self._graph(identifier, "citations", refresh)

    def references(self, identifier: str, *, refresh: bool = False) -> dict[str, Any]:
        """Works this one cites, from OpenCitations, each marked if the library holds it."""
        return self._graph(identifier, "references", refresh)

    def _graph_ids(self, identifier: str) -> Ids:
        ident = normalize(identifier)
        rec = self.lookup(ident)
        if rec and (rec.get("doi") or rec.get("pmid")):
            return {"doi": rec.get("doi"), "pmid": rec.get("pmid")}
        if ident.kind in ("doi", "pmid"):
            return {ident.kind: ident.value}
        work = self._oa_work(ident)  # free; turns a W-id or PMCID into a DOI/PMID
        if work:
            doi, pmid = _work_doi(work), _work_pmid(work)
            if doi or pmid:
                return {"doi": doi, "pmid": pmid}
        raise NotFound(f"{identifier}: no DOI or PMID to ask OpenCitations with")

    def _graph(self, identifier: str, direction: str, refresh: bool) -> dict[str, Any]:
        if direction not in DIRECTIONS:
            raise ValueError("direction must be 'citations' or 'references'")
        ids = self._graph_ids(identifier)
        name = doi_key(ids["doi"]) if ids.get("doi") else f"pmid-{ids['pmid']}"
        key = f"{PREFIX}citations/{direction}/{name}.json"
        status, edges = "ok", None
        if not refresh and self.store.has(key):
            cached = json.loads(self.store.get(key))
            if time.time() - cached.get("_cached_at", 0) <= self.search_ttl_days * 86400:
                edges, status = cached["edges"], "cache"
                for e in edges:  # entries cached by early versions, before the field was renamed
                    if "creation" in e and "citing_date" not in e:
                        e["citing_date"] = e.pop("creation")
        if edges is None:
            edges = self.opencitations.edges(ids, direction)
            self._put_json(key, {"_cached_at": time.time(), "of": ids, "edges": edges})
        items = []
        for e in edges:
            row = next(
                (
                    r
                    for f in ("openalex", "doi", "pmid")
                    for r in [self._by(f, e["ids"].get(f))]
                    if r
                ),
                None,
            )
            items.append(
                {
                    **e,
                    "in_library": bool(row),
                    "full_text_in_library": bool(row and row["full_text"]),
                    "work": row["work"] if row else None,
                }
            )
        return {
            "of": ids,
            "direction": direction,
            "status": status,
            "n": len(items),
            "held": sum(1 for i in items if i["in_library"]),
            "source": "OpenCitations Index v2 (open citation links; no citation intent)",
            "items": items,
        }

    def search_library(
        self, query: str, *, full_text: bool = False, limit: int = 50
    ) -> list[Record]:
        """Term matching over held titles and authors, and optionally inside stored full text."""
        terms = [t.lower() for t in query.split() if t]
        out = []
        for rec in self.index().values():
            hay = " ".join([rec.get("title") or "", " ".join(rec.get("authors") or [])]).lower()
            hit = all(t in hay for t in terms)
            where = "title/authors" if hit else None
            if not hit and full_text and rec.get("full_text"):
                body = self.store.get(f"{PREFIX}works/{rec['work']}/{_TEXT[0]}").decode().lower()
                if all(t in body for t in terms):
                    hit, where = True, "full text"
            if hit:
                out.append({**rec, "matched_in": where})
            if len(out) >= limit:
                break
        return out

    # -- a copy the operator legitimately holds -----------------------------------------------

    def add_local(self, path: str | Path, identifier: str, *, rights: str) -> Record:
        """Store a PDF you legitimately hold, under a rights statement you declare."""
        if not rights or not rights.strip():
            raise ValueError(
                "declare the rights under which this copy is held "
                "(e.g. 'author-supplied copy', 'purchased 2026-01-31')"
            )
        data = Path(path).read_bytes()
        if not is_pdf(data):
            raise ValueError(f"{path} is not a PDF")
        ident = normalize(identifier)
        work = self._oa_work(ident)
        if work is None:
            raise NotFound(f"OpenAlex has no work for {ident}")
        wid = _work_id(work)
        text, method = pdf_text(data)
        ok, reason = looks_like_prose(text)
        base = f"{PREFIX}works/{wid}/"
        self.store.put(base + "fulltext.pdf", data, "application/pdf")
        files = {"fulltext.pdf": _sha256(data)}
        if ok and text:
            self.store.put(base + _TEXT[0], text.encode(), _TEXT[1])
            files[_TEXT[0]] = _sha256(text.encode())
        doi = _work_doi(work)
        prov = {
            "work": wid,
            "retrieved": _now(),
            "full_text": ok,
            "file_stored": True,
            "route": "operator-supplied:pdf",
            "source_url": None,
            "format": "pdf",
            "license": None,
            "version": None,
            "md5_verified": None,
            "text_method": method,
            "text_gate": reason,
            "oa_status": (work.get("open_access") or {}).get("oa_status"),
            "rights": rights.strip(),
            "refused": None,
            "providers": [],
            "attempts": [],
            "sha256": files,
            "ids": {"openalex": wid, "doi": doi},
            "retry_after": None,
        }
        self._put_json(base + "work.json", work)
        self._put_json(base + "provenance.json", prov, indent=1)
        if doi:
            self._point_doi(doi, wid)
        self.store.assert_private(base + "fulltext.pdf")
        rec = self._row(work, prov)
        self.index()[wid] = rec
        self._save_index()
        return rec

    # -- maintenance ---------------------------------------------------------------------------

    def rebuild_index(self) -> int:
        """Rebuild the catalogue from the authoritative per-work provenance objects."""
        self._index = {}
        for key in self.store.keys(f"{PREFIX}works/"):
            if key.endswith("/provenance.json"):
                wid = key.split("/")[2]
                prov = json.loads(self.store.get(key))
                prov.setdefault("work", wid)
                work = json.loads(self.store.get(f"{PREFIX}works/{wid}/work.json"))
                self._index[wid] = self._row(work, prov)
        self._save_index()
        return len(self._index)

    def adopt_orphans(self) -> list[dict[str, Any]]:
        """Move works keyed by an arrival identifier (`pmcid-...`, `doi-...`) to their OpenAlex id.

        For each such row, OpenAlex is asked again (free lookups, PMCIDs via NCBI). If it now knows
        the work, the objects are copied under `works/W.../` with the OpenAlex record, or, when that
        work is already held with full text, the orphan is dropped as a duplicate. The DOI pointer
        is repointed, and the orphan's objects are deleted only after the copy is in place.
        """
        report: list[dict[str, Any]] = []
        for key, _rec in sorted(self.index().items()):
            if key.startswith("W"):
                continue
            prov = json.loads(self.store.get(f"{PREFIX}works/{key}/provenance.json"))
            ids = prov.get("ids") or {}
            work = None
            for kind in ("doi", "pmid", "pmcid", "arxiv"):
                if ids.get(kind):
                    work = self._oa_work(Ident(kind, ids[kind]))
                    if work is not None:
                        break
            if work is None:
                report.append(
                    {"orphan": key, "action": "kept", "why": "OpenAlex still has no record"}
                )
                continue
            wid = _work_id(work)
            old, new = f"{PREFIX}works/{key}/", f"{PREFIX}works/{wid}/"
            held = self.lookup(Ident("openalex", wid))
            if held and held["full_text"]:
                action = "dropped: duplicate of a held work"
            else:
                for k in self.store.keys(old):
                    name = k[len(old) :]
                    if name in ("work.json", "provenance.json"):
                        continue
                    ctype = _CTYPE_BY_NAME.get(name, "application/octet-stream")
                    self.store.put(new + name, self.store.get(k), ctype)
                prov["ids"] = {
                    **ids,
                    "openalex": wid,
                    "doi": ids.get("doi") or _work_doi(work),
                    "pmid": ids.get("pmid") or _work_pmid(work),
                }
                prov["work"] = wid
                prov["oa_status"] = (work.get("open_access") or {}).get("oa_status")
                prov["adopted_from"] = key
                self._put_json(new + "work.json", work)
                self._put_json(new + "provenance.json", prov, indent=1)
                for name in prov.get("sha256") or {}:
                    self.store.assert_private(new + name)
                self.index()[wid] = self._row(work, prov)
                action = "moved"
            doi = _work_doi(work) or ids.get("doi")
            if doi:
                self._point_doi(doi, wid)
            for k in self.store.keys(old):
                self.store.delete(k)
            self.index().pop(key, None)
            self._save_index()
            report.append({"orphan": key, "action": action, "work": wid})
        return report

    def status(self) -> dict[str, Any]:
        idx = self.index()
        return {
            "works": len(idx),
            "with_full_text": sum(1 for r in idx.values() if r["full_text"]),
            "not_obtainable": sum(1 for r in idx.values() if not r["full_text"]),
            "store": type(self.store).__name__,
            "locate_providers": [p.name for p in self.providers],
            "openalex_key": bool(self.oa.api_key),
            "network_calls": getattr(self.http, "calls", None),
            "openalex_spent_usd": round(self.oa.spent_usd, 4),
            "openalex_remaining_usd": self.oa.usage.remaining_usd,
        }
