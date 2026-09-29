from __future__ import annotations

import hashlib

import pytest

from conftest import JATS, PDF, FakeProvider, good_provider
from paper_fetch import BadApiKey
from paper_fetch.providers import Location, ProviderUnavailable
from paper_fetch.resolve import REFUSED, resolve


def _one(fmt: str, body: bytes | None, md5: str | None = None) -> FakeProvider:
    return FakeProvider("p", [Location(f"https://e.example/x.{fmt}", fmt, "p", md5=md5)], body)


def test_no_open_copy_anywhere_is_refused_without_a_download() -> None:
    a, b = FakeProvider("none"), FakeProvider("also-none")
    r = resolve({"doi": "10.5555/x"}, [a, b])
    assert r.fulltext is None
    assert r.refused == REFUSED
    assert r.attempts == [
        ("none", "no open-access copy known"),
        ("also-none", "no open-access copy known"),
    ]
    assert a.downloads == b.downloads == 0


@pytest.mark.parametrize(
    ("fmt", "body", "why"),
    [
        ("pdf", b"<!DOCTYPE html><html><head>", "HTML page"),
        ("pdf", b"nope " * 900, "not a PDF"),
        ("pdf", None, "no body"),
        ("jats-xml", b"<article><body>", "xml-parse-failed"),
    ],
)
def test_bad_candidates_are_refused(fmt: str, body: bytes | None, why: str) -> None:
    r = resolve({}, [_one(fmt, body)])
    assert r.fulltext is None
    assert r.refused is None  # a copy was reported; it just did not survive validation
    assert why in r.attempts[-1][1]


def test_md5_mismatch_is_refused_and_match_is_recorded() -> None:
    r = resolve({}, [_one("jats-xml", JATS, md5="0" * 32)])
    assert r.fulltext is None
    assert "md5 mismatch" in r.attempts[-1][1]
    good = hashlib.md5(JATS, usedforsecurity=False).hexdigest()
    r = resolve({}, [_one("jats-xml", JATS, md5=good)])
    assert r.fulltext is not None
    assert r.fulltext.md5_verified is True
    assert r.attempts[-1][1].startswith("ok (md5 verified")


def test_provider_text_is_accepted_when_readable() -> None:
    from conftest import PROSE  # noqa: PLC0415

    r = resolve({}, [_one("txt", PROSE.encode())])
    assert r.fulltext is not None
    assert r.fulltext.text_method == "provider-text"


def test_unreadable_text_is_stored_but_not_accepted() -> None:
    r = resolve({}, [_one("txt", b"1 2 3 " * 1000)])
    assert r.fulltext is None
    assert "NOT readable" in r.attempts[-1][1]


def test_failures_are_per_provider_and_the_next_one_is_used() -> None:
    down = FakeProvider("down", raise_=ProviderUnavailable("down rate-limited us (HTTP 429)"))
    broken = FakeProvider("broken", raise_=KeyError("oops"))
    r = resolve({}, [down, broken, good_provider()])
    assert r.fulltext is not None
    assert r.fulltext.route == "good:pdf"
    assert r.fulltext.data == PDF
    assert ("down", "unavailable: down rate-limited us (HTTP 429)") in r.attempts
    assert any(a[0] == "broken" and a[1].startswith("error: KeyError") for a in r.attempts)


def test_a_download_failure_moves_on_to_the_next_location() -> None:
    class Flaky(FakeProvider):
        def download(self, loc: Location) -> bytes | None:
            if loc.url.endswith("1.pdf"):
                raise TimeoutError
            return PDF

    locs = [Location(f"https://e.example/{i}.pdf", "pdf", "flaky") for i in (1, 2)]
    r = resolve({}, [Flaky("flaky", locs)])
    assert r.fulltext is not None
    assert r.fulltext.url.endswith("2.pdf")
    assert ("flaky:pdf", "download failed: TimeoutError") in r.attempts


def test_a_provider_missing_its_key_is_skipped_without_downloading() -> None:
    gated = good_provider("gated")
    gated.needs = ("PAPER_FETCH_TEST_NEVER_SET",)
    r = resolve({}, [gated])
    assert r.fulltext is None
    assert gated.downloads == 0
    assert r.attempts == [("gated", "skipped: needs PAPER_FETCH_TEST_NEVER_SET")]


def test_a_bad_openalex_key_propagates() -> None:
    with pytest.raises(BadApiKey):
        resolve({}, [FakeProvider("oa", raise_=BadApiKey("bad"))])

    class BadDownload(FakeProvider):
        def download(self, loc: Location) -> bytes | None:
            raise BadApiKey("bad")

    with pytest.raises(BadApiKey):
        resolve({}, [BadDownload("oa", [Location("u", "pdf", "oa")])])
