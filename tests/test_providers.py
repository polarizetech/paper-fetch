"""Each adapter parses a small synthetic fixture shaped like the service's live response."""

from __future__ import annotations

import pytest

from conftest import PDF, DeadHttp, FakeHttp, J, fixture
from paper_fetch import OpenAlex
from paper_fetch.http import Response
from paper_fetch.providers import (
    DEFAULT_SEARCH,
    DOAJ,
    HAL,
    OSF,
    PLOS,
    REGISTRY,
    Hit,
    Location,
    OpenAIRE,
    Provider,
    ProviderUnavailable,
    build,
    clean_doi,
    describe,
)
from paper_fetch.providers.adapters import (
    PMCS3,
    Biorxiv,
    Core,
    EuropePMC,
    OpenAlexProvider,
    PubMed,
    Unpaywall,
)

# ---------------------------------------------------------------------------------- base


def test_clean_doi() -> None:
    assert clean_doi(" https://doi.org/10.5555/ABC. ") == "10.5555/abc"
    assert clean_doi("doi:10.5555/x") == "10.5555/x"
    assert clean_doi(None) is None
    assert clean_doi("") is None


def test_hit_json_round_trip() -> None:
    h = Hit("p", "T", 2020, {"doi": "10.5555/x"}, True, ["A"], [Location("u", "pdf", "p")])
    assert Hit.from_json(h.to_json()) == h


def test_a_429_makes_a_provider_unavailable_and_names_the_key() -> None:
    p = PubMed(FakeHttp({"eutils": Response(429, {}, b"", "")}))
    with pytest.raises(ProviderUnavailable, match="NCBI_API_KEY"):
        p.search("x")


def test_an_unreachable_provider_is_unavailable() -> None:
    p = Provider(DeadHttp())
    p.name = "dead"
    with pytest.raises(ProviderUnavailable, match="unreachable"):
        p._get("https://x")


def test_base_provider_defaults() -> None:
    p = Provider(FakeHttp({"ok": Response(200, {}, b"body", "")}))
    assert p.locate({"doi": "10.5555/x"}) == []
    assert p.download(Location("https://ok", "pdf", "p")) == b"body"
    assert p.download(Location("https://missing", "pdf", "p")) is None
    with pytest.raises(NotImplementedError):
        p.search("x")


# ---------------------------------------------------------------------------------- registry


def test_unknown_provider_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown provider"):
        build(FakeHttp(), names=["pubmed", "googlescholar"])


@pytest.mark.parametrize("name", ["pmc-s3", "biorxiv", "unpaywall"])
def test_locate_only_providers_cannot_search(name: str) -> None:
    with pytest.raises(ValueError, match="cannot search"):
        build(FakeHttp(), names=[name], purpose="search")


@pytest.mark.parametrize("name", ["doaj", "web", "pubmed"])
def test_search_only_providers_are_never_copy_sources(name: str) -> None:
    with pytest.raises(ValueError, match="cannot locate"):
        build(FakeHttp(), names=[name], purpose="locate")


def test_build_rejects_an_unknown_purpose() -> None:
    with pytest.raises(ValueError, match="purpose"):
        build(FakeHttp(), purpose="download")


def test_environment_sets_the_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_FETCH_PROVIDERS", "europepmc, pmc-s3")
    assert [p.name for p in build(FakeHttp())] == ["europepmc", "pmc-s3"]
    monkeypatch.setenv("PAPER_FETCH_SEARCH_PROVIDERS", "pubmed")
    assert [p.name for p in build(FakeHttp(), purpose="search")] == ["pubmed"]


def test_defaults_and_registry() -> None:
    assert not any("google" in n or "scholar" in n for n in REGISTRY)
    assert "web" not in DEFAULT_SEARCH
    for gone in ("arxiv", "semanticscholar", "zenodo", "crossref"):
        assert gone not in REGISTRY
    d = {x["name"]: x for x in describe(FakeHttp())}
    assert set(d) == set(REGISTRY)
    assert d["unpaywall"]["available"] is False
    assert d["unpaywall"]["why"] == "needs PAPER_FETCH_EMAIL"
    assert d["openalex"]["recommends"] == ["OPENALEX_API_KEY"]


# ---------------------------------------------------------------------------------- OpenAlex


WORK = {
    "id": "https://openalex.org/W5",
    "open_access": {"is_oa": True},
    "has_content": {"pdf": True, "grobid_xml": True},
    "best_oa_location": {"license": "cc-by"},
    "locations": [
        {"is_oa": True, "pdf_url": "https://pub.example/a.pdf", "version": "acceptedVersion"},
        {"is_oa": True, "pdf_url": "https://pub.example/p.pdf", "version": "publishedVersion"},
        {"is_oa": False, "pdf_url": "https://pub.example/closed.pdf"},
    ],
}


