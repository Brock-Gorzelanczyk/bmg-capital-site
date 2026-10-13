"""Load a filing into pages with locations the report can cite.

A Document keeps:
  * pages: PDF page index (1-based), the printed page number when the
    running header or footer shows one, cleaned text
  * body: all page texts joined (table rows folded onto one line) and the
    character offset where each page starts, so any match in the body maps
    back to a page
  * sections: 10-K / 10-Q Item headings found after the table of contents
  * warnings: empty pages, likely scanned pages, instruction-like text
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional


class ReadError(Exception):
    """The document cannot be read; the message is shown to the user."""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


@dataclass
class Page:
    index: int                 # 1-based PDF page (or chunk number for other formats)
    printed: Optional[str]     # printed page number when the page shows one
    text: str                  # cleaned, rows folded
    chars: int = 0


@dataclass
class Section:
    item: str                  # "1", "1A", "7", "8"
    title: str
    offset: int
    page: int


@dataclass
class Document:
    sha256: str
    title: str
    kind: str
    pages: list[Page]
    body: str = ""
    page_starts: list[tuple[int, int]] = field(default_factory=list)   # (offset, page index)
    sections: list[Section] = field(default_factory=list)
    warnings: list[dict] = field(default_factory=list)
    removed_boilerplate: int = 0
    xbrl: list[dict] = field(default_factory=list)                     # inline XBRL facts (HTML filings)
    dei: dict = field(default_factory=dict)                            # cover facts tagged in iXBRL (dei:)

    def page_at(self, offset: int) -> Optional[int]:
        pg = None
        for ps, idx in self.page_starts:
            if ps <= offset:
                pg = idx
            else:
                break
        return pg

    def printed_page(self, index: Optional[int]) -> Optional[str]:
        if index is None:
            return None
        for p in self.pages:
            if p.index == index:
                return p.printed
        return None

    def section_at(self, offset: int) -> Optional[Section]:
        cur = None
        for s in self.sections:
            if s.offset <= offset:
                cur = s
            else:
                break
        return cur

    def location(self, offset: int) -> dict:
        """Where a body offset sits: PDF page, printed page, section."""
        pg = self.page_at(offset)
        sec = self.section_at(offset)
        return {"pdf_page": pg, "printed_page": self.printed_page(pg),
                "section": f"Item {sec.item}. {sec.title}" if sec else None}


# ----------------------------------------------------------------------
_INJECTION = re.compile(
    r"ignore (?:all |any )?(?:previous|prior|above) (?:instructions|prompts)|disregard (?:the|all|previous) (?:instructions|rules)"
    r"|you are (?:now )?(?:an? )?(?:ai|assistant|chatgpt|claude|language model)|system prompt|developer message"
    r"|\bact as\b.{0,40}\b(?:assistant|analyst bot)\b|(?:reveal|print|show) (?:your|the) (?:secrets?|api keys?|instructions)",
    re.I)


def _row_complete(row: list) -> bool:
    j = " ".join(row)
    last = row[-1].split()[-1] if row[-1].split() else ""
    return bool(re.search(r"[A-Za-z]", j)) and bool(re.fullmatch(r"\(?\$?\(?-?[\d,]*\d(?:\.\d+)?\)?%?|[\u2014\u2013-]", last)) \
        and not re.fullmatch(r"(?:19|20)\d\d", last)


def _fold(txt: str) -> str:
    """Tables extract one cell per line; fold runs of short fragments back
    into one row so "Revenue $ 576.5 $ 842.7" reads as one line."""
    folded, row = [], []
    for ln in txt.split("\n"):
        t = ln.strip()
        if t and len(t) < 28:
            # A row that already has a label and ends in a value is complete:
            # the next fragment that starts with a word begins a new row
            # (otherwise "Total assets 2,000 1,800" and "Term debt 450 500"
            # would merge). A trailing bare year is a column header, not a value.
            if row and re.match(r"[A-Za-z]", t) and _row_complete(row):
                folded.append("  ".join(row))
                row = []
            row.append(t)
            continue
        if row:
            folded.append("  ".join(row))
            row = []
        folded.append(t)
    if row:
        folded.append("  ".join(row))
    return re.sub(r"\n{3,}", "\n\n", "\n".join(folded)).strip()


def _norm(line: str) -> str:
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", line.strip().lower()))


def _strip_boilerplate(pages_raw: list[list[str]]) -> tuple[list[list[str]], list[Optional[str]], int]:
    """Drop running headers and footers (the same line, digits aside, on at
    least 40% of pages) and read printed page numbers from them."""
    n = len(pages_raw)
    printed: list[Optional[str]] = [None] * n
    if n < 4:
        return pages_raw, printed, 0
    edge = Counter()
    for lines in pages_raw:
        cand = {_norm(l) for l in lines[:2] + lines[-3:] if l.strip()}
        edge.update(cand)
    running = {k for k, c in edge.items() if c >= max(3, 0.4 * n) and len(k) < 120}
    removed = 0
    out = []
    for i, lines in enumerate(pages_raw):
        keep = []
        for j, l in enumerate(lines):
            at_edge = j < 2 or j >= len(lines) - 3
            s = l.strip()
            if at_edge and _norm(l) in running:
                m = re.search(r"(\d{1,3})\s*$", s)
                if m and printed[i] is None:
                    printed[i] = m.group(1)
                removed += 1
                continue
            # a bare page number at the very top or bottom
            if at_edge and re.fullmatch(r"\d{1,3}", s) and printed[i] is None and (j == 0 or j == len(lines) - 1):
                printed[i] = s
                removed += 1
                continue
            keep.append(l)
        out.append(keep)
    return out, printed, removed


_ITEM = re.compile(r"(?m)^[ \t ]*(?:PART[ \t ]+[IV]+[ \t ]+)?Item[ \t ]+(\d{1,2}[A-C]?)\.?[ \t ]+([A-Z\[][^\n]{2,160}?)(?=[ \t ]{2,}|$)")


def _sections(body: str, doc: "Document") -> list[Section]:
    hits = [(m.start(), m.group(1).upper(), re.sub(r"\s+", " ", m.group(2)).strip()) for m in _ITEM.finditer(body)]
    if not hits:
        return []
    per_page = Counter(doc.page_at(o) for o, _, _ in hits)
    out: dict[str, Section] = {}
    for off, item, title in hits:
        pg = doc.page_at(off)
        if per_page[pg] >= 5:          # the table of contents page
            continue
        title = re.sub(r"\s+\d{1,3}$", "", title)   # "Business 1" in a contents line
        if item not in out:
            out[item] = Section(item=item, title=title[:80], offset=off, page=pg or 0)
    return sorted(out.values(), key=lambda s: s.offset)


def _pages_from(data: bytes, kind: str) -> list[dict]:
    import fact_checker as fc
    if kind == "html":
        try:
            from . import htmltext
            pages = htmltext.to_pages(data)
            if sum(len(p["text"]) for p in pages) > 200:
                return pages
        except Exception:
            pass                       # fall back to the generic extractor
    try:
        return fc.extract(data, kind)
    except Exception as e:
        raise ReadError(400, f"Could not extract text from this {kind.upper()} file ({e.__class__.__name__}). "
                             "If it is a scanned document, it needs OCR, which this reader does not have.")


def load(data: bytes, kind: str, title: str, max_chars: int = 600_000) -> Document:
    sha = hashlib.sha256(data).hexdigest()
    raw = _pages_from(data, kind)
    pages_raw = [[l for l in (p.get("text") or "").split("\n")] for p in raw]
    cleaned, printed, removed = _strip_boilerplate(pages_raw) if (kind == "pdf" or len(pages_raw) >= 5) else (pages_raw, [None] * len(pages_raw), 0)
    doc = Document(sha256=sha, title=title, kind=kind, pages=[], removed_boilerplate=removed)
    parts, pos = [], 0
    empty = []
    for i, lines in enumerate(cleaned):
        t = _fold("\n".join(lines).strip()) if kind != "html" else "\n".join(lines).strip()
        idx = i + 1
        doc.pages.append(Page(index=idx, printed=printed[i], text=t, chars=len(t)))
        if len(re.sub(r"\s+", "", t)) < 20:
            empty.append(idx)
            continue
        doc.page_starts.append((pos, idx))
        parts.append(t)
        pos += len(t) + 2
    doc.body = "\n\n".join(parts)
    if len(doc.body) > max_chars:
        doc.warnings.append({"code": "truncated", "message": f"Only the first {max_chars:,} characters of extracted text were analysed."})
        doc.body = doc.body[:max_chars]
    total = len(doc.pages)
    if len(re.sub(r"\s+", "", doc.body)) < 40:
        raise ReadError(422, "No readable text was found. This looks like a scanned or image-only document; "
                             "it needs OCR, which this reader does not have, so no report was produced.")
    if empty:
        share = len(empty) / max(1, total)
        doc.warnings.append({"code": "empty_pages", "pages": empty,
                             "message": f"{len(empty)} of {total} pages had no extractable text"
                                        + (" (likely scanned images; not analysed)" if share > 0.2 else " (cover art, charts or blank pages)") + "."})
    for m in _INJECTION.finditer(doc.body):
        doc.warnings.append({"code": "instruction_like_text", "pdf_page": doc.page_at(m.start()),
                             "message": "The document contains instruction-like text (\"" + m.group(0)[:60] + "\"). "
                                        "It was treated as ordinary document content and did not change how the report was built."})
        break
    doc.sections = _sections(doc.body, doc)
    if kind == "pdf":
        # The contents page lists printed page numbers; if it points well past
        # the last page in the file, the upload is an excerpt.
        toc = [int(m.group(1)) for m in re.finditer(r"(?m)^[ \t]*(?:PART[ \t]+[IV]+\.?[ \t]+)?Item[ \t]+\d{1,2}[A-C]?\.[^\n]{3,160}?[ \t]+(\d{1,3})[ \t]*(?:Signatures?[ \t]+\d{1,3})?$", doc.body[:20000])]
        last = max([int(p.printed) for p in doc.pages if p.printed and p.printed.isdigit()] + [len(doc.pages)])
        if len(toc) >= 4 and max(toc) > last + 3:
            doc.warnings.append({"code": "incomplete", "message": f"The contents page refers to page {max(toc)}, but the file ends at page {last}. "
                                                                  "This looks like an excerpt; sections after that point were not available to read."})
    if kind == "html":
        try:
            from . import xbrl
            doc.xbrl = xbrl.parse(data)
            doc.dei = xbrl.dei(data)
        except Exception as e:
            doc.warnings.append({"code": "xbrl_failed", "message": f"Inline XBRL tags could not be read ({e.__class__.__name__}); tables were used instead."})
    return doc
