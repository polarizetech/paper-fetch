"""The passage index: which passage of which held paper answers a query.

    lib.retrieve("does slow-wave sleep improve recall", collection="review")

The catalogue answers "which papers"; this answers "which passage", which is what quoting and
grounding need. It is a local SQLite file derived from the store's full texts: FTS5 (BM25) always,
plus dense vectors when an embedding model is configured, fused by reciprocal rank. Deleting it
loses nothing; `paper-fetch index` rebuilds it.

- **Offsets are exact.** A passage is `text[start:end]` of the paper's *indexed text*: the stored
  full text with hidden formatting characters (zero-width, bidirectional controls) removed. The
  paper row keeps that text's SHA-256 and how many hidden characters were removed, so a quote can
  be traced to the exact stored copy and concealed text is visible to the caller.
- **Positions are stable.** A passage's `ord` (its place in its paper) is the same whenever the same
  text is indexed, so `<work>#p<ord>` survives rebuilding the index. Row ids do not.
- **The reference list is not indexed**: a bibliography entry matches every query about its topic
  and supports nothing.
- **Embeddings are optional and deterministic.** `PAPER_FETCH_EMBED_MODEL` (e.g. `bge-m3`) names a
  model served by an Ollama-compatible endpoint (`PAPER_FETCH_EMBED_URL`, default
  `http://127.0.0.1:11434`); it turns text into vectors and generates nothing. Unset, retrieval is
  lexical. Vectors need numpy (the `retrieval` extra). An index built with one model refuses
  another: vectors from different models are not comparable.

A few hundred papers is ~10^4 passages, so dense search is an exact matrix product. There is no
approximate-nearest-neighbour index to tune, drift, or explain.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .http import Http

__all__ = [
    "HIDDEN",
    "Embedder",
    "OllamaEmbedder",
    "Passage",
    "PassageIndex",
    "chunk",
    "clean",
    "embedder_from_env",
    "fts_query",
    "index_path",
]

#: The version of `chunk`. A paper indexed by an older one is re-chunked the next time it is
#: indexed (its unchanged passages keep their vectors). 2: reference lists without a heading
#: paragraph are recognised and dropped.
#:
#: The version is kept in its own table, `chunked`, beside the `indexed_at` of the paper row it
#: describes, and the `papers` table is unchanged: servers still running the previous code share
#: this file and keep working, and a paper one of them re-indexes simply counts as version 1
#: again (its row has a new `indexed_at`, so the old note no longer matches).
CHUNKER = 2

SCHEMA = """
create table if not exists meta (key text primary key, value text);
create table if not exists papers (
    work text primary key, doi text, pmid text, pmcid text, title text, year integer,
    authors text, is_retracted integer, oa_status text, license text, route text, format text,
    text_sha256 text, n_chars integer, hidden_chars integer, indexed_at text
);
create table if not exists passages (
    id integer primary key, work text not null, ord integer not null,
    start integer not null, "end" integer not null, text text not null
);
create index if not exists passages_work on passages(work);
create virtual table if not exists passages_fts
    using fts5(text, content='passages', content_rowid='id');
