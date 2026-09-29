# Wiring a project into paper-fetch

## Depend on it

From git, with the extras you need — `s3` for the shared bucket (boto3), `mcp` for the server:

```bash
uv add "paper-fetch[s3] @ git+https://github.com/polarizetech/paper-fetch@<tag-or-commit>"
pip install "paper-fetch[s3] @ git+https://github.com/polarizetech/paper-fetch@<tag-or-commit>"
```

**Across repositories, pin a tag or a full commit sha** — never the moving `main`, and never a
path import of a checkout. No tag has been cut yet (2026-09-29), so pin the commit; `uv.lock`
records it either way. The core needs only `pypdf` and `fonttools`; the identifier handling and the
guards need neither extra.

## Setup, once per machine

Everything is environment variables ([README § Configure](README.md#configure) has the table).
`paper-fetch` and `paper-fetch-mcp` also read `KEY=value` lines from every `*.env` file in
`~/.config/paper-fetch/` (or `$PAPER_FETCH_ENV_DIR`), taking only `PAPER_FETCH_*` and the five
provider keys and never overriding a variable already set. Keep those files mode 600 and out of
every repository.

The shared library is the organisation's Spaces bucket, through the S3 store. For example
`~/.config/paper-fetch/spaces.env`:

```bash
PAPER_FETCH_STORE=s3
PAPER_FETCH_S3_BUCKET=<bucket>
PAPER_FETCH_S3_ENDPOINT_URL=https://<region>.digitaloceanspaces.com
PAPER_FETCH_S3_REGION=<region>
PAPER_FETCH_S3_ACCESS_KEY_ID=...
PAPER_FETCH_S3_SECRET_ACCESS_KEY=...
```

It is the same `papers/` layout the monorepo's `paperlib` wrote, so an existing bucket is read as
is. Only these names are taken: credentials kept under other variable names (a Spaces file written
for another tool) are ignored, so copy the values across. Without the two key variables boto3's own
credential chain applies. The privacy check makes an unsigned GET against
`https://<bucket>.<endpoint host>`; set `PAPER_FETCH_S3_PUBLIC_URL` if objects are served from
elsewhere. Unset, `PAPER_FETCH_STORE` is `local` (`~/.local/share/paper-fetch`), which shares
nothing with other machines.

Optional, same directory:

```bash
PAPER_FETCH_EMAIL=...            # polite pools + the Unpaywall route (Unpaywall requires one); never sent unless set
OPENALEX_API_KEY=...             # enables OpenAlex's own full texts; $0.10 -> $1.00/day
OPENCITATIONS_ACCESS_TOKEN=...   # sent as OpenCitations asks; a wrong one 403s
NCBI_API_KEY=...  CORE_API_KEY=...   # raise limits
SEARXNG_URL=http://127.0.0.1:8888    # a local SearXNG instance -- enables the web fallback
```

A free OpenAlex key is at <https://openalex.org/users>. **A wrong key raises `BadApiKey`** — it does
not quietly fall back, because a bad key breaks even free lookups. `paper-fetch providers` prints
what each provider needs and whether it is ready; `paper-fetch status` shows which store is in use.

## Import it

```python
from paper_fetch import Library, NotFound
from paper_fetch.config import load_env

load_env()  # Library.default() does NOT read ~/.config/paper-fetch/*.env; the CLI and MCP server do
lib = Library.default()  # the store named by PAPER_FETCH_STORE, providers from the environment
```

Skip `load_env()` if the variables are already in the process environment. A consumer that wants
to fall back rather than fail when the bucket is unreachable wraps this itself: `research` does it in
`scripts/_papers.py` (`lib, note = library()` gives the bucket if credentials are set, else an
in-memory store and a `note` saying so; `library(require_store=True)` exits instead).

## The calls you will use

```python
rec = lib.fetch("10.1371/journal.pcbi.1003285")  # DOI, "W2036318837", "pmid:…", "PMC…", "arxiv:…"
rec["from"]  # "library" | "retrieved" | "not-obtainable"
rec["full_text"]  # True only if readable text passed the gate

text = lib.text(rec["work"])  # raises NotFound if there is no readable text
prov = lib.provenance(rec["work"])  # route, source URL, licence, sha256s, every attempt
lib.verify(rec["work"])  # re-hash stored files against provenance

res = lib.search("auditory steady-state response 40 Hz")  # every enabled search provider
res["providers"]  # {"pubmed": {"status": "ok", "n": 10}, "core": {"status": "unavailable", ...}}
# hits are merged by DOI/PMCID/PMID; these are what to fetch next
[h for h in res["hits"] if not h["full_text_in_library"]]
lib.search("myogenic", providers=["europepmc", "pubmed"])  # choose providers per call
res["providers"]["web"]  # fallback: ran only if no open-access hit ("why_ran"), else "not-needed"
[h["urls"] for h in res["hits"]]  # web hits carry URLs; their ids are parsed, unverified

lib.search_library("myogenic", full_text=True)  # only what we already hold, no network

# who cites it (OpenCitations); lib.references(...) for what it cites
g = lib.citations("10.1016/j.cub.2024.06.028")
g["n"], g["held"]  # how many, how many already in the library
[i["ids"]["doi"] for i in g["items"] if not i["in_library"]]  # what to fetch next
```

Field-by-field shapes are in [README § MCP server](README.md#tool-contract); the Python calls return
the same records.

## Choosing providers

```python
from paper_fetch import Library, OpenAlex, S3Store, build

oa = OpenAlex()
lib = Library(
    S3Store.from_env(),
    oa,
    providers=build(oa.http, openalex_client=oa, names=["pmc-s3", "europepmc"]),
)
```

or set `PAPER_FETCH_PROVIDERS` / `PAPER_FETCH_SEARCH_PROVIDERS`. **Always read
`res["providers"]`**: `unavailable` means the service refused us, which is not the same answer as
zero results.

## Over MCP

`paper-fetch-mcp` is a stdio server (install with the `mcp` extra; add `s3` for the bucket). Point a
client at it with credentials left in `~/.config/paper-fetch/*.env`, not in the client config:

```json
{
  "mcpServers": {
    "papers": {
      "command": "uvx",
      "args": ["--from", "paper-fetch[mcp,s3] @ git+https://github.com/polarizetech/paper-fetch@<tag-or-commit>", "paper-fetch-mcp"]
    }
  }
}
```

Eight tools — `search`, `fetch`, `library`, `text`, `provenance`, `citations`, `providers`,
`status` — each returning `{"ok": true, "data": ...}` or `{"ok": false, "code", "error"}`; the
contract is in [README § MCP server](README.md#mcp-server). There is deliberately no tool for
`add_local`.

## Cost discipline

- **`fetch()` of a paper already held makes zero outside network calls** — on this machine or any
  other sharing the store. Call it freely.
- **`search()` costs $0.001 against OpenAlex's allowance** the first time and $0 afterwards for 30
  days. Prefer `search_library()` when the question is about papers already in hand.
- `lib.status()` reports what this process spent and what OpenAlex says remains.
- Bulk collection of a topic: read [`HARVEST.md`](HARVEST.md) first.

## A copy you legitimately hold

```python
lib.add_local(
    "paper.pdf", "10.1109/MCSE.2019.2900945", rights="purchased via Article Galaxy 2026-09-13"
)
```

or `paper-fetch add paper.pdf 10.1109/MCSE.2019.2900945 --rights "..."`. `rights` is required and
stored verbatim. This is the only way a non-open-access paper enters the library.

## What it refuses

| you did | it does |
|---|---|
| fetched a work that is not open access | records it `not-obtainable` with the reason, **before any network call** for full text |
| a `pdf_url` returned an HTML page | refuses it and tries the next route |
| extracted text that is not prose | refuses to store `fulltext.txt`, keeps the file and says why |
| `add_local` with no rights | `ValueError` |
| an identifier that is a bare number | `ValueError` — use `pmid:` |
| a wrong `OPENALEX_API_KEY` | `BadApiKey` |
| an unknown provider name | `ValueError` with the known list |
| `PAPER_FETCH_STORE=s3` without `PAPER_FETCH_S3_BUCKET` | `RuntimeError` naming the variables |
| a provider 429s or times out | reported `unavailable` by name; the rest still answer |
| a provider-published md5 does not match | the copy is refused and the next route tried |
| a stored object turns out anonymously readable | `NotPrivate` |

## What it does not do

- Citation intent (supporting / contrasting) — use scite. `lib.citations()` gives the links, not the stance.
- Titles for citation-graph rows — OpenCitations returns identifiers only; `fetch()` or search for the rest.
- Query Google Scholar — it has no API and its terms forbid automated querying.
- Download anything a web search found — a web hit is a lead; its parsed DOI goes through `fetch()`.
- Make anything public — papers are private by construction; publication is dataset-publish's job
  and papers are deliberately not on its allow-list.
- Fetch datasets — [dataset-fetch](https://github.com/polarizetech/dataset-fetch).
