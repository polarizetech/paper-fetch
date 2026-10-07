"""What a provider is, so any one of them can be added, removed or swapped by name.

A provider does at most two things, and says which:

- **search**: turn a query into `Hit`s (metadata, identifiers, open-access locations if known)
- **locate**: given identifiers for a known paper, return the `Location`s of legal full-text
  copies it knows about

Retrieval, validation, text extraction and storage are the library's job, not a provider's. That is
what makes providers swappable: a provider never decides what counts as "full text", never writes
to the store, and never marks anything as read.

## The legal line lives here, per location

A provider returns a `Location` **only for copies it itself reports as open access**: Europe PMC's
`isOpenAccess`, PMC's `is_pmc_openaccess` (or an NIH author manuscript under PMC's TDM licence),
OpenAlex's per-location `is_oa`, Unpaywall's `best_oa_location`, a preprint server's own copy. A
provider with no way to say a copy is open returns nothing. So there is no code path that follows
a paywall, whichever providers are enabled.

## Failure is per provider, and loud

A 429 or a missing required key makes ONE provider `unavailable`, reported by name. It never
empties a federated search silently: "nothing found" and "the service refused us" are different
answers, and only the first is about the literature.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from ..http import HttpClient, Response

__all__ = [
    "PAUSE_OUTAGE_S",
    "PAUSE_QUOTA_S",
    "PAUSE_RATE_LIMIT_S",
    "Hit",
    "Ids",
    "Location",
    "Provider",
    "ProviderBroken",
    "ProviderUnavailable",
    "clean_doi",
]

#: Identifiers keyed by scheme: doi, pmid, pmcid, openalex, arxiv, plus provider-specific ones.
Ids = dict[str, Any]


#: How long a provider is left alone after it fails, in seconds (`Library` keeps the clock).
PAUSE_OUTAGE_S = 60.0  # unreachable, a 5xx, a refusal: ask again soon
PAUSE_RATE_LIMIT_S = 300.0  # a 429: asking again at once is how a limit becomes a ban
PAUSE_QUOTA_S = float("inf")  # a spent allowance: not again in this process


class ProviderUnavailable(RuntimeError):
    """An OUTSIDE problem: the service is down, refused us, rate-limited us or has no allowance
    left. Reported per provider, never swallowed, and never a reason to stop: the other providers
    have answered. `pause_s` says how long to leave the provider alone (None: `PAUSE_OUTAGE_S`).
    """

    def __init__(self, message: str, *, pause_s: float | None = None) -> None:
        super().__init__(message)
        self.pause_s = pause_s


class ProviderBroken(RuntimeError):
    """A DEFECT, here or in how we use the service: it rejected what we sent, or answered in a
    shape this code does not read. Waiting will not fix it, so it is not `unavailable`: it is
    reported as `error`, named in the search's `broken` list and logged with its traceback
    (`problems.py`), so it gets fixed instead of being read as "nothing found".
    """


def clean_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip()
    for pre in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.lower().startswith(pre):
            d = d[len(pre) :]
    return d.lower().rstrip(".,;") or None


@dataclass
class Location:
    url: str
    fmt: str  # "pdf" | "jats-xml" | "tei-xml" | "txt"
    provider: str
    license: str | None = None
    version: str | None = None
    md5: str | None = None  # when the provider publishes one, the download is verified
    note: str = ""


@dataclass
class Hit:
    provider: str
    title: str | None
    year: int | None
    ids: Ids  # doi, pmid, pmcid, openalex, arxiv -- normalised
    is_oa: bool | None
    authors: list[str | None] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Hit:
        d = dict(d)
        d["locations"] = [Location(**loc) for loc in d.get("locations", [])]
        return cls(**d)


class Provider:
    name: str = ""
    label: str = ""
    can_search: ClassVar[bool] = False
    can_locate: ClassVar[bool] = False
    needs: tuple[str, ...] = ()  # environment variables that MUST be set
    recommends: tuple[str, ...] = ()  # environment variables that raise limits
    min_interval_s: float = 0.0  # polite spacing between this provider's requests
    timeout_s: float = 20.0  # one slow provider must not stall a federated search
    terms: str = ""  # one line: what the service allows and what it costs
    cacheable: bool = True  # False where the service's terms on storing results are unchecked
    metered: bool = False  # True where calls are rationed: asked the query as written, once

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self._last = 0.0
        self.calls = 0
        # Requests are spaced per provider. Two threads working out their wait from the same
        # `_last` would both go at once, which is how a 10-per-minute limit gets broken.
        self._pace = threading.Lock()

    def available(self) -> tuple[bool, str]:
        missing = [k for k in self.needs if not os.environ.get(k)]
        return (False, f"needs {', '.join(missing)}") if missing else (True, "")

    def _get(self, url: str, headers: dict[str, str] | None = None) -> Response:
        with self._pace:
            wait = self.min_interval_s - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            self.calls += 1
        try:
            r = self.http.get(url, headers, timeout=self.timeout_s, retries=1)
        except ConnectionError as e:
            raise ProviderUnavailable(f"{self.name} unreachable: {str(e)[-120:]}") from e
        if r.status == 429:
            hint = (
                f"; set {', '.join(self.recommends)} for higher limits" if self.recommends else ""
            )
            raise ProviderUnavailable(
                f"{self.name} rate-limited us (HTTP 429){hint}", pause_s=PAUSE_RATE_LIMIT_S
            )
        return r

    def _answered(self, r: Response) -> Response:
        """The answer to a QUERY, or the reason there is none.

        A query endpoint answers 200 with an empty list when nothing matches, so anything else is
        a failure, and returning `[]` for it would report an outage or a defect as "no results".
        A 5xx or a 403 is the service's side (`ProviderUnavailable`); a 401 or any other 4xx means
        it rejected our key or our request (`ProviderBroken`). Not for lookups by identifier,
        where a 404 is an honest "not known".
        """
        if r.status == 200:
            return r
        if r.status >= 500 or r.status == 403:
            raise ProviderUnavailable(f"{self.name} answered HTTP {r.status}")
        detail = r.body[:160].decode("utf-8", "replace").strip()
        raise ProviderBroken(f"{self.name} rejected our request (HTTP {r.status}): {detail}")

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        raise NotImplementedError(f"{self.name} does not search")

    def locate(self, ids: Ids) -> list[Location]:  # the default knows no copies
        return []

    def download(self, loc: Location) -> bytes | None:
        r = self._get(loc.url)
        return r.body if r.status == 200 else None

    def describe(self) -> dict[str, Any]:
        ok, why = self.available()
        return {
            "name": self.name,
            "label": self.label,
            "search": self.can_search,
            "locate": self.can_locate,
            "needs": list(self.needs),
            "recommends": [k for k in self.recommends if not os.environ.get(k)],
            "available": ok,
            "why": why,
            "terms": self.terms,
        }