create table if not exists vectors (passage_id integer primary key, vec blob not null);
create table if not exists chunked (
    work text primary key, version integer not null, indexed_at text not null
);
"""

#: Zero-width, bidirectional-control and invisible formatting characters: used to hide text from
#: human readers. Written as escapes so the pattern itself is visible in review.
HIDDEN = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")
REFERENCES_HEADING = re.compile(
    r"^\s*(references|bibliography|literature cited|works cited)\s*$", re.I
)
# The same heading on a line of its own inside a paragraph, optionally numbered ("7. References").
REFERENCES_LINE = re.compile(
    r"(?im)^[ \t]*(?:\d{1,2}\.?[ \t]+)?(references|bibliography|literature cited|works cited|"
    r"reference list)[ \t]*:?[ \t]*$"
)
# A numbered bibliography entry at the start of a line: "12 Dias da Silva VJ", "[3] Smith".
NUMBERED_ENTRY = re.compile(r"(?m)^[ \t]*\[?(\d{1,3})[\].)]?[ \t]+(?=[A-Z])")
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
MIN_ENTRIES = 8  # a shorter numbered run is more likely a list in the body than a bibliography
SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")
WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-]+")
STOPWORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for", "from", "has", "have",
    "how", "in", "is", "it", "its", "of", "on", "or", "that", "the", "their", "this", "to", "was",
    "were", "what", "when", "where", "which", "who", "why", "with", "not", "no", "than", "then",
    "there", "these", "those",
})  # fmt: skip

Embedder = Callable[[list[str]], list[list[float]]]


def clean(text: str) -> tuple[str, int]:
    """The text as indexed, and how many hidden characters were removed from it."""
    return HIDDEN.sub("", text), len(HIDDEN.findall(text))


def references_start(text: str) -> int | None:
    """Where the reference list begins, or None if none is recognised.

    Two signals, both conservative. A heading on a line of its own ("References", "7. References")
    past the first third of the text. Or, for text extracted from a PDF, where the heading is often
    lost: a run of consecutively numbered entries that starts at 1, has at least MIN_ENTRIES
    entries, begins in the second half of the text, runs to its last tenth, and mostly carries
    publication years. A numbered list in the body fails at least one of those.
    """
    heading = next(
        (m.start() for m in REFERENCES_LINE.finditer(text) if m.start() > len(text) / 3), None
    )
    best: tuple[int, int] | None = None  # (entries, start of entry 1)
    run: list[int] = []  # start offsets of the entries of the current run
    expect = 1
    entries = [(m.start(), int(m.group(1))) for m in NUMBERED_ENTRY.finditer(text)]
    for at, number in [*entries, (len(text), -1)]:
        if number == expect:
            run.append(at)
            expect += 1
            continue
        if len(run) >= MIN_ENTRIES and run[0] > len(text) / 2 and run[-1] > len(text) * 0.9:
            spans = zip(run, [*run[1:], len(text)], strict=True)
            dated = sum(bool(YEAR.search(text[a:b])) for a, b in spans)
            if dated >= len(run) / 2 and (best is None or len(run) > best[0]):
                best = (len(run), run[0])
        run, expect = ([at], 2) if number == 1 else ([], 1)
    starts = [x for x in (heading, best[1] if best else None) if x is not None]
    return min(starts) if starts else None


def chunk(text: str, target: int = 1100, hard_max: int = 1600) -> list[tuple[int, int, str]]:
    """Split text into (start, end, passage) on paragraph, then sentence, boundaries.

    `text[start:end] == passage` always holds. The reference list is dropped (`references_start`).
    """
    cut = references_start(text)
    body = text if cut is None else text[:cut]
    spans: list[tuple[int, int]] = []
    pos = 0
    for para in re.split(r"(\n\s*\n)", body):
        start, pos = pos, pos + len(para)
        if not para.strip():
            continue
        if REFERENCES_HEADING.match(para):
            break
        if len(para) <= hard_max:
            spans.append((start, pos))
            continue
        cursor = start
        for piece in SENTENCE_END.split(para):
            at = body.find(piece, cursor, pos)
            if at < 0:
                continue
            spans.append((at, at + len(piece)))
            cursor = at + len(piece)

    out: list[tuple[int, int, str]] = []
    cur_start: int | None = None
    cur_end = 0
    for start, end in spans:
        if cur_start is not None and end - cur_start > hard_max:
            out.append((cur_start, cur_end, text[cur_start:cur_end]))
            cur_start = None
        if cur_start is None:
            cur_start = start
        cur_end = end
        if cur_end - cur_start >= target:
            out.append((cur_start, cur_end, text[cur_start:cur_end]))
            cur_start = None
    if cur_start is not None:
        out.append((cur_start, cur_end, text[cur_start:cur_end]))
    return [(s, e, t) for s, e, t in out if len(t.strip()) >= 80]


def fts_query(query: str) -> str:
    """Any of the query's content words, each quoted so FTS5 syntax in a query is inert."""
    terms = [w.lower() for w in WORD.findall(query)]
    return " OR ".join(f'"{t}"' for t in dict.fromkeys(terms) if t not in STOPWORDS)


