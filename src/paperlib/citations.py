"""OpenCitations Index: who cites a paper, and what it cites.

    GET https://api.opencitations.net/index/v2/citations/doi:<doi>        incoming
    GET https://api.opencitations.net/index/v2/references/doi:<doi>       outgoing
    GET https://api.opencitations.net/index/v2/citation-count/doi:<doi>

This is not a provider. A provider finds papers by query or locates an open copy; OpenCitations does
neither. It answers a different question (the citation graph around a paper you already have an
identifier for), so it sits beside the providers as `Library.citations()` / `Library.references()`.

Observed against the live API (September 2026):

- **No key needed.** An access token is optional (sent in the `authorization` header, read from
  `OPENCITATIONS_ACCESS_TOKEN`) and the service asks applications to send one. Documented limit:
  **180 requests/minute per IP**. A WRONG token gets **403 "Invalid token"** even where none is
  needed, so it raises by name.
- A well-cited paper's full citation list (600+ rows) arrives in a few seconds.
- **A request can stall for a minute** while an identical retry answers in under a second, so
  requests carry a 60 s timeout and one retry, and a failure is raised by name rather than returned
  as zero citations.
- Each row names both works as a space-separated identifier list
  (`omid:br/061101803237 doi:10.1371/journal.pcbi.1003285 openalex:W2036318837 pmid:24204232`),
  plus `creation` (the CITING work's date, returned here as `citing_date`), `timespan`, and
  journal/author self-citation flags.
- Identifier schemes accepted: DOI, PMID, OMID (ISSN for venues).

What it does not carry: citation intent (supporting / contrasting). A citation is not agreement.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
from typing import Any

from .http import HttpClient
from .providers.base import Ids, ProviderUnavailable, clean_doi

__all__ = ["BASE", "CitationsUnavailable", "OpenCitations", "parse_pids"]

BASE = "https://api.opencitations.net/index/v2"
DIRECTIONS = ("citations", "references")


class CitationsUnavailable(ProviderUnavailable):
    """OpenCitations refused or did not answer. Never reported as zero citations."""


def parse_pids(field: str | None) -> Ids:
    """'omid:br/1 doi:10.1/X openalex:W2 pmid:3' -> {'omid': 'br/1', 'doi': '10.1/x', ...}"""
    ids: Ids = {}
    for tok in (field or "").split():
        scheme, _, value = tok.partition(":")
        if not value:
            continue
        scheme = scheme.lower()
        if scheme == "doi":
            ids["doi"] = clean_doi(value)
        elif scheme in ("pmid", "openalex", "omid"):
            ids[scheme] = value
    return ids


class OpenCitations:
    name = "opencitations"
    min_interval_s = 60 / 180 + 0.02  # documented 180 requests/minute per IP
    timeout_s = 60.0

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._last = 0.0
        self.calls = 0

    def _get(self, path: str) -> Any:
        wait = self.min_interval_s - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.calls += 1
        headers = {"Accept": "application/json"}
        token = os.environ.get("OPENCITATIONS_ACCESS_TOKEN")
        if token:
            headers["authorization"] = token
        try:
            r = self.http.get(f"{BASE}/{path}", headers, timeout=self.timeout_s, retries=1)
        except ConnectionError as e:
            raise CitationsUnavailable(f"opencitations unreachable: {str(e)[-120:]}") from e
        if r.status == 429:
            raise CitationsUnavailable(
                "opencitations rate-limited us (HTTP 429); documented limit "
                "180/min per IP; set OPENCITATIONS_ACCESS_TOKEN"
            )
        if r.status == 403 and token:
            # A wrong token gets 403 "Invalid token" even on endpoints that need none, so a bad
            # OPENCITATIONS_ACCESS_TOKEN breaks every call rather than being ignored.
            raise CitationsUnavailable(
                "opencitations rejected OPENCITATIONS_ACCESS_TOKEN (HTTP 403 "
                "'Invalid token'); fix or unset it"
            )
        if r.status != 200:
            raise CitationsUnavailable(f"opencitations HTTP {r.status} for {path}")
        return json.loads(r.body)

    @staticmethod
    def _id(ids: Ids) -> str:
        if ids.get("doi"):
            return "doi:" + urllib.parse.quote(ids["doi"], safe="/()._-;:")
        if ids.get("pmid"):
            return f"pmid:{ids['pmid']}"
        raise ValueError("OpenCitations needs a DOI or a PMID")

    def edges(self, ids: Ids, direction: str) -> list[dict[str, Any]]:
        """direction 'citations' (works citing this one) or 'references' (works this one cites)."""
        if direction not in DIRECTIONS:
            raise ValueError("direction must be 'citations' or 'references'")
        other = "citing" if direction == "citations" else "cited"
        # `creation` is the CITING work's publication date: on a references list every row
        # carries the same date (the paper's own), so it is named for what it is.
        return [
            {
                "ids": parse_pids(row.get(other)),
                "oci": row.get("oci"),
                "citing_date": row.get("creation"),
                "timespan": row.get("timespan"),
                "journal_self_citation": row.get("journal_sc") == "yes",
                "author_self_citation": row.get("author_sc") == "yes",
            }
            for row in self._get(f"{direction}/{self._id(ids)}")
        ]

    def count(self, ids: Ids, direction: str) -> int:
        if direction not in DIRECTIONS:
            raise ValueError("direction must be 'citations' or 'references'")
        path = "citation-count" if direction == "citations" else "reference-count"
        rows = self._get(f"{path}/{self._id(ids)}")
        return int(rows[0]["count"]) if rows else 0
