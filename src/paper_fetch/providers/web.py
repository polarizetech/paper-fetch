"""Web search: the fallback for when the scholarly indexes find nothing open.

The scholarly providers only see what an index has catalogued. A university repository PDF, a
lab page hosting an accepted manuscript, or a preprint on a server none of them crawl can still
be on the open web. This provider asks a general web search engine and turns what comes back
into `Hit`s, parsing DOIs, PMIDs, PMCIDs and arXiv IDs out of the result URL and snippet.

## Backend: a SearXNG instance (`SEARXNG_URL`) with the JSON format enabled

Run your own instance (for example the official Docker image) and enable `json` under
`search.formats` in its `settings.yml`. We recommend disabling SearXNG's Google engines: Google
Scholar in particular has no API and its terms forbid automated querying, and a meta-search engine
configured to scrape it inherits that problem.

## What a web hit is, and is not

- **Search only, never a copy source.** `can_locate` is False. A PDF link from a web page is not
  evidence of an open licence, so nothing found here is downloaded unless an identifier it
  carries is later fetched through the normal providers, which decide openness themselves.
- **Identifiers are PARSED, not resolved.** A DOI pulled out of a URL is a lead. `ids_parsed` in
  `extra` says so; `fetch()` resolves it against OpenAlex before anything is stored.
- **`is_oa` is None**: unknown, never guessed.
- **Not cached** (`cacheable = False`): what a result page may be stored as depends on the engines
  SearXNG queried, and that has not been checked.
"""

from __future__ import annotations

import json
import os
import re
import urllib.parse

from .base import Hit, Ids, Provider, ProviderUnavailable, clean_doi

__all__ = ["WebSearch", "parse_ids"]

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>&#?]+)", re.I)
_DOI_TAIL = re.compile(r"(/(full|abstract|pdf|epdf|html|fulltext|meta)|\.pdf|\.html?)$", re.I)
_PMID = re.compile(r"pubmed\.ncbi\.nlm\.nih\.gov/(\d{5,9})")
_PMCID = re.compile(r"\b(PMC\d{4,9})\b", re.I)
_ARXIV = re.compile(r"arxiv\.org/(?:abs|pdf)/([a-z\-]+/\d{7}|\d{4}\.\d{4,5})", re.I)
_TAG = re.compile(r"<[^>]+>")


def parse_ids(url: str, text: str = "") -> Ids:
    """Identifiers found in a result's URL (preferred) or snippet. Unverified by construction."""
    u = urllib.parse.unquote(url or "")
    ids: Ids = {}
    m = _DOI.search(u) or _DOI.search(text or "")
    if m:
        d = m.group(1).rstrip(".,;)")
        while _DOI_TAIL.search(d):
            d = _DOI_TAIL.sub("", d)
        ids["doi"] = clean_doi(d)
    if m := _PMID.search(u):
        ids["pmid"] = m.group(1)
    if m := _PMCID.search(u):
        ids["pmcid"] = m.group(1).upper()
    if m := _ARXIV.search(u):
        ids["arxiv"] = m.group(1)
    return ids


class WebSearch(Provider):
    name, label = "web", "Web search (SearXNG) -- fallback"
    can_search, can_locate = True, False
    cacheable = False
    needs = ("SEARXNG_URL",)
    min_interval_s = 1.0
    timeout_s = 30.0
    terms = (
        "SearXNG meta-search; opt-in by instance URL. Results are leads: identifiers parsed from "
        "URLs, openness unknown, nothing downloaded from here. Not Google Scholar."
    )

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        ok, why = self.available()
        if not ok:
            raise ProviderUnavailable(f"web search not configured: {why}")
        base = os.environ["SEARXNG_URL"].rstrip("/")
        r = self._get(
            f"{base}/search?q={urllib.parse.quote(query)}&format=json",
            {"Accept": "application/json"},
        )
        if r.status == 403:
            raise ProviderUnavailable(
                "web (searxng) refused format=json -- enable it in settings.yml"
            )
        if r.status != 200:
            raise ProviderUnavailable(f"web (searxng) HTTP {r.status}")
        body = json.loads(r.body)
        dead = [e[0] for e in body.get("unresponsive_engines") or [] if e]
        out = []
        for x in (body.get("results") or [])[:limit]:
            url = x.get("url") or ""
            snippet = _TAG.sub("", x.get("content") or "")
            ids = parse_ids(url, snippet)
            out.append(
                Hit(
                    self.name,
                    _TAG.sub("", x.get("title") or "") or None,
                    None,
                    ids,
                    None,
                    [],
                    [],
                    {
                        "url": url,
                        "snippet": snippet[:300],
                        "engines": x.get("engines") or [],
                        "unresponsive_engines": dead,
                        "ids_parsed": bool(ids),
                    },
                )
            )
        if not out and dead:
            raise ProviderUnavailable(
                f"web (searxng) returned nothing; unresponsive engines: {', '.join(dead)}"
            )
        return out
