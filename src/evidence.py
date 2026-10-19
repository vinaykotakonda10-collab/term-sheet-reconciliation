"""Verification that LLM evidence quotes really exist in the source document.

A quote is accepted when it appears in the document after a well-defined
normalisation, tried in this order:

1. ``normalized`` – Unicode NFKC, typographic quotes/dashes folded to ASCII,
   whitespace runs collapsed to one space.
2. ``whitespace_insensitive`` – as above but ignoring whitespace entirely, which
   covers words hyphenated across PDF line breaks ("write-\\ndown").

No other edits (case changes, paraphrase, ellipses) are tolerated. The page
reported is the page where the quote was found, not the page the LLM claimed.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass

from .document_parser import ParsedDocument
from .normalizer import normalize_for_match, strip_all_whitespace


@dataclass(frozen=True)
class EvidenceMatch:
    found: bool
    page: int | None = None
    match_type: str | None = None
    text_view: str | None = None


class _IndexedText:
    def __init__(self, pages: list[tuple[int, str]], separator: str) -> None:
        self.page_numbers = [number for number, _ in pages]
        self.starts: list[int] = []
        parts: list[str] = []
        offset = 0
        for _, text in pages:
            self.starts.append(offset)
            parts.append(text)
            offset += len(text) + len(separator)
        self.text = separator.join(parts)

    def pages_containing(self, needle: str) -> list[int]:
        found: list[int] = []
        start = self.text.find(needle)
        while start != -1 and len(found) < 50:
            page = self.page_numbers[bisect_right(self.starts, start) - 1]
            if page not in found:
                found.append(page)
            start = self.text.find(needle, start + 1)
        return found


class EvidenceLocator:
    def __init__(self, document: ParsedDocument) -> None:
        self._indexes: list[tuple[str, str, _IndexedText]] = []
        for view in ("layout", "reading_order"):
            texts = [(p.number, p.layout_text if view == "layout" else p.reading_text) for p in document.pages]
            self._indexes.append(
                ("normalized", view, _IndexedText([(n, normalize_for_match(t)) for n, t in texts], " "))
            )
            self._indexes.append(
                ("whitespace_insensitive", view, _IndexedText([(n, strip_all_whitespace(t)) for n, t in texts], ""))
            )

    def locate(self, quote: str, claimed_page: int | None = None) -> EvidenceMatch:
        needles = {
            "normalized": normalize_for_match(quote),
            "whitespace_insensitive": strip_all_whitespace(quote),
        }
        if not needles["whitespace_insensitive"]:
            return EvidenceMatch(found=False)
        for match_type in ("normalized", "whitespace_insensitive"):
            for index_type, view, index in self._indexes:
                if index_type != match_type:
                    continue
                pages = index.pages_containing(needles[match_type])
                if pages:
                    page = claimed_page if claimed_page in pages else pages[0]
                    return EvidenceMatch(found=True, page=page, match_type=match_type, text_view=view)
        return EvidenceMatch(found=False)
