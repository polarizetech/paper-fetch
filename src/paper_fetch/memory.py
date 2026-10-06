"""Search memory: every search the library ran, what it was about and what it found.

    lib.recall("heart rate variability athletes")   # searches on this concept, and their papers
    lib.recall(profile="cardiovascular")                  # everything searched in a discipline
    lib.recall(collection="my-review")                    # a project's search history
    lib.recall(work="W2036318837")                        # which searches found this paper

It is one JSON-lines object, `papers/memory/searches.jsonl`, in the same store as the papers, so
every process and machine that shares the library shares its memory. It is written the way the
catalogue is: re-read, then this process's new rows added, so concurrent writers keep each other's
rows (bar the instant between one read and one write).

Recall is deterministic. A query is reduced to its **concepts**: lower-cased content words, with
every synonym a profile declares replaced by its indexed term, so "HRV" and "heart rate
variability" are one concept. Past searches are scored by the overlap of concepts (Jaccard).
"""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from .store import PREFIX, Store

__all__ = ["MEMORY", "SearchMemory", "concepts"]

MEMORY = f"{PREFIX}memory/searches.jsonl"
HITS_KEPT = 50  # per search; enough to say what it found without storing the whole answer

_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "between",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "than",
        "that",
        "the",
        "their",
        "there",
        "these",
        "this",
        "to",
        "vs",
        "versus",
        "was",
        "were",
        "what",
        "when",
        "whether",
        "which",
        "who",
        "why",
        "will",
        "with",
        "within",
        "without",
    }
)
_WORD = re.compile(r"[a-z0-9][a-z0-9/-]*")


def concepts(text: str, vocabulary: Iterable[tuple[re.Pattern[str], str]] = ()) -> frozenset[str]:
    """Content words of `text`, synonyms mapped to their indexed terms, a plural -s dropped."""
    t = text
    for pat, label in vocabulary:
        t = pat.sub(label, t)
    words = set()
    for word in _WORD.findall(t.lower()):
        if word in _STOP or len(word) < 2:
            continue
        plural = len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "is", "us"))
        words.add(word[:-1] if plural else word)
    return frozenset(words)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


class SearchMemory:
    def __init__(self, store: Store, vocabulary: Iterable[tuple[re.Pattern[str], str]] = ()):
        self.store = store
        self.vocabulary = list(vocabulary)
        # record() is read, append, write: two threads of one server must not interleave it.
        self._lock = threading.Lock()

    def entries(self) -> list[dict[str, Any]]:
        self.store.invalidate(MEMORY)
        if not self.store.has(MEMORY):
            return []
        return [json.loads(ln) for ln in self.store.get(MEMORY).decode().splitlines() if ln.strip()]

    def record(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Add one search. Returns the stored row, with its `id` and `at`."""
        at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        row = {
            "id": f"s{time.time_ns():x}{secrets.token_hex(2)}",  # sorts by time
            "at": at,
            **entry,
            "hits": entry.get("hits", [])[:HITS_KEPT],
        }
        with self._lock:
            rows = self.entries()
            rows.append(row)
            body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows)
            self.store.put(MEMORY, body.encode(), "application/x-ndjson")
        return row

    def recall(
        self,
        query: str = "",
        *,
        profile: str | None = None,
        collection: str | None = None,
        works: set[str] | None = None,
        limit: int = 10,
        min_score: float = 0.2,
        rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Past searches, most similar (or, with no query, most recent) first."""
        want = concepts(query, self.vocabulary) if query else frozenset()
        out = []
        for r in rows if rows is not None else self.entries():
            if profile and r.get("profile") != profile:
                continue
            if collection and r.get("collection") != collection:
                continue
            if works and not any({h.get("work"), h.get("key")} & works for h in r["hits"]):
                continue
            score = _jaccard(want, frozenset(r.get("concepts", ()))) if want else 1.0
            if score >= min_score:
                out.append({**r, "score": round(score, 3)})
        out.sort(key=lambda r: (r["at"], r["id"]), reverse=True)
        out.sort(key=lambda r: -r["score"])  # stable: equally similar searches stay newest first
        return out[:limit]

    def first_seen(self, rows: list[dict[str, Any]], collection: str | None) -> dict[str, Any]:
        """For each hit key: the earliest search (in `collection`, if given) that found it."""
        seen: dict[str, dict[str, Any]] = {}
        for r in sorted(rows, key=lambda r: (r["at"], r["id"])):
            if collection and r.get("collection") != collection:
                continue
            for h in r["hits"]:
                seen.setdefault(h["key"], {"search": r["id"], "query": r["query"], "at": r["at"]})
        return seen
