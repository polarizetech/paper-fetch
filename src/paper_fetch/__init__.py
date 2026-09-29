"""paper-fetch: one deduplicated library of legal open-access full texts, found through OpenAlex.

    from paper_fetch import Library

    lib = Library.default()                        # local store + providers from the environment
    rec = lib.fetch("10.1371/journal.pcbi.1003285")
    text = lib.text(rec["work"])

The command line is ``paper-fetch`` (or ``python -m paper_fetch``); the MCP server is
``paper-fetch-mcp``.
"""

from .citations import CitationsUnavailable, OpenCitations
from .http import Http, Response
from .ids import Ident, normalize
from .library import Library, NotFound
from .openalex import BadApiKey, NeedsApiKey, OpenAlex
from .providers import REGISTRY, Hit, Location, Provider, ProviderUnavailable, build, describe
from .resolve import resolve
from .store import PREFIX, LocalStore, MemoryStore, NotPrivate, S3Store, Store, store_from_env
from .text import looks_like_prose, pdf_text, xml_text

__version__ = "0.1.0"

__all__ = [
    "PREFIX",
    "REGISTRY",
    "BadApiKey",
    "CitationsUnavailable",
    "Hit",
    "Http",
    "Ident",
    "Library",
    "LocalStore",
    "Location",
    "MemoryStore",
    "NeedsApiKey",
    "NotFound",
    "NotPrivate",
    "OpenAlex",
    "OpenCitations",
    "Provider",
    "ProviderUnavailable",
    "Response",
    "S3Store",
    "Store",
    "__version__",
    "build",
    "describe",
    "looks_like_prose",
    "normalize",
    "pdf_text",
    "resolve",
    "store_from_env",
    "xml_text",
]