def test_openalex_without_key_offers_open_locations_only() -> None:
    p = OpenAlexProvider(FakeHttp(), OpenAlex(http=FakeHttp(), api_key="", email=""))
    locs = p.locate({"_openalex_work": WORK})
    assert [x.url for x in locs] == ["https://pub.example/p.pdf", "https://pub.example/a.pdf"]
    assert p.locate({"doi": "10.5555/x"}) == []


def test_openalex_with_key_offers_its_own_copies_first_and_keeps_urls_clean() -> None:
    http = FakeHttp({"content.openalex.org/works/W5.pdf": Response(200, {}, PDF, "")})
    p = OpenAlexProvider(http, OpenAlex(http=http, api_key="SECRET", email=""))
    locs = p.locate({"_openalex_work": WORK})
    assert [x.fmt for x in locs[:2]] == ["tei-xml", "pdf"]
    assert "content.openalex.org" in locs[0].url
    assert not any("SECRET" in x.url for x in locs)
    assert p.download(locs[1]) == PDF
    assert "api_key=SECRET" in http.log[-1]


def test_openalex_search_parses_hits() -> None:
    http = FakeHttp({"api.openalex.org/works?": J(fixture("openalex_search.json"))})
    hits = OpenAlexProvider(http, OpenAlex(http=http, api_key="", email="")).search("example")
    assert [h.ids for h in hits] == [
        {"doi": "10.5555/example.001", "openalex": "W7"},
        {"doi": None, "openalex": "W8"},
    ]
    assert hits[0].authors == ["Ada Example"]
    assert hits[0].is_oa is True
    assert "filter=is_oa%3Atrue" in http.log[0]


# ---------------------------------------------------------------------------------- Europe PMC


def test_europepmc_search() -> None:
    http = FakeHttp({"europepmc/webservices/rest/search": J(fixture("europepmc_search.json"))})
    hits = EuropePMC(http).search("x")
    assert hits[0].locations[0].fmt == "jats-xml"
    assert hits[0].locations[0].url.endswith("/PMC9/fullTextXML")
    assert hits[0].ids["doi"] == "10.5555/a"
    assert hits[0].authors == ["Example A", "Sample B", "Third C"]
    assert hits[0].extra["journal"] == "Example Journal"
    assert hits[1].locations == []  # closed
    assert hits[2].locations[0].version == "preprint"
    assert "OPEN_ACCESS%3Ay" in http.log[0]


def test_europepmc_locate() -> None:
    http = FakeHttp({"europepmc/webservices/rest/search": J(fixture("europepmc_search.json"))})
    p = EuropePMC(http)
    assert len(p.locate({"pmcid": "PMC9"})) == 1
    assert "PMCID%3APMC9" in http.log[-1]
    assert len(p.locate({"doi": "10.5555/a"})) == 1
    assert p.locate({}) == []
    assert EuropePMC(FakeHttp()).locate({"doi": "10.5555/a"}) == []


# ---------------------------------------------------------------------------------- PubMed


def test_pubmed_search(monkeypatch: pytest.MonkeyPatch) -> None:
    routes = {
        "esearch.fcgi": J(fixture("pubmed_esearch.json")),
        "esummary.fcgi": J(fixture("pubmed_esummary.json")),
    }
    http = FakeHttp(routes)
    h = PubMed(http).search("reproducible")
    assert h[0].ids == {
        "pmid": "24204232",
        "pmcid": "PMC3812051",
        "doi": "10.1371/journal.pcbi.1003285",
    }
    assert h[0].year == 2013
    assert h[0].is_oa is True
    assert "free%20full%20text" in http.log[0]
    assert "tool=paper-fetch" in http.log[0]
    assert "email" not in http.log[0]

    monkeypatch.setenv("PAPER_FETCH_EMAIL", "someone@example.org")
    monkeypatch.setenv("NCBI_API_KEY", "k")
    http2 = FakeHttp(routes)
    p = PubMed(http2)
    p.min_interval_s = 0
    p.search("reproducible", oa_only=False)
    assert "email=someone%40example.org" in http2.log[0]
    assert "api_key=k" in http2.log[0]
    assert "free%20full%20text" not in http2.log[0]


def test_pubmed_empty_and_failed() -> None:
    assert (
        PubMed(FakeHttp({"esearch.fcgi": J({"esearchresult": {"idlist": []}})})).search("x") == []
    )
    assert PubMed(FakeHttp()).search("x") == []


# ---------------------------------------------------------------------------------- PMC S3

