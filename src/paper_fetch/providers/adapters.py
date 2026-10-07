"""The scholarly-index adapters. Every behaviour noted below was observed against the live service
(September 2026) unless it says otherwise.

- `openalex`: search ($0.001 each) and locate (OA locations; OpenAlex's own cached copies when
  `OPENALEX_API_KEY` is set).
- `europepmc`: search (including bioRxiv/medRxiv preprints) and locate (OA JATS XML by PMCID).
- `pubmed`: search only, MeSH-aware, `free full text[sb]` when open access is asked for; it yields
  PMCIDs that the PMC routes then locate. Optional `NCBI_API_KEY`.
- `pmc-s3`: locate only. The PMC OA subset and NIH author manuscripts (TDM licence) on AWS Open
  Data: XML, text and PDF, each with an md5.
- `core`: search and locate (repository PDFs). Optional `CORE_API_KEY`. No licence field.
- `biorxiv`: locate only (the API has no keyword search): JATS XML and licence by DOI.
- `unpaywall`: locate only, best OA PDF by DOI. Needs `PAPER_FETCH_EMAIL` (Unpaywall requires it).

**Deliberately absent:**
- **Google Scholar**: no API, and its terms forbid automated querying. OpenAlex, OpenAIRE and
  CORE cover its discovery role.
- **Semantic Scholar and arXiv**: both answered HTTP 429 to unkeyed clients on essentially every
  request during testing, so they added latency and no papers. arXiv identifiers still normalise,
  and arXiv papers are reached through OpenAlex (`10.48550/arxiv.*` DOIs) and the aggregators.
- **Crossref as a full-text route**: its `link` entries are *text-mining* links under publisher
  TDM licences, which is not open access. Crossref stays a metadata source.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse
from typing import Any

from ..http import HttpClient
from ..openalex import CONTENT, OpenAlex, OpenAlexUnavailable
from .base import Hit, Ids, Location, Provider, ProviderUnavailable, clean_doi

__all__ = ["PMCS3", "Biorxiv", "Core", "EuropePMC", "OpenAlexProvider", "PubMed", "Unpaywall"]

_Q = urllib.parse.quote


def _authors(items: list[dict[str, Any]] | None, key: str = "name") -> list[str | None]:
    return [a.get(key) for a in (items or [])[:3]]


# ---------------------------------------------------------------------------------- OpenAlex


class OpenAlexProvider(Provider):
    name, label = "openalex", "OpenAlex"
    can_search = can_locate = True
    recommends = ("OPENALEX_API_KEY",)
    terms = "CC0 metadata. Usage metered in USD: $0.10/day unkeyed, search $0.001, lookup $0."

    def __init__(self, http: HttpClient, client: OpenAlex | None = None) -> None:
        super().__init__(http)
        self.client = client or OpenAlex(http=http)

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        try:
            raw = self.client.search(
                query, filters={"is_oa": "true"} if oa_only else None, per_page=limit
            )
        except OpenAlexUnavailable as e:
            raise ProviderUnavailable(str(e), pause_s=e.pause_s) from e
        out = []
        for w in raw.get("results", []):
            oa = w.get("open_access") or {}
            authors = [
                (a.get("author") or {}).get("display_name") for a in w.get("authorships") or []
            ]
            out.append(
                Hit(
                    self.name,
                    w.get("display_name"),
                    w.get("publication_year"),
                    {"doi": clean_doi(w.get("doi")), "openalex": w["id"].rsplit("/", 1)[-1]},
                    oa.get("is_oa"),
                    authors[:3],
                    extra={
                        "oa_status": oa.get("oa_status"),
                        "has_content": w.get("has_content"),
                        "is_retracted": w.get("is_retracted"),
                    },
                )
            )
        return out

    def locate(self, ids: Ids) -> list[Location]:
        work = ids.get("_openalex_work")
        if work is None:
            return []
        wid = work["id"].rsplit("/", 1)[-1]
        best = work.get("best_oa_location") or {}
        locs = []
        has = work.get("has_content") or {}
        if self.client.api_key and (work.get("open_access") or {}).get("is_oa"):
            for flag, ext, fmt in (("grobid_xml", "grobid-xml", "tei-xml"), ("pdf", "pdf", "pdf")):
                if has.get(flag):
                    locs.append(
                        Location(
                            f"{CONTENT}/works/{wid}.{ext}",
                            fmt,
                            self.name,
                            best.get("license"),
                            best.get("version"),
                            note="OpenAlex cached copy",
                        )
                    )
        rank = {"publishedVersion": 0, "acceptedVersion": 1, "submittedVersion": 2}
        open_pdfs = [
            loc for loc in work.get("locations") or [] if loc.get("is_oa") and loc.get("pdf_url")
        ]
        for loc in sorted(open_pdfs, key=lambda x: rank.get(x.get("version"), 9)):
            locs.append(
                Location(loc["pdf_url"], "pdf", self.name, loc.get("license"), loc.get("version"))
            )
        return locs

    def download(self, loc: Location) -> bytes | None:
        if loc.url.startswith(f"{CONTENT}/works/"):
            wid, _, ext = loc.url.rsplit("/", 1)[-1].partition(".")
            return self.client.content(wid, ext)  # carries the key; provenance keeps a clean URL
        return super().download(loc)


# ---------------------------------------------------------------------------------- Europe PMC


class EuropePMC(Provider):
    name, label = "europepmc", "Europe PMC"
    can_search = can_locate = True
    min_interval_s = 0.15
    terms = "Free REST API, no key. Indexes PubMed, PMC and preprints (bioRxiv, medRxiv)."
    BASE = "https://www.ebi.ac.uk/europepmc/webservices/rest"

    def _hits(self, q: str, limit: int) -> list[dict[str, Any]]:
        r = self._get(
            f"{self.BASE}/search?format=json&resultType=core&pageSize={limit}&query={_Q(q)}"
        )
        self._answered(r)
        return json.loads(r.body).get("resultList", {}).get("result", [])

    def _loc(self, x: dict[str, Any]) -> Location | None:
        if x.get("isOpenAccess") != "Y":
            return None
        preprint = x.get("source") == "PPR"
        rid = x.get("pmcid") or (x.get("id") if preprint and x.get("inEPMC") == "Y" else None)
        if not rid:
            return None
        return Location(
            f"{self.BASE}/{rid}/fullTextXML",
            "jats-xml",
            self.name,
            x.get("license"),
            "preprint" if preprint else "publishedVersion",
        )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        q = f"({query}) AND OPEN_ACCESS:y" if oa_only else query
        out = []
        for x in self._hits(q, limit):
            loc = self._loc(x)
            journal = ((x.get("journalInfo") or {}).get("journal") or {}).get("title")
            out.append(
                Hit(
                    self.name,
                    x.get("title"),
                    int(x["pubYear"]) if x.get("pubYear") else None,
                    {
                        "doi": clean_doi(x.get("doi")),
                        "pmid": x.get("pmid"),
                        "pmcid": x.get("pmcid"),
                        "europepmc": x.get("id"),
                    },
                    x.get("isOpenAccess") == "Y",
                    list((x.get("authorString") or "").split(", ")[:3]),
                    [loc] if loc else [],
                    extra={"source": x.get("source"), "journal": journal},
                )
            )
        return out

    def locate(self, ids: Ids) -> list[Location]:
        if ids.get("pmcid"):
            q = f"PMCID:{ids['pmcid']}"
        elif ids.get("doi"):
            q = f'DOI:"{ids["doi"]}"'
        else:
            return []
        return [loc for loc in (self._loc(x) for x in self._hits(q, 5)) if loc][:1]


# ---------------------------------------------------------------------------------- PubMed


class PubMed(Provider):
    name, label = "pubmed", "PubMed (NCBI E-utilities)"
    can_search = True
    recommends = ("NCBI_API_KEY",)
    min_interval_s = 0.34
    terms = (
        "NCBI E-utilities. 3 req/s unkeyed, 10 with NCBI_API_KEY. Finds papers; PMC holds copies."
    )
    BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

    def __init__(self, http: HttpClient) -> None:
        super().__init__(http)
        if os.environ.get("NCBI_API_KEY"):
            self.min_interval_s = 0.11

    @staticmethod
    def _p() -> str:
        p = {"tool": "paper-fetch"}
        if os.environ.get("PAPER_FETCH_EMAIL"):
            p["email"] = os.environ["PAPER_FETCH_EMAIL"]
        if os.environ.get("NCBI_API_KEY"):
            p["api_key"] = os.environ["NCBI_API_KEY"]
        return urllib.parse.urlencode(p)

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        term = f"({query}) AND free full text[sb]" if oa_only else query
        r = self._get(
            f"{self.BASE}/esearch.fcgi?db=pubmed&retmode=json&retmax={limit}"
            f"&term={_Q(term)}&{self._p()}"
        )
        self._answered(r)
        pmids = json.loads(r.body).get("esearchresult", {}).get("idlist", [])
        if not pmids:
            return []
        r = self._get(
            f"{self.BASE}/esummary.fcgi?db=pubmed&retmode=json&id={','.join(pmids)}&{self._p()}"
        )
        self._answered(r)
        res = json.loads(r.body).get("result", {})
        out = []
        for pmid in pmids:
            d = res.get(pmid) or {}
            aid = {a.get("idtype"): a.get("value") for a in d.get("articleids", [])}
            year = re.match(r"\d{4}", d.get("pubdate") or "")
            out.append(
                Hit(
                    self.name,
                    d.get("title"),
                    int(year.group()) if year else None,
                    {"pmid": pmid, "pmcid": aid.get("pmc"), "doi": clean_doi(aid.get("doi"))},
                    True if aid.get("pmc") else None,
                    _authors(d.get("authors")),
                    extra={"journal": d.get("fulljournalname")},
                )
            )
        return out


# ---------------------------------------------------------------------------------- PMC S3


class PMCS3(Provider):
    name, label = "pmc-s3", "PMC Open Access subset (AWS)"
    can_locate = True
    terms = "PMC OA subset on AWS Open Data, anonymous. Per-article licence; md5 per file."
    BASE = "https://pmc-oa-opendata.s3.amazonaws.com"

    @staticmethod
    def _version(key: str) -> int:
        m = re.search(r"\.(\d+)/", key)
        return int(m.group(1)) if m else 0

    def locate(self, ids: Ids) -> list[Location]:
        pmcid = ids.get("pmcid")
        if not pmcid:
            return []
        r = self._get(f"{self.BASE}/?list-type=2&max-keys=50&prefix={_Q(pmcid)}.")
        if r.status != 200:
            return []
        keys = re.findall(r"<Key>([^<]+\.json)</Key>", r.body.decode())
        if not keys:
            return []
        meta_r = self._get(f"{self.BASE}/{max(keys, key=self._version)}")
        if meta_r.status != 200:
            return []
        m = json.loads(meta_r.body)
        # Two kinds of record are readable here. The OA subset (`is_pmc_openaccess`) carries a reuse
        # licence. An NIH author manuscript (`is_manuscript`, `license_code: "TDM"`) is outside that
        # subset but is deposited in this bucket for text and data mining, and is free to read on
        # PMC. It is accepted and labelled so it cannot pass as a reuse licence: the library is
        # private, and TDM permits reading and mining, not republishing.
        tdm_manuscript = bool(m.get("is_manuscript")) and m.get("license_code") == "TDM"
        if not (m.get("is_pmc_openaccess") or tdm_manuscript):
            return []
        notes = ["retracted"] if m.get("is_retracted") else []
        if tdm_manuscript and not m.get("is_pmc_openaccess"):
            notes.append("PMC author manuscript, TDM licence: read and mine, not reuse")
        locs = []
        for field_, fmt in (("xml_url", "jats-xml"), ("pdf_url", "pdf"), ("text_url", "txt")):
            s3 = m.get(field_)
            if not s3:
                continue
            path, _, q = s3.replace("s3://pmc-oa-opendata/", "").partition("?")
            md5 = urllib.parse.parse_qs(q).get("md5", [None])[0]
            locs.append(
                Location(
                    f"{self.BASE}/{path}",
                    fmt,
                    self.name,
                    m.get("license_code"),
                    "manuscript" if m.get("is_manuscript") else "publishedVersion",
                    md5,
                    note="; ".join(notes),
                )
            )
        return locs


# ---------------------------------------------------------------------------------- CORE


class Core(Provider):
    name, label = "core", "CORE (open repository aggregator)"
    can_search = can_locate = True
    recommends = ("CORE_API_KEY",)
    min_interval_s = 1.0
    terms = "Aggregates OA repository copies. ~10 req/window unkeyed. Responses carry no licence."
    BASE = "https://api.core.ac.uk/v3/search/works/"

    @staticmethod
    def _headers() -> dict[str, str] | None:
        k = os.environ.get("CORE_API_KEY")
        return {"Authorization": f"Bearer {k}"} if k else None

    def _results(self, q: str, limit: int) -> list[dict[str, Any]]:
        r = self._get(f"{self.BASE}?q={_Q(q)}&limit={limit}", self._headers())
        self._answered(r)
        return json.loads(r.body).get("results", [])

    def _hit(self, x: dict[str, Any]) -> Hit:
        loc = (
            [
                Location(
                    x["downloadUrl"],
                    "pdf",
                    self.name,
                    note="repository copy; CORE reports no licence",
                )
            ]
            if x.get("downloadUrl")
            else []
        )
        return Hit(
            self.name,
            x.get("title"),
            x.get("yearPublished"),
            {
                "doi": clean_doi(x.get("doi")),
                "pmid": x.get("pubmedId"),
                "arxiv": x.get("arxivId"),
                "core": str(x.get("id")),
            },
            bool(loc),
            _authors(x.get("authors")),
            loc,
        )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        return [self._hit(x) for x in self._results(query, limit)]

    def locate(self, ids: Ids) -> list[Location]:
        if not ids.get("doi"):
            return []
        for x in self._results(f'doi:"{ids["doi"]}"', 3):
            if clean_doi(x.get("doi")) == ids["doi"] and x.get("downloadUrl"):
                return self._hit(x).locations
        return []


# ---------------------------------------------------------------------------- bioRxiv / medRxiv


class Biorxiv(Provider):
    name, label = "biorxiv", "bioRxiv / medRxiv"
    can_locate = True
    min_interval_s = 0.5
    terms = "Free API; no keyword search -- find preprints through europepmc or openalex."
    DOI_PREFIXES = ("10.1101/", "10.64898/")

    def locate(self, ids: Ids) -> list[Location]:
        doi = ids.get("doi") or ""
        if not doi.startswith(self.DOI_PREFIXES):
            return []
        for server in ("biorxiv", "medrxiv"):
            r = self._get(f"https://api.biorxiv.org/details/{server}/{doi}")
            if r.status != 200:
                continue
            coll = json.loads(r.body).get("collection") or []
            if coll:
                latest = max(coll, key=lambda c: int(c.get("version") or 0))
                if latest.get("jatsxml"):
                    return [
                        Location(
                            latest["jatsxml"],
                            "jats-xml",
                            self.name,
                            latest.get("license"),
                            f"preprint v{latest.get('version')}",
                            note=server,
                        )
                    ]
        return []


# ---------------------------------------------------------------------------------- Unpaywall


class Unpaywall(Provider):
    name, label = "unpaywall", "Unpaywall"
    can_locate = True
    needs = ("PAPER_FETCH_EMAIL",)
    min_interval_s = 0.1
    terms = "Free; requires a contact email on every request."

    def locate(self, ids: Ids) -> list[Location]:
        if not ids.get("doi"):
            return []
        email = _Q(os.environ["PAPER_FETCH_EMAIL"])
        r = self._get(f"https://api.unpaywall.org/v2/{_Q(ids['doi'])}?email={email}")
        if r.status != 200:
            return []
        loc = json.loads(r.body).get("best_oa_location") or {}
        if not loc.get("url_for_pdf"):
            return []
        return [
            Location(loc["url_for_pdf"], "pdf", self.name, loc.get("license"), loc.get("version"))
        ]
