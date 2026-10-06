"""Named collections: a project's papers, inside the one shared library.

    lib.create_collection("sleep-review", profile="neuroscience", description="...")
    lib.search("slow oscillations memory", collection="sleep-review")  # profile applied; remembered
    lib.fetch("10.1371/journal.pcbi.1003285", collection="sleep-review")  # held once, listed
    lib.collection("sleep-review")                                      # members + search history

A paper is stored once, whichever collections list it; a collection is only a named list of
members, a default discipline profile and a description. Each collection is one small JSON object,
`papers/collections/<name>.json`. It is re-read before every change and written straight after, so
two processes adding to one collection keep both additions (bar the instant between read and
write). A collection's search history is the search memory filtered by its name.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import UTC, datetime
from typing import Any

from .store import PREFIX, Store

__all__ = ["COLLECTIONS", "Collections"]

COLLECTIONS = f"{PREFIX}collections/"
_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _key(name: str) -> str:
    if not _NAME.fullmatch(name):
        raise ValueError(f"collection name {name!r}: use 1-64 of a-z, 0-9 and '-'")
    return f"{COLLECTIONS}{name}.json"


class Collections:
    def __init__(self, store: Store) -> None:
        self.store = store
        # Each change is read, modify, write: two threads of one server must not interleave them.
        self._lock = threading.RLock()

    def names(self) -> list[str]:
        return sorted(
            k[len(COLLECTIONS) : -len(".json")]
            for k in self.store.keys(COLLECTIONS)
            if k.endswith(".json")
        )

    def get(self, name: str) -> dict[str, Any] | None:
        key = _key(name)
        self.store.invalidate(key)
        return json.loads(self.store.get(key)) if self.store.has(key) else None

    def require(self, name: str) -> dict[str, Any]:
        c = self.get(name)
        if c is None:
            known = ", ".join(self.names()) or "none"
            raise ValueError(f"no collection {name!r} (known: {known}); create it first")
        return c

    def _put(self, c: dict[str, Any]) -> dict[str, Any]:
        c["updated"] = _now()
        self.store.put(_key(c["name"]), json.dumps(c, indent=1).encode(), "application/json")
        return c

    def create(
        self, name: str, *, profile: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Create a collection, or update the profile / description of an existing one."""
        with self._lock:
            c = self.get(name) or {
                "name": name,
                "profile": None,
                "description": "",
                "created": _now(),
                "members": {},
            }
            if profile is not None:
                c["profile"] = profile or None
            if description is not None:
                c["description"] = description
            return self._put(c)

    def add(self, name: str, members: dict[str, dict[str, Any]]) -> dict[str, Any]:
        """Add members (key -> {title, via, ...}); an existing member keeps when it was added."""
        with self._lock:
            c = self.require(name)
            for key, info in members.items():
                old = c["members"].get(key)
                c["members"][key] = {**info, "added": old["added"] if old else _now()}
            return self._put(c)

    def remove(self, name: str, keys: list[str]) -> dict[str, Any]:
        with self._lock:
            c = self.require(name)
            missing = [k for k in keys if c["members"].pop(k, None) is None]
            if missing:
                raise ValueError(f"not in {name}: {', '.join(missing)}")
            return self._put(c)

    def delete(self, name: str) -> None:
        with self._lock:
            self.require(name)
            self.store.delete(_key(name))
