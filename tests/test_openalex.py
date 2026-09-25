from __future__ import annotations

import pytest

from conftest import PDF, DeadHttp, FakeHttp, J, fixture
from paperlib import BadApiKey, NeedsApiKey, OpenAlex
from paperlib.http import Response
from paperlib.idconv import enrich
from paperlib.ids import Ident

# ---------------------------------------------------------------------------------- OpenAlex


def test_work_lookup_and_usage_accounting() -> None:
    headers = {
        "x-ratelimit-cost-usd": "0",
        "x-ratelimit-remaining-usd": "0.099",
        "x-ratelimit-limit-usd": "0.1",
        "x-ratelimit-remaining": "99",
    }
    http = FakeHttp(
        {"works/doi:10.5555/example.001": J(fixture("openalex_work.json"), headers=headers)}
    )
    oa = OpenAlex(http=http, api_key="", email="someone@example.org")
    work = oa.work(Ident("doi", "10.5555/example.001"))
    assert work is not None
    assert work["id"].endswith("W7")
    assert "mailto=someone%40example.org" in http.log[0]
    assert "select=" in http.log[0]
    assert (oa.usage.remaining_usd, oa.usage.limit_usd, oa.usage.remaining_calls) == (
        0.099,
        0.1,
        99,
    )
    assert oa.work(Ident("pmid", "1")) is None  # 404


def test_search_costs_are_summed() -> None:
    http = FakeHttp(
        {"/works?": J(fixture("openalex_search.json"), headers={"x-ratelimit-cost-usd": "0.001"})}
    )
    oa = OpenAlex(http=http, api_key="", email="")
    assert len(oa.search("x", filters={"is_oa": "true"})["results"]) == 2
    oa.search("y")
    assert oa.spent_usd == pytest.approx(0.002)
    assert "filter=is_oa%3Atrue" in http.log[0]
    assert "filter" not in http.log[1]
    assert OpenAlex(http=FakeHttp(), api_key="", email="").search("z") == {
        "results": [],
        "meta": {},
    }


@pytest.mark.parametrize(
    ("status", "exc", "match"),
    [
        (401, BadApiKey, "key is wrong"),
        (429, RuntimeError, "rate limit"),
        (500, RuntimeError, "500"),
    ],
)
def test_errors_are_loud(status: int, exc: type[Exception], match: str) -> None:
    oa = OpenAlex(
        http=FakeHttp({"api.openalex.org": Response(status, {}, b"", "")}), api_key="bad", email=""
    )
    with pytest.raises(exc, match=match):
        oa.work(Ident("openalex", "W1"))


def test_work_refuses_kinds_openalex_cannot_answer() -> None:
    with pytest.raises(ValueError, match="arxiv"):
        OpenAlex(http=FakeHttp(), api_key="", email="").work(Ident("arxiv", "2401.12345"))


def test_content_needs_a_key() -> None:
    with pytest.raises(NeedsApiKey):
        OpenAlex(http=FakeHttp(), api_key="", email="").content("W1", "pdf")
    with pytest.raises(ValueError, match="kind"):
        OpenAlex(http=FakeHttp(), api_key="k", email="").content("W1", "docx")
    ok = OpenAlex(
        http=FakeHttp({"content.openalex.org/works/W1.pdf": Response(200, {}, PDF, "")}),
        api_key="k",
        email="",
    )
    assert ok.content("W1", "pdf") == PDF
    assert ok.content("W2", "pdf") is None
    bad = OpenAlex(
        http=FakeHttp({"content.openalex.org": Response(401, {}, b"", "")}), api_key="k", email=""
    )
    with pytest.raises(BadApiKey):
        bad.content("W1", "grobid-xml")


def test_environment_supplies_key_and_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENALEX_API_KEY", "envkey")
    monkeypatch.setenv("PAPERLIB_EMAIL", "someone@example.org")
    oa = OpenAlex(http=FakeHttp())
    assert (oa.api_key, oa.email) == ("envkey", "someone@example.org")


# ---------------------------------------------------------------------------------- NCBI idconv


def test_idconv_fills_missing_identifiers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPERLIB_EMAIL", "someone@example.org")
    http = FakeHttp({"idconv": J(fixture("idconv.json"))})
    ids, note = enrich({"doi": "10.5555/example.001", "pmid": None, "pmcid": None}, http)
    assert (ids["pmcid"], ids["pmid"]) == ("PMC3333333", "33333333")
    assert note == "ncbi idconv filled pmcid, pmid"
    assert "email=someone%40example.org" in http.log[0]


@pytest.mark.parametrize(
    ("ids", "http", "note"),
    [
        ({"doi": "d", "pmid": "1", "pmcid": "PMC1"}, FakeHttp(), "complete"),
        ({"doi": None}, FakeHttp(), "nothing to convert from"),
        ({"doi": "d"}, DeadHttp(), "ncbi idconv unreachable: ConnectionError"),
        ({"doi": "d"}, FakeHttp(), "ncbi idconv HTTP 404"),
        (
            {"doi": "d"},
            FakeHttp({"idconv": Response(200, {}, b"<html>", "")}),
            "ncbi idconv: unparseable response",
        ),
        (
            {"doi": "d"},
            FakeHttp({"idconv": J({"records": [{"status": "error"}]})}),
            "ncbi idconv: no record",
        ),
        (
            {"doi": "d", "pmid": "1", "pmcid": None},
            FakeHttp({"idconv": J({"records": [{"pmid": "1"}]})}),
            "ncbi idconv: nothing new",
        ),
    ],
)
def test_idconv_never_fails(
    ids: dict[str, str | None], http: FakeHttp | DeadHttp, note: str
) -> None:
    assert enrich(ids, http)[1] == note
