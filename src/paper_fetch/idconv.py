"""Fill in missing identifiers before any provider is asked for a copy.

## Why this exists

OpenAlex records sometimes carry a PMID but no PMCID. Without the PMCID the PMC bucket (the most
verifiable route: anonymous, md5 per file) is never asked, and the library would report "no
open-access copy known" for a paper that is in PMC. NCBI's ID converter fills the gap, and it
also knows the PMCIDs of preprints that PMC hosts.

The converter asks for `tool` and `email`; the email is sent only if `PAPER_FETCH_EMAIL` is set.

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
    params = {"ids": q, "format": "json", "tool": "paper-fetch"}
    if os.environ.get("PAPER_FETCH_EMAIL"):
        params["email"] = os.environ["PAPER_FETCH_EMAIL"]
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


EPMC = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def europepmc_meta(ids: dict[str, Any], http: HttpClient) -> dict[str, Any] | None:
    """Title, year, first authors and ids for a paper OpenAlex does not know, from Europe PMC.

    Looked up by PMCID, else DOI, else PMID. None when nothing is found or the service fails:
    never fatal.
    """
    if ids.get("pmcid"):
        q = f"PMCID:{ids['pmcid']}"
    elif ids.get("doi"):
        q = f'DOI:"{ids["doi"]}"'
    elif ids.get("pmid"):
        q = f"EXT_ID:{ids['pmid']} AND SRC:MED"
    else:
        return None
    params = urllib.parse.urlencode({"query": q, "format": "json", "resultType": "core"})
    try:
        r = http.get(f"{EPMC}?{params}", timeout=15, retries=1)
        if r.status != 200:
            return None
        rows = json.loads(r.body).get("resultList", {}).get("result", [])
    except Exception:  # noqa: BLE001 -- enrichment is never fatal
        return None
    if not rows:
        return None
    x = rows[0]
    year = str(x.get("pubYear") or "")
    return {
        "title": x.get("title"),
        "year": int(year) if year.isdigit() else None,
        "authors": [a for a in (x.get("authorString") or "").rstrip(".").split(", ")[:3] if a],
        "doi": (x.get("doi") or "").lower() or None,
        "pmid": x.get("pmid"),
        "pmcid": x.get("pmcid"),
    }
