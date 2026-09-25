"""Bytes to readable text, and a gate that refuses to call garbage "full text".

## The failure this exists to stop

A tempting shortcut turns a PDF into "text" by deleting non-ASCII bytes. On Sandve et al. 2013
("Ten Simple Rules for Reproducible Computational Research", PLOS Comput Biol, CC BY) that
produces ~83,000 characters of PDF font-width tables ("354 781 604 927 750 822 ...") which do not
contain a single sentence of the paper, while `pypdf` on the same file recovers ~25,000 characters
of the actual text.

A PDF's content streams are compressed. There is no byte-level shortcut. So text comes from a
real parser or not at all, and `looks_like_prose` gates the result either way.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET

__all__ = ["is_html", "is_pdf", "looks_like_prose", "pdf_text", "xml_text"]

MIN_CHARS = 2000
MIN_WORD_FRACTION = 0.55
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]{1,}[.,;:)]?")


def is_pdf(data: bytes) -> bool:
    return data[:1024].lstrip().startswith(b"%PDF-")


def is_html(data: bytes) -> bool:
    head = data[:512].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html")) or b"<head" in head


def looks_like_prose(text: str | None) -> tuple[bool, str]:
    """True only for text a person could read. Returns (ok, reason)."""
    if not text:
        return False, "empty"
    if len(text) < MIN_CHARS:
        return False, f"only {len(text)} characters"
    tokens = text.split()
    words = sum(1 for t in tokens if _WORD.fullmatch(t))
    frac = words / max(len(tokens), 1)
    if frac < MIN_WORD_FRACTION:
        return False, (
            f"only {frac:.0%} of tokens are words -- this is what PDF operators or font "
            "tables look like, not a paper"
        )
    return True, f"{len(text)} characters, {frac:.0%} word tokens"


def pdf_text(data: bytes) -> tuple[str | None, str]:
    """(text, method). Never falls back to a byte strip."""
    if not is_pdf(data):
        return None, "not-a-pdf"
    import pypdf  # noqa: PLC0415 -- deferred: importing pypdf costs ~100 ms and most calls hit the cache

    try:
        reader = pypdf.PdfReader(io.BytesIO(data))
        text = "\n\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception as e:  # noqa: BLE001 -- a malformed PDF is an outcome, recorded by name
        return None, f"pypdf-failed: {type(e).__name__}"
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"-\n(?=[a-z])", "", text)  # re-join words hyphenated across lines
    return text.strip(), f"pypdf {pypdf.__version__}"


def xml_text(data: bytes) -> tuple[str | None, str]:
    """JATS (Europe PMC, PMC, bioRxiv) or TEI (GROBID) to text; body paragraphs where a body exists.

    The parser is the standard library's, which does not resolve external entities.
    """
    try:
        root = ET.fromstring(data)  # expat does not fetch external entities
    except ET.ParseError as e:
        return None, f"xml-parse-failed: {e}"
    for el in root.iter():  # drop namespaces: JATS and TEI both use them
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    body = root.find(".//body")
    scope = body if body is not None else root
    paras = [" ".join("".join(p.itertext()).split()) for p in scope.iter("p")]
    text = "\n\n".join(p for p in paras if p)
    if not text:
        text = " ".join("".join(scope.itertext()).split())
    kind = "tei" if root.tag == "TEI" else "jats" if root.tag == "article" else root.tag
    return text, f"xml-{kind}"