@dataclass
class Passage:
    work: str
    ord: int
    start: int
    end: int
    text: str
    bm25_rank: int | None = None
    dense_rank: int | None = None
    score: float = 0.0

    @property
    def id(self) -> str:
        """Stable across rebuilds of the same text: the paper and the passage's position in it."""
        return f"{self.work}#p{self.ord}"

    @property
    def sha(self) -> str:
        return hashlib.sha256(self.text.encode()).hexdigest()[:8]

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "sha": self.sha, **asdict(self)}


class OllamaEmbedder:
    """Vectors from an Ollama-compatible `/api/embed` endpoint. Deterministic; generates nothing."""

    def __init__(
        self,
        model: str,
        url: str = "http://127.0.0.1:11434",
        timeout: float = 120,
        http: Http | None = None,
    ) -> None:
        self.model, self.url, self.timeout = model, url.rstrip("/"), timeout
        self.http = http or Http()

    def __call__(self, texts: list[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": texts, "keep_alive": "30m"}
        try:
            out = self.http.post_json(f"{self.url}/api/embed", payload, timeout=self.timeout)
        except ConnectionError as e:
            raise ConnectionError(f"embedding model {self.model}: {e}") from e
        return out["embeddings"]


def embedder_from_env() -> tuple[str, Embedder | None]:
    """(model name, embedder) from PAPER_FETCH_EMBED_MODEL / _URL; ("", None) when unset."""
    model = os.environ.get("PAPER_FETCH_EMBED_MODEL", "").strip()
    if not model:
        return "", None
    url = os.environ.get("PAPER_FETCH_EMBED_URL", "http://127.0.0.1:11434")
    return model, OllamaEmbedder(model, url)


def index_path() -> Path:
    """PAPER_FETCH_INDEX, else passages.sqlite beside the local store's default directory."""
    env = os.environ.get("PAPER_FETCH_INDEX")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("PAPER_FETCH_DATA_DIR") or (
        Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "paper-fetch"
    )
    return Path(base).expanduser() / "passages.sqlite"


def _numpy() -> Any:
    try:
        import numpy as np  # noqa: PLC0415 -- optional: only dense retrieval needs it
    except ImportError as e:
        raise RuntimeError(
            "dense retrieval needs numpy: install paper-fetch[retrieval], "
            "or unset PAPER_FETCH_EMBED_MODEL for lexical retrieval"
        ) from e
    return np


def _normalise(np: Any, vec: Sequence[float] | Any) -> Any:
    arr = np.asarray(vec, dtype=np.float32)
    norm = float(np.linalg.norm(arr))
    return arr / norm if norm else arr


class PassageIndex:
    def __init__(self, path: Path, model: str = "", embed: Embedder | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self.model, self.embed = path, model, embed if model else None
        # Several processes (MCP servers, the CLI) may share the file; WAL lets readers run while
        # one writes, and writers wait for each other rather than failing.
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._closer = weakref.finalize(self, self.db.close)
        self.db.execute("pragma journal_mode=wal")
        self.db.executescript(SCHEMA)
        known = self.db.execute("select value from meta where key='embedding_model'").fetchone()
        if known and known[0] != model:
            raise RuntimeError(
                f"the passage index at {path} was built with embedding model "
                f"{known[0] or 'none'!r}, not {model or 'none'!r}; vectors from different models "
                "are not comparable. "
                "Rebuild it (paper-fetch index --rebuild) or point PAPER_FETCH_INDEX elsewhere."
            )
        with self.db:
            self.db.execute("insert or ignore into meta values ('embedding_model', ?)", (model,))
        self._matrix: tuple[Any, Any] | None = None
        self._np: Any = _numpy() if self.embed else None

    def close(self) -> None:
        self._closer()

    # -- writing -------------------------------------------------------------------------------

    def has(self, work: str, text_sha256: str | None = None) -> bool:
        """Whether the work is indexed by the current chunker (and, if given, from this text)."""
        row = self.db.execute(
            "select p.text_sha256, c.version from papers p left join chunked c "
            "on c.work = p.work and c.indexed_at = p.indexed_at where p.work=?",
            (work,),
        ).fetchone()
        return (
            bool(row)
            and (row[1] or 1) == CHUNKER
            and (text_sha256 is None or row[0] == text_sha256)
        )

    def add(self, record: dict[str, Any], raw_text: str, batch: int = 16) -> int:
        """Index one paper's full text; replaces an earlier copy. Returns passages added."""
        work = record["work"]
        text, hidden = clean(raw_text)
        digest = hashlib.sha256(text.encode()).hexdigest()
        if self.has(work, digest):
            return 0
        pieces = chunk(text)
        vectors: list[bytes | None] = [None] * len(pieces)
        if self.embed is not None and self._np is not None:
            # Passages this work already has, word for word, keep their vectors: re-chunking a
            # paper (a new chunker, an erratum appended) embeds only what is new.
            known = dict(
                self.db.execute(
                    "select p.text, v.vec from passages p join vectors v on v.passage_id = p.id "
                    "where p.work=?",
                    (work,),
                )
            )
            vectors = [known.get(p[2]) for p in pieces]
            todo = [i for i, v in enumerate(vectors) if v is None]
            for at in range(0, len(todo), batch):
                group = todo[at : at + batch]
                vecs = self.embed([pieces[i][2] for i in group])
                for i, v in zip(group, vecs, strict=True):
                    vectors[i] = _normalise(self._np, v).tobytes()
        with self.db:
            # Take the write lock before reading what to replace, so a concurrent writer cannot
            # commit this work between the check and the delete.
            self.db.execute("begin immediate")
            if self.has(work, digest):
                return 0
            self._remove(work)
            indexed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            self.db.execute(
                "insert into papers values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    work,
                    record.get("doi"),
                    record.get("pmid"),
                    record.get("pmcid"),
                    record.get("title"),
                    record.get("year"),
                    json.dumps(record.get("authors") or []),
                    int(bool(record.get("is_retracted"))),
                    record.get("oa_status"),
                    record.get("license"),
                    record.get("route"),
                    record.get("format"),
                    digest,
                    len(text),
                    hidden,
                    indexed_at,
                ),
            )
            self.db.execute(
                "insert or replace into chunked values (?,?,?)", (work, CHUNKER, indexed_at)
            )
            for ord_, ((start, end, body), vec) in enumerate(zip(pieces, vectors, strict=True)):
                cur = self.db.execute(
                    'insert into passages (work, ord, start, "end", text) values (?,?,?,?,?)',
                    (work, ord_, start, end, body),
                )
                self.db.execute(
                    "insert into passages_fts (rowid, text) values (?,?)", (cur.lastrowid, body)
                )
                if vec is not None:
                    self.db.execute("insert into vectors values (?,?)", (cur.lastrowid, vec))
        self._matrix = None
        return len(pieces)

    def remove(self, work: str) -> None:
        with self.db:
            self._remove(work)
        self._matrix = None

    def _remove(self, work: str) -> None:
        rows = self.db.execute("select id, text from passages where work=?", (work,)).fetchall()
        for pid, body in rows:
            self.db.execute(
                "insert into passages_fts (passages_fts, rowid, text) values ('delete',?,?)",
                (pid, body),
            )
            self.db.execute("delete from vectors where passage_id=?", (pid,))
        self.db.execute("delete from passages where work=?", (work,))
        self.db.execute("delete from papers where work=?", (work,))
        self.db.execute("delete from chunked where work=?", (work,))

    # -- reading -------------------------------------------------------------------------------

    def paper(self, work: str) -> dict[str, Any] | None:
        cur = self.db.execute("select * from papers where work=?", (work,))
        row = cur.fetchone()
        if not row:
            return None
        out = dict(zip([c[0] for c in cur.description], row, strict=True))
        out["authors"] = json.loads(out["authors"] or "[]")
        out["is_retracted"] = bool(out["is_retracted"])
        return out

    def works(self) -> list[str]:
        return [r[0] for r in self.db.execute("select work from papers order by work")]

    def passages(self, work: str, start_ord: int = 0, limit: int = 4) -> list[Passage]:
        """A paper's passages in order, from position `start_ord`."""
        rows = self.db.execute(
            'select work, ord, start, "end", text from passages where work=? and ord>=? '
            "order by ord limit ?",
            (work, start_ord, limit),
        )
        return [Passage(*r) for r in rows]

    def stats(self) -> dict[str, Any]:
        one = self.db.execute
        return {
            "papers": one("select count(*) from papers").fetchone()[0],
            "passages": one("select count(*) from passages").fetchone()[0],
            "with_vectors": one("select count(*) from vectors").fetchone()[0],
            # Papers chunked by an older chunker; `paper-fetch index --refresh` brings them up.
            "stale": one(
                "select count(*) from papers p left join chunked c "
                "on c.work = p.work and c.indexed_at = p.indexed_at "
                "where coalesce(c.version, 1) != ?",
                (CHUNKER,),
            ).fetchone()[0],
            "embedding_model": self.model or None,
            "path": str(self.path),
        }

    def _vectors(self) -> tuple[Any, Any]:
        np = self._np
        if self._matrix is None:
            rows = self.db.execute(
                "select passage_id, vec from vectors order by passage_id"
            ).fetchall()
            ids = np.array([r[0] for r in rows], dtype=np.int64)
            mat = (
                np.vstack([np.frombuffer(r[1], dtype=np.float32) for r in rows])
                if rows
                else np.zeros((0, 1), dtype=np.float32)
            )
            self._matrix = (ids, mat)
        return self._matrix

    def query_vectors(self, queries: list[str]) -> list[Any]:
        """One vector per query, in one embedding call; Nones when retrieval is lexical."""
        if self.embed is None or not queries:
            return [None] * len(queries)
        return list(self.embed(queries))

    def search(
        self,
        query: str,
        query_vector: Sequence[float] | None = None,
        *,
        works: Iterable[str] | None = None,
        limit: int = 20,
        pool: int = 100,
        rrf_k: int = 60,
    ) -> list[Passage]:
        """Hybrid retrieval. Lexical and dense rankings are fused by rank, not by score, because
        BM25 scores and cosine similarities are on unrelated scales."""
        allowed: set[int] | None = None
        if works is not None:
            wl = list(works)
            if not wl:
                return []
            marks = ",".join("?" * len(wl))
            allowed = {
                r[0]
                for r in self.db.execute(f"select id from passages where work in ({marks})", wl)
            }
            if not allowed:
                return []

        ranks: dict[int, dict[str, int]] = {}
        match = fts_query(query)
        if match:
            rows = self.db.execute(
                "select rowid from passages_fts where passages_fts match ? "
                "order by bm25(passages_fts) limit ?",
                (match, pool * (4 if allowed is not None else 1)),
            ).fetchall()
            kept = [r[0] for r in rows if allowed is None or r[0] in allowed][:pool]
            for rank, pid in enumerate(kept, 1):
                ranks.setdefault(pid, {})["bm25"] = rank

        if query_vector is not None and self._np is not None:
            np = self._np
            ids, mat = self._vectors()
            if len(ids):
                scores = mat @ _normalise(np, query_vector)
                if allowed is not None:
                    scores = np.where(np.isin(ids, list(allowed)), scores, -np.inf)
                top = np.argsort(-scores)[:pool]
                for rank, i in enumerate(top, 1):
                    if np.isfinite(scores[i]):
                        ranks.setdefault(int(ids[i]), {})["dense"] = rank

        fused = sorted(
            ((sum(1.0 / (rrf_k + r) for r in rk.values()), pid, rk) for pid, rk in ranks.items()),
            key=lambda t: -t[0],
        )[:limit]
        out = []
        for score, pid, rk in fused:
            row = self.db.execute(
                'select work, ord, start, "end", text from passages where id=?', (pid,)
            ).fetchone()
            if row:
                out.append(
                    Passage(
                        *row,
                        bm25_rank=rk.get("bm25"),
                        dense_rank=rk.get("dense"),
                        score=round(score, 5),
                    )
                )
        return out