PMC_LIST = Response(
    200,
    {},
    b"<ListBucketResult><Contents><Key>PMC1.1/PMC1.1.json</Key></Contents>"
    b"<Contents><Key>PMC1.2/PMC1.2.json</Key></Contents></ListBucketResult>",
    "",
)


def _pmc(meta_overrides: dict[str, object]) -> tuple[PMCS3, FakeHttp]:
    meta = {**fixture("pmc_s3_meta.json"), **meta_overrides}
    http = FakeHttp({"?list-type=2": PMC_LIST, "PMC1.2.json": J(meta)})
    return PMCS3(http), http


def test_pmc_s3_highest_version_with_md5_and_licence() -> None:
    p, http = _pmc({})
    locs = p.locate({"pmcid": "PMC1"})
    assert any("PMC1.2.json" in u for u in http.log)
    assert [(x.fmt, x.md5, x.license) for x in locs] == [
        ("jats-xml", "abc", "CC BY"),
        ("pdf", "def", "CC BY"),
    ]
    assert locs[0].url == "https://pmc-oa-opendata.s3.amazonaws.com/PMC1.2/PMC1.2.xml"


def test_pmc_s3_refuses_records_outside_the_oa_subset() -> None:
    assert _pmc({"is_pmc_openaccess": False})[0].locate({"pmcid": "PMC1"}) == []
    not_manuscript = {"is_pmc_openaccess": False, "is_manuscript": False, "license_code": "TDM"}
    assert _pmc(not_manuscript)[0].locate({"pmcid": "PMC1"}) == []


def test_pmc_s3_labels_tdm_author_manuscripts() -> None:
    tdm = {"is_pmc_openaccess": False, "is_manuscript": True, "license_code": "TDM"}
    locs = _pmc(tdm)[0].locate({"pmcid": "PMC1"})
    assert len(locs) == 2
    assert all(x.license == "TDM" and x.version == "manuscript" for x in locs)
    assert all("not reuse" in x.note for x in locs)


def test_pmc_s3_needs_a_pmcid() -> None:
    http = FakeHttp()
    assert PMCS3(http).locate({"doi": "10.5555/x"}) == []
    assert http.calls == 0
    assert (
        PMCS3(FakeHttp({"?list-type=2": Response(200, {}, b"<x/>", "")})).locate({"pmcid": "PMC1"})
        == []
    )


# ---------------------------------------------------------------------------------- CORE


def test_core_claims_no_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({"api.core.ac.uk": J(fixture("core_search.json"))})
    hits = Core(http).search("x")
    assert hits[0].locations[0].license is None
    assert "no licence" in hits[0].locations[0].note
    assert hits[1].locations == []
    assert http.headers[0] == {}
    monkeypatch.setenv("CORE_API_KEY", "k")
    locs = Core(http).locate({"doi": "10.5555/x"})
    assert locs[0].url == "https://core.ac.uk/download/1.pdf"
    assert http.headers[-1] == {"Authorization": "Bearer k"}
    assert Core(http).locate({"doi": "10.5555/other"}) == []
    assert Core(http).locate({}) == []


# ---------------------------------------------------------------------------------- bioRxiv


def test_biorxiv_latest_version() -> None:
    p = Biorxiv(FakeHttp({"api.biorxiv.org/details/biorxiv/": J(fixture("biorxiv_details.json"))}))
    locs = p.locate({"doi": "10.1101/2024.01.01.000001"})
    assert locs[0].url.endswith("v2.xml")
    assert locs[0].license == "cc_no"
    assert locs[0].version == "preprint v2"
    assert Biorxiv(FakeHttp()).locate({"doi": "10.1101/2024.01.01.000001"}) == []
    assert Biorxiv(FakeHttp()).locate({"doi": "10.1371/x"}) == []


# ---------------------------------------------------------------------------------- Unpaywall


def test_unpaywall_needs_an_email(monkeypatch: pytest.MonkeyPatch) -> None:
    http = FakeHttp({"api.unpaywall.org": J(fixture("unpaywall.json"))})
    assert Unpaywall(http).available() == (False, "needs PAPER_FETCH_EMAIL")
    monkeypatch.setenv("PAPER_FETCH_EMAIL", "someone@example.org")
    locs = Unpaywall(http).locate({"doi": "10.5555/x"})
    assert [(x.url, x.license) for x in locs] == [("https://repo.example.org/a.pdf", "cc-by")]
    assert "email=someone%40example.org" in http.log[-1]
    assert Unpaywall(http).locate({}) == []
    assert Unpaywall(FakeHttp({"api.unpaywall.org": J({})})).locate({"doi": "10.5555/x"}) == []


# ---------------------------------------------------------------------------------- OpenAIRE


