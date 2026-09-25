from __future__ import annotations

import re

from conftest import FONT_TABLE, JATS, PDF, PROSE, TEI
from paperlib.text import is_html, is_pdf, looks_like_prose, pdf_text, xml_text


def test_prose_passes_the_gate() -> None:
    ok, why = looks_like_prose(PROSE)
    assert ok, why


def test_font_tables_fail_the_gate() -> None:
    """What a byte-level 'extraction' of a PDF looks like: numbers, not words."""
    ok, why = looks_like_prose(FONT_TABLE)
    assert not ok
    assert "tokens are words" in why


def test_short_and_empty_text_fail() -> None:
    assert looks_like_prose(None) == (False, "empty")
    assert looks_like_prose("A short note.")[1].startswith("only 13 characters")


def test_pdf_text_reads_a_real_pdf() -> None:
    text, method = pdf_text(PDF)
    assert text is not None
    assert "unfamiliar with your project" in re.sub(r"\s+", " ", text)
    assert method.startswith("pypdf ")


def test_pdf_text_refuses_non_pdf_and_reports_parser_failure() -> None:
    assert pdf_text(b"<html>") == (None, "not-a-pdf")
    text, method = pdf_text(b"%PDF-1.4\nthis is not really a pdf")
    assert text is None or text == ""
    assert method.startswith(("pypdf", "pypdf-failed"))


def test_format_sniffing() -> None:
    assert is_pdf(b"  %PDF-1.7")
    assert not is_pdf(b"<html>")
    assert is_html(b"<!DOCTYPE html><html><head>")
    assert is_html(b"\n<HTML>")
    assert not is_html(PDF)


def test_jats_and_tei_body_paragraphs() -> None:
    jt, jm = xml_text(JATS)
    tt, tm = xml_text(TEI)
    assert jm == "xml-jats"
    assert tm == "xml-tei"
    assert jt is not None
    assert tt is not None
    assert "unfamiliar" in jt
    assert "unfamiliar" in tt


def test_xml_without_paragraphs_falls_back_to_all_text() -> None:
    text, method = xml_text(b"<doc><title>Only a title</title></doc>")
    assert (text, method) == ("Only a title", "xml-doc")


def test_malformed_xml_is_reported() -> None:
    text, method = xml_text(b"<article><body>")
    assert text is None
    assert method.startswith("xml-parse-failed")
