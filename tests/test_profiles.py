"""Discipline profiles: loading, validation, expansion, anchor matching, profile-aware search."""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import FakeProvider, make_library
from paper_fetch.profiles import Anchor, Profile, load_profiles, profile, vocabulary
from paper_fetch.providers import Hit

HRV = Anchor(
    "free-text", "heart rate variability", synonyms=("HRV", "RMSSD", "R-R interval variability")
)
BARO = Anchor("mesh", "Baroreflex", "D017704", synonyms=("baroreceptor reflex",))
CARDIO = Profile("cardio", "Cardiology", "Hearts.", anchors=(HRV, BARO))


def test_the_built_in_profiles_load_and_are_valid() -> None:
    found = load_profiles()
    assert {"cardiovascular", "neuroscience", "respiratory", "vestibular"} <= set(found)
    for p in found.values():
        assert p.anchors, p.slug
        assert p.search_guidance, p.slug
        assert p.origin == "built-in"
        g = p.guidance()
        assert g["terms"] == list(p.terms())
        assert {"scope", "search_guidance", "anchors", "measures", "sources"} <= set(g)


def test_an_operator_profile_adds_and_replaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPER_FETCH_PROFILE_DIR", str(tmp_path))
    (tmp_path / "mine.toml").write_text(
        'slug = "cardiovascular"\nlabel = "Mine"\nscope = "Only mine."\n'
        '[[anchors]]\nsystem = "free-text"\nlabel = "thing"\n'
    )
    found = load_profiles()
    assert found["cardiovascular"].label == "Mine"
    assert found["cardiovascular"].origin.endswith("mine.toml")
    assert "neuroscience" in found
    with pytest.raises(ValueError, match="unknown profile 'nope'"):
        profile("nope", found)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"system": "mesh", "label": "X", "identifier": "C14"}, "real descriptor id"),
        ({"system": "free-text", "label": "X", "identifier": "D000001"}, "no identifier"),
        ({"system": "free-text", "label": " "}, "needs the label"),
    ],
)
def test_anchor_guards(kwargs: dict[str, str], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        Anchor(**kwargs)  # type: ignore[arg-type]


def test_profile_guards() -> None:
    with pytest.raises(ValueError, match="kebab-case"):
        Profile("Bad Slug", "x", "y")
    with pytest.raises(ValueError, match="scope"):
        Profile("ok", "x", " ")
    with pytest.raises(ValueError, match="unknown providers"):
        Profile("ok", "x", "y", providers=("scholar",))


def test_expansion_swaps_a_synonym_for_the_indexed_term_only() -> None:
    assert CARDIO.expand("HRV in endurance athletes") == [
        "HRV in endurance athletes",
        "heart rate variability in endurance athletes",
    ]
    # The indexed term itself is never swapped for a synonym.
    assert CARDIO.expand("heart rate variability in athletes") == [
        "heart rate variability in athletes"
    ]
    # Whole words only: "HRVs" and "shrv" are not "HRV".
    assert CARDIO.expand("shrv values") == ["shrv values"]
    assert CARDIO.expand("HRV and baroreceptor reflex", limit=1) == [
        "HRV and baroreceptor reflex",
        "HRV and Baroreflex",
    ]


def test_anchor_matching_prefers_the_longest_term() -> None:
    assert CARDIO.anchors_in("R-R interval variability after baroreceptor reflex loss") == [
        "heart rate variability",
        "Baroreflex",
    ]
    assert CARDIO.anchors_in(None) == []


def test_vocabulary_maps_synonyms_across_profiles() -> None:
    other = Profile("other", "O", "o", anchors=(Anchor("free-text", "sleep", synonyms=("zzz",)),))
    vocab = vocabulary({"cardio": CARDIO, "other": other})
    text = "HRV and zzz"
    for pat, label in vocab:
        text = pat.sub(label, text)
    assert text == "heart rate variability and sleep"


def test_profile_aware_search() -> None:
    lib, _ = make_library()
    lib._profiles = {"cardio": CARDIO}
    seen: list[str] = []

    class Recording(FakeProvider):
        def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
            seen.append(query)
            if "heart rate variability" in query:
                return [
                    Hit("rec", "Heart rate variability in rowers", 2019, {"doi": "10.5555/a"}, True)
                ]
            return [
                Hit("rec", "Athletes and their shoes", 2024, {"doi": "10.5555/b"}, True),
                Hit("rec", "HRV in rowers", 2019, {"doi": "10.5555/a"}, True),
            ]

    lib.search_providers = [Recording("rec")]
    res = lib.search("HRV in athletes", profile="cardio", web_fallback=False)
    assert seen == ["HRV in athletes", "heart rate variability in athletes"]
    assert res["variants"] == ["heart rate variability in athletes"]
    assert res["profile"] == "cardio"
    assert set(res["variant_providers"]) == {"heart rate variability in athletes"}
    first, second = res["hits"]
    assert first["ids"]["doi"] == "10.5555/a"  # names the field's anchor: ranks first
    assert first["profile_match"] == ["heart rate variability"]
    assert first["found_by"] == ["HRV in athletes", "heart rate variability in athletes"]
    assert second["profile_match"] == []
    plain = lib.search("HRV in athletes", profile="cardio", expand=False, web_fallback=False)
    assert plain["variants"] == []
    with pytest.raises(ValueError, match="unknown profile"):
        lib.search("x", profile="nope")
