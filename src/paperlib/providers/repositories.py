"""Publisher and repository APIs: PLOS, OSF Preprints (through SHARE), HAL, DOAJ.

Every response shape below was read off the live service (September 2026), with no key.

`plos`
: Search: Solr `everything:` field (body text included). Locate: PLOS DOIs only, JATS XML
  (`type=manuscript`) then PDF (`type=printable`); the URLs are built, no lookup call. The licence
  is absent from the response; PLOS publishes under CC BY (CC0 for some US-government work).
  Documented limit: 10 requests/minute.

`osf`
: Search: SHARE `index-card-search`, resourceType Preprint (PsyArXiv and the other OSF servers).
  Locate: SHARE `sameAs` filter (the `identifier` filter returned nothing for a DOI it holds), then
  `osf.io/<id>/download`, only when the card's `rights` is Creative Commons. OSF's own v2 API was
  too slow to use (502 after ~30 s).

`hal`
: Search: `/search?fq=submitType_s:file` (records WITH a file only). Locate: `q=doiId_s:"..."`,
  `fileMain_s` when `openAccess_bool`. The licence is a CC URL or HAL's own deposit authorisation
  (`hal-authorisation-v1`), which permits reading, not reuse, and is recorded verbatim.

`doaj`
: Search only: `/api/search/articles/<q>`. Its full-text links are mostly publisher landing pages,
  which the library's validator refuses by design. Every DOAJ journal is open access, so hits are
  `is_oa`.

Zenodo is not included: its search matched too loosely to be useful for literature lookup.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from .base import Hit, Ids, Location, Provider, clean_doi

__all__ = ["DOAJ", "HAL", "OSF", "PLOS"]

_Q = urllib.parse.quote
_CC = re.compile(r"creativecommons\.org|^\s*cc[\s\-_0]|creative\s*commons|public\s*domain", re.I)


def _year(s: str | None) -> int | None:
    y = (s or "")[:4]
    return int(y) if y.isdigit() else None


# ---------------------------------------------------------------------------------- PLOS


class PLOS(Provider):
    name, label = "plos", "PLOS (publisher API)"
    can_search = can_locate = True
    min_interval_s = 6.1  # documented limit: 10 requests per minute
    terms = (
        "No key. 10/min, 300/h, 7,200/day, max 100 rows; attribute PLOS. All content CC BY or CC0."
    )
    SEARCH = "https://api.plos.org/search"
    FILE = "https://journals.plos.org/plosone/article/file"

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        # A quoted query stays a phrase; otherwise every word must appear. Quoting everything
        # turns a four-word query into an exact phrase and finds nothing.
        terms = query if '"' in query else " AND ".join(re.findall(r"[\w\-]+", query))
        q = _Q(f"everything:({terms})")
        r = self._get(
            f"{self.SEARCH}?q={q}&fl=id,title_display,publication_date,journal,author_display"
            f"&fq=doc_type:full&rows={min(limit, 100)}&wt=json"
        )
        if r.status != 200:
            return []
        out = []
        for d in json.loads(r.body).get("response", {}).get("docs", []):
            doi = clean_doi(d.get("id"))
            out.append(
                Hit(
                    self.name,
                    d.get("title_display"),
                    _year(d.get("publication_date")),
                    {"doi": doi},
                    True,
                    (d.get("author_display") or [])[:3],
                    self.locate({"doi": doi}),
                    {"journal": d.get("journal")},
                )
            )
        return out

    def locate(self, ids: Ids) -> list[Location]:
        doi = ids.get("doi") or ""
        if not doi.startswith("10.1371/journal."):
            return []
        lic = "CC BY (PLOS policy; not stated in the API response)"
        base = f"{self.FILE}?id={_Q(doi, safe='/')}"
        return [
            Location(f"{base}&type=manuscript", "jats-xml", self.name, lic),
            Location(f"{base}&type=printable", "pdf", self.name, lic),
        ]


# ---------------------------------------------------------------------------------- OSF via SHARE


def _v(items: Any, key: str = "@value") -> list[Any]:
    return [i.get(key) for i in (items or []) if isinstance(i, dict) and i.get(key)]


def _first(items: Any) -> Any:
    vals = _v(items)
    return vals[0] if vals else None


class OSF(Provider):
    name, label = "osf", "OSF Preprints incl. PsyArXiv (via SHARE)"
    can_search = can_locate = True
    min_interval_s = 1.0
    timeout_s = 30.0
    terms = "No key. Search and lookup through SHARE; the file through osf.io/<id>/download."
    SHARE = "https://share.osf.io/api/v3/index-card-search"
    _OSF_DOI = re.compile(r"^10\.\d{5}/osf\.io/([a-z0-9]{5,})$", re.I)

    def _cards(self, params: str) -> list[dict[str, Any]]:
        r = self._get(f"{self.SHARE}?{params}", {"Accept": "application/vnd.api+json"})
        if r.status != 200:
            return []
        return [
            i["attributes"]["resourceMetadata"]
            for i in json.loads(r.body).get("included", [])
            if i.get("type") == "index-card"
        ]

    @staticmethod
    def _osf_id(rm: dict[str, Any]) -> str | None:
        for u in [*_v(rm.get("identifier")), rm.get("@id") or ""]:
            m = re.match(r"^https?://osf\.io/([a-z0-9]{5,})/?$", u, re.I)
            if m:
                return m.group(1).lower()
        return None

    def _hit(self, rm: dict[str, Any]) -> Hit:
        doi = next((clean_doi(u) for u in _v(rm.get("identifier")) if "doi.org/" in u), None)
        rights = [x for x in rm.get("rights") or [] if isinstance(x, dict)]
        names = [_first(x.get("name")) or x.get("@id") for x in rights]
        lic = next(
            (
                n
                for n, x in zip(names, rights, strict=True)
                if _CC.search(x.get("@id") or "") or _CC.search(n or "")
            ),
            None,
        )
        oid = self._osf_id(rm)
        publisher = rm.get("publisher") or []
        server = _first(publisher[0].get("name")) if publisher else None
        locs = (
            [
                Location(
                    f"https://osf.io/{oid}/download",
                    "pdf",
                    self.name,
                    lic,
                    "preprint",
                    note=f"{server or 'OSF'} preprint; licence from the SHARE record",
                )
            ]
            if (oid and lic)
            else []
        )
        return Hit(
            self.name,
            _first(rm.get("title")),
            _year(_first(rm.get("dateCreated"))),
            {"doi": doi, "osf": oid},
            bool(lic),
            [_first(c.get("name")) for c in (rm.get("creator") or [])[:3]],
            locs,
            {"server": server, "rights": names},
        )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        params = (
            f"cardSearchText={_Q(query)}&cardSearchFilter%5BresourceType%5D=Preprint"
            f"&page%5Bsize%5D={min(limit, 20)}"
        )
        hits = [self._hit(rm) for rm in self._cards(params)]
        return [h for h in hits if h.is_oa] if oa_only else hits

    def locate(self, ids: Ids) -> list[Location]:
        m = self._OSF_DOI.match(ids.get("doi") or "")
        if not m:
            return []
        ident = _Q(f"https://doi.org/{ids['doi']}", safe="")
        for rm in self._cards(f"cardSearchFilter%5BsameAs%5D={ident}&page%5Bsize%5D=1"):
            h = self._hit(rm)
            if h.ids.get("osf") == m.group(1).lower():
                return h.locations
        return []


# ---------------------------------------------------------------------------------- HAL


class HAL(Provider):
    name, label = "hal", "HAL (French national open archive)"
    can_search = can_locate = True
    min_interval_s = 0.5
    terms = "No key. Deposits carry a CC licence or HAL's deposit authorisation (read, not reuse)."
    BASE = "https://api.archives-ouvertes.fr/search/"
    FL = (
        "halId_s,title_s,doiId_s,producedDateY_i,fileMain_s,licence_s,openAccess_bool,"
        "authFullName_s"
    )

    def _docs(self, q: str, rows: int) -> list[dict[str, Any]]:
        r = self._get(
            f"{self.BASE}?q={_Q(q)}&fq=submitType_s:file&fl={self.FL}&rows={rows}&wt=json"
        )
        if r.status != 200:
            return []
        return json.loads(r.body).get("response", {}).get("docs", [])

    def _hit(self, d: dict[str, Any]) -> Hit:
        is_open = bool(d.get("openAccess_bool"))
        locs = (
            [
                Location(
                    d["fileMain_s"],
                    "pdf",
                    self.name,
                    d.get("licence_s"),
                    note="HAL openAccess_bool; licence recorded verbatim",
                )
            ]
            if is_open and d.get("fileMain_s")
            else []
        )
        return Hit(
            self.name,
            (d.get("title_s") or [None])[0],
            d.get("producedDateY_i"),
            {"doi": clean_doi(d.get("doiId_s")), "hal": d.get("halId_s")},
            is_open,
            (d.get("authFullName_s") or [])[:3],
            locs,
            {},
        )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        hits = [self._hit(d) for d in self._docs(query, min(limit, 50))]
        return [h for h in hits if h.is_oa] if oa_only else hits

    def locate(self, ids: Ids) -> list[Location]:
        if not ids.get("doi"):
            return []
        for d in self._docs(f'doiId_s:"{ids["doi"]}"', 3):
            if clean_doi(d.get("doiId_s")) == ids["doi"]:
                return self._hit(d).locations
        return []


# ---------------------------------------------------------------------------------- DOAJ


class DOAJ(Provider):
    name, label = "doaj", "DOAJ (open-access journal directory)"
    can_search, can_locate = True, False
    min_interval_s = 0.5
    terms = "No key. 2 req/s, bursts of 5. Search only: its full-text links are landing pages."
    BASE = "https://doaj.org/api/search/articles/"

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        r = self._get(f"{self.BASE}{_Q(query, safe='')}?pageSize={min(limit, 100)}")
        if r.status != 200:
            return []
        out = []
        for x in json.loads(r.body).get("results", []):
            b = x.get("bibjson") or {}
            doi = next(
                (clean_doi(i.get("id")) for i in b.get("identifier", []) if i.get("type") == "doi"),
                None,
            )
            out.append(
                Hit(
                    self.name,
                    b.get("title"),
                    _year(str(b.get("year") or "")),
                    {"doi": doi},
                    True,
                    [a.get("name") for a in (b.get("author") or [])[:3]],
                    [],
                    {"journal": (b.get("journal") or {}).get("title")},
                )
            )
        return out
