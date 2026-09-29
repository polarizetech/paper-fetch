"""OpenAIRE Graph, the EU scholarly aggregator. Observed against the live API (September 2026).

    GET https://api.openaire.eu/graph/v1/researchProducts?search=<q>&type=publication
        &bestOpenAccessRightLabel=OPEN&pageSize=N
    GET https://api.openaire.eu/graph/v1/researchProducts?pid=<doi>

- **No key.** Unauthenticated responses carry `x-ratelimit-limit: 7199` (read as a per-hour
  allowance; the unit is not in the header).
- **DOI lookup works** (`pid=`), and one record merges versions: a journal article can come back
  with its preprint DOI, PMID and PMCID in `pids`.
- **Open status is reported per RECORD, not per copy.** `bestAccessRight.label` is `OPEN` on the
  record; the `instances` (one per copy) carry `urls` and a `license` string ("CC BY",
  "CC BY NC ND") but no access right of their own. So this adapter returns a location only when
  the record is OPEN **and** that instance names an open licence (Creative Commons or public
  domain). A copy with no licence string is not returned, even on an OPEN record.
- Instance URLs include landing pages (doi.org, PubMed, PMC article pages). Those are skipped here:
  the library's validator would refuse them anyway, and every refusal costs a download.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from .base import Hit, Ids, Location, Provider, clean_doi

__all__ = ["OpenAIRE"]

_LANDING = re.compile(
    r"^https?://(dx\.)?doi\.org/|pubmed\.ncbi\.nlm\.nih\.gov/|"
    r"(www\.)?ncbi\.nlm\.nih\.gov/pmc/articles/|pmc\.ncbi\.nlm\.nih\.gov/articles/|"
    r"europepmc\.org/(abstract|article)/",
    re.I,
)
_OPEN_LICENCE = re.compile(
    r"^\s*(cc[\s\-_]|cc0|creative\s*commons|public\s*domain)|creativecommons\.org", re.I
)
_FILE_URL = re.compile(r"\.pdf($|\?)|/pdf\b|/files/|type=printable|/download", re.I)


def _is_open(rec: dict[str, Any]) -> bool:
    return ((rec.get("bestAccessRight") or {}).get("label") or "").upper() == "OPEN"


class OpenAIRE(Provider):
    name, label = "openaire", "OpenAIRE Graph (EU aggregator)"
    can_search = can_locate = True
    min_interval_s = 0.5
    terms = (
        "Open metadata graph, no key; ~7,200 requests per window unkeyed. Open access is stated "
        "per record, licence per copy."
    )
    BASE = "https://api.openaire.eu/graph/v1/researchProducts"

    def _results(self, params: str) -> list[dict[str, Any]]:
        r = self._get(f"{self.BASE}?{params}", {"Accept": "application/json"})
        if r.status != 200:
            return []
        return json.loads(r.body).get("results") or []

    @staticmethod
    def _ids(rec: dict[str, Any]) -> Ids:
        ids: Ids = {}
        for p in rec.get("pids") or []:
            scheme, value = (p.get("scheme") or "").lower(), p.get("value")
            if not value:
                continue
            if scheme == "doi" and "doi" not in ids:
                ids["doi"] = clean_doi(value)
            elif scheme == "pmid" and "pmid" not in ids:
                ids["pmid"] = str(value)
            elif scheme in ("pmc", "pmcid") and "pmcid" not in ids:
                ids["pmcid"] = str(value).upper()
        ids["openaire"] = rec.get("id")
        return ids

    def _locations(self, rec: dict[str, Any]) -> list[Location]:
        if not _is_open(rec):
            return []
        out: list[Location] = []
        seen: set[str] = set()
        for inst in rec.get("instances") or []:
            lic = inst.get("license")
            if not lic or not _OPEN_LICENCE.search(lic):
                continue
            for url in inst.get("urls") or []:
                if url in seen or _LANDING.search(url):
                    continue
                seen.add(url)
                out.append(
                    Location(
                        url,
                        "pdf",
                        self.name,
                        lic,
                        inst.get("type"),
                        note="record OPEN per OpenAIRE; licence stated on this copy",
                    )
                )
        out.sort(key=lambda loc: 0 if _FILE_URL.search(loc.url) else 1)
        return out[:4]

    def _hit(self, rec: dict[str, Any]) -> Hit:
        year = (rec.get("publicationDate") or "")[:4]
        return Hit(
            self.name,
            rec.get("mainTitle"),
            int(year) if year.isdigit() else None,
            self._ids(rec),
            _is_open(rec),
            [a.get("fullName") for a in (rec.get("authors") or [])[:3]],
            self._locations(rec),
            {"oa_color": rec.get("openAccessColor")},
        )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        params = f"search={urllib.parse.quote(query)}&type=publication&pageSize={min(limit, 100)}"
        if oa_only:
            params += "&bestOpenAccessRightLabel=OPEN"
        return [self._hit(r) for r in self._results(params)]

    def locate(self, ids: Ids) -> list[Location]:
        if not ids.get("doi"):
            return []
        for rec in self._results(f"pid={urllib.parse.quote(ids['doi'], safe='/')}"):
            dois = {
                clean_doi(p.get("value"))
                for p in rec.get("pids") or []
                if (p.get("scheme") or "").lower() == "doi"
            }
            if ids["doi"] in dois:
                return self._locations(rec)
        return []
