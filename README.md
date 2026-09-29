# paper-fetch

A deduplicated, private library of **legal open-access** scientific papers. Give it a DOI, OpenAlex
ID, PMID, PMCID or arXiv ID; it looks in what it already holds first, and only then asks a chain
of open-access providers for a copy. Every copy it keeps is validated (checksum, format, readable
text) and stored with a provenance record: where it came from, under what licence, and every
route that was tried.

It ships as a Python library, a command line (`paper-fetch`), and an MCP server (`paper-fetch-mcp`) so
that an LLM agent can search, fetch and read papers through a small, stable tool contract.

```console
$ paper-fetch fetch 10.1371/journal.pcbi.1003285
✓ W2036318837  [retrieved]  2013  Ten Simple Rules for Reproducible Computational Research
    oa=gold  route=pmc-s3:jats-xml  format=jats-xml  license=CC BY
$ paper-fetch fetch 10.1371/journal.pcbi.1003285       # second time: no network at all
✓ W2036318837  [library]  2013  Ten Simple Rules for Reproducible Computational Research
```

## What it will and will not do

- **Open-access copies only.** A provider returns a location only for a copy *it* reports as open
  access (Europe PMC's `isOpenAccess`, PMC's open-access subset, OpenAlex's per-location `is_oa`,
  Unpaywall's `best_oa_location`, a preprint server's own copy, a Creative Commons licence on the
  record). There is no code path that follows a paywall, scrapes a publisher page or uses a shadow
  library. If no provider reports an open copy, nothing is downloaded and the answer says so.
- **Open access is a right to read, not to republish.** Many OA copies carry no reuse licence at
  all, and NIH author manuscripts in PMC are under a text-and-data-mining licence. So the store is
  private (files `0600`, directories `0700`, checked after every write; S3 objects never get an
  ACL and are checked with an anonymous request), and each copy records the licence exactly as its
  provider reported it. Quote briefly and cite; do not redistribute stored files.
- **A copy you legitimately hold** (an author copy, a purchased PDF) can be added with
  `paper-fetch add file.pdf <id> --rights "..."`. The rights statement is required and recorded. This
  is deliberately not an MCP tool: that statement is the operator's to make, not a model's.
- **Validated, not assumed.** A candidate is refused if its md5 does not match the provider's
  published checksum, if it is an HTML page (a login screen served as "PDF"), if it is not the
  format claimed, or if the extracted text does not look like prose.
- **Loud, per-provider failure.** A rate-limited or unreachable provider is reported by name as
  `unavailable`; it is never silently folded into "no results".
- **Not Google Scholar.** It has no API and its terms forbid automated querying.

## Install

Python 3.11+.

```bash
pip install "paper-fetch[mcp]"          # CLI + MCP server
pip install "paper-fetch[mcp,s3]"       # ... plus the S3-compatible store (boto3)
# or, from a checkout:
uv sync && uv run paper-fetch --help
```

