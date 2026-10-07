"""OpenAlex: discovery, and (with an API key) OpenAlex's own copies of open-access full texts.

## Observed against the live API, not taken from documentation (September 2026)

- **Usage is metered in USD.** Every response carries `x-ratelimit-cost-usd`,
  `x-ratelimit-remaining-usd` and `x-ratelimit-limit-usd`. Without a key the daily allowance was
  **$0.10**, a `search` cost **$0.001**, and a single-work lookup cost **$0**. So lookups are
  free and searches are not, which is why `Library` checks its own index before either, and
  caches searches.
- **`search=` searches FULL TEXT.** The echoed query was `fulltext.search:<terms>`.
- **`best_oa_location.pdf_url` is frequently null even for gold OA** (a PLOS paper had no
  `pdf_url` on any of its five locations), so OpenAlex alone does not deliver a PDF link.
- **The content API** (`content.openalex.org/works/{W}.pdf` and `.grobid-xml`) serves OpenAlex's
  cached copies, flagged per work by `has_content = {pdf, grobid_xml}`. It returns
  **401 "API key required"** without a key.
- **A WRONG key breaks even the free endpoints**: `api.openalex.org` answered 401 to an invalid
  `api_key`. So a bad key raises here rather than being silently ignored: falling back to
  unauthenticated calls would hide a misconfiguration behind a working-looking tool.

The key is read from `OPENALEX_API_KEY`. A contact email for the polite pool is sent **only** if
`PAPER_FETCH_EMAIL` is set; nothing here carries a default address.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .http import Http, HttpClient
from .ids import Ident

__all__ = [
    "BASE",
    "CONTENT",
    "BadApiKey",
    "NeedsApiKey",
    "OpenAlex",
    "OpenAlexUnavailable",
    "Usage",
]

BASE = "https://api.openalex.org"
CONTENT = "https://content.openalex.org"

#: The work fields this library keeps. `select=` also makes a lookup cheaper to transfer.
WORK_FIELDS = (
    "id,doi,ids,title,display_name,publication_year,publication_date,type,"
    "authorships,primary_location,locations,best_oa_location,open_access,"
    "has_content,is_retracted,cited_by_count,abstract_inverted_index"
)


class BadApiKey(RuntimeError):
    pass


class NeedsApiKey(RuntimeError):
    pass


class OpenAlexUnavailable(RuntimeError):
    """OpenAlex is out of allowance or failing on its side: an outside problem, not a defect.
    `pause_s` is how long to leave it alone."""

    def __init__(self, message: str, *, pause_s: float | None = None) -> None:
        super().__init__(message)
        self.pause_s = pause_s


@dataclass
class Usage:
    cost_usd: float | None = None
    remaining_usd: float | None = None
    limit_usd: float | None = None
    remaining_calls: int | None = None


def _f(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class OpenAlex:
    def __init__(
        self,
        http: HttpClient | None = None,
        api_key: str | None = None,
        email: str | None = None,
    ) -> None:
        self.http: HttpClient = http or Http()
        self.api_key = api_key if api_key is not None else os.environ.get("OPENALEX_API_KEY")
        self.email = email if email is not None else os.environ.get("PAPER_FETCH_EMAIL")
        self.usage = Usage()
        self.spent_usd = 0.0

    # -- plumbing ---------------------------------------------------------------------------

    def _params(self, extra: dict[str, Any] | None = None) -> str:
        p = dict(extra or {})
        if self.api_key:
            p["api_key"] = self.api_key
        if self.email:
            p["mailto"] = self.email
        return urllib.parse.urlencode(p)

    def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        r = self.http.get(f"{BASE}{path}?{self._params(params)}")
        h = r.headers
        remaining = h.get("x-ratelimit-remaining")
        self.usage = Usage(
            _f(h.get("x-ratelimit-cost-usd")),
            _f(h.get("x-ratelimit-remaining-usd")),
            _f(h.get("x-ratelimit-limit-usd")),
            int(remaining) if remaining and remaining.isdigit() else None,
        )
        self.spent_usd += self.usage.cost_usd or 0.0
        if r.status == 401:
            raise BadApiKey(
                "OpenAlex answered 401. With OPENALEX_API_KEY set this means the key is wrong -- "
                "and a wrong key breaks even free lookups, so this refuses rather than falling "
                "back to unauthenticated calls that would hide the misconfiguration."
            )
        if r.status == 404:
            return None
        if r.status == 429:
            # The allowance is per day, so asking again within the hour only spends requests.
            raise OpenAlexUnavailable(
                f"OpenAlex rate limit reached (remaining ${self.usage.remaining_usd})",
                pause_s=3600.0,
            )
        if r.status >= 500:
            raise OpenAlexUnavailable(f"OpenAlex answered HTTP {r.status} on {path}")
        if r.status != 200:
            raise RuntimeError(f"OpenAlex {r.status} on {path}: {r.body[:200]!r}")
        return json.loads(r.body)

    # -- lookups (free) -----------------------------------------------------------------------

    def work(self, ident: Ident) -> dict[str, Any] | None:
        """The OpenAlex work for an openalex/doi/pmid identifier, or None if OpenAlex has none."""
        paths = {
            "openalex": f"/works/{ident.value}",
            "doi": f"/works/doi:{ident.value}",
            "pmid": f"/works/pmid:{ident.value}",
            "pmcid": f"/works/pmcid:{ident.value}",
        }
        if ident.kind not in paths:
            raise ValueError(f"OpenAlex cannot be asked by {ident.kind}")
        return self._get_json(paths[ident.kind], {"select": WORK_FIELDS})

    # -- search (costs money) -----------------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        filters: dict[str, str] | None = None,
        per_page: int = 25,
        page: int = 1,
    ) -> dict[str, Any]:
        f = ",".join(f"{k}:{v}" for k, v in (filters or {}).items())
        params: dict[str, Any] = {
            "search": query,
            "per_page": per_page,
            "page": page,
            "select": "id,doi,display_name,publication_year,authorships,open_access,"
            "has_content,cited_by_count,is_retracted",
        }
        if f:
            params["filter"] = f
        return self._get_json("/works", params) or {"results": [], "meta": {}}

    # -- content (needs a key) ----------------------------------------------------------------

    def content(self, work_id: str, kind: str) -> bytes | None:
        if kind not in ("pdf", "grobid-xml"):
            raise ValueError("kind is 'pdf' or 'grobid-xml'")
        if not self.api_key:
            raise NeedsApiKey(
                "OpenAlex content downloads require OPENALEX_API_KEY "
                "(free at https://openalex.org/users)."
            )
        r = self.http.get(f"{CONTENT}/works/{work_id}.{kind}?{self._params()}")
        if r.status == 401:
            raise BadApiKey("content.openalex.org rejected OPENALEX_API_KEY")
        return r.body if r.status == 200 else None
