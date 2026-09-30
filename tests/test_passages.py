"""The passage index and retrieval: exact offsets, stable ids, hybrid ranking, scope, surfaces."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from conftest import make_library
from paper_fetch import Library
from paper_fetch import mcp_server as srv
from paper_fetch.cli import main as cli_main
from paper_fetch.passages import OllamaEmbedder, Passage, PassageIndex, chunk, clean, fts_query

DOI = "10.5555/example.001"  # W7 in make_library's OpenAlex fixture
VOCAB = ["sleep", "memory", "heart", "rate", "exercise", "recall", "spindle", "athlete"]


def embed(texts: list[str]) -> list[list[float]]:
    """A deterministic bag-of-words embedding over a tiny vocabulary."""
    return [[float(t.lower().count(w)) + 0.01 for w in VOCAB] for t in texts]


def para(topic: str, n: int = 6) -> str:
    return " ".join(f"Sentence {i} is about {topic} in some detail." for i in range(n))


PAPER_A = "\n\n".join(
    [
        para("sleep spindle and memory recall"),
        para("methods and participants"),
        "References\n\nSmith J. Sleep and memory. 2001.",
    ]
)
PAPER_B = "\n\n".join([para("heart rate in the athlete"), para("exercise and heart rate")])


def rec(work: str, **kw: Any) -> dict[str, Any]:
    return {"work": work, "title": f"Title {work}", "year": 2020, "full_text": True, **kw}


@pytest.fixture
def idx(tmp_path: Path) -> PassageIndex:
    i = PassageIndex(tmp_path / "p.sqlite", "fake", embed)
    i.add(rec("WA"), PAPER_A)
    i.add(rec("WB"), PAPER_B)
    return i


# ---------------------------------------------------------------------------------- chunking


def test_chunk_offsets_are_exact_and_references_are_dropped() -> None:
    long = "\n\n".join(para(f"topic {i}", n) for i, n in ((0, 5), (1, 80), (2, 5)))
    long += "\n\nReferences\n\nA. 2001."
    pieces = chunk(long)
    assert len(pieces) >= 3  # the 80-sentence paragraph is split at sentence boundaries
    for start, end, body in pieces:
        assert long[start:end] == body
        assert len(body) <= 1600
    assert all("2001" not in b for _, _, b in pieces)


def test_clean_removes_and_counts_hidden_characters() -> None:
    assert clean("vis\u200bible\u202etext") == ("visibletext", 2)
    assert fts_query('the "sleep" AND memory OR near(x)') == '"sleep" OR "memory" OR "near"'
    assert fts_query("the of and") == ""


# ---------------------------------------------------------------------------------- the index


def test_hybrid_search_and_scope(idx: PassageIndex) -> None:
    top = idx.search("spindle memory", embed(["spindle memory"])[0], limit=3)
    assert top[0].work == "WA"
    assert (top[0].bm25_rank, top[0].dense_rank) == (1, 1)
    assert top[0].id == "WA#p0"
    lexical = idx.search("athlete heart", None, limit=2)
    assert lexical[0].work == "WB"
    assert lexical[0].dense_rank is None
    only_a = idx.search("heart rate", embed(["heart rate"])[0], works=["WA"])
    assert {p.work for p in only_a} <= {"WA"}
    assert idx.search("heart", None, works=[]) == []
    assert idx.search("heart", None, works=["W-none"]) == []


def test_ids_are_stable_and_unchanged_text_is_not_reindexed(tmp_path: Path) -> None:
    a = PassageIndex(tmp_path / "a.sqlite", "fake", embed)
    a.add(rec("WA"), PAPER_A)
    b = PassageIndex(tmp_path / "b.sqlite", "fake", embed)
    b.add(rec("WB"), PAPER_B)  # different row ids for WA below
    b.add(rec("WA"), PAPER_A)
    assert [p.id for p in a.passages("WA", 0, 9)] == [p.id for p in b.passages("WA", 0, 9)]
    assert [p.sha for p in a.passages("WA", 0, 9)] == [p.sha for p in b.passages("WA", 0, 9)]
    assert a.add(rec("WA"), PAPER_A) == 0
    assert a.add(rec("WA"), PAPER_A + "\n\n" + para("an erratum")) > 0  # changed text: replaced
    assert a.stats()["papers"] == 1


def test_offsets_refer_to_the_cleaned_text(tmp_path: Path) -> None:
    i = PassageIndex(tmp_path / "p.sqlite")
    raw = para("hidden\u200b text")
    i.add(rec("WH"), raw)
    text, hidden = clean(raw)
    p = i.passages("WH")[0]
    assert text[p.start : p.end] == p.text
    paper = i.paper("WH")
    assert paper is not None
    assert paper["hidden_chars"] == hidden == 6
    assert i.stats()["with_vectors"] == 0  # no embedding model: lexical only


def test_an_index_refuses_a_different_embedding_model(tmp_path: Path) -> None:
    PassageIndex(tmp_path / "p.sqlite", "fake", embed)
    with pytest.raises(RuntimeError, match="not comparable"):
        PassageIndex(tmp_path / "p.sqlite", "other", embed)
    with pytest.raises(RuntimeError, match="'fake', not 'none'"):
        PassageIndex(tmp_path / "p.sqlite")


def test_remove(idx: PassageIndex) -> None:
    idx.remove("WA")
    assert idx.works() == ["WB"]
    assert all(p.work == "WB" for p in idx.search("sleep heart", embed(["sleep heart"])[0]))


def test_ollama_embedder_posts_to_the_configured_endpoint() -> None:
    sent: list[tuple[str, Any]] = []

    class Http:
        def post_json(self, url: str, payload: Any, *, timeout: float | None = None) -> Any:
            sent.append((url, payload))
            return {"embeddings": [[1.0, 0.0]]}

    e = OllamaEmbedder("bge-m3", "http://h:1/", http=Http())  # type: ignore[arg-type]
    assert e(["x"]) == [[1.0, 0.0]]
    assert sent == [
        ("http://h:1/api/embed", {"model": "bge-m3", "input": ["x"], "keep_alive": "30m"})
    ]


# ---------------------------------------------------------------------------------- the library


def _library(tmp_path: Path) -> Library:
    lib, _ = make_library()
    lib.passages = PassageIndex(tmp_path / "lib.sqlite", "fake", embed)
    lib.fetch(DOI)
    return lib


def test_retrieve_indexes_its_scope_on_demand(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    lib.create_collection("review")
    lib.collect("review", [DOI, "10.5555/not-held"])
    res = lib.retrieve("unfamiliar project files", collection="review", limit=2)
    assert res["scope"] == 1
    assert res["indexed"]["indexed"] == ["W7"]
    assert res["indexed"]["not_held"] == ["doi:10.5555/not-held"]
    top = res["results"][0]["passages"][0]
    assert top["id"].startswith("W7#p")
    assert top["paper"]["title"] == "An example open-access article"
    assert top["paper"]["is_retracted"] is False
    assert lib.text("W7")[top["start"] : top["end"]] == top["text"]
    assert lib.retrieve("again", identifiers=["W7"])["indexed"]["unchanged"] == 1


def test_retrieve_several_queries_caps_per_paper_and_reranks(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    lib.passages.add(rec("WA"), PAPER_A)
    lib.passages.add(rec("WB"), PAPER_B)
    res = lib.retrieve(["sleep memory", "heart rate"], limit=5, per_paper=1)
    firsts = [r["passages"][0]["work"] for r in res["results"]]
    assert firsts == ["WA", "WB"]
    for r in res["results"]:
        works = [p["work"] for p in r["passages"]]
        assert len(works) == len(set(works))

    class Reverse:
        name = "reverse"

        def score(self, query: str, passages: list[Passage]) -> list[float]:
            return [float(i) for i in range(len(passages))]  # last fused becomes first

    lib.reranker = Reverse()
    ranked = lib.retrieve("sleep memory", limit=30)
    assert ranked["reranker"] == "reverse"
    scores = [p["rerank_score"] for p in ranked["results"][0]["passages"]]
    assert scores == sorted(scores, reverse=True)
    plain = lib.retrieve("sleep memory", rerank=False)
    assert plain["reranker"] is None
    assert all("rerank_score" not in p for p in plain["results"][0]["passages"])
    lib.reranker = None
    with pytest.raises(ValueError, match="PAPER_FETCH_RERANK_MODEL"):
        lib.retrieve("x", rerank=True)


def test_paper_passages(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    got = lib.paper_passages(DOI, 1, 1)
    assert [p["ord"] for p in got["passages"]] == [1]
    assert got["work"] == "W7"


def test_default_index_path_and_lexical_default(tmp_path: Path) -> None:
    lib, _ = make_library()
    assert lib.passages.path == tmp_path / "data" / "passages.sqlite"
    assert lib.passages.stats()["embedding_model"] is None
    assert lib.reranker is None


# ---------------------------------------------------------------------------------- surfaces


def test_mcp_retrieve_passages_and_index(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    srv._library = lib
    try:
        assert srv.retrieve()["code"] == "tool_error"
        got = srv.retrieve(query="unfamiliar project", identifiers=[DOI], limit=1)["data"]
        assert len(got["results"][0]["passages"]) == 1
        assert srv.passages(DOI, 0, 2)["data"]["passages"][0]["id"] == "W7#p0"
        assert srv.passages("10.5555/nope")["code"] == "not_found"
        report = srv.index()["data"]
        assert (report["unchanged"], report["index"]["papers"]) == (1, 1)
    finally:
        srv._library = None


def test_cli_index_and_retrieve(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lib = _library(tmp_path)
    assert cli_main(["index"], library=lib) == 0
    out = capsys.readouterr().out
    assert out.startswith("indexed 1 paper(s)")
    assert json.loads(out.splitlines()[-1])["papers"] == 1
    assert cli_main(["retrieve", "unfamiliar project", "-n", "1"], library=lib) == 0
    out = capsys.readouterr().out
    assert re.search(r"^W7#p\d+  \[\d+:\d+\]  2020", out, re.M)
    assert "scope: whole index" in out


def test_cli_rebuild_starts_a_fresh_index(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    lib.passages.add(rec("WX"), PAPER_A)  # a stale row
    assert cli_main(["index", "--rebuild"], library=lib) == 0
    capsys.readouterr()
    assert lib.passages.works() == ["W7"]


def test_the_embedder_is_read_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    from paper_fetch.passages import embedder_from_env  # noqa: PLC0415

    assert embedder_from_env() == ("", None)
    monkeypatch.setenv("PAPER_FETCH_EMBED_MODEL", "bge-m3")
    monkeypatch.setenv("PAPER_FETCH_EMBED_URL", "http://h:2")
    model, e = embedder_from_env()
    assert model == "bge-m3"
    assert isinstance(e, OllamaEmbedder)
    assert e.url == "http://h:2"


def test_relevance_by_embedding_and_by_concepts(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    got = lib.relevance(["sleep and memory"], ["Sleep spindles and memory", "Heart rate in rowers"])
    assert got["method"] == "embedding:fake"
    first, second = got["scores"]
    assert first > second
    lexical, _ = make_library()
    lexical.passages = PassageIndex(tmp_path / "lex.sqlite")
    got = lexical.relevance(["sleep memory"], ["memory and sleep", "rowing"])
    assert got == {"scores": [1.0, 0.0], "method": "concepts"}
    assert lexical.relevance([], ["x"]) == {"scores": [0.0], "method": "none"}
    srv._library = lexical
    try:
        assert srv.relevance(["sleep"], ["sleep"])["data"]["scores"] == [1.0]
    finally:
        srv._library = None


def test_passages_carry_their_route(tmp_path: Path) -> None:
    lib = _library(tmp_path)
    paper = lib.paper_passages(DOI)["passages"][0]["paper"]
    assert (paper["route"], paper["format"]) == ("good:pdf", "pdf")