The core depends only on the standard library and [`pypdf`](https://pypi.org/project/pypdf/)
(pure Python, used for PDF text extraction; no external binaries such as `pdftotext`). `mcp` is
needed only for the server, `boto3` only for the S3 store.

## Configure

Everything is configured with environment variables; none is required. Both `paper-fetch` and
`paper-fetch-mcp` also read `KEY=value` lines from `*.env` files in `~/.config/paper-fetch/` (or
`$PAPER_FETCH_ENV_DIR`), taking only the variables below and never overriding the environment. Keep
those files out of repositories (`chmod 600`).

| Variable | Purpose |
|---|---|
| `PAPER_FETCH_EMAIL` | Contact email. Sent to OpenAlex (polite pool), NCBI and PubMed, and in the User-Agent. **Required by Unpaywall**; without it the `unpaywall` route is skipped. Never sent unless set. |
| `OPENALEX_API_KEY` | Optional ([free](https://openalex.org/users)). Raises limits and enables OpenAlex's own cached full texts. A *wrong* key fails loudly rather than silently degrading. |
| `OPENCITATIONS_ACCESS_TOKEN` | Optional; OpenCitations asks applications to send one. |
| `NCBI_API_KEY` | Optional; raises PubMed from 3 to 10 requests/s. |
| `CORE_API_KEY` | Optional; raises CORE's limits. |
| `SEARXNG_URL` | Optional. A SearXNG instance with JSON output enabled, used as a last-resort *search* fallback (never a download source). |
| `PAPER_FETCH_PROVIDERS` | Comma-separated locate order (default below). |
| `PAPER_FETCH_SEARCH_PROVIDERS` | Comma-separated federated-search set (default below). |
| `PAPER_FETCH_WEB_FALLBACK` | `0` disables the web fallback. |
| `PAPER_FETCH_STORE` | `local` (default), `s3` or `memory`. |
| `PAPER_FETCH_DATA_DIR` | Local store directory. Default `~/.local/share/paper-fetch` (`$XDG_DATA_HOME/paper-fetch`). |
| `PAPER_FETCH_CACHE` | Read-through cache for the S3 store. Default `~/.cache/paper-fetch`. |
| `PAPER_FETCH_S3_BUCKET` | S3 store: bucket (required when `PAPER_FETCH_STORE=s3`). |
| `PAPER_FETCH_S3_ENDPOINT_URL`, `PAPER_FETCH_S3_REGION` | S3 store: endpoint for non-AWS services (DigitalOcean Spaces, MinIO, R2, ...) and region. |
| `PAPER_FETCH_S3_ACCESS_KEY_ID`, `PAPER_FETCH_S3_SECRET_ACCESS_KEY` | S3 store credentials; if unset, boto3's usual credential chain applies. |
| `PAPER_FETCH_S3_PUBLIC_URL` | S3 store: the anonymous base URL used to prove objects are *not* public. Default `https://<bucket>.<endpoint host>`. |

## Providers

| name | search | locate | notes |
|---|---|---|---|
| `pmc-s3` | | yes | PMC open-access subset on AWS Open Data; md5 per file. |
| `europepmc` | yes | yes | Includes bioRxiv/medRxiv preprints; OA JATS XML. |
| `plos` | yes | yes | PLOS DOIs only; 10 requests/minute. |
| `openalex` | yes | yes | Metered in USD (search ~$0.001, lookups free); searches are cached. |
| `biorxiv` | | yes | JATS XML and licence by DOI. |
| `openaire` | yes | yes | Only copies with an explicit open licence on an OPEN record. |
| `hal` | yes | yes | Licence recorded verbatim (HAL's deposit authorisation is not a reuse licence). |
| `osf` | yes | yes | OSF preprints via SHARE; only Creative Commons copies. |
| `core` | yes | yes | Repository copies; CORE reports no licence, and none is claimed. |
| `unpaywall` | | yes | Needs `PAPER_FETCH_EMAIL`. |
| `pubmed` | yes | | Finds papers and PMCIDs; PMC routes hold the copies. |
| `doaj` | yes | | Search only (its full-text links are landing pages). |
| `web` | fallback | | SearXNG; identifiers are parsed from URLs and never trusted as open. |

Default locate order: `pmc-s3, europepmc, plos, openalex, biorxiv, openaire, hal, osf, core,
unpaywall`. Default search set: `openalex, europepmc, pubmed, openaire, plos, osf, hal, doaj,
core`, with `web` asked only when none of those returns an open-access hit. NCBI's ID converter
fills in missing PMCIDs/PMIDs before providers are asked, and OpenCitations answers the citation
graph. `paper-fetch providers` lists what is enabled and what each needs, without touching the network.

## Command line

```text
paper-fetch fetch <doi|W123|pmid:N|PMCN|arxiv:ID> [...] [--force]
paper-fetch search "<query>" [--providers a,b] [--include-closed] [-n N] [--refresh]
paper-fetch providers
paper-fetch library ["<query>"] [--full-text]
paper-fetch text <id>
paper-fetch provenance <id>
paper-fetch verify <id>                # re-hash stored files against their recorded sha256
paper-fetch add <file.pdf> <id> --rights "..."
paper-fetch citations <id> [--references] [-n N] [--refresh]
paper-fetch status | rebuild-index | adopt-orphans
```

`python -m paper_fetch` is the same program. Exit codes: `1` not found, `2` unavailable or bad input,
`3` checksum verification failed.

## Python

```python
from paper_fetch import Library

lib = Library.default()
rec = lib.fetch("10.1371/journal.pcbi.1003285")  # rec["from"]: library | retrieved | not-obtainable
text = lib.text(rec["work"])  # raises NotFound if there is no readable text
prov = lib.provenance(rec["work"])
res = lib.search("reproducible computational research")
```

`Library(store, openalex, providers=[...], search_providers=[...])` accepts any store with the
`Store` protocol and any `Provider` subclasses, which is how the test suite runs offline.

## MCP server

`paper-fetch-mcp` is a stdio MCP server (works with `mcp` 1.x and 2.x). Any MCP client can run it:

```json
{
  "mcpServers": {
    "papers": {
      "command": "paper-fetch-mcp",
      "args": [],
      "env": {
        "PAPER_FETCH_EMAIL": "you@example.org",
        "PAPER_FETCH_DATA_DIR": "/path/to/paper-fetch"
      }
    }
  }
}
```

Without a global install, use `uvx`: `"command": "uvx", "args": ["--from",
"paper-fetch[mcp] @ git+https://github.com/polarizetech/paper-fetch", "paper-fetch-mcp"]`.
Secrets can stay out of client configuration by putting them in `~/.config/paper-fetch/*.env`.

### Tool contract

Every tool returns one JSON object:

```json
{"ok": true, "data": "..."}
{"ok": false, "code": "not_found | unavailable | tool_error", "error": "human-readable message"}
```

`not_found`: not held, no readable text, or no DOI/PMID to ask OpenCitations with.
`unavailable`: a remote service refused or did not answer; this never means "zero results".
`tool_error`: bad arguments or configuration (unparseable identifier, unknown provider, bad key).

| tool | arguments | `data` |
|---|---|---|
| `search` | `query: str`, `include_closed: bool = false`, `limit: int = 10` | `{"query", "providers": {name: {"status", ...}}, "hits": [...]}` |
| `fetch` | `identifier: str` | a catalogue row (below) plus `"from"` |
| `library` | `query: str = ""`, `full_text: bool = false`, `limit: int = 50` | `{"works": [row, ...], "n": int}` |
| `text` | `identifier: str`, `offset: int = 0`, `max_chars: int = 100000` | `{"text", "offset", "end", "total_chars"}` |
| `provenance` | `identifier: str` | the stored provenance record (below) |
| `citations` | `identifier: str`, `direction: "citations" \| "references" = "citations"`, `limit: int = 25` | `{"of", "direction", "status", "n", "held", "source", "items": [...]}` |
| `providers` | | `{"providers": [{"name", "label", "search", "locate", "needs", "recommends", "available", "why", "terms"}]}` |
| `status` | | `{"works", "with_full_text", "not_obtainable", "store", "locate_providers", "openalex_key", "network_calls", "openalex_spent_usd", "openalex_remaining_usd"}` |

**`search`**: `providers[name].status` is `ok` or `cache` (with `n`, `cost_usd`), `skipped`
(missing configuration; `why`), `unavailable` or `error` (`why`), or for `web` also `not-needed`
(with `why_ran` when it did run). Each hit is `{"title", "year", "ids": {"doi", "pmid", "pmcid",
"openalex", "arxiv", ...}, "authors", "providers": [...], "is_oa", "locations": ["provider:format",
...], "urls": [...], "in_library", "full_text_in_library", "work"}`, merged across providers by
identifier and sorted by how many providers returned it, then by year.

**Catalogue row** (`fetch`, `library`): `work` (the OpenAlex ID, or `doi-...`/`pmcid-...` when
OpenAlex has no record), `doi`, `pmid`, `pmcid`, `title`, `year`, `authors` (first three),
`full_text` (true only if readable text passed the gate), `oa_status`, `license`, `route`
(`provider:format`), `format`, `retrieved`, `retry_after` (when a paper with no open copy will be
asked about again), `is_retracted`; `fetch` adds `from` (`library` | `retrieved` |
`not-obtainable`) and `library` adds `matched_in` when a query was given.

**`text`**: `max_chars` is clamped to 1,000..100,000. Page through with `offset = end` until
`end >= total_chars`.

**Provenance record**: `work`, `retrieved`, `full_text`, `file_stored`, `route`, `source_url`,
`format`, `license`, `version`, `md5_verified`, `text_method`, `text_gate`, `oa_status`, `rights`,
`refused`, `providers` (tried, in order), `attempts` (`[[route, outcome], ...]`), `sha256`
(`{file: hex}`), `ids`, `ids_note`, `retry_after`.

**`citations`**: `n` is the total; `items` holds at most `limit`, newest citing work first for
`direction="citations"`. Each item is `{"ids": {"doi", "pmid", "openalex", "omid"}, "oci",
"citing_date", "timespan", "journal_self_citation", "author_self_citation", "in_library",
"full_text_in_library", "work"}`. There is no citation intent: a citation is not agreement.

## Storage layout

```text
papers/index.jsonl                     catalogue (derived; `paper-fetch rebuild-index` recreates it)
papers/doi/<quoted-doi>.json           DOI -> work pointer
papers/works/<work>/work.json          the OpenAlex record
papers/works/<work>/provenance.json    authoritative: routes tried, source, licence, sha256s
papers/works/<work>/fulltext.{pdf,jats.xml,tei.xml,provider.txt}
papers/works/<work>/fulltext.txt       only when the text passed the readability gate
papers/searches/<provider>/<sha256>.json
papers/citations/<direction>/<id>.json
```

The same layout is used on disk and in a bucket. A lookup reads the catalogue first and falls back
to the per-work provenance, so a stale or deleted catalogue can slow a lookup down but can never
cause a second download.

## Limits

- Coverage is whatever the providers report as open; a paper with no open copy stays
  `not-obtainable` (asked about again after 30 days, since embargoes lift).
- Text is machine-extracted: figures, tables and equations may be missing or garbled. Scanned
  PDFs without a text layer fail the readability gate (there is no OCR).
- Licences are recorded as reported, not adjudicated. CORE and some repositories report none.
- Searches are cached for 30 days, so a repeated query can miss papers indexed since
  (`--refresh` bypasses the cache).
- Rate limits are the providers': PLOS allows 10 requests/minute, unkeyed OpenAlex has a small
  daily budget, and so on. Requests are spaced politely per provider.
- `library` search is term matching over titles and authors (optionally full text), not ranking.

## Development

```bash
uv sync                                   # Python 3.11+; installs the dev group
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run pytest --cov
```

The suite is offline: HTTP is replaced by a fake that serves small synthetic fixtures
(`tests/fixtures/`), and one test drives the real `paper-fetch-mcp` entry point over stdio. CI runs
all of the above on Python 3.11, 3.12 and 3.13.

## License

MIT. See [LICENSE](LICENSE). The papers you retrieve are under their own licences.
