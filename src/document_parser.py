"""Text extraction from PDF (PyMuPDF) and Word (python-docx) term sheets.

Each page is kept in two views:

* ``layout_text`` – PyMuPDF's layout-sorted text, which keeps table labels and
  values on the same line ("16. Coupon Rate 10.75% p.a."). This is what the
  LLM reads.
* ``reading_text`` – plain reading-order text. Evidence quotes are verified
  against both views, so a quote copied from either is accepted, but a quote
  that appears in neither is rejected.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".pdf", ".docx"}


class DocumentParseError(Exception):
    """The document could not be read or contains no extractable text."""


class UnsupportedDocumentError(DocumentParseError):
    """The file type is not supported."""


@dataclass(frozen=True)
class PageText:
    number: int
    layout_text: str
    reading_text: str


@dataclass(frozen=True)
class ParsedDocument:
    name: str
    path: Path
    file_type: str
    sha256: str
    pages: tuple[PageText, ...]
    page_numbering: str  # "physical" (PDF) or "page_breaks" (DOCX explicit breaks)

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def total_chars(self) -> int:
        return sum(len(p.layout_text) for p in self.pages)


def compact_layout(text: str) -> str:
    """Trim layout padding: strip lines, shrink long space runs, drop repeated blank lines."""
    lines = [re.sub(r"[ \t]{3,}", "  ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    return "\n".join(out).strip()


def parse_document(path: str | Path) -> ParsedDocument:
    path = Path(path)
    if not path.exists():
        raise DocumentParseError(f"Term sheet not found: {path}")
    suffix = path.suffix.lower()
    if suffix == ".doc":
        raise UnsupportedDocumentError(f"{path.name}: legacy .doc files are not supported; save as .docx or PDF")
    if suffix not in SUPPORTED_SUFFIXES:
        raise UnsupportedDocumentError(f"{path.name}: unsupported file type {suffix!r} (supported: PDF, DOCX)")

    data = path.read_bytes()
    if not data:
        raise DocumentParseError(f"{path.name}: file is empty")
    sha256 = hashlib.sha256(data).hexdigest()

    if suffix == ".pdf":
        pages, numbering = _parse_pdf(path, data), "physical"
    else:
        pages, numbering = _parse_docx(path, data), "page_breaks"

    if not any(p.reading_text.strip() for p in pages):
        raise DocumentParseError(
            f"{path.name}: no extractable text found (scanned/image-only document?). OCR is not supported."
        )
    document = ParsedDocument(path.name, path, suffix.lstrip("."), sha256, tuple(pages), numbering)
    logger.info(
        "Parsed %s: %d page(s), %d characters",
        path.name, document.page_count, document.total_chars,
        extra={"event": "document_parsed", "document": path.name, "pages": document.page_count},
    )
    return document


def _parse_pdf(path: Path, data: bytes) -> list[PageText]:
    import pymupdf

    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:  # PyMuPDF raises several exception types for corrupt files
        raise DocumentParseError(f"{path.name}: not a readable PDF ({exc})") from exc
    with doc:
        if doc.needs_pass:
            raise DocumentParseError(f"{path.name}: PDF is password protected")
        pages = []
        for index, page in enumerate(doc, start=1):
            pages.append(
                PageText(
                    number=index,
                    layout_text=compact_layout(page.get_text("text", sort=True)),
                    reading_text=page.get_text("text").strip(),
                )
            )
    return pages


def _parse_docx(path: Path, data: bytes) -> list[PageText]:
    """Paragraphs and tables in body order; explicit page breaks start a new page."""
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        document = Document(io.BytesIO(data))
    except Exception as exc:  # python-docx raises PackageNotFoundError, KeyError, ...
        raise DocumentParseError(f"{path.name}: not a readable Word document ({exc})") from exc

    pages: list[list[str]] = [[]]
    for element in document.element.body.iterchildren():
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            if element.xpath("./w:pPr/w:pageBreakBefore") and pages[-1]:
                pages.append([])
            pages[-1].append(paragraph.text)
            if element.xpath(".//w:br[@w:type='page']"):
                pages.append([])
        elif element.tag == qn("w:tbl"):
            table = Table(element, document)
            for row in table.rows:
                cells: list[str] = []
                for cell in row.cells:
                    text = " ".join(cell.text.split())
                    if text and (not cells or cells[-1] != text):  # merged cells repeat their text
                        cells.append(text)
                if cells:
                    pages[-1].append(" | ".join(cells))
            pages[-1].append("")

    result = []
    for number, lines in enumerate((p for p in pages if any(line.strip() for line in p)), start=1):
        text = "\n".join(lines).strip()
        result.append(PageText(number=number, layout_text=compact_layout(text), reading_text=text))
    return result
