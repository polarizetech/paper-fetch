# HARVEST.md — bulk-collecting a topic into the library

**Read this before harvesting a subject area into paper-fetch.** It is the procedure for the request
*"find every paper you can on X and keep the archive current"*, using only what the tool has today.
Nothing here needs new code. Where a step is slow or capped, the number is measured and the cap is
named, so a run can be sized before it starts rather than abandoned halfway.

The operator's framing (2026-09-16): give the agent a topic, get a stocked archive, and keep it
stocked. The collections/scheduling design that would automate this is **not built** — see
*If we build it* at the end. Until then, an agent runs the steps below in a session.

## 0. Before fetching anything — four questions for the operator

A harvest spends money (small), disk (large) and hours (many), so confirm:

1. **The subject**, concretely enough to become filters: OpenAlex topic ids, a keyword query, or
   both. *"reproducible computational research"* is a starting point, not a definition.
2. **How far back.** Two years is the usual answer; "everything" multiplies every number below.
3. **A cap for this run** — papers, not vibes. 500 is a comfortable session; 12,000 is days.
4. **Open access only?** The library retrieves nothing else, so a big fraction of any field will be
   listed as not obtainable. That is the expected outcome, not a failure.

Also confirm **which store** the run writes to (`PAPER_FETCH_STORE`): a harvest into the shared
bucket is visible to every consumer; one into a local directory is not.

## 1. Turn the subject into filters

OpenAlex groups every paper into a **topic** (~4,500 of them). Topic listing is free.

```bash
export OPENALEX_API_KEY=...   # the key paper-fetch reads from ~/.config/paper-fetch/*.env
curl -s "https://api.openalex.org/topics?search=reproducibility&per_page=8&select=id,display_name,works_count&api_key=$OPENALEX_API_KEY" \
  | python3 -m json.tool | grep -E 'id|display_name|works_count'
```

A topic is **broad** — one measured topic held 17,386 papers from the last two years alone. Expect to pair the topic with a keyword filter (`title_and_abstract.search:`)
and to tell the operator both counts before fetching.

## 2. Count and price the harvest BEFORE fetching (always)

```bash
F="topics.id:T<id>,from_publication_date:2024-09-01,is_oa:true"
curl -s "https://api.openalex.org/works?filter=$F&per_page=1&select=id&api_key=$OPENALEX_API_KEY" \
  | python3 -c "import json,sys;m=json.load(sys.stdin)['meta'];print(m['count'],'works, list cost/page $',m['cost_usd'])"
```

Measured 2026-09-16, with our free key:

| what | measured |
|---|---|
| list page of 200 works | **$0.0001** — two years of one topic is about **$0.01** |
| daily allowance | **$1.00** keyed ($0.10 unkeyed) |
| **daily request cap** | **10,000 requests** — the real limit, not the money |
| one topic, 2 years | 17,386 works; **12,022 open access** |
| one topic, 16 days | 370 works |
| catalogue row | ~600 bytes per held paper |

**Report these to the operator and get a cap before step 4.**

## 3. Collect the candidate list (cheap, no downloads)

Page with a cursor, keeping only identifiers. `per_page=200`, `select=id,doi,title,publication_year,open_access`.
Save to a scratch file (`$TMPDIR`, never a repository). This costs pennies and can be redone freely,
so **do it in full even when the fetch will be capped** — the list is what makes the run resumable
and what tells you the true size of the field.

## 4. Fetch, in batches, against a budget

Feed identifiers to `Library.fetch()` (a DOI, or the `W…` id), or to `paper-fetch fetch id1 id2 …`.
It is index-first, so re-running costs nothing for papers already held, and a paper it could not
obtain is remembered for 30 days. From Python, load the env files first
(`paper_fetch.config.load_env()`) — `Library.default()` does not read them; the CLI does.

Three limits that decide how you write the loop:

- **One OpenAlex request per paper minimum**, so the 10,000/day cap means roughly **2,000–3,000
  papers per day** once the other providers' calls are counted. A 12,000-paper backfill is 4–6 days.
- **The catalogue is re-read and rewritten after every paper.** Each save reads the latest
  `papers/index.jsonl` from the store and writes it back whole (so concurrent writers keep each
  other's rows). At 600 bytes a row, a 5,000-paper library moves ~3 MB down and ~3 MB up per
  fetch. Tolerable for a few hundred papers per session; painful beyond that, and batching that
  write is the first code change worth making.
- **With the S3 store, every stored file is also written to the read-through cache**
  (`PAPER_FETCH_CACHE`, default `~/.cache/paper-fetch`, under `s3/<bucket>/`). A two-year topic
  backfill is plausibly 15–20 GB, so check free disk first, point the cache at scratch for a big run and clear it afterwards: `PAPER_FETCH_CACHE=$TMPDIR/paper-fetch-cache`. The bucket
  copy is the real one; the cache only saves a re-download. With the local store there is no
  cache — `PAPER_FETCH_DATA_DIR` *is* the library, so size its disk for the whole backfill.

Between batches, print progress, and **stop at the operator's cap** rather than continuing quietly.

## 5. Record what the run did

Write a short note beside the collection (a scratch markdown file is fine, or the consuming
project's repository if the subject has a corpus there): the filters used, the date window,
candidates found, fetched, not obtainable with the commonest reason, OpenAlex spend
(`paper-fetch status` reports this process's spend and what OpenAlex says remains), and where it
stopped. Without it the next session cannot tell an unfinished harvest from a finished one.

## 6. Updating later

**OpenAlex's "added since" filter needs a paid plan** — measured: `from_created_date` returns
*"Plan upgrade required"*. So an update re-runs the same filter with
`from_publication_date` set **~60 days before the last run**, not at it: indexes add papers late, and
the overlap is what catches them. Re-fetching the overlap is free, because held papers cost nothing.

For the same reason, PubMed is the better update source when the field is biomedical — its
`datetype=edat` really is "entered the database on", which OpenAlex charges for.

## What a harvest may not do

- **Open-access copies only.** No paywall route, no shadow libraries, no scraping a publisher's PDF
  link — the library refuses all of it and the refusal is the point. A field where most papers are
  closed produces a small archive and an honest "not obtainable" list.
- **Never quote a count of "all papers on X".** What a harvest holds is *what these indexes list
  under these filters, that are open access* — three qualifiers that all belong in the sentence.
- **Do not widen the cap on your own.** Papers, storage and spend are the operator's.

## If we build it

The design discussed 2026-09-16, deliberately not built: a **collection** (topic definition in git,
membership list in the store), a **harvest** step that pages each source by date window with a
trailing re-check, a **screening** step that keeps exclusions with reasons rather than dropping them
silently, a **fetch queue** with a daily budget, and a **launchd job** with a per-run report. The
two scale fixes above (batch the catalogue write, optional cache bypass) are prerequisites for it.
Nearest prior art: [living-review-updater](https://github.com/mattebso/living-review-updater) for
re-running a search against what you have already seen, and
[findpapers](https://github.com/jonatasgrosman/findpapers) for one query across many databases.
