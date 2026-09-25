"""Fill in missing identifiers before any provider is asked for a copy.

## Why this exists

OpenAlex records sometimes carry a PMID but no PMCID. Without the PMCID the PMC bucket (the most
verifiable route: anonymous, md5 per file) is never asked, and the library would report "no
open-access copy known" for a paper that is in PMC. NCBI's ID converter fills the gap, and it
also knows the PMCIDs of preprints that PMC hosts.

The converter asks for `tool` and `email`; the email is sent only if `PAPERLIB_EMAIL` is set.

A failure here is never fatal: identifiers are an optimisation for the providers, not a gate.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

from .http import HttpClient

__all__ = ["URL", "enrich"]

URL = "https://pmc.ncbi.nlm.nih.gov/tools/idconv/api/v1/articles/"


def enrich(ids: dict[str, Any], http: HttpClient) -> tuple[dict[str, Any], str]:
    """Return (ids with any missing pmcid/pmid/doi filled, a one-line note for provenance)."""
    if ids.get("pmcid") and ids.get("pmid") and ids.get("doi"):
        return ids, "complete"
    q = ids.get("doi") or ids.get("pmid") or ids.get("pmcid")
    if not q:
        return ids, "nothing to convert from"
    params = {"ids": q, "format": "json", "tool": "paperlib"}
    if os.environ.get("PAPERLIB_EMAIL"):
        params["email"] = os.environ["PAPERLIB_EMAIL"]
    try:
        r = http.get(f"{URL}?{urllib.parse.urlencode(params)}", timeout=15, retries=1)
    except Exception as e:  # noqa: BLE001 -- never fatal, recorded in provenance instead
        return ids, f"ncbi idconv unreachable: {type(e).__name__}"
    if r.status != 200:
        return ids, f"ncbi idconv HTTP {r.status}"
    try:
        recs = json.loads(r.body).get("records") or []
    except ValueError:
        return ids, "ncbi idconv: unparseable response"
    if not recs or recs[0].get("status") == "error":
        return ids, "ncbi idconv: no record"
    rec, filled = recs[0], []
    out = dict(ids)
    for k in ("pmcid", "pmid", "doi"):
        v = rec.get(k)
        if v and not out.get(k):
            out[k] = str(v).lower() if k == "doi" else str(v)
            filled.append(k)
    return out, f"ncbi idconv filled {', '.join(filled)}" if filled else "ncbi idconv: nothing new"
