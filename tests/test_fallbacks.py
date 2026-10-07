"""When a provider fails: an outside problem is worked around; a defect is flagged."""

from __future__ import annotations

import json
import stat
from pathlib import Path
from typing import Any

import pytest

from conftest import FakeHttp, FakeProvider, J, fixture, make_library
from paper_fetch import cli, problems
from paper_fetch.http import Response
from paper_fetch.openalex import OpenAlex, OpenAlexUnavailable
from paper_fetch.providers import Hit, ProviderBroken, ProviderUnavailable
from paper_fetch.providers.adapters import OpenAlexProvider
from paper_fetch.providers.base import PAUSE_OUTAGE_S, PAUSE_QUOTA_S, PAUSE_RATE_LIMIT_S
from paper_fetch.resolve import resolve


class Counting(FakeProvider):
    """A FakeProvider that counts how often it was really asked."""

    asked = 0

    def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
        self.asked += 1
        return super().search(query, oa_only=oa_only, limit=limit)


def _lib(*providers: FakeProvider) -> Any:
    lib, _ = make_library()
    lib.search_providers = list(providers)
    return lib


def _search(lib: Any, q: str = "x", **k: Any) -> dict[str, Any]:
    return lib.search(q, oa_only=False, web_fallback=False, remember=False, **k)


OPEN = Hit("good", "An open paper", 2020, {"doi": "10.5555/open"}, True)


# -- an outside problem: the others answer, and the provider is left alone for a while -----------


def test_the_other_providers_answer_when_one_is_unavailable() -> None:
    down = Counting("down", raise_=ProviderUnavailable("down unreachable: timed out"))
    good = Counting("good", hits=[OPEN])
    res = _search(_lib(down, good))
    assert res["providers"]["down"] == {
        "status": "unavailable",
        "why": "down unreachable: timed out",
    }
    assert res["providers"]["good"]["status"] == "ok"
    assert [h["ids"]["doi"] for h in res["hits"]] == ["10.5555/open"]
    assert "broken" not in res


