"""Discipline profiles: what a field is indexed under, and how a search uses that. Data, not code.

    lib.search("HRV in endurance athletes", profile="cardiovascular")

A profile is the search half of a scientific discipline:

- **anchors**: controlled-vocabulary terms (MeSH descriptors, or declared free text) with the
  synonyms people write instead. A query that uses a synonym is also run with the anchor's indexed
  term, and every hit is marked with the anchors its title names.
- **providers**: the search providers that cover the field, when it is not the default set.
- **measures**: quantities the field reports, with their units, for a planner to use in queries.
- **search_guidance** and **scope**: prose for whatever writes the queries (a person or a model).
  paper-fetch itself never interprets prose; everything it does with a profile is deterministic.

Built-in profiles ship in `paper_fetch/profile_data/*.toml`. A file with the same slug in
`$PAPER_FETCH_PROFILE_DIR` (default `~/.config/paper-fetch/profiles/`) replaces the built-in one,
and a new slug adds a profile. `paper-fetch profiles` lists what is loaded.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

from .providers import REGISTRY

__all__ = ["Anchor", "Measure", "Profile", "Source", "load_profiles", "profile", "vocabulary"]

_MESH_ID = re.compile(r"D\d{6,9}")
_SLUG = re.compile(r"[a-z][a-z0-9-]*")


@dataclass(frozen=True)
class Anchor:
    """A term the field is indexed under. A `mesh` anchor carries a real descriptor id; a term
    with no descriptor is declared `free-text`, because a plausible-looking id is worse than none.
    """

    system: str
    label: str
    identifier: str = ""
    synonyms: tuple[str, ...] = ()
    tree: str = ""

    def __post_init__(self) -> None:
        if not self.label.strip():
            raise ValueError("an anchor needs the label the field is indexed under")
        if self.system == "mesh" and not _MESH_ID.fullmatch(self.identifier):
            raise ValueError(
                f"anchor {self.label!r}: a mesh anchor needs a real descriptor id (D......), "
                f"got {self.identifier!r}; declare it as free-text instead"
            )
        if self.system == "free-text" and self.identifier:
            raise ValueError(f"anchor {self.label!r}: free-text has no identifier")

    def terms(self) -> tuple[str, ...]:
        return (self.label, *self.synonyms)


@dataclass(frozen=True)
class Measure:
    label: str
    units: tuple[str, ...]


@dataclass(frozen=True)
class Source:
    """A literature source the field treats as authoritative. Recorded, never fetched from."""

    key: str
    label: str
    role: str = "literature"
    url: str = ""
    why: str = ""


def _phrase(term: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![\w-]){re.escape(term)}(?![\w-])", re.IGNORECASE)


@dataclass(frozen=True)
class Profile:
    slug: str
    label: str
    scope: str
    anchors: tuple[Anchor, ...] = ()
    measures: tuple[Measure, ...] = ()
    sources: tuple[Source, ...] = ()
    providers: tuple[str, ...] = ()  # search providers; empty means the configured default
    search_guidance: str = ""
    examples: tuple[str, ...] = ()
    origin: str = "built-in"
    _patterns: tuple[tuple[Anchor, str, re.Pattern[str]], ...] = field(
        default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not _SLUG.fullmatch(self.slug):
            raise ValueError(f"profile slug {self.slug!r} must be lower kebab-case")
        if not self.scope.strip():
            raise ValueError(f"profile {self.slug}: describe its scope")
        unknown = [p for p in self.providers if p not in REGISTRY]
        if unknown:
            raise ValueError(f"profile {self.slug}: unknown providers {unknown}")
        # Longest terms first, so "heart rate variability" wins over "heart rate".
        pats = sorted(
            ((a, t, _phrase(t)) for a in self.anchors for t in a.terms()),
            key=lambda x: -len(x[1]),
        )
        object.__setattr__(self, "_patterns", tuple(pats))

    def terms(self) -> tuple[str, ...]:
        """Every term the field is indexed under, de-duplicated, in declaration order."""
        return tuple(dict.fromkeys(t for a in self.anchors for t in a.terms()))

    def anchors_in(self, text: str | None) -> list[str]:
        """The labels of the anchors `text` names, by label or synonym."""
        if not text:
            return []
        return list(dict.fromkeys(a.label for a, _, pat in self._patterns if pat.search(text)))

    def expand(self, query: str, limit: int = 2) -> list[str]:
        """The query, plus up to `limit` variants that swap a synonym for its indexed term.

        Only that direction: a synonym is how people write, the label is what a database indexes
        under. Replacing the label with a synonym would search for the less-indexed wording.
        """
        out = [query]
        for anchor, term, pat in self._patterns:
            if len(out) > limit:
                break
            if term == anchor.label or not pat.search(query):
                continue
            variant = pat.sub(anchor.label, query, count=1)
            if variant.lower() not in {q.lower() for q in out}:
                out.append(variant)
        return out

    def guidance(self) -> dict[str, Any]:
        """What a query writer needs: scope, vocabulary, units and advice. For planner prompts."""
        return {
            "slug": self.slug,
            "label": self.label,
            "scope": self.scope,
            "search_guidance": self.search_guidance,
            "terms": list(self.terms()),
            "anchors": [
                {"system": a.system, "id": a.identifier, "label": a.label, "synonyms": a.synonyms}
                for a in self.anchors
            ],
            "measures": [{"label": m.label, "units": list(m.units)} for m in self.measures],
            "providers": list(self.providers),
            "sources": [
                {"key": s.key, "label": s.label, "role": s.role, "url": s.url, "why": s.why}
                for s in self.sources
            ],
            "examples": list(self.examples),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "label": self.label,
            "scope": self.scope,
            "anchors": len(self.anchors),
            "providers": list(self.providers),
            "origin": self.origin,
        }


def _parse(data: dict[str, Any], origin: str) -> Profile:
    try:
        return Profile(
            slug=data["slug"],
            label=data["label"],
            scope=data["scope"],
            anchors=tuple(
                Anchor(
                    system=a["system"],
                    label=a["label"],
                    identifier=a.get("id", ""),
                    synonyms=tuple(a.get("synonyms", ())),
                    tree=a.get("tree", ""),
                )
                for a in data.get("anchors", ())
            ),
            measures=tuple(
                Measure(m["label"], tuple(m["units"])) for m in data.get("measures", ())
            ),
            sources=tuple(Source(**s) for s in data.get("sources", ())),
            providers=tuple(data.get("providers", ())),
            search_guidance=data.get("search_guidance", "").strip(),
            examples=tuple(data.get("examples", ())),
            origin=origin,
        )
    except (KeyError, TypeError) as e:
        raise ValueError(f"profile {origin}: {type(e).__name__}: {e}") from e


def profile_dir() -> Path:
    env = os.environ.get("PAPER_FETCH_PROFILE_DIR")
    if env:
        return Path(env).expanduser()
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "paper-fetch" / "profiles"


def load_profiles() -> dict[str, Profile]:
    """Built-in profiles, then the operator's: a file with a built-in slug replaces it."""
    found: dict[str, Profile] = {}
    for entry in sorted(resources.files("paper_fetch.profile_data").iterdir(), key=str):
        if entry.name.endswith(".toml"):
            p = _parse(tomllib.loads(entry.read_text()), "built-in")
            found[p.slug] = p
    user = profile_dir()
    if user.is_dir():
        for path in sorted(user.glob("*.toml")):
            p = _parse(tomllib.loads(path.read_text()), str(path))
            found[p.slug] = p
    return found


def profile(slug: str, profiles: dict[str, Profile] | None = None) -> Profile:
    known = profiles if profiles is not None else load_profiles()
    if slug not in known:
        raise ValueError(f"unknown profile {slug!r}; known: {', '.join(sorted(known)) or 'none'}")
    return known[slug]


def vocabulary(profiles: dict[str, Profile]) -> list[tuple[re.Pattern[str], str]]:
    """Every synonym across profiles, mapped to its indexed term, longest first.

    Search memory normalises queries with this, so "HRV in athletes" and "heart rate variability
    in athletes" are recognised as the same concept whichever profile either search used.
    """
    pairs = {t.lower(): a.label for p in profiles.values() for a in p.anchors for t in a.synonyms}
    return [(_phrase(t), label) for t, label in sorted(pairs.items(), key=lambda x: -len(x[0]))]
