"""Where a legal full text comes from: ask each provider in order, validate every candidate.

## The legal line

Providers return `Location`s only for copies they themselves report as open access (see
`providers/base.py`). So a work with no open copy anywhere ends here with **no download attempted**
and every provider's answer recorded. There is no paywall route, whichever providers are enabled.
A copy the operator legitimately holds enters through `Library.add_local` with its rights declared.

Open access is not a redistribution licence (many OA copies carry no licence at all), so every
stored copy records the licence its provider reported, and the store is private.

## What a candidate must survive before it counts

- **md5**, when the provider publishes one (the PMC bucket does, per file)
- **not an HTML page**: a "PDF" link that returns a login screen is the commonest way a library
  silently fills with junk
- **format**: a PDF must begin `%PDF-`, XML must parse
- **readable text**: `looks_like_prose`, which the output of a byte-level PDF "extraction" fails
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field

from .openalex import BadApiKey
from .providers import Ids, Location, Provider, ProviderUnavailable
from .text import is_html, is_pdf, looks_like_prose, pdf_text, xml_text

__all__ = ["Attempt", "FullText", "Resolution", "resolve"]

#: (route, outcome), e.g. ("pmc-s3:jats-xml", "ok (md5 verified, text readable)").
Attempt = tuple[str, str]

REFUSED = (
    "No enabled provider reported an open-access copy. This library retrieves OA copies only; "
    "a copy you legitimately hold goes in through Library.add_local with its rights declared."
)


@dataclass
class FullText:
    fmt: str
    data: bytes
    route: str
    url: str
    license: str | None
    version: str | None
    text: str | None
    text_method: str
    text_ok: bool
    text_reason: str
    md5_verified: bool | None = None


@dataclass
class Resolution:
    fulltext: FullText | None
    attempts: list[Attempt] = field(default_factory=list)
    refused: str | None = None


def _accept(loc: Location, data: bytes | None, attempts: list[Attempt]) -> FullText | None:
    route = f"{loc.provider}:{loc.fmt}"
    if not data:
        attempts.append((route, "no body"))
        return None
    md5_ok = None
    if loc.md5:
        md5_ok = hashlib.md5(data, usedforsecurity=False).hexdigest() == loc.md5
        if not md5_ok:
            attempts.append(
                (route, "md5 mismatch against the provider's published checksum -- refused")
            )
            return None
    if is_html(data):
        attempts.append((route, "returned an HTML page, not the paper -- refused"))
        return None
    if loc.fmt == "pdf":
        if not is_pdf(data):
            attempts.append((route, "body is not a PDF (%PDF- missing) -- refused"))
            return None
        text, method = pdf_text(data)
    elif loc.fmt == "txt":
        text, method = data.decode("utf-8", "replace"), "provider-text"
    else:
        text, method = xml_text(data)
        if text is None:
            attempts.append((route, method))
            return None
    ok, reason = looks_like_prose(text)
    verified = "md5 verified, " if md5_ok else ""
    readable = "readable" if ok else "NOT readable: " + reason
    attempts.append((route, f"ok ({verified}text {readable})"))
    return FullText(
        loc.fmt,
        data,
        route,
        loc.url,
        loc.license,
        loc.version,
        text if ok else None,
        method,
        ok,
        reason,
        md5_ok,
    )


def resolve(ids: Ids, providers: Sequence[Provider]) -> Resolution:
    """Ask each provider in turn; return the first candidate whose text passes every check."""
    attempts: list[Attempt] = []
    found_any = False
    for p in providers:
        ok, why = p.available()
        if not ok:
            attempts.append((p.name, f"skipped: {why}"))
            continue
        try:
            locs = p.locate(ids)
        except BadApiKey:
            raise
        except ProviderUnavailable as e:
            attempts.append((p.name, f"unavailable: {e}"))
            continue
        except Exception as e:  # noqa: BLE001 -- one broken provider must not stop the others
            attempts.append((p.name, f"error: {type(e).__name__}: {str(e)[:120]}"))
            continue
        if not locs:
            attempts.append((p.name, "no open-access copy known"))
            continue
        found_any = True
        for loc in locs:
            try:
                data = p.download(loc)
            except BadApiKey:
                raise
            except Exception as e:  # noqa: BLE001 -- recorded; the next location is tried
                attempts.append((f"{p.name}:{loc.fmt}", f"download failed: {type(e).__name__}"))
                continue
            ft = _accept(loc, data, attempts)
            if ft and ft.text_ok:
                return Resolution(ft, attempts)
    return Resolution(None, attempts, None if found_any else REFUSED)
