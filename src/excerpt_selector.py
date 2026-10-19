"""Selects the parts of a term sheet that are sent to the LLM.

Free-tier LLM APIs limit tokens per minute (Groq: 8K/min for gpt-oss), while a
term sheet is often 30-40K characters of mostly covenant boilerplate. When a
document exceeds the character budget, it is split into blank-line separated
blocks; blocks are scored against field-specific patterns and the best ones
are packed into the budget in document order.

The selection is fully traceable: the pages sent, the characters sent and a
per-field "candidate passage found" flag are recorded with every extraction.
Failure is conservative – a field whose passage is not sent comes back null and
is reported as *Unverifiable*, never as a fabricated value.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .document_parser import ParsedDocument
from .field_catalog import FIELD_NAMES

MAX_BLOCK_CHARS = 900
HEADER_BLOCKS = 3  # opening blocks of page 1 (title, ISIN, issuer) are always sent

# First pattern of each tuple is the field's label (weight 3); the rest are supporting cues (weight 1).
FIELD_PATTERNS: dict[str, tuple[str, ...]] = {
    "ISIN": (r"\bISIN\b",),
    "Issuer": (r"(?m)^\W*(?:\d+\.\s*)?issuer\b\s*:?", r"\bissuer\s*:"),
    "Parent": (r"(?m)^\W*(?:\d+\.\s*)?parent\b\s*:?", r"\bholding company\b"),
    "Maturity": (r"\bmaturity date\b|\btenor\b", r"\bperpetual\b", r"\bmaturity\b", r"\bredemption date\b"),
    "Notional": (r"\bnotional\b", r"\btrade (?:amount|size)\b"),
    "Coupon": (r"\bcoupon rate\b|\binterest rate\b", r"\d\s*%\s*p\.?\s*a\b", r"\bcoupon type\b"),
    "Currency": (r"\bcurrency\b", r"\b(?:USD|EUR|GBP|INR|NOK|SEK|JPY|CHF)\b", r"\bRs\.?\s?\d", r"\bRupees\b"),
    "SettlementDate": (r"\bsettlement date\b|\bpay[\s-]*in[\s-]*date\b", r"\bsettlement\b", r"\bvalue date\b"),
    "IssueDate": (r"\bissue date\b|\bdate\s+of\s+allotment\b", r"\ballotment\b", r"\bissue schedule\b",
                  r"\bissue (?:opening|closing) date\b"),
    "IssueAmount": (r"\bissue (?:size|amount)\b", r"\baggregate (?:nominal|principal)\b", r"\bcrores?\b",
                    r"\bover[\s-]*subscription\b"),
    "IssuePrice": (r"\bissue price\b", r"\bat par\b", r"% of the nominal"),
    "NominalAmountPerBond": (r"\bface value\b|\bnominal amount\b|\bdenomination\b", r"\bper bond\b"),
    "InterestPaymentDate": (r"\binterest payment date\b|\bcoupon payment dates?\b",
                            r"\bfirst (?:interest|coupon)\b", r"\banniversary\b"),
    "InterestPaymentFrequency": (r"\bpayment frequency\b|\binterest payments\b", r"\bsemi[\s-]*annual",
                                 r"\bannually\b", r"\bquarterly\b", r"\bin arrears?\b"),
    "DayCountFraction": (r"\bday[\s-]*count\b|\bcomputation of interest\b",
                         r"\bactual\s*/\s*(?:actual|360|365)\b", r"\b30/360\b"),
    "BusinessDayConvention": (r"\bbusiness day convention\b", r"\bunadjusted\b",
                              r"\b(?:next|preceding|following) business day\b"),
    "BusinessDayLocation": (r"\bbusiness day is\b|\bfinancial cent(?:re|er)s?\b", r"\bbusiness days?\b"),
    "AmortizationType": (r"\bamorti[sz]ation\b|\brepaid in full\b", r"\bbullet\b", r"\bperpetual\b",
                         r"\bredemption\b"),
    "MinimumSubscription": (r"\bminimum (?:subscription|application|investment)\b", r"\bin multiples of\b"),
}
_COMPILED = {
    name: [re.compile(p, re.IGNORECASE) for p in patterns] for name, patterns in FIELD_PATTERNS.items()
}


@dataclass(frozen=True)
class Block:
    page: int
    index: int
    text: str


@dataclass
class ExcerptSelection:
    strategy: str  # "full_document" | "keyword_excerpts"
    text: str
    pages: list[int]
    chars: int
    field_coverage: dict[str, bool]
    blocks_selected: int
    blocks_total: int
    blocks: list[Block] = field(default_factory=list, repr=False)


def split_blocks(document: ParsedDocument) -> list[Block]:
    blocks: list[Block] = []
    for page in document.pages:
        for raw in re.split(r"\n\s*\n", page.layout_text):
            raw = raw.strip()
            if not raw:
                continue
            for chunk in _split_long(raw):
                blocks.append(Block(page=page.number, index=len(blocks), text=chunk))
    return blocks


def _split_long(text: str) -> list[str]:
    if len(text) <= MAX_BLOCK_CHARS:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines():
        if current and len(current) + len(line) + 1 > MAX_BLOCK_CHARS:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def block_field_scores(text: str) -> dict[str, int]:
    scores: dict[str, int] = {}
    for name, patterns in _COMPILED.items():
        for position, pattern in enumerate(patterns):
            if pattern.search(text):
                scores[name] = max(scores.get(name, 0), 3 if position == 0 else 1)
    return scores


def select_excerpts(document: ParsedDocument, max_chars: int) -> ExcerptSelection:
    blocks = split_blocks(document)
    field_scores = [block_field_scores(b.text) for b in blocks]

    if sum(len(b.text) + 2 for b in blocks) <= max_chars:
        chosen = list(range(len(blocks)))
        strategy = "full_document"
    else:
        strategy = "keyword_excerpts"
        priority: dict[int, float] = {}
        for i, scores in enumerate(field_scores):
            if scores:
                priority[i] = float(sum(scores.values()))
        for i, scores in enumerate(field_scores):  # context neighbours of labelled blocks
            if any(v >= 3 for v in scores.values()):
                for j in (i - 1, i + 1):
                    if 0 <= j < len(blocks) and blocks[j].page == blocks[i].page:
                        priority.setdefault(j, 0.5)
        for i in range(min(HEADER_BLOCKS, len(blocks))):
            if blocks[i].page == document.pages[0].number:
                priority[i] = 1000.0

        chosen, used = [], 0
        for i in sorted(priority, key=lambda k: (-priority[k], k)):
            cost = len(blocks[i].text) + 2
            if used + cost <= max_chars:
                chosen.append(i)
                used += cost
        chosen.sort()

    text = _render(blocks, chosen)
    coverage = {
        name: any(name in field_scores[i] for i in chosen) for name in FIELD_NAMES
    }
    return ExcerptSelection(
        strategy=strategy,
        text=text,
        pages=sorted({blocks[i].page for i in chosen}),
        chars=len(text),
        field_coverage=coverage,
        blocks_selected=len(chosen),
        blocks_total=len(blocks),
        blocks=[blocks[i] for i in chosen],
    )


def _render(blocks: list[Block], chosen: list[int]) -> str:
    parts: list[str] = []
    previous: Block | None = None
    for i in chosen:
        block = blocks[i]
        if previous is None or block.page != previous.page:
            parts.append(f"[Page {block.page}]")
        elif block.index != previous.index + 1:
            parts.append("[...]")
        parts.append(block.text)
        previous = block
    return "\n\n".join(parts)
