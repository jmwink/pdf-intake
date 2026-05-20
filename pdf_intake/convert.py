import contextlib
import os
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pymupdf
import pymupdf4llm

from pdf_intake.errors import ScannedPDFError
from pdf_intake.ocr import run_ocrmypdf


HEADER_FOOTER_MIN_PAGES = 5  # heuristic unreliable on short documents
HEADER_FOOTER_PAGE_RATIO = 0.5  # drop lines appearing on > 50% of pages


@dataclass
class ConvertResult:
    markdown: str
    used_ocr: bool
    ocr_pdf_path: Optional[Path]
    page_count: int


@contextlib.contextmanager
def _stdout_to_stderr():
    """Redirect fd 1 → fd 2 around a C-extension call.

    pymupdf4llm's auto-OCR path writes "=== Document parser messages ===" lines
    to fd 1 directly (from MuPDF's C layer), bypassing Python's sys.stdout. The
    `inspect` subcommand requires stdout to be valid JSON only, so we move those
    messages to stderr for the duration of the conversion call.
    """
    sys.stdout.flush()
    saved = os.dup(1)
    try:
        os.dup2(2, 1)
        yield
    finally:
        sys.stdout.flush()
        os.dup2(saved, 1)
        os.close(saved)


def needs_ocr(doc: pymupdf.Document) -> bool:
    """Spike 2 Method 1: any page with an empty text layer → route through ocrmypdf."""
    return any(
        len(doc.load_page(i).get_text()) == 0 for i in range(doc.page_count)
    )


def _strip_repeating_headers_footers(pages: list[str]) -> list[str]:
    """Drop lines appearing on > HEADER_FOOTER_PAGE_RATIO of pages.

    Identifies repeating headers/footers across the document. The first
    occurrence of any such line is preserved on its original page only if
    that page is the *first* page containing the line — this is how a real
    chapter title escapes the strip (it appears as a chapter heading on
    page 0 and as a running header on later pages; we keep page 0's copy).
    """
    if len(pages) < HEADER_FOOTER_MIN_PAGES:
        return pages

    line_pages: dict[str, list[int]] = {}
    for i, body in enumerate(pages):
        seen_on_page: set[str] = set()
        for raw in body.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line in seen_on_page:
                continue
            seen_on_page.add(line)
            line_pages.setdefault(line, []).append(i)

    threshold = len(pages) * HEADER_FOOTER_PAGE_RATIO
    repeating = {line: ps for line, ps in line_pages.items() if len(ps) > threshold}
    if not repeating:
        return pages

    # Keep the first occurrence (earliest page); drop all later occurrences.
    keep_on: dict[str, int] = {line: ps[0] for line, ps in repeating.items()}

    cleaned: list[str] = []
    for i, body in enumerate(pages):
        out_lines = []
        for raw in body.splitlines():
            line = raw.strip()
            if line in repeating and keep_on[line] != i:
                continue
            out_lines.append(raw)
        cleaned.append("\n".join(out_lines))
    return cleaned


def _pymupdf4llm_per_page(doc: pymupdf.Document) -> list[str]:
    """Run pymupdf4llm with page_chunks=True so we can post-process per page."""
    with _stdout_to_stderr():
        chunks = pymupdf4llm.to_markdown(
            doc, header=False, footer=False, page_chunks=True
        )
    # page_chunks returns a list of dicts; each has a 'text' key with that page's markdown.
    return [chunk["text"] for chunk in chunks]


def _raw_per_page(doc: pymupdf.Document) -> list[str]:
    return [doc.load_page(i).get_text() for i in range(doc.page_count)]


def convert(
    pdf_path: Path,
    min_chars: int,
    min_words_per_page: int,
    *,
    no_ocr: bool = False,
    keep_pagination: bool = False,
) -> ConvertResult:
    doc = pymupdf.open(pdf_path)
    page_count = doc.page_count
    use_ocr = needs_ocr(doc) and not no_ocr
    ocr_pdf_path: Optional[Path] = None

    if use_ocr:
        ocr_pdf_path = run_ocrmypdf(pdf_path)
        doc = pymupdf.open(ocr_pdf_path)
        pages = _raw_per_page(doc)
    else:
        if needs_ocr(doc) and no_ocr:
            print(
                "warning: --no-ocr set; scanned pages will read as column-jumbled "
                "via pymupdf4llm's built-in path",
                file=sys.stderr,
            )
        pages = _pymupdf4llm_per_page(doc)

    if not keep_pagination:
        pages = _strip_repeating_headers_footers(pages)

    md = "\n\n".join(p.strip() for p in pages if p.strip())

    non_ws_chars = sum(1 for c in md if not c.isspace())
    word_count = len(md.split())
    words_per_page = word_count / page_count if page_count else 0

    if non_ws_chars < min_chars or words_per_page < min_words_per_page:
        raise ScannedPDFError(
            f"extracted {non_ws_chars} non-whitespace chars, "
            f"{words_per_page:.1f} words/page across {page_count} pages"
        )

    return ConvertResult(
        markdown=md,
        used_ocr=use_ocr,
        ocr_pdf_path=ocr_pdf_path,
        page_count=page_count,
    )
