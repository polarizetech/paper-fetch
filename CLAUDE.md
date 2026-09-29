# CLAUDE.md — paper-fetch

**One private, deduplicated library of open-access full texts, found through TWELVE swappable
providers plus a web-search fallback, stored in a local directory or any S3-compatible bucket (the
organisation's Spaces bucket in practice), and checked before anything is downloaded.** A paper
retrieved once is never retrieved again, by any project, on any machine that shares the store.

Extracted from the audio-projects monorepo's `tools/paper-library` (package `paperlib`): first
released here 2026-09-24, renamed `paper-fetch` 2026-09-28; the monorepo copy retired 2026-09-29.
Dates below before 2026-09-24 are measurements made on the monorepo copy, same code.

Run (from a checkout; an installed copy drops the `uv run`):

```
uv run paper-fetch fetch 10.1371/journal.pcbi.1003285
uv run paper-fetch search "auditory steady-state response 40 Hz"     # every provider, merged
uv run paper-fetch providers                                         # what is on, what each needs; no network
uv run paper-fetch library "reproducible" --full-text
uv run paper-fetch citations 10.1016/j.cub.2024.06.028               # who cites it; --references for what it cites
```

Configuration is `PAPER_FETCH_*` plus five provider keys, read from the environment and from
`~/.config/paper-fetch/*.env` — the table is in [README § Configure](README.md#configure). The store
is `PAPER_FETCH_STORE=local` (default, `~/.local/share/paper-fetch`), `s3` or `memory`.

Tests: `uv run pytest` — **214/214** offline, no credentials, no network (HTTP is a fake serving
`tests/fixtures/`). The monorepo's eight live `--network` checks were not brought over; there are
no live tests here. Lint and types: [README § Development](README.md#development). Wiring a
project in → [`INTEGRATION.md`](INTEGRATION.md).

**Asked to collect a whole subject area and keep it current? Read
[`HARVEST.md`](HARVEST.md) first** (2026-09-16) — the procedure for a bulk topic harvest with what
it costs measured: the OpenAlex daily cap is **10,000 requests**, not the $1, so roughly 2,000–3,000
papers a day; a list page of 200 costs $0.0001; one broad topic is 17,386 works over two years,
12,022 of them open access; `from_created_date` needs a **paid plan**, so updates re-query by
publication date with a ~60-day overlap. It also names the two things that bite at scale — the
catalogue is re-read and rewritten per paper (~600 B/row), and with the S3 store every stored file
is also copied into `PAPER_FETCH_CACHE` (8.5 GB free on the bench Mac, 2026-09-16). No collections,
screening or scheduling are built; the design for them is at the end of that file.

## ⭐ Over MCP

`paper-fetch-mcp` (`src/paper_fetch/mcp_server.py`, the official `mcp` SDK: `FastMCP` on 1.x,
`MCPServer` on 2.x; needs the `mcp` extra). Eight tools: `search`, `fetch`, `library`, `text` (paged,
100,000 characters by default, clamped to 1,000..100,000), `provenance`, `citations`, `providers`,
`status`. Every result is `{"ok": true, "data": ...}` or `{"ok": false, "code": "not_found" |
"unavailable" | "tool_error", "error": ...}`; `unavailable` is never "zero results". The full
contract is in [README § MCP server](README.md#mcp-server). The server's instructions to the model
say open access is a right to read, not to republish. **Storing an operator-held copy is not a
tool** — its rights statement is the operator's to make; use `paper-fetch add`.

It reads credentials itself from `~/.config/paper-fetch/*.env` (or `$PAPER_FETCH_ENV_DIR`), because
a client launches it with no shell to `source` in. It takes only `PAPER_FETCH_*` and the five
provider keys — **any other secret in the same file, such as a full-account cloud token, is not
loaded** (`tests/test_config.py`) — and never overrides a variable already set. The CLI does the
same; `Library.default()` called from Python does not (see INTEGRATION.md).

Verified 2026-09-16 (on the monorepo copy) with every credential scrubbed from the parent
environment: all eight tools answered against the live bucket (175 works, 148 with full text).
Here, `tests/test_mcp_server.py` drives the real entry point over stdio.

## Why it exists

- **Retrieval of papers had no shared home.** OpenAlex was being called ad hoc from `research`
  scripts through a local disk cache, and full texts were fetched onto one machine with no shared
  index. This library replaced both (2026-09-13).
- **[dataset-fetch](https://github.com/polarizetech/dataset-fetch)** owns retrieval of *datasets*
  into a content-addressable cache; its types are dataset-shaped (pinned snapshots, BIDS selectors)
  and a paper is not one. **dataset-publish** owns making things *public*, and papers must never
  be. So this is a separate job with a separate prefix, and it imports neither.
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

| provider | search | locate | key | measured 2026-09-13 |
|---|---|---|---|---|
| `openalex` | $0.001 | OA locations; own copies with a key | optional | $0.10 unkeyed / **$1.00 keyed** per day; own PDF retrieved with key |
| `europepmc` | ✓ incl. preprints | OA JATS by PMCID | — | 10,127 OA hits, "auditory steady-state response" |
| `pubmed` | ✓ MeSH-aware | (yields PMCIDs) | optional `NCBI_API_KEY` | 3 req/s; **"Evoked Potentials, Auditory"[MeSH] + free full text: 6,501** |
| `pmc-s3` | — | PMC OA subset, plus NIH author manuscripts under PMC's TDM licence (since 2026-09-15): xml/pdf/txt **with md5** | — | anonymous AWS bucket; PDF byte-identical to the publisher's (131,527 B) |
| `openaire` | ✓ | copies with an open licence, on records OpenAIRE marks OPEN | — | 2026-09-14: 1,825 publications for "auditory steady-state response"; ~7,200 req/window unkeyed; open status is per RECORD, licence per copy; retrieved England 2024 *Aerial electroreception* from Bristol's repository |
| `plos` | ✓ every word, or a quoted phrase | JATS then PDF, built from the DOI | — | 2026-09-14: **4/4 retrieved** (waggle dance); docs limit 10/min, so 6.1 s spacing; licence not in the response (CC BY by policy) |
| `osf` | ✓ via SHARE (PsyArXiv and other OSF servers) | `osf.io/<id>/download` when the preprint is CC-licensed | — | **4/4 retrieved** (ASSR, ERP, phase-locking preprints) after fixing the DOI lookup: SHARE's `identifier` filter returned 0 for a DOI it holds, `sameAs` finds it; OSF's own v2 API 502s, so it is not used; **0 bee preprints** on SHARE |
| `hal` | ✓ file records only | `fileMain_s` when `openAccess_bool` | — | **4/4 retrieved** (waggle dance); licence is often HAL's deposit authorisation, which permits reading, not reuse |
| `doaj` | ✓ search only | — (links are landing pages) | — | found 4 papers not yet held, all then fetched through other providers — including *The Electronic Bee Spy* (electrostatic recordings of honeybee communication) |
| `core` | ✓ | repository PDF | optional `CORE_API_KEY` | 17.5 M; 10 req/window; **no licence field** |
| `biorxiv` | — (API has no keyword search: 404) | JATS + licence by DOI | — | retrieved a 2026 bioRxiv preprint |
| `unpaywall` | — | best OA PDF | `PAPER_FETCH_EMAIL` | **first live run 2026-09-14: 2/3 retrieved** (PLOS, Frontiers); knew no open copy of England 2024, which OpenAIRE found in Bristol's repository |
| `web` | **fallback only** | — never a copy source | `SEARXNG_URL` | **live via local SearXNG** 2026-09-14: 10 results for a 40 Hz ASSR query, identifiers parsed from PLOS, PubMed, PMC and Frontiers URLs |

**Health check 2026-09-14** (one query, every provider, cache bypassed): every provider above answered.

**Removed 2026-09-14 at the operator's request** (in the monorepo's history at `fde2391b`, before
extraction): **`arxiv`** (HTTP 429 on the first request, every run since 2026-09-13),
**`semanticscholar`** (429 on every unkeyed call), and **`zenodo`** (retrieved 4/4 with md5
verification, but its search matched loosely — optimisation-algorithm papers for "waggle dance").
arXiv **identifiers** are still understood (`arxiv:ID`, resolved through the arXiv DataCite DOI):
they arrive from CORE, OpenAIRE and OpenAlex, and an arXiv paper is still fetched through those.

**Web search is the FALLBACK, not a tenth index** (`providers/web.py`, 2026-09-14). It runs inside
`search()` only when the providers above return **no open-access hit** — its report says `why_ran`,
or `not-needed` when an open hit exists — or when `web` is named explicitly; `web_fallback=False` /
`PAPER_FETCH_WEB_FALLBACK=0` turns it off. Backend: a SearXNG instance, opt-in by `SEARXNG_URL`. (A
Brave Search API backend existed 2026-09-13 → 14 and was removed at the operator's request; it is in
the monorepo's commit `b43ccbbc`.) A SearXNG answer with **no results and unresponsive engines is
reported `unavailable`**, not empty — the silent degradation noted below, closed where it can be. A
web hit carries its URL and **identifiers parsed out of the URL or snippet, unverified**; `is_oa` is
`None`; `can_locate` is False, so nothing is ever downloaded from a web result — a parsed DOI goes
through `fetch()` and the normal providers decide openness. **Web results are not cached**
(`cacheable = False`): what may be stored depends on the engines SearXNG queried, and that has not
been checked.

### The local SearXNG instance (2026-09-14)

On the bench machine: `docker` container `searxng` (image `searxng/searxng`, `--restart
unless-stopped`) on **`127.0.0.1:8888` only**; its config `settings.yml` (mode 600, holds the
instance secret) lives outside any repository, and `SEARXNG_URL=http://127.0.0.1:8888` goes in
`~/.config/paper-fetch/*.env`. JSON output is enabled, the rate limiter is off because nothing
outside this machine can reach it, and outbound timeouts were raised to 8 s after every engine
timed out at the 3 s default.

**Every Google engine is removed** (`google`, `google scholar`, `google news`, `google images`,
`google videos`, `google cse`, `google cse images`) — the same terms-of-service reason Google Scholar
is not a provider. The first `remove` list missed `google cse`, which then supplied half the results;
the enabled engine list was read back from `/config` to confirm.

**Measured on day one: DuckDuckGo returned `CAPTCHA` to this instance on its first three searches**,
so it was **removed** from the engine list at the operator's request. The fallback runs on **one
general engine, Brave's web results scraped by SearXNG** (plus Wikipedia/Wikidata). The engines it
queries do not offer this access by agreement, so blocking is expected and the instance will
degrade. Zero results with unresponsive engines is reported `unavailable`, but a PARTIAL block still
just returns fewer results. Check `unresponsive_engines` in its JSON when results look thin:
`curl -s "http://127.0.0.1:8888/search?q=test&format=json" | python3 -m json.tool | grep -A5 unresponsive`.

**Deliberately not providers:** **Google Scholar** (no API; its terms forbid automated querying;
OpenAlex and CORE cover its discovery role), **Crossref as a full-text route** (its `link`s are
publisher *text-mining* licences such as `springer.com/tdm`, not open access), and **DOAJ as a copy
source** (its full-text links are landing pages, which the validator refuses — it is a search-only
provider). `tests/test_guarantees.py` asserts there is no Google Scholar and no Crossref full-text
route.

**Federated search reports every provider by name** — `ok`, `cache`, `skipped` (missing key),
`unavailable` (429 / unreachable) or `error` — and merges hits across providers by DOI → PMCID →
PMID → OpenAlex → arXiv. Measured: 4 answering providers returned 18 distinct papers for one query,
two found by both Europe PMC and PubMed, and a repeat served every provider from the store at $0.

**Identifiers are enriched before any provider is asked.** OpenAlex's record for a 2014 PLOS paper
had no PMCID, so the PMC bucket — the most verifiable route — reported "no copy" for a paper that is
in PMC. NCBI's ID converter (moved to `pmc.ncbi.nlm.nih.gov` along with PMC) filled `PMC3899078`, and
the re-fetch came from the bucket **with its md5 verified**. It also returned a PMCID for a 2026
bioRxiv preprint. A failure there is never fatal.

**Two defects found by running it, both pinned by tests.** `Http` retried a **429** internally and
then raised a generic `ConnectionError`, so no provider ever saw the rate limit: a federated search
reported arXiv and Semantic Scholar as `error` and took **2 minutes**. A 429 (any 4xx) is now
returned on the first attempt and becomes `unavailable`; providers carry a 20 s timeout and one
retry, and the same search then reported the two refusals correctly. And OpenAlex lacking a PMCID
(above) had been silently skipping the best route.

## ⭐ The citation graph — OpenCitations (2026-09-15)

`lib.citations(id)` (works that cite it) and `lib.references(id)` (works it cites), from the
**OpenCitations Index v2** (`src/paper_fetch/citations.py`). **Not a provider**: it neither searches
by query nor locates a copy, so it sits beside the providers rather than in the registry. Every
returned work is marked `in_library` / `full_text_in_library`, which makes it a snowballing tool:
read what cites a paper you hold, then `fetch()` the ones you don't.

- **Measured live:** no key needed (an optional `OPENCITATIONS_ACCESS_TOKEN` goes in the
  `authorization` header, as the docs ask of applications, and belongs in
  `~/.config/paper-fetch/*.env`; a wrong token gets **403 "Invalid token"**, raised by name);
  documented limit **180 requests/min per IP**, and requests are spaced to it; Sandve 2013 → **664
  citations in 2.3 s** and 26 references; England 2024 *Aerial electroreception* → 5 citing works,
  **2 already held**.
- **One request stalled 60 s and the identical retry answered in 0.7 s**, so calls carry a 60 s
  timeout and one retry, and a refusal raises `CitationsUnavailable` — never an empty list.
- **Identifiers:** OpenCitations takes a DOI or PMID. A W-id, PMCID or arXiv id is turned into one
  through the free OpenAlex lookup first; a work with neither raises `NotFound`.
- **Cached** in the store at `papers/citations/{citations,references}/<doi>.json` for 30 days
  (citations accumulate, so `refresh=True` / `--refresh` re-asks).
- **A field renamed on the way through:** OpenCitations' `creation` is the **citing** work's date,
  so on a references list every row carries the same date — the paper's own. It is returned as
  `citing_date`, and the CLI shows it only for citations.
- **What it does not carry:** titles (identifiers only), and **citation intent**. Supporting /
  contrasting classification stays with scite.

## Candidate providers surveyed live (2026-09-14)

Anonymous probes, no 429s. Verdicts in the order they would be built. PLOS, OSF and HAL were then
built; Zenodo was built and removed (above).

| service | key | search | copy | measured | verdict |
|---|---|---|---|---|---|
| PLOS (`api.plos.org/search`) | — | ✓ full-text field; 138 for "auditory steady-state response" | JATS XML (`type=manuscript`) and direct PDF | licence not in the response (CC-BY by policy); docs: 10/min, 7200/day | add |
| Zenodo (`zenodo.org/api/records`) | — | ✓ 7 ASSR / 856 "electroencephalography" | direct file, `license.id` per record | `x-ratelimit-limit: 30` on search; many uploads are publisher PDFs, so trust only the licence field | add |
| OSF Preprints / PsyArXiv | — | via SHARE (`share.osf.io/api/v3/index-card-search`, 2.4 s); v2 API filters titles only and timed out (502 at 60 s) unscoped | direct file (signed redirect) with md5, licence by separate call | preprint DOI is `links.preprint_doi`; `attributes.doi` null on 3 of 3 | add |
| HAL | — | ✓ modest (5–7 hits) | `fileMain_s` direct PDF, `licence_s` | many records are metadata-only notices | add, low yield |
| Springer Nature OA | free key | — | JATS | unkeyed 401; mostly BMC/SpringerOpen, largely already in PMC | maybe |
| DOAJ | — | ✓ but quoted phrases not honoured | landing pages only | licence null | search only, maybe |
| ChemRxiv, Internet Archive Scholar | — | — | — | Cloudflare challenge / bot-verify redirect | skip |
| scholar.archive.org (re-probed 2026-09-16) | — | `/search?format=json` 302s to `/verify`, an ALTCHA challenge | — | `robots.txt` disallows `/search` and `/verify`; `api.fatcat.wiki` timed out at 60 s twice | **skip** — no sanctioned programmatic route; revisit only if IA publishes one |
| metapub FindIt (0.7.4, checked 2026-09-16) | — | — | publisher PDF URLs, verified by fetching | **0 of the 27 not-obtainable works** (0 of the 10 from the ADHD literature map): 21 `DENIED` (paywalled ScienceDirect/Nature/Springer/APA/LWW links returning non-PDF or 403), 4 errors (no PMID, or a valid PMID rejected), 1 no handler, 1 incomplete PubMed record | **skip** — it finds publisher copies, and for these works every publisher copy is closed |
| Research Square | — | undocumented site endpoint | — | not a public API | skip |
| fatcat | — | — | — | timed out | skip |

## Measured against the live APIs (2026-09-13), each of which changed the code

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

`research`'s old `fetch_fulltext.py` turned a PDF into text by deleting non-ASCII bytes. **Measured
on Sandve et al. 2013 (PLOS, CC-BY): 83,466 characters of PDF font-width tables** ("354 781 604 927
…") that do **not** contain the paper's own Rule 1 sentence — and it would have recorded
`full_text_read: true` with a sha256 of that output. `pypdf` on the same file recovers 24,702
characters including the sentence. PDF content streams are compressed; there is no byte-level
shortcut. Reported to `research` 2026-09-13; its PDF path now goes through this library. **Any
`research` ledger entry marked `full_text_read: true` via that script's old Unpaywall/PDF path
should be treated as unread.**

## The refusals

- **Open-access copies only, decided per location.** A provider returns a `Location` only for a
  copy it itself reports as open (Europe PMC `isOpenAccess`, PMC `is_pmc_openaccess` — or, by
  operator ruling 2026-09-15, an NIH author manuscript PMC deposits under `license_code: "TDM"`,
  stored with licence `TDM` and version `manuscript`, which permits reading and mining and is not a
  reuse licence — OpenAlex per-location `is_oa`, Unpaywall `best_oa_location`, a preprint server's
  own copy), so a paper with no open copy anywhere ends with **no download attempted** and every
  provider's answer recorded. No paywall route exists whichever providers are enabled, and no
  shadow libraries — *a copy that cannot be legally obtained is recorded as unread*, enforced at
  retrieval. A copy the operator legitimately holds enters through `add_local` / `paper-fetch add`,
  which **refuses to store it without a declared `rights` string**.
- **Private, always.** No code path sets an ACL (asserted, `tests/test_guarantees.py`), every write
  goes through a prefix guard that refuses anything outside `papers/` or containing `..`, and each
  stored full text is checked the way a stranger would ask: on S3 an **unsigned GET** that must not
  succeed; on disk, files `0600` in directories `0700`, with any group- or world-accessible file or
  directory on the path refused. Measured on the Spaces bucket (2026-09-13): **403** on a stored
  PDF, a stored text and the catalogue; dataset-publish's org-wide `audit` independently reported
  `papers  [PRIVATE]`. Open access is a right to read, not to republish, and `bronze` OA carries no
  licence at all.
- **Validated before it counts.** A "PDF" must begin `%PDF-`, XML must parse, and an HTML page is
  refused — publisher PDF links that return a login page are the commonest way a library fills with
  junk. A provider-published md5 must match. Text must pass `looks_like_prose` (≥ 2,000 characters,
  ≥ 55% word tokens), which the measured font-table output fails. **No byte-strip extraction exists
  anywhere** (asserted).
- **No email is sent unless configured.** `PAPER_FETCH_EMAIL` enables OpenAlex's polite pool and the
  Unpaywall route (Unpaywall requires one). There is no default address in the code (asserted).

## Index-first, and why a stale index cannot cause a re-download

`fetch()` looks in the catalogue first, then at the **authoritative** per-work objects
(`papers/doi/<doi>.json` → `papers/works/W…/provenance.json`), and only then asks OpenAlex. **The
catalogue is derived**; losing it costs a slower lookup and an automatic repair, never a second
download — `tests/test_library.py::test_a_deleted_catalogue_is_repaired_without_network` deletes it
and asserts zero network calls. `paper-fetch rebuild-index` recreates it whole.

Measured live: a warm re-fetch of three papers made **0** outside calls; with the local cache
**deleted** (a different machine) **0**; by W-id and by a capitalised DOI URL, **0**.

A paper that could not be obtained is **remembered** with a `retry_after` 30 days out, because OA
status changes (embargoes lift, preprints get deposited) — and without it every lookup of a
paywalled paper would re-query four services.

**Several processes share one catalogue** (MCP servers, the CLI, other machines on the same
bucket). Since 2026-09-29 (`bee1bfe`) the catalogue is always read from the store, never from the
S3 read-through cache, and a save re-reads it and writes only this process's added, changed and
removed rows onto the latest copy (`tests/test_shared_index.py`). Before that, a process wrote back
its whole in-memory copy — or trusted a stale cached one — and silently dropped whatever others
had added. Two saves in the same instant can still race; the window is the time between one read
and one write, and the authoritative per-work objects repair any row lost in it.

## Honest status

- **One consumer: `research`** (since 2026-09-13; through this repo since 2026-09-29, installed
  `paper-fetch[s3]` from git and pointed at the same Spaces bucket, identical `papers/` layout).
  Its `scripts/_papers.py` builds the library (the bucket if credentials are present, otherwise an
  in-memory store that says so). Two scripts use it: **`novelty_probe.py`** (prior-art search,
  closed access included) and **`fetch_fulltext.py`** (retrieval). `novelty_probe` had the same
  defect `Http` had: it caught every exception as `[]`, so a rate-limited index counted toward *all
  indexes empty* and could flag a CANDIDATE on a search that never ran — measured on its first run
  through the library, Semantic Scholar answered 0/5 and arXiv 1/5. A refusal now forces
  `INCONCLUSIVE`; it runs the web fallback on any formulation the indexes found nothing for, a web
  hit counts as prior art, and web search being unconfigured does not force `INCONCLUSIVE`. Its
  `reputation.py`, `find_contradictions.py`, `verify_citations.py` and `check_retractions.py` still
  call registries directly: they need citation counts, citation intent and Crossref update notices,
  none of which this library carries.
- **57 works in the library, 55 with full text** (2026-09-14), after 18 more from the new providers'
  searches (waggle dance, honeybee communication, ASSR/ERP/phase-locking preprints). Stored through
  the full chain, most copies still came from the PMC bucket or OpenAlex — the new providers earn
  their place mostly by **finding** papers; OSF and OpenAIRE also supplied copies no earlier
  provider had. 175 works, 148 with full text, by 2026-09-16.
- **37 works in the library, 35 with full text** (2026-09-13). 29 of them are the research repo's
  open-access papers (its `FULL_TEXT_READ.yaml` list plus stored `papers/` folders), fetched by DOI
  rather than copied, so each has its own provenance: 24 from the PMC bucket with md5 verified,
  4 from OpenAlex's own copy, 1 from CORE (Sutton 2016 — CORE's locate path, previously
  unexercised). Kennedy 2014 (*Nat Neurosci*, green OA per OpenAlex) was **not obtainable** by any
  route. Seven research papers are closed access and were not attempted. Earlier holdings: Sandve
  2013, Bidelman 2024 (publisher PDF via OpenAlex), Noble 2009 (Europe PMC), McFadden et al.'s
  40 Hz ASSR reliability 2014 (**PMC bucket, md5 verified**), an eLife 2025 alpha paper (Europe
  PMC), a 2026 bioRxiv 40 Hz preprint (**bioRxiv JATS, licence `cc_no`** — free to read, not a reuse
  licence), and Hinsen 2019 recorded *not obtainable* (closed).
- **The OpenAlex content route HAS run (2026-09-13), with a key**: MNE-Python (Gramfort et al.
  2013, W2169918686) retrieved from `content.openalex.org/…pdf`, licence cc-by, text passed the
  gate, and the key appears **0 times** in stored provenance. A key raises the daily allowance from
  **$0.10 to $1.00** (measured from the rate-limit headers). The `grobid-xml` variant is still
  unexercised.
- **Every remaining provider has run live.** Unpaywall's first run was 2026-09-14, once the contact
  email was set at the operator's request; that address (now `PAPER_FETCH_EMAIL`) is sent to
  Unpaywall, OpenAlex (polite pool), NCBI (`idconv`, E-utilities) and in the User-Agent, as each
  service asks.
- **Papers OpenAlex does not know** are keyed `doi-…` / `arxiv-…` / `pmid-…` / `pmcid-…` by the
  identifier they arrived with; tested with a fake, not yet met in the wild.
- **A PMCID is converted before OpenAlex is asked** (fixed 2026-09-16). OpenAlex cannot look a work
  up by PMCID — `/works/pmcid:…` is a 404 in both spellings and `filter=ids.pmcid:` matches nothing —
  so a PMCID-only fetch used to store the paper untitled under `pmcid-…`, and one paper twice. NCBI's
  ID converter now supplies the DOI/PMID first. `Library.adopt_orphans()` (`paper-fetch
  adopt-orphans`) moved the seven existing orphans onto their OpenAlex ids (six moved, one dropped
  as a duplicate of a held work); it deletes an orphan's objects only after its copy is in place,
  through the stores' `delete()`.
- **No citation intent.** scite's distinguishing feature — classifying citing statements as
  supporting or contrasting — is not here; OpenAlex and OpenCitations do not supply it.
- **No ranking or semantic search over the library** — `search_library` is term matching over
  titles, authors and, optionally, stored text.
- **The catalogue is one JSON-lines object re-read and rewritten on each change** — safe for
  several writers bar the instant-race above, but its cost grows with the library (HARVEST.md).
- **Dependencies:** `pypdf` (pure Python, BSD-3) for PDF text, and `fonttools` since 2026-09-28 so
  pypdf can decode CFF (Type 1C) font encodings instead of warning and possibly mis-mapping glyphs.
  `boto3` only with the `s3` extra, `mcp` only with the `mcp` extra.
