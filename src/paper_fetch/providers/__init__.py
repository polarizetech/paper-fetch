"""The provider registry. Providers are named, ordered, and swappable without touching code.

    PAPER_FETCH_PROVIDERS=pmc-s3,europepmc,openalex          # locate order (where copies come from)
    PAPER_FETCH_SEARCH_PROVIDERS=europepmc,pubmed            # federated search set

An unknown name RAISES with the known list, rather than being dropped: a typo that silently
removed a provider would shrink every search with nothing on screen saying so.

Default locate order is cheapest-and-most-verifiable first: the PMC bucket (anonymous, md5 per
file, plain text already extracted), then Europe PMC, then PLOS and OpenAlex's own locations, then
the preprint servers, then repository aggregators.
"""

from __future__ import annotations

import os
from collections.abc import Sequence

from ..http import HttpClient
from ..openalex import OpenAlex
from .adapters import PMCS3, Biorxiv, Core, EuropePMC, OpenAlexProvider, PubMed, Unpaywall
from .base import Hit, Ids, Location, Provider, ProviderUnavailable, clean_doi
from .openaire import OpenAIRE
from .repositories import DOAJ, HAL, OSF, PLOS
from .scite import Scite
from .web import WebSearch

__all__ = [
    "DEFAULT_LOCATE",
    "DEFAULT_SEARCH",
    "DOAJ",
    "FALLBACK_SEARCH",
    "HAL",
    "OSF",
    "PLOS",
    "REGISTRY",
    "Hit",
    "Ids",
    "Location",
    "OpenAIRE",
    "Provider",
    "ProviderUnavailable",
    "Scite",
    "WebSearch",
    "build",
    "clean_doi",
    "describe",
]

REGISTRY: dict[str, type[Provider]] = {
    p.name: p
    for p in (
        OpenAlexProvider,
        EuropePMC,
        PubMed,
        PMCS3,
        Core,
        Biorxiv,
        Unpaywall,
        OpenAIRE,
        PLOS,
        OSF,
        HAL,
        DOAJ,
        Scite,
        WebSearch,
    )
}

DEFAULT_LOCATE = (
    "pmc-s3",
    "europepmc",
    "plos",
    "openalex",
    "biorxiv",
    "openaire",
    "hal",
    "osf",
    "core",
    "unpaywall",
)
DEFAULT_SEARCH = (
    "openalex",
    "europepmc",
    "pubmed",
    "openaire",
    "plos",
    "osf",
    "hal",
    "doaj",
    "core",
)
# `scite` is not in the default set: its subscription meters calls (250 a month), so a search asks
# it only when told to (`search(also=["scite"])`, `--scite`, or by naming it in the set).
# `web` is not in the default set: it runs as a FALLBACK (Library.search) when the set above finds
# no open-access hit, or when named explicitly. PAPER_FETCH_WEB_FALLBACK=0 turns the fallback off.
FALLBACK_SEARCH = "web"


def _names(names: Sequence[str] | None, env_var: str, default: Sequence[str]) -> list[str]:
    if names is None:
        raw = os.environ.get(env_var)
        names = [n.strip() for n in raw.split(",") if n.strip()] if raw else list(default)
    unknown = [n for n in names if n not in REGISTRY]
    if unknown:
        raise ValueError(f"unknown provider(s) {unknown}; known: {', '.join(REGISTRY)}")
    return list(names)


def _make(cls: type[Provider], http: HttpClient, openalex_client: OpenAlex | None) -> Provider:
    if cls is OpenAlexProvider:
        return OpenAlexProvider(http, openalex_client)
    return cls(http)


def build(
    http: HttpClient,
    *,
    openalex_client: OpenAlex | None = None,
    names: Sequence[str] | None = None,
    purpose: str = "locate",
) -> list[Provider]:
    """Instantiate providers by name (or from the environment, or the defaults) for a purpose."""
    if purpose == "locate":
        env_var, default = "PAPER_FETCH_PROVIDERS", DEFAULT_LOCATE
    elif purpose == "search":
        env_var, default = "PAPER_FETCH_SEARCH_PROVIDERS", DEFAULT_SEARCH
    else:
        raise ValueError("purpose is 'locate' or 'search'")
    out = []
    for n in _names(names, env_var, default):
        cls = REGISTRY[n]
        if not (cls.can_locate if purpose == "locate" else cls.can_search):
            raise ValueError(f"provider {n!r} cannot {purpose}")
        out.append(_make(cls, http, openalex_client))
    return out


def describe(http: HttpClient) -> list[dict[str, object]]:
    """Every registered provider: what it can do, what it needs, and its terms. No network."""
    return [_make(cls, http, None).describe() for cls in REGISTRY.values()]
