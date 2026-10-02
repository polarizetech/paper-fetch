"""One identifier, one canonical form.

A library that stores a paper twice under two spellings of its DOI has a deduplication problem
that looks exactly like a working cache.

DOIs are case-insensitive by specification, so they are lowercased. OpenAlex work IDs are
upper-case `W` + digits. PMIDs and PMCIDs need an explicit prefix: a bare number is refused,
because `12345678` is a valid PMID and could also be half of anything else.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

__all__ = ["Ident", "doi_key", "normalize", "pmcid_from_openalex"]

_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)
_W = re.compile(r"^W\d+$", re.I)
_PMCID = re.compile(r"^PMC\d+$", re.I)
_ARXIV = re.compile(r"^(\d{4}\.\d{4,5}|[a-z\-]+(\.[A-Z]{2})?/\d{7})(v\d+)?$", re.I)


class Ident(tuple[str, str]):
    """(kind, value). kind is 'openalex' | 'doi' | 'pmid' | 'pmcid' | 'arxiv'."""

    __slots__ = ()

    def __new__(cls, kind: str, value: str) -> Ident:
        return super().__new__(cls, (kind, value))

    @property
    def kind(self) -> str:
        return self[0]

    @property
    def value(self) -> str:
        return self[1]

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def _strip_prefix(s: str, prefixes: tuple[str, ...]) -> str:
    low = s.lower()
    for pre in prefixes:
        if low.startswith(pre):
            return s[len(pre) :]
    return s


_KEYED = re.compile(r"^(doi|pmcid|pmid|arxiv)-(.+)$", re.I)


def normalize(identifier: str) -> Ident:
    """Parse a DOI, OpenAlex ID, `pmid:N`, PMCID or `arxiv:ID` into its canonical `Ident`.

    Also accepts the work ids the library itself mints when OpenAlex has no record
    (`doi-10.1214%2F12-aoas565supp`, `pmcid-PMC5519140`, `pmid-123`, `arxiv-...`): a work the
    library created must be accepted by every command that takes a work id.

    Raises ValueError for anything else, including a bare number.
    """
    s = (identifier or "").strip()
    if not s:
        raise ValueError("empty identifier")
    keyed = _KEYED.match(s)
    if keyed:
        kind, rest = keyed.group(1).lower(), urllib.parse.unquote(keyed.group(2))
        s = f"{kind}:{rest}" if kind in ("pmid", "arxiv") else rest
    s = _strip_prefix(s, ("https://openalex.org/", "http://openalex.org/", "openalex:"))
    if _W.match(s):
        return Ident("openalex", "W" + s[1:])
    s = _strip_prefix(s, ("https://doi.org/", "http://doi.org/", "https://dx.doi.org/", "doi:"))
    if re.match(r"^10\.\d{4,9}%2F", s, re.I):  # a DOI as one object-key segment (doi_key)
        s = urllib.parse.unquote(s)
    low = s.lower()
    if _DOI.match(s):
        return Ident("doi", low.rstrip(".,;"))
    if low.startswith("pmid:"):
        v = s[5:].strip()
        if v.isdigit():
            return Ident("pmid", v)
    if low.startswith("arxiv:"):
        v = s[6:].strip()
        if _ARXIV.match(v):
            return Ident("arxiv", re.sub(r"v\d+$", "", v))
    if low.startswith("pmcid:"):
        s = s[6:].strip()
    if _PMCID.match(s):
        return Ident("pmcid", "PMC" + s[3:])
    raise ValueError(
        f"unrecognised identifier {identifier!r}. Use a DOI (10.xxxx/...), an OpenAlex ID (W123), "
        "'pmid:123', 'PMC123' or 'arxiv:2401.12345'. A bare number is refused on purpose."
    )


def doi_key(doi: str) -> str:
    """A DOI as one safe object-key segment. Lowercased first: DOIs are case-insensitive."""
    return urllib.parse.quote(doi.lower(), safe="")


def pmcid_from_openalex(ids: dict[str, Any] | None) -> str | None:
    """OpenAlex writes PMCIDs as URLs whose last segment may or may not carry the 'PMC' prefix."""
    raw = (ids or {}).get("pmcid")
    if not raw:
        return None
    tail = raw.rstrip("/").rsplit("/", 1)[-1]
    tail = tail[3:] if tail.upper().startswith("PMC") else tail
    return f"PMC{tail}" if tail.isdigit() else None
