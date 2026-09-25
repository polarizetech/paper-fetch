"""Structural guarantees, checked against the source rather than promised in prose."""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "paperlib"
FILES = {p.relative_to(SRC).as_posix(): p.read_text() for p in SRC.rglob("*.py")}


def _code(text: str) -> str:
    """Source with docstrings and comments removed."""
    no_docs = re.sub(r'"""[\s\S]*?"""', "", text)
    return "\n".join(line.split("#", 1)[0] for line in no_docs.splitlines())


CODE = {name: _code(text) for name, text in FILES.items()}


def test_no_code_path_sets_an_object_acl() -> None:
    assert not [n for n, c in CODE.items() if "ACL" in c]


def test_no_email_address_is_hard_coded() -> None:
    assert not [n for n, c in CODE.items() if re.search(r"[\w.+-]+@[\w-]+\.\w{2,}", c)]


def test_no_google_scholar_and_no_crossref_full_text_route() -> None:
    assert not [n for n, c in CODE.items() if "scholar.google" in c or "api.crossref.org" in c]


def test_no_byte_strip_text_extraction() -> None:
    assert not [n for n, c in CODE.items() if r"[^\x09\x0a\x0d\x20-\x7e]" in c]


def test_only_the_http_module_and_store_touch_urllib_request() -> None:
    users = sorted(n for n, c in CODE.items() if "urlopen" in c)
    assert users == ["http.py", "store.py"]


def test_core_imports_are_stdlib_plus_pypdf() -> None:
    optional = {"mcp_server.py": {"mcp"}, "store.py": {"boto3", "botocore"}}
    for name, text in FILES.items():
        mods: set[str] = set()
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
            elif isinstance(node, ast.Import):
                mods |= {a.name.split(".")[0] for a in node.names}
        third_party = {m for m in mods if m not in sys.stdlib_module_names}
        assert third_party <= {"pypdf"} | optional.get(name, set()), (name, third_party)


@pytest.mark.parametrize("name", sorted(FILES))
def test_every_module_has_a_docstring_or_is_trivial(name: str) -> None:
    tree = ast.parse(FILES[name])
    assert ast.get_docstring(tree) or len(tree.body) <= 3, name