def test_an_unavailable_provider_is_not_asked_again_until_its_pause_ends(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr("paper_fetch.library.time.monotonic", lambda: now[0])
    down = Counting("down", raise_=ProviderUnavailable("down answered HTTP 503"))
    lib = _lib(down, Counting("good", hits=[OPEN]))
    _search(lib)
    assert down.asked == 1
    assert lib.paused() == {"down": "not asked for 60 s, after: down answered HTTP 503"}

    now[0] += 30
    again = _search(lib, "y")
    assert down.asked == 1  # not asked
    assert again["providers"]["down"] == {
        "status": "unavailable",
        "why": "not asked for 30 s, after: down answered HTTP 503",
        "paused": True,
    }
    assert again["providers"]["good"]["status"] == "ok"

    now[0] += PAUSE_OUTAGE_S
    _search(lib, "z")
    assert down.asked == 2  # the pause is over: asked again


def test_refresh_asks_a_paused_provider_anyway() -> None:
    down = Counting("down", raise_=ProviderUnavailable("down answered HTTP 503"))
    lib = _lib(down)
    _search(lib)
    _search(lib, refresh=True)
    assert down.asked == 2


@pytest.mark.parametrize(
    ("pause", "span"),
    [
        (None, "for 60 s"),
        (PAUSE_RATE_LIMIT_S, "for 300 s"),
        (PAUSE_QUOTA_S, "for the rest of this process"),
    ],
)
def test_the_pause_is_as_long_as_the_failure_says(pause: float | None, span: str) -> None:
    lib = _lib(Counting("down", raise_=ProviderUnavailable("why", pause_s=pause)))
    _search(lib)
    assert lib.paused() == {"down": f"not asked {span}, after: why"}
    assert lib.status()["paused_providers"] == lib.paused()


def test_a_transport_failure_is_unavailable_not_a_defect() -> None:
    lib = _lib(Counting("down", raise_=ConnectionError("GET https://x failed: timed out")))
    res = _search(lib)
    assert res["providers"]["down"]["status"] == "unavailable"
    assert "broken" not in res
    assert problems.recent() == []


def test_a_cached_answer_is_served_while_the_provider_is_paused() -> None:
    p = Counting("flaky", hits=[OPEN])
    lib = _lib(p)
    assert _search(lib, "cached query")["providers"]["flaky"]["status"] == "ok"
    lib._pause("flaky", PAUSE_QUOTA_S, "allowance spent")
    assert _search(lib, "cached query")["providers"]["flaky"]["status"] == "cache"
    assert _search(lib, "new query")["providers"]["flaky"]["paused"] is True
    assert p.asked == 1


# -- a defect: named, logged with its traceback, and never paused --------------------------------


def test_a_defect_is_named_in_the_result_and_logged_with_its_traceback() -> None:
    bad = Counting("bad", raise_=KeyError("resultList"))
    lib = _lib(bad, Counting("good", hits=[OPEN]))
    res = _search(lib, "theta beta ratio")
    assert res["providers"]["bad"] == {
        "status": "error",
        "why": "KeyError: 'resultList'",
        "broken": True,
    }
    assert res["broken"] == [
        {"provider": "bad", "error": "KeyError: 'resultList'", "log": str(problems.log_path())}
    ]
    assert res["providers"]["good"]["status"] == "ok"  # and the search still answered

    (row,) = problems.recent()
    assert (row["provider"], row["operation"], row["error"]) == (
        "bad",
        "search",
        "KeyError: 'resultList'",
    )
    assert row["context"] == {"query": "theta beta ratio", "oa_only": False}
    assert "Traceback (most recent call last)" in row["traceback"]
    assert "conftest.py" in row["traceback"]
    assert stat.S_IMODE(problems.log_path().stat().st_mode) == 0o600
    assert lib.status()["provider_problems"] == {"logged": 1, "log": str(problems.log_path())}


def test_a_defect_is_not_paused_so_it_is_seen_every_time() -> None:
    bad = Counting("bad", raise_=ProviderBroken("bad rejected our request (HTTP 400): no"))
    lib = _lib(bad)
    _search(lib)
    res = _search(lib, "y")
    assert bad.asked == 2
    assert lib.paused() == {}
    assert res["broken"][0]["provider"] == "bad"
    assert len(problems.recent()) == 2


def test_a_defect_in_a_profile_variant_is_named_too() -> None:
    class BadOnVariant(Counting):
        def search(self, query: str, *, oa_only: bool = True, limit: int = 10) -> list[Hit]:
            self.asked += 1
            if self.asked > 1:
                raise ValueError("variant broke")
            return []

    lib = _lib(BadOnVariant("picky"))
    slug = next(iter(lib.profiles()))
    prof = lib.profile(slug)
    synonym = next(a.synonyms[0] for a in prof.anchors if a.synonyms)
    res = _search(lib, f"{synonym} in children", profile=slug)
    assert res["providers"]["picky"]["status"] == "ok"
    assert [b["provider"] for b in res["broken"]] == ["picky"]


def test_a_defect_while_locating_a_copy_is_logged() -> None:
    bad = FakeProvider("bad", raise_=TypeError("NoneType is not subscriptable"))
    r = resolve({"doi": "10.5555/a", "_openalex_work": {"big": "object"}}, [bad])
    assert r.attempts == [("bad", "error: TypeError: NoneType is not subscriptable")]
    (row,) = problems.recent()
    assert (row["provider"], row["operation"]) == ("bad", "locate")
    assert row["context"] == {"doi": "10.5555/a"}  # identifiers, not the whole work record


def test_an_outage_while_locating_is_not_logged_as_a_defect() -> None:
    r = resolve({"doi": "10.5555/a"}, [FakeProvider("down", raise_=ConnectionError("timed out"))])
    assert r.attempts == [("down", "unavailable: timed out")]
    assert problems.recent() == []


def test_recording_a_defect_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    monkeypatch.setenv("PAPER_FETCH_PROBLEMS", str(blocker / "under-a-file" / "log.jsonl"))
    row = problems.record("p", "search", ValueError("boom"))
    assert row["error"] == "ValueError: boom"
    assert problems.recent() == []


def test_an_unreadable_line_in_the_log_is_skipped() -> None:
    problems.record("p", "search", ValueError("one"))
    with problems.log_path().open("a") as fh:
        fh.write("{not json\n")
    problems.record("p", "search", ValueError("two"))
    assert [r["error"] for r in problems.recent()] == ["ValueError: one", "ValueError: two"]
    assert [r["error"] for r in problems.recent(1)] == ["ValueError: two"]


# -- the command line -----------------------------------------------------------------------------


def test_cli_says_a_provider_is_broken_and_lists_the_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    lib = _lib(Counting("bad", raise_=KeyError("resultList")), Counting("good", hits=[OPEN]))
    assert cli.main(["search", "x", "--include-closed"], library=lib) == 0
    out = capsys.readouterr()
    assert (
        "! BROKEN: bad failed because of a defect, not an outage: KeyError: 'resultList'" in out.err
    )
    assert "paper-fetch problems" in out.err
    assert "1 distinct papers" in out.out

    assert cli.main(["problems", "--trace"], library=lib) == 0
    out = capsys.readouterr().out
    assert "bad          search  KeyError: 'resultList'" in out
    assert "Traceback (most recent call last)" in out


def test_cli_search_without_a_defect_prints_no_warning(capsys: pytest.CaptureFixture[str]) -> None:
    lib = _lib(Counting("down", raise_=ProviderUnavailable("down answered HTTP 503")))
    cli.main(["search", "x"], library=lib)
    out = capsys.readouterr()
    assert "BROKEN" not in out.err
    assert "unavailable  down answered HTTP 503" in out.out


# -- OpenAlex's own limits are outside problems ---------------------------------------------------


@pytest.mark.parametrize(
    ("status", "pause", "match"),
    [(429, 3600.0, "rate limit reached"), (502, None, "HTTP 502")],
)
def test_openalex_out_of_allowance_or_failing_is_unavailable(
    status: int, pause: float | None, match: str
) -> None:
    http = FakeHttp({"api.openalex.org/works": Response(status, {}, b"", "")})
    client = OpenAlex(http=http, api_key="", email="")
    with pytest.raises(OpenAlexUnavailable, match=match):
        client.search("x")
    with pytest.raises(ProviderUnavailable, match=match) as e:
        OpenAlexProvider(http, client).search("x")
    assert e.value.pause_s == pause


def test_openalex_rejecting_the_request_is_a_defect() -> None:
    http = FakeHttp({"api.openalex.org/works": Response(400, {}, b"bad filter", "")})
    lib, _ = make_library()
    lib.search_providers = [OpenAlexProvider(http, OpenAlex(http=http, api_key="", email=""))]
    res = _search(lib)
    assert res["providers"]["openalex"]["status"] == "error"
    assert res["broken"][0]["provider"] == "openalex"


# -- web search is the last resort ----------------------------------------------------------------


def test_web_search_is_asked_when_every_scholarly_provider_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    lib, _ = make_library()
    lib.http = FakeHttp({"searx.example/search": J(fixture("searxng.json"))})
    lib.search_providers = [
        Counting("one", raise_=ProviderUnavailable("one answered HTTP 503")),
        Counting("two", raise_=ProviderUnavailable("two rate-limited us (HTTP 429)")),
    ]
    res = lib.search("q", remember=False)
    assert {n: r["status"] for n, r in res["providers"].items()} == {
        "one": "unavailable",
        "two": "unavailable",
        "web": "ok",
    }
    assert res["providers"]["web"]["why_ran"] == "no hits from the scholarly providers"
    assert res["hits"]


def test_the_log_holds_one_json_object_per_line() -> None:
    problems.record("p", "search", ValueError("a\nb"), {"query": "q"})
    lines = problems.log_path().read_text().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["context"] == {"query": "q"}


# -- without a web search of its own, the library asks its caller to search ----------------------


def test_with_no_web_search_configured_the_caller_is_asked_to_search() -> None:
    closed = Hit("closed", "A closed paper", 2020, {"doi": "10.5555/closed"}, False)
    lib = _lib(Counting("usual", hits=[closed]))
    res = lib.search("phantom limb", oa_only=False, remember=False)
    assert res["providers"]["web"] == {
        "status": "skipped",
        "why": "needs SEARXNG_URL",
        "why_ran": "no open-access hit from the scholarly providers",
    }
    ask = res["ask_the_web"]
    assert (ask["why"], ask["query"], ask["web_search_here"]) == (
        "no open-access hit from the scholarly providers",
        "phantom limb",
        "skipped: needs SEARXNG_URL",
    )
    assert "`leads`" in ask["how"]
    assert "`fetch`" in ask["how"]
    empty = _lib(Counting("usual", hits=[]))
    assert empty.search("nothing at all", remember=False)["ask_the_web"]["why"] == (
        "no hits from the scholarly providers"
    )


def test_the_caller_is_not_asked_when_the_fallback_is_off_or_not_needed() -> None:
    lib = _lib(Counting("usual", hits=[OPEN]))
    assert "ask_the_web" not in lib.search("x", remember=False)  # an open hit: not needed
    lib2 = _lib(Counting("usual", hits=[]))
    assert "ask_the_web" not in lib2.search("x", web_fallback=False, remember=False)


def test_the_caller_is_not_asked_when_the_librarys_own_web_search_answered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    lib, _ = make_library()
    lib.http = FakeHttp({"searx.example/search": J(fixture("searxng.json"))})
    lib.search_providers = []
    res = lib.search("q", remember=False)
    assert res["providers"]["web"]["status"] == "ok"
    assert "ask_the_web" not in res


def test_the_caller_is_asked_when_the_librarys_own_web_search_is_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SEARXNG_URL", "https://searx.example")
    lib, _ = make_library()
    lib.http = FakeHttp({"searx.example/search": Response(502, {}, b"", "")})
    lib.search_providers = []
    res = lib.search("q", remember=False)
    assert res["providers"]["web"]["status"] == "unavailable"
    assert res["ask_the_web"]["web_search_here"].startswith("unavailable: web (searxng) HTTP 502")


def test_leads_reads_identifiers_from_what_the_caller_found_and_marks_what_is_held() -> None:
    lib, _ = make_library()
    held = lib.fetch("10.5555/example.001")
    assert held["full_text"]
    res = lib.leads(
        [
            "https://doi.org/10.5555/EXAMPLE.001",
            "https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0073216",
            "https://pubmed.ncbi.nlm.nih.gov/24204232/",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC3812051/",
            "https://arxiv.org/abs/2401.01234",
            "see doi:10.1371/journal.pone.0073216 for details",  # the same paper again
            "https://lab.example.edu/people/someone",
        ]
    )
    assert [(x["fetch"], x["in_library"], x["full_text_in_library"]) for x in res["leads"]] == [
        ("10.5555/example.001", True, True),
        ("10.1371/journal.pone.0073216", False, False),
        ("pmid:24204232", False, False),
        ("PMC3812051", False, False),
        ("arxiv:2401.01234", False, False),
    ]
    first = res["leads"][0]
    assert (first["work"], first["verified"], first["key"]) == (
        held["work"],
        False,
        "doi:10.5555/example.001",
    )
    assert res["no_identifier"] == ["https://lab.example.edu/people/someone"]
    assert lib.leads([]) == {"leads": [], "no_identifier": []}


def test_leads_makes_no_network_call() -> None:
    lib, http = make_library()
    before = http.calls
    lib.leads(["https://doi.org/10.5555/x", "https://pubmed.ncbi.nlm.nih.gov/123456/"])
    assert http.calls == before


def test_cli_asks_for_a_web_search_and_reads_leads(capsys: pytest.CaptureFixture[str]) -> None:
    lib = _lib(Counting("usual", hits=[]))
    cli.main(["search", "x"], library=lib)
    out = capsys.readouterr().out
    assert "no open copy found (no hits from the scholarly providers)" in out
    assert "paper-fetch leads <url>" in out
    cli.main(["leads", "https://doi.org/10.5555/abc", "https://nowhere.example/"], library=lib)
    out = capsys.readouterr().out
    assert "10.5555/abc" in out
    assert "no identifier in: https://nowhere.example/" in out


def test_mcp_leads_tool() -> None:
    from paper_fetch import mcp_server  # noqa: PLC0415

    lib, _ = make_library()
    mcp_server._library = lib
    try:
        res = mcp_server.leads(["https://doi.org/10.5555/abc"])
    finally:
        mcp_server._library = None
    assert res["ok"]
    assert res["data"]["leads"][0]["fetch"] == "10.5555/abc"
