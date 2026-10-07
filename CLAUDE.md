# CLAUDE.md — paper-fetch

**One private, deduplicated library of open-access full texts, found through TWELVE swappable
providers, plus scite on request and a web-search fallback, stored in a local directory or any S3-compatible bucket, and
checked before anything is downloaded.** A paper retrieved once is never retrieved again, by any
project, on any machine that shares the store.

Run (from a checkout; an installed copy drops the `uv run`):

```
uv run paper-fetch fetch 10.1371/journal.pcbi.1003285
uv run paper-fetch search "reproducible computational research"   # every provider, merged
uv run paper-fetch providers                                        # what is on, what each needs; no network
uv run paper-fetch library "reproducible" --full-text
uv run paper-fetch citations 10.1371/journal.pcbi.1003285           # who cites it; --references for what it cites
```

Configuration is `PAPER_FETCH_*` plus five provider keys, read from the environment and from
`~/.config/paper-fetch/*.env` — the table is in [README § Configure](README.md#configure). The store
is `PAPER_FETCH_STORE=local` (default, `~/.local/share/paper-fetch`), `s3` or `memory`.

Tests: `uv run pytest` — offline, no credentials, no network (HTTP is a fake serving
`tests/fixtures/`). There are no live tests. Lint and types: [README § Development](README.md#development).
Wiring a project in → [`INTEGRATION.md`](INTEGRATION.md).

**Asked to collect a whole subject area and keep it current? Read [`HARVEST.md`](HARVEST.md)
first** — the procedure for a bulk topic harvest with what it costs: the OpenAlex daily cap is
**10,000 requests**, not the $1, so roughly 2,000–3,000 papers a day; a list page of 200 costs
$0.0001; one broad OpenAlex topic can hold well over 10,000 works in two years;
`from_created_date` needs a **paid plan**, so updates re-query by publication date with a ~60-day
overlap. It also names the two things that bite at scale — the catalogue is re-read and rewritten
per paper (~600 B/row), and with the S3 store every stored file is also copied into
`PAPER_FETCH_CACHE`. Collections exist (below); harvesting, screening and scheduling are not built,
and the design for them is at the end of that file.

## ⭐ Discipline profiles, collections, search memory — all deterministic

The search half of a scientific field lives here, not in the applications that read papers: an
application says what field and concept it wants, and paper-fetch knows how that field is indexed
and what was searched before. **No language model runs inside paper-fetch**; everything below is
string matching and bookkeeping, so its behaviour is testable and repeatable.

- **Profiles** (`profiles.py`, data in `profile_data/*.toml`; the operator's in
  `~/.config/paper-fetch/profiles/` replace or add by slug). Anchors are controlled terms (a `mesh`
  anchor must carry a real `D......` id; a term without one is `free-text`) with synonyms.
  `search(profile=)` runs the query plus up to two variants that swap a synonym for its indexed
  term — **never the reverse**, which would search for the less-indexed wording — merges them
  (`found_by`), marks each hit's `profile_match` from its title, and ranks matching hits first.
  A profile's `providers` replaces the search set only when it names some; the built-ins do not,
  because narrowing to PubMed/Europe PMC/OpenAlex would drop the repository providers that find
  open copies. `guidance()` is what an application's query writer reads: scope, terms, measures,
  sources and prose advice. The four built-ins were converted from the evidence engine's domains.
- **Collections** (`collection.py`): `papers/collections/<name>.json`, a named member list with a
  default profile. A paper is stored once; members are work ids when held, else the identifier
  given until fetched (`fetch(collection=)` swaps it). Re-read before each change.
- **Memory** (`memory.py`): `papers/memory/searches.jsonl`, one row per search (query, variants,
  concepts, profile, collection, provider statuses, up to 50 hits). Written like the catalogue:
  re-read, append, write. Concepts are content words with every profile synonym mapped to its
  indexed term, so "HRV" and "heart rate variability" recall each other; similarity is Jaccard.
  Each search reports up to three earlier ones on its concept, and each hit's `seen_before`.
- **Passages** (`passages.py`, `rerank.py`): a local SQLite index (FTS5 + optional vectors, fused
  by reciprocal rank) derived from held full texts, **not in the store**: one per machine, at
  `PAPER_FETCH_INDEX`. Moved here from the evidence engine, same chunking, same hidden-character
  cleaning, so offsets and `text_sha256` mean the same as they did there. Embeddings come from
  an Ollama-compatible `/api/embed` (`PAPER_FETCH_EMBED_MODEL`), posted through `Http.post_json`
  so `http.py` stays the only module that opens URLs besides the store; the cross-encoder is
  ONNX (`PAPER_FETCH_RERANK_MODEL`). Both are deterministic: vectors and scores, never text. An
  index refuses a different embedding model. `retrieve` indexes its scope on demand; a passage
  id `<work>#p<ord>` survives rebuilds. The model-based reranker and the prompt-injection scan
  stay in the application: they concern a model reading the text. **The reference list is not
  indexed**, found two ways (`references_start`): a heading on a line of its own past the first
  third of the text, or, for PDF text that lost its heading, a run of at least 8 consecutively
  numbered entries from 1 that starts in the second half, reaches the last tenth and mostly
  carries years. Measured on 80 held papers: 11 of 12 PDFs had an unrecognised list (the old rule
  needed a paragraph that was exactly "References"); every cut read was correct; XML sources were
  unaffected. An unnumbered author-year list with no heading is still not recognised. The chunker
  has a version (`CHUNKER`, in a `chunked` table so the `papers` table is unchanged for servers
  still running older code); `paper-fetch index` re-chunks stale papers and reuses the vectors of
  passages whose text did not change. Measured with bge-m3:
  15 papers → 1,405 passages in 94 s; a query then takes ~10 ms.

## ⭐ Over MCP

`paper-fetch-mcp` (`src/paper_fetch/mcp_server.py`, the official `mcp` SDK: `FastMCP` on 1.x,
`MCPServer` on 2.x; needs the `mcp` extra). Tools: `search` (with `profile`, `collection`, `scite`),
`fetch` (with `collection`), `library`, `text` (paged, 100,000 characters by default, clamped to
1,000..100,000), `provenance`, `citations`, `providers`, `status`, `profiles`, `recall`,
`collections`, `collection`, `create_collection`, `collect`, `uncollect`, `retrieve`, `passages`,
`index`, `relevance` (scores hit titles against research sub-questions, to order fetches). Every result is `{"ok": true, "data": ...}` or `{"ok": false, "code": "not_found" |
"unavailable" | "tool_error", "error": ...}`; `unavailable` is never "zero results". The full
contract is in [README § MCP server](README.md#mcp-server). The server's instructions to the model
say open access is a right to read, not to republish. **Storing a copy the user holds is not a
tool** — its rights statement is the user's to make; use `paper-fetch add`.

It reads credentials itself from `~/.config/paper-fetch/*.env` (or `$PAPER_FETCH_ENV_DIR`), because
a client launches it with no shell to `source` in. It takes only `PAPER_FETCH_*` and the five
provider keys — **any other secret in the same file, such as a full-account cloud token, is not
loaded** (`tests/test_config.py`) — and never overrides a variable already set. The CLI does the
same; `Library.default()` called from Python does not (see INTEGRATION.md).
`tests/test_mcp_server.py` drives the real entry point over stdio.

## Why it exists

- **Retrieval of papers needs one shared home.** Calling OpenAlex ad hoc from scripts through a
  local disk cache, and fetching full texts onto one machine with no shared index, duplicates work
  and loses provenance. This library replaces both.
- **Papers are not datasets and are never published.** A paper is not dataset-shaped (no pinned
  snapshots or file selectors), and a stored paper must never be made public. So this is a
  separate job with its own store prefix.
- **It owns the store's `papers/` prefix** and the catalogue at `papers/index.jsonl`. Nothing else
  should write there.

## ⭐ Providers — named, ordered, swappable

`src/paper_fetch/providers/` holds one adapter per service behind one contract: a provider may
**search** (query → hits) and/or **locate** (identifiers → locations of legal full-text copies). It
never validates, stores or marks anything read — that is the library's job, which is what makes
providers interchangeable. Choose them by name, no code change:

```bash
PAPER_FETCH_PROVIDERS=pmc-s3,europepmc,plos,openalex,biorxiv,openaire,hal,osf,core,unpaywall   # locate order
PAPER_FETCH_SEARCH_PROVIDERS=openalex,europepmc,pubmed,openaire,plos,osf,hal,doaj,core          # search set
```

Those are the defaults. An **unknown name raises** with the known list — a typo that silently
dropped a provider would shrink every search with nothing on screen saying so. A locate-only
provider named in the search set (or the reverse) also raises.

| provider | search | locate | key | notes (measured September 2026) |
|---|---|---|---|---|
| `openalex` | $0.001 | OA locations; own copies with a key | optional | $0.10 unkeyed / **$1.00 keyed** per day; own PDF retrieved with a key |
| `europepmc` | ✓ incl. preprints | OA JATS by PMCID | — | large open-access coverage for biomedical queries |
| `pubmed` | ✓ MeSH-aware | (yields PMCIDs) | optional `NCBI_API_KEY` | 3 req/s; a MeSH heading plus a free-full-text filter narrows well |
| `pmc-s3` | — | PMC OA subset, plus NIH author manuscripts under PMC's TDM licence: xml/pdf/txt **with md5** | — | anonymous AWS bucket; PDF byte-identical to the publisher's |
| `openaire` | ✓ | copies with an open licence, on records OpenAIRE marks OPEN | — | ~7,200 req/window unkeyed; open status is per RECORD, licence per copy; finds institutional-repository copies other providers miss |
| `plos` | ✓ every word, or a quoted phrase | JATS then PDF, built from the DOI | — | docs limit 10/min, so 6.1 s spacing; licence not in the response (CC BY by policy) |
| `osf` | ✓ via SHARE (PsyArXiv and other OSF servers) | `osf.io/<id>/download` when the preprint is CC-licensed | — | SHARE's `identifier` filter returned 0 for a DOI it holds, `sameAs` finds it; OSF's own v2 API 502s, so it is not used |
| `hal` | ✓ file records only | `fileMain_s` when `openAccess_bool` | — | licence is often HAL's deposit authorisation, which permits reading, not reuse |
| `doaj` | ✓ search only | — (links are landing pages) | — | finds papers other indexes miss; copies then come through other providers |
| `scite` | **opt-in** (`also=["scite"]`, `--scite`, MCP `scite: true`), via its MCP server | — never a copy source | a sign-in (`paper-fetch scite-login`), no key | **250 calls a month**; 210M+ papers searched through citing sentences; hits carry tallies and editorial notices; not cached |
| `core` | ✓ | repository PDF | optional `CORE_API_KEY` | 17.5 M records; 10 req/window; **no licence field** |
| `biorxiv` | — (API has no keyword search: 404) | JATS + licence by DOI | — | |
| `unpaywall` | — | best OA PDF | `PAPER_FETCH_EMAIL` | sometimes knows no open copy that OpenAIRE finds in a repository |
| `web` | **fallback only** | — never a copy source | `SEARXNG_URL` | identifiers parsed from PLOS, PubMed, PMC and Frontiers URLs |

**Removed:** **`arxiv`** (HTTP 429 on the first request, every run), **`semanticscholar`** (429 on
every unkeyed call), and **`zenodo`** (retrieved with md5 verification, but its search matched
loosely). arXiv **identifiers** are still understood (`arxiv:ID`, resolved through the arXiv
DataCite DOI): they arrive from CORE, OpenAIRE and OpenAlex, and an arXiv paper is still fetched
through those.

**Web search is the FALLBACK, not another index** (`providers/web.py`). It runs inside `search()`
only when the providers above return **no open-access hit** — its report says `why_ran`, or
`not-needed` when an open hit exists — or when `web` is named explicitly; `web_fallback=False` /
`PAPER_FETCH_WEB_FALLBACK=0` turns it off. Backend: a SearXNG instance, opt-in by `SEARXNG_URL`. A
SearXNG answer with **no results and unresponsive engines is reported `unavailable`**, not empty. A
web hit carries its URL and **identifiers parsed out of the URL or snippet, unverified**; `is_oa` is
`None`; `can_locate` is False, so nothing is ever downloaded from a web result — a parsed DOI goes
through `fetch()` and the normal providers decide openness. **Web results are not cached**
(`cacheable = False`): what may be stored depends on the engines SearXNG queried.

### Running SearXNG for the fallback

Bind it to **loopback only** (for example `127.0.0.1:8888`), keep its `settings.yml` (it holds the
instance secret) outside any repository, enable JSON output, and put `SEARXNG_URL` in
`~/.config/paper-fetch/*.env`. With nothing outside the machine able to reach it, the rate limiter
can be off; raise outbound timeouts (8 s worked where the 3 s default timed out every engine).

**Remove every Google engine** (`google`, `google scholar`, `google news`, `google images`,
`google videos`, `google cse`, `google cse images`) — the same terms-of-service reason Google Scholar
is not a provider; read the enabled list back from `/config` to confirm, since `google cse` is easy
to miss. DuckDuckGo tends to answer a new instance with a CAPTCHA and can be removed too. The
engines SearXNG scrapes do not offer this access by agreement, so blocking is expected and results
degrade: zero results with unresponsive engines is reported `unavailable`, but a PARTIAL block just
returns fewer results. Check `unresponsive_engines` in its JSON when results look thin:
`curl -s "http://127.0.0.1:8888/search?q=test&format=json" | python3 -m json.tool | grep -A5 unresponsive`.

**Deliberately not providers:** **Google Scholar** (no API; its terms forbid automated querying;
OpenAlex and CORE cover its discovery role), **Crossref as a full-text route** (its `link`s are
publisher *text-mining* licences such as `springer.com/tdm`, not open access), and **DOAJ as a copy
source** (its full-text links are landing pages, which the validator refuses — it is a search-only
provider). `tests/test_guarantees.py` asserts there is no Google Scholar and no Crossref full-text
route.

**Federated search reports every provider by name** — `ok`, `cache`, `skipped` (missing key),
`unavailable` (429 / unreachable) or `error` — and merges hits across providers by DOI → PMCID →
PMID → OpenAlex → arXiv. A repeat search is served from the store at $0.

**Identifiers are enriched before any provider is asked.** OpenAlex's record for a 2014 PLOS paper
had no PMCID, so the PMC bucket — the most verifiable route — reported "no copy" for a paper that is
in PMC. NCBI's ID converter (at `pmc.ncbi.nlm.nih.gov`) filled the PMCID, and the re-fetch came from
the bucket **with its md5 verified**. A failure there is never fatal.

**Two defects found by running it, both pinned by tests.** `Http` retried a **429** internally and
then raised a generic `ConnectionError`, so no provider ever saw the rate limit, and a federated
search reported rate-limited providers as `error` and took **2 minutes**. A 429 (any 4xx) is now
returned on the first attempt and becomes `unavailable`; providers carry a 20 s timeout and one
retry. And OpenAlex lacking a PMCID (above) had been silently skipping the best route.

## ⭐ scite — through its MCP server, with the subscriber's sign-in

`providers/scite.py` and `scite_auth.py`. scite's REST search (`/api_partner/search`) answers
**401** without a partner token, which comes from their sales team, not with a subscription. Its
MCP server (`https://api.scite.ai/mcp`) takes the subscriber's own sign-in instead: OAuth 2.1 with
open dynamic client registration, a public client (`token_endpoint_auth_method: none`), PKCE S256
and the scopes `mcp offline_access`. So the provider is a small MCP client, written against
`Http` (the `mcp` SDK is not a core dependency and would open its own sockets): `initialize`,
`notifications/initialized`, then `tools/call search_literature`.

- **`paper-fetch scite-login`** registers this machine, opens the browser, receives the redirect
  on a loopback port and stores `scite-oauth.json` beside the `*.env` files, 0600. **Never in the
  store**: a bucket is shared; a sign-in is one person's. `scite-logout` deletes it.
- **Measured 2026-10-05:** the access token lasts **15 minutes** and the **refresh token rotates on
  every use**. Processes share the file, so it is re-read before a refresh and once more after a
  refused one (another process may have just spent the token). A search is 3 POSTs on a new
  session (3.7 s for 5 hits), 1 after; answers came back as plain JSON, and an event stream is
  read too.
- **Search only.** A hit keeps DOI, title, year, three authors, scite's `isOa`/`oaStatus`, the
  tally and editorial notices. **Its access links are dropped: they carry the account's email
  address** and route to a document-delivery service; abstracts and citation statements are
  dropped too. `oa_only` asks for three times `limit` (capped at 50, since every hit carries a
  citation list) and drops what scite marks closed, because the tool has no open-access filter.
- **Not cached** (`cacheable = False`): scite's terms on storing results in a shared store are
  unchecked. The search memory still records which DOIs a search returned, as for any provider.
- **Opt-in, because it is rationed: 250 MCP calls a month** on the subscription. It was in the
  default search set for one day (2026-10-05): a gateway and a dozen sessions searching, each
  profile search asking up to three variants, spent the month's allowance, after which the tool
  answers "You have reached your monthly MCP usage limit" and scite's own connector may be out
  too. So `scite` is not in `DEFAULT_SEARCH` and `Provider.metered` is True for it: a search asks
  it only through `also=["scite"]` (`--scite`; MCP `search(scite=true)`) or when it is named in
  the set, and then **once, with the query as written**, never a profile's variants. The opted-in
  instance is built once per `Library`, so its session is reused, and one thread uses it at a time.
- Not signed in is `skipped` (`available()` reads only the file). An expired sign-in, a 429, the
  monthly limit or a tool error is `unavailable` and says what to do.
- A merged hit carries `scite` (tally, `oa_status`, `editorial_notices`) when scite found it.

## ⭐ The citation graph — OpenCitations

`lib.citations(id)` (works that cite it) and `lib.references(id)` (works it cites), from the
**OpenCitations Index v2** (`src/paper_fetch/citations.py`). **Not a provider**: it neither searches
by query nor locates a copy, so it sits beside the providers rather than in the registry. Every
returned work is marked `in_library` / `full_text_in_library`, which makes it a snowballing tool:
read what cites a paper you hold, then `fetch()` the ones you don't.

- **No key needed.** An optional `OPENCITATIONS_ACCESS_TOKEN` goes in the `authorization` header,
  as the docs ask of applications, and belongs in `~/.config/paper-fetch/*.env`; a wrong token gets
  **403 "Invalid token"**, raised by name. The documented limit is **180 requests/min per IP**, and
  requests are spaced to it. Sandve 2013 → 664 citations in 2.3 s and 26 references.
- **One request stalled 60 s and the identical retry answered in 0.7 s**, so calls carry a 60 s
  timeout and one retry, and a refusal raises `CitationsUnavailable` — never an empty list.
- **Identifiers:** OpenCitations takes a DOI or PMID. A W-id, PMCID or arXiv id is turned into one
  through the free OpenAlex lookup first; a work with neither raises `NotFound`.
- **Cached** in the store at `papers/citations/{citations,references}/<doi>.json` for 30 days
  (citations accumulate, so `refresh=True` / `--refresh` re-asks).
- **A field renamed on the way through:** OpenCitations' `creation` is the **citing** work's date,
  so on a references list every row carries the same date — the paper's own. It is returned as
  `citing_date`, and the CLI shows it only for citations.
- **What it does not carry:** titles (identifiers only), and **citation intent** (supporting or
  contrasting); services such as scite classify that.

## Candidate providers surveyed (September 2026)

Anonymous probes, no 429s. PLOS, OSF and HAL were then built; Zenodo was built and removed (above).

| service | key | search | copy | observed | verdict |
|---|---|---|---|---|---|
| PLOS (`api.plos.org/search`) | — | ✓ full-text field | JATS XML (`type=manuscript`) and direct PDF | licence not in the response (CC-BY by policy); docs: 10/min, 7200/day | add |
| Zenodo (`zenodo.org/api/records`) | — | ✓ | direct file, `license.id` per record | `x-ratelimit-limit: 30` on search; many uploads are publisher PDFs, so trust only the licence field | add |
| OSF Preprints / PsyArXiv | — | via SHARE (`share.osf.io/api/v3/index-card-search`, 2.4 s); v2 API filters titles only and timed out (502 at 60 s) unscoped | direct file (signed redirect) with md5, licence by separate call | preprint DOI is `links.preprint_doi`; `attributes.doi` often null | add |
| HAL | — | ✓ modest | `fileMain_s` direct PDF, `licence_s` | many records are metadata-only notices | add, low yield |
| Springer Nature OA | free key | — | JATS | unkeyed 401; mostly BMC/SpringerOpen, largely already in PMC | maybe |
| DOAJ | — | ✓ but quoted phrases not honoured | landing pages only | licence null | search only, maybe |
| ChemRxiv, Internet Archive Scholar | — | — | — | Cloudflare challenge / bot-verify redirect | skip |
| scholar.archive.org | — | `/search?format=json` 302s to `/verify`, an ALTCHA challenge | — | `robots.txt` disallows `/search` and `/verify`; `api.fatcat.wiki` timed out at 60 s twice | **skip** — no sanctioned programmatic route |
| metapub FindIt (0.7.4) | — | — | publisher PDF URLs, verified by fetching | on a sample of not-obtainable works: none recovered — paywalled publisher links returned non-PDF or 403 | **skip** — it finds publisher copies, and for closed works every publisher copy is closed |
| Research Square | — | undocumented site endpoint | — | not a public API | skip |
| fatcat | — | — | — | timed out | skip |

## Measured against the live APIs, each of which changed the code

- **OpenAlex usage is metered in USD.** Without a key: a **$0.10** allowance, a `search` costs
  **$0.001**, a single-work lookup costs **$0**. So `fetch()` uses only free lookups, searches are
  cached in the store for 30 days, and a repeat search reports `cost $0.0`.
- **OpenAlex hosts its own copies of OA full texts** (`content.openalex.org/works/{W}.pdf` and
  `.grobid-xml`, flagged by `has_content`) — and they **require a free API key**. Without one those
  routes are skipped and say so. Neither `api_key=` nor a Bearer header was accepted with an invalid
  key, and **a wrong key breaks even the free endpoints (401)** — so a bad key raises `BadApiKey`
  rather than silently falling back to unauthenticated calls that would hide the misconfiguration.
- **`best_oa_location.pdf_url` is often null even for gold OA** — a PLOS paper had no `pdf_url` on
  any of five locations — so OpenAlex alone does not deliver PDFs, and the resolver continues to
  Europe PMC's JATS XML and Unpaywall.
- **`search=` searches full text**: OpenAlex echoed the query as `fulltext.search:<terms>`.

## Why there is a prose gate: byte-strip "extraction"

A common shortcut turns a PDF into text by deleting non-ASCII bytes. **Measured on Sandve et al.
2013 (PLOS, CC-BY): it yields 83,466 characters of PDF font-width tables** ("354 781 604 927 …")
that do **not** contain the paper's own Rule 1 sentence. `pypdf` on the same file recovers 24,702
characters including the sentence. PDF content streams are compressed; there is no byte-level
shortcut, and any text produced that way should be treated as unread.

## The refusals

- **Open-access copies only, decided per location.** A provider returns a `Location` only for a
  copy it itself reports as open (Europe PMC `isOpenAccess`, PMC `is_pmc_openaccess` — or an NIH
  author manuscript PMC deposits under `license_code: "TDM"`, stored with licence `TDM` and version
  `manuscript`, which permits reading and mining and is not a reuse licence — OpenAlex per-location
  `is_oa`, Unpaywall `best_oa_location`, a preprint server's own copy), so a paper with no open copy
  anywhere ends with **no download attempted** and every provider's answer recorded. No paywall
  route exists whichever providers are enabled, and no shadow libraries — *a copy that cannot be
  legally obtained is recorded as unread*, enforced at retrieval. A copy the user legitimately holds
  enters through `add_local` / `paper-fetch add`, which **refuses to store it without a declared
  `rights` string**.
- **Private, always.** No code path sets an ACL (asserted, `tests/test_guarantees.py`), every write
  goes through a prefix guard that refuses anything outside `papers/` or containing `..`, and each
  stored full text is checked the way a stranger would ask: on S3 an **unsigned GET** that must not
  succeed; on disk, files `0600` in directories `0700`, with any group- or world-accessible file or
  directory on the path refused. Measured on an S3-compatible bucket: **403** on a stored PDF,
  a stored text and the catalogue. Open access is a right to read, not to republish, and `bronze`
  OA carries no licence at all.
- **Validated before it counts.** A "PDF" must begin `%PDF-`, XML must parse, and an HTML page is
  refused — publisher PDF links that return a login page are the commonest way a library fills with
  junk. A provider-published md5 must match. Text must pass `looks_like_prose` (≥ 2,000 characters,
  ≥ 55% word tokens), which the byte-strip output above fails. **No byte-strip extraction exists
  anywhere** (asserted).
- **No email is sent unless configured.** `PAPER_FETCH_EMAIL` enables OpenAlex's polite pool and the
  Unpaywall route (Unpaywall requires one). There is no default address in the code (asserted).

## Index-first, and why a stale index cannot cause a re-download

`fetch()` looks in the catalogue first, then at the **authoritative** per-work objects
(`papers/doi/<doi>.json` → `papers/works/W…/provenance.json`), and only then asks OpenAlex. **The
catalogue is derived**; losing it costs a slower lookup and an automatic repair, never a second
download — `tests/test_library.py::test_a_deleted_catalogue_is_repaired_without_network` deletes it
and asserts zero network calls. `paper-fetch rebuild-index` recreates it whole.

Measured: a warm re-fetch of three papers made **0** outside calls; with the local cache **deleted**
(a different machine) **0**; by W-id and by a capitalised DOI URL, **0**.

A paper that could not be obtained is **remembered** with a `retry_after` 30 days out, because OA
status changes (embargoes lift, preprints get deposited) — and without it every lookup of a
paywalled paper would re-query four services.

**Several processes share one catalogue** (MCP servers, the CLI, other machines on the same
bucket). Since 2026-09-29 (`bee1bfe`) the catalogue is always read from the store, never from the
S3 read-through cache, and a save re-reads it and writes only this process's added, changed and
removed rows onto the latest copy (`tests/test_shared_index.py`). Before that, a process wrote back
its whole in-memory copy — or trusted a stale cached one — and silently dropped whatever others
had added. Two saves in the same instant can still race; the window is the time between one read
and one write, and the authoritative per-work objects repair any row lost in it
(`paper-fetch rebuild-index`).

**Several threads share one Library.** The MCP library runs every tool call on a worker thread
(`anyio.to_thread`), so one server's calls overlap. That was assumed away at first and failed in
service: 13 `retrieve` calls died on the passage index's one SQLite connection ("bad parameter or
other API misuse", once garbled text). Since 2026-10-06 each shared part has a lock, and none is
held across a fetch or a search: the passage index's connection (embedding stays outside); the
in-memory catalogue (readers walk a snapshot from `_rows()`, writers go through `_record` /
`_forget`, and a save updates the dictionary in place without ever emptying it, so a lookup
mid-save cannot take a held paper for one to fetch); the search memory's and a collection's
read-modify-write; the lazily built parts; and each provider's request spacing, which two threads
could otherwise both pass at once. `tests/test_threads.py` runs each from 16 threads and fails
without its lock. The locks are per process: across processes the paragraph above still applies.

## Honest status

- **Every provider has run live**, including the OpenAlex content route (with a key: a PDF
  retrieved from `content.openalex.org`, licence cc-by, text passed the gate, and the key appears
  **0 times** in stored provenance). The `grobid-xml` variant is still unexercised.
- **`PAPER_FETCH_EMAIL`**, when set, is sent to Unpaywall, OpenAlex (polite pool), NCBI (`idconv`,
  E-utilities) and in the User-Agent, as each service asks.
- **Papers OpenAlex does not know** are keyed `doi-…` / `arxiv-…` / `pmid-…` / `pmcid-…` by the
  identifier they arrived with; tested with a fake, not yet met in the wild.
- **A PMCID is converted before OpenAlex is asked.** OpenAlex cannot look a work up by PMCID —
  `/works/pmcid:…` is a 404 in both spellings and `filter=ids.pmcid:` matches nothing — so a
  PMCID-only fetch used to store the paper untitled under `pmcid-…`, and one paper twice. NCBI's ID
  converter now supplies the DOI/PMID first. `Library.adopt_orphans()` (`paper-fetch
  adopt-orphans`) moves such orphans onto their OpenAlex ids; it deletes an orphan's objects only
  after its copy is in place, through the stores' `delete()`.
- **Citation intent is counts only, and only from scite.** A hit scite found carries its tally
  (supporting / contrasting / mentioning); the citing statements themselves are not kept, and
  `lib.citations()` (OpenCitations) still gives links without stance.
- **Profile ranking and recall are lexical.** Profile ranking uses titles only (providers return no
  abstracts), and recall matches words, not meaning: "memory consolidation" and "sleep-dependent
  learning" are different concepts to it unless a profile declares one a synonym of the other.
- **The memory is one object rewritten per search**, like the catalogue (~1–3 KB per search).
- **The catalogue is one JSON-lines object re-read and rewritten on each change** — safe for
  several writers bar the instant-race above, but its cost grows with the library (HARVEST.md).
- **Dependencies:** `pypdf` (pure Python, BSD-3) for PDF text, and `fonttools` so pypdf can decode
  CFF (Type 1C) font encodings instead of warning and possibly mis-mapping glyphs. `boto3` only with
  the `s3` extra, `mcp` only with the `mcp` extra.
