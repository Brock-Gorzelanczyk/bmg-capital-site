"""SEC HTML (including inline XBRL) to page texts the table parser can read.

* One line per table row, cells separated by two spaces; "$", "(" and ")"
  or "%" cells are joined to the number they belong to, so a row reads
  "Total revenues  $ 2,215.4  $ 2,983.1" as it does in a PDF.
* Block elements end a line. Hidden content (the iXBRL header, display:none)
  is dropped, as are scripts and styles.
* Page breaks written into the filing (page-break-before/after, the way
  EDGAR documents mark printed pages) split the text into pages, so
  running footers and printed page numbers work as they do for PDFs.
"""
from __future__ import annotations

import re

_BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol", "table", "tr", "section", "article",
          "header", "footer", "blockquote", "hr", "center", "dl", "dt", "dd", "pre"}
_SKIP = {"script", "style", "head", "title", "noscript", "ix:header", "xml"}
_BREAK = re.compile(r"page-break-(before|after)\s*:\s*always|break-(before|after)\s*:\s*page", re.I)
_HIDDEN = re.compile(r"display\s*:\s*none", re.I)
PAGE = "\f"


def _tag(el) -> str:
    t = el.tag if isinstance(el.tag, str) else ""
    return t.lower().split("}")[-1] if "}" in t else t.lower()


def _clean(s: str) -> str:
    return re.sub(r"[ \t    ​]+", " ", s or "")


def _cell_text(td) -> str:
    t = _clean("".join(td.itertext())).strip()        # tags can split one number ("202" + "5")
    return re.sub(r"\s+\)", ")", re.sub(r"\(\s+", "(", t))


def _row(tr) -> str:
    cells = [c for c in (_cell_text(td) for td in tr if _tag(td) in ("td", "th")) if c]
    out: list[str] = []
    for c in cells:
        if out and (c in (")", "%", ")%", "%)") or re.fullmatch(r"\)?%?\)?", c)):
            out[-1] += c
        elif out and out[-1] in ("$", "(", "($", "$(", "€", "£"):
            out[-1] = out[-1] + " " + c if out[-1] == "$" else out[-1] + c
        else:
            out.append(c)
    return "  ".join(out)


def to_pages(data: bytes) -> list[dict]:
    from lxml import html as lh
    root = lh.fromstring(data)
    buf: list[str] = []

    def emit(s: str):
        if s:
            buf.append(_clean(s))

    def walk(el):
        tag = _tag(el)
        style = el.get("style") or "" if hasattr(el, "get") else ""
        if tag in _SKIP or _HIDDEN.search(style):
            return
        brk = _BREAK.search(style)
        if brk and (brk.group(1) or brk.group(2) or "").lower() == "before":
            buf.append(PAGE)
        if tag == "table":
            buf.append("\n")
            for tr in el.iter():
                if _tag(tr) == "tr":
                    r = _row(tr)
                    if r:
                        buf.append(r + "\n")
            buf.append("\n")
        else:
            block = tag in _BLOCK
            if block:
                buf.append("\n")
            emit(el.text)
            for ch in el:
                if isinstance(ch.tag, str):
                    walk(ch)
                emit(ch.tail)
            if block:
                buf.append("\n")
        if brk and (brk.group(1) or brk.group(2) or "").lower() == "after":
            buf.append(PAGE)

    body = root.find(".//body")
    walk(body if body is not None else root)
    text = "".join(buf)
    pages = []
    for chunk in text.split(PAGE):
        lines = [l.strip() for l in chunk.split("\n")]
        t = "\n".join(l for l in lines if l)
        if t.strip():
            pages.append({"text": t})
    return pages or [{"text": ""}]