def _openaire() -> tuple[OpenAIRE, FakeHttp]:
    rec = fixture("openaire_record.json")
    restricted = {**rec, "id": "r2", "pids": [], "bestAccessRight": {"label": "RESTRICTED"}}
    http = FakeHttp(
        {
            "researchProducts?pid=": J({"results": [rec]}),
            "researchProducts?search=": J({"results": [rec, restricted]}),
        }
    )
    return OpenAIRE(http), http


def test_openaire_open_per_record_licence_per_copy() -> None:
    p, _ = _openaire()
    locs = p.locate({"doi": "10.5555/example.002"})
    assert [x.url for x in locs] == [
        "https://journal.example.org/article/file?id=x&type=printable",
        "https://repo.example.edu/landing/7",
    ]
    assert p.locate({"doi": "10.5555/other"}) == []
    assert p.locate({}) == []


def test_openaire_ids_and_search() -> None:
    p, http = _openaire()
    assert p._ids(fixture("openaire_record.json")) == {
        "doi": "10.5555/example.002",
        "pmid": "22222222",
        "pmcid": "PMC2222222",
        "openaire": "doi_dedup___::0001",
    }
    hits = p.search("example", oa_only=False)
    assert hits[0].year == 2017
    assert hits[1].locations == []
    assert hits[1].is_oa is False
    p.search("example")
    assert "bestOpenAccessRightLabel=OPEN" in http.log[-1]


# ---------------------------------------------------------------------------------- PLOS


def test_plos_search_builds_locations_without_a_lookup() -> None:
    http = FakeHttp({"api.plos.org/search": J(fixture("plos_search.json"))})
    p = PLOS(http)
    hits = p.search("reproducible computational research")
    assert hits[0].ids["doi"] == "10.1371/journal.pcbi.1003285"
    assert [x.fmt for x in hits[0].locations] == ["jats-xml", "pdf"]
    assert hits[0].year == 2013
    assert http.calls == 1
    assert "reproducible%20AND%20computational%20AND%20research" in http.log[-1]
    p.search('"computational research"')
    assert "%22computational%20research%22" in http.log[-1]
    assert p.locate({"doi": "10.5555/not-plos"}) == []
    assert PLOS(FakeHttp()).search("x") == []


# ---------------------------------------------------------------------------------- OSF


def test_osf_share_cards() -> None:
    cards = J(fixture("osf_cards.json"))
    http = FakeHttp({"cardSearchText=": cards, "sameAs%5D=https%3A%2F%2Fdoi.org": cards})
    p = OSF(http)
    hits = p.search("example", oa_only=False)
    assert hits[0].ids == {"doi": "10.31234/osf.io/abcde", "osf": "abcde"}
    assert [x.url for x in hits[0].locations] == ["https://osf.io/abcde/download"]
    assert hits[0].extra["server"] == "PsyArXiv"
    assert hits[0].year == 2024
    assert hits[1].locations == []
    assert not hits[1].is_oa
    assert len(p.search("example")) == 1
    assert p.locate({"doi": "10.31234/osf.io/abcde"})
    assert "sameAs" in http.log[-1]
    n = http.calls
    assert p.locate({"doi": "10.5555/x"}) == []
    assert http.calls == n
    assert p.locate({"doi": "10.31234/osf.io/zzzzz"}) == []


# ---------------------------------------------------------------------------------- HAL / DOAJ


def test_hal_keeps_the_licence_verbatim() -> None:
    http = FakeHttp({"api.archives-ouvertes.fr/search": J(fixture("hal_search.json"))})
    p = HAL(http)
    hits = p.search("example", oa_only=False)
    assert "fq=submitType_s:file" in http.log[-1]
    assert hits[0].locations[0].license == "https://about.hal.science/hal-authorisation-v1/"
    assert hits[1].locations == []
    assert len(p.search("example")) == 1
    assert p.locate({"doi": "10.5555/hal.001"})[0].url.endswith("/document")
    assert p.locate({"doi": "10.5555/other"}) == []
    assert p.locate({}) == []


def test_doaj_is_search_only_and_takes_the_doi_not_the_issn() -> None:
    p = DOAJ(FakeHttp({"doaj.org/api/search/articles": J(fixture("doaj_search.json"))}))
    assert [(h.ids["doi"], h.locations, h.year) for h in p.search("example")] == [
        ("10.5555/doaj.001", [], 2021)
    ]
    assert DOAJ(FakeHttp()).search("x") == []


def test_empty_answers_are_empty() -> None:
    for cls in (OpenAIRE, HAL, OSF, Core):
        assert cls(FakeHttp()).search("x", oa_only=False) == []
