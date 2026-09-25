from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import PDF, FakeProvider, J, fixture, make_library
from paperlib import cli
from paperlib.providers import Hit

DOI = "10.5555/example.001"


def run(argv: list[str], capsys: pytest.CaptureFixture[str], **kw: object) -> tuple[int, str, str]:
    code = cli.main(argv, **kw)  # type: ignore[arg-type]
    out, err = capsys.readouterr()
    return code, out, err


def test_fetch_text_provenance_verify(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    code, out, _ = run(["fetch", DOI], capsys, library=lib)
    assert code == 0
    assert out.startswith("✓ W7  [retrieved]  2020  An example open-access article")
    code, out, _ = run(["text", "W7"], capsys, library=lib)
    assert "unfamiliar" in out
    code, out, _ = run(["provenance", "W7"], capsys, library=lib)
    assert json.loads(out)["route"] == "good:pdf"
    code, out, _ = run(["verify", "W7"], capsys, library=lib)
    assert (code, json.loads(out)["ok"]) == (0, True)
    lib.store.put("papers/works/W7/fulltext.txt", b"edited")
    assert run(["verify", "W7"], capsys, library=lib)[0] == 3


def test_fetch_not_obtainable_prints_every_attempt(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library(providers=[FakeProvider("none")])
    _, out, _ = run(["fetch", DOI], capsys, library=lib)
    assert "✗ W7  [not-obtainable]" in out
    assert "no open-access copy known" in out
    assert "OA copies only" in out


def test_library_status_rebuild(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    _, out, _ = run(["library"], capsys, library=lib)
    assert "1 work(s)" in out
    _, out, _ = run(["library", "unfamiliar", "--full-text"], capsys, library=lib)
    assert "[full text]" in out
    _, out, _ = run(["status"], capsys, library=lib)
    assert json.loads(out)["works"] == 1
    _, out, _ = run(["rebuild-index"], capsys, library=lib)
    assert out.strip() == "catalogue rebuilt: 1 works"
    _, out, _ = run(["adopt-orphans"], capsys, library=lib)
    assert json.loads(out) == []


def test_search_and_providers(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    lib.fetch(DOI)
    lib.search_providers = [
        FakeProvider("a", hits=[Hit("a", "An example", 2020, {"doi": DOI}, True)]),
        FakeProvider("gated", needs=("PAPERLIB_TEST_NEVER_SET",)),
    ]
    _, out, _ = run(["search", "example", "-n", "3"], capsys, library=lib)
    assert "1 distinct papers" in out
    assert " ■ 2020 [a] An example  10.5555/example.001" in out
    assert "needs PAPERLIB_TEST_NEVER_SET" in out
    _, out, _ = run(["providers"], capsys, library=lib)
    assert "pmc-s3" in out
    assert "needs PAPERLIB_EMAIL" in out


def test_citations(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library({"index/v2/citations/": J(fixture("opencitations_citations.json"))})
    _, out, _ = run(["citations", "10.5555/cited.001", "-n", "1"], capsys, library=lib)
    assert "10.5555/cited.001 cited by 2 works [ok]" in out
    assert "2025-06" in out
    assert "self-cite" in out
    assert "1 more (-n)" in out
    lib2, _ = make_library({"index/v2/references/": J([])})
    _, out, _ = run(["citations", "10.5555/x", "--references"], capsys, library=lib2)
    assert "cites 0 works" in out


def test_add(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library()
    pdf = tmp_path / "held.pdf"
    pdf.write_bytes(PDF)
    code, out, _ = run(["add", str(pdf), DOI, "--rights", "author copy"], capsys, library=lib)
    assert code == 0
    assert json.loads(out)["route"] == "operator-supplied:pdf"


def test_errors_are_short_and_have_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    lib, _ = make_library({"index/v2": J({}, 429)})
    assert run(["text", DOI], capsys, library=lib)[0:3:2] == (
        1,
        f"not found: {DOI} is not in the library -- fetch() it first\n",
    )
    code, _, err = run(["fetch", "12345"], capsys, library=lib)
    assert code == 2
    assert err.startswith("error: unrecognised identifier")
    code, _, err = run(["citations", DOI], capsys, library=lib)
    assert code == 2
    assert err.startswith("unavailable:")


def test_default_library_from_environment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PAPERLIB_STORE", "memory")
    code, out, _ = run(["library"], capsys)
    assert code == 0
    assert "0 work(s)" in out


def test_module_entry_point(monkeypatch: pytest.MonkeyPatch) -> None:
    import runpy  # noqa: PLC0415

    monkeypatch.setenv("PAPERLIB_STORE", "memory")
    monkeypatch.setattr("sys.argv", ["paperlib", "status"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("paperlib", run_name="__main__")
    assert exc.value.code == 0
