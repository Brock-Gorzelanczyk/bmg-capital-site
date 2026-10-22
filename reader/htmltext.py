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

# An exhibit made of slide images (Tesla's quarterly update, UnitedHealth's release) carries each slide's text as white
# 1pt type under its image: one run-on line per slide, the statements included ("Total revenues 28,095 24,901 22,387
# 28,236 28,046 Total gross profit ..."). When most of the document's text is that type, each slide's text is set out
# in lines here: the slide title first (the image prints it at the top; the text layer can list it last), then the
# caption, the column header and one line per table row, as a printed table reads. No word or figure is added or dropped.
_TINY = re.compile(r"font-size\s*:\s*[0-2](?:\.\d+)?pt", re.I)
_SPACED = re.compile(r"(?:(?<=\s)|^)(?:[A-Z&\u2013\-](?=\s)\s+){3,}[A-Z&\u2013\-](?=\s|$|\()")
_PERIOD_TOK = re.compile(r"(?:Q[1-4]|[1-4]Q)[\-\u2013']?(?:19|20)?\d\d|\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)-(?:19|20)?\d\d", re.I)
_NUM_TOK = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?|\$?[\u2014\u2013\-]")
_MARK_TOK = re.compile(r"\(\d{1,2}\)")
_CHANGE_TOK = re.compile(r"[\u2014\u2013\-]|[-+]?\d+(?:\.\d+)?%|n/?m", re.I)
_UNIT_TOK = re.compile(r"bps?|pp|x|%|GWh|MW|mil\)?")
_CAPS_HEAD = {"ASSETS", "REVENUES", "REVENUE", "LIABILITIES", "EQUITY", "EXPENSES"}
_SUBHEAD = re.compile(r"(Current assets|Current liabilities|Non-?current assets|Non-?current liabilities|Changes in operating assets and liabilities)\s+(?=\S)")
_TITLE_WORDS = {"STATEMENT", "STATEMENTS", "OF", "CASH", "FLOWS", "FLOW", "OPERATIONS", "BALANCE", "SHEET", "SHEETS", "INCOME", "COMPREHENSIVE",
                "FINANCIAL", "POSITION", "CONDITION", "EQUITY", "STOCKHOLDERS", "SHAREHOLDERS", "CHANGES", "IN", "RECONCILIATION", "GAAP", "TO",
                "NON", "INFORMATION", "SUMMARY", "OPERATIONAL", "HIGHLIGHTS", "OUTLOOK", "ADDITIONAL", "KEY", "METRICS", "CONSOLIDATED",
                "CONDENSED", "RESULTS", "AND", "THE", "FOR", "SUPPORTING", "INFRASTRUCTURE", "MANUFACTURING", "HARDWARE", "SERVICES", "OTHER",
                "PHOTOS", "CHARTS", "SEGMENT", "SEGMENTS", "REVENUE", "REVENUES", "EARNINGS", "OPERATING", "EXPENSES", "DATA", "SELECTED",
                "QUARTERLY", "ANNUAL", "GUIDANCE", "\u2013", "-", "&"}


def _split_title(s: str) -> str:
    """'STATEMENTOFCASHFLOWS' (a spaced title printed without word gaps) back into words, when every part is a title word."""
    best = {0: []}
    for i in range(len(s)):
        if i not in best:
            continue
        for w in _TITLE_WORDS:
            if s.startswith(w, i) and (i + len(w) not in best or len(best[i]) + 1 < len(best[i + len(w)])):
                best[i + len(w)] = best[i] + [w]
    return " ".join(best[len(s)]).replace(" \u2013 ", "\u2013").replace(" - ", "-") if len(s) in best else s


def _unspace(run: str) -> str:
    """'S T A T E M E N T   O F   O P E R A T I O N S' -> 'STATEMENT OF OPERATIONS': letters one space apart, words wider."""
    return " ".join(_split_title(w.replace(" ", "")) for w in re.split(r"\s{2,}", run.strip()))


def _slide_row(out: list, label: list, cells: list) -> None:
    lab = list(label)
    k = 0
    while k < len(lab) and re.fullmatch(r"[A-Z][A-Z&,'\u2019\-]*", lab[k]):
        k += 1
    # a section heading in capitals run into the first row: "REVENUES Automotive sales", "CASH FLOWS FROM OPERATING ACTIVITIES Net income"
    if 0 < k < len(lab) and re.search(r"[a-z]", lab[k]) and (k >= 2 or lab[0] in _CAPS_HEAD):
        out.append(" ".join(lab[:k]))
        lab = lab[k:]
    m = _SUBHEAD.match(" ".join(lab))
    if m:
        out.append(m.group(1))
        lab = lab[len(m.group(1).split()):]
    # the per-share heading run into its Basic row: "Net income per share of common stock attributable to common stockholders Basic"
    if len(lab) >= 4 and lab[-1] in ("Basic", "Diluted"):
        out.append(" ".join(lab[:-1]))
        lab = lab[-1:]
    out.append(" ".join(lab) + "  " + "  ".join(cells))


def _slide_cells(toks: list, k: int) -> tuple[list, int]:
    """The run of figures starting at toks[k], "$" joined to its figure ("$ 0.43")."""
    cells = []
    while k < len(toks) and (toks[k] == "$" or _NUM_TOK.fullmatch(toks[k])):
        if toks[k] == "$" and k + 1 < len(toks) and _NUM_TOK.fullmatch(toks[k + 1]):
            cells.append("$ " + toks[k + 1])
            k += 2
            continue
        if toks[k] != "$":
            cells.append(toks[k])
        k += 1
    return cells, k


def _slide_table(toks: list, i: int, j: int):
    """The lines of one table whose column header is toks[i:j], and where it ends; None when a row has more figures than
    the header has columns (Bank of America: "3Q26 3Q25" over Reported, Charge, Adjusted and Reported), so no row is
    cut to fit a header that does not describe it."""
    head = toks[i:j]
    yoy = j < len(toks) and toks[j].lower() in ("yoy", "qoq")
    if yoy:
        head.append(toks[j])
        j += 1
    ncols = len(head) - yoy
    lines, label, k = ["  ".join(head)], [], j
    while k < len(toks):
        t = toks[k]
        if _PERIOD_TOK.fullmatch(t) and k + 1 < len(toks) and _PERIOD_TOK.fullmatch(toks[k + 1]):
            break                                        # the next table's header
        if not label and _MARK_TOK.fullmatch(t) and k + 1 < len(toks) and re.match(r"[A-Z][a-z]", toks[k + 1]):
            break                                        # "(1) Breakdown of our debt ...": the footnotes
        if not (t == "$" or _NUM_TOK.fullmatch(t)):
            label.append(t)
            k += 1
            if len(label) > 30:                          # running text: no row label is this long
                k -= len(label)
                label = []
                break
            continue
        cells, m = _slide_cells(toks, k)
        marks = []
        while label and len(cells) > ncols and _MARK_TOK.fullmatch(cells[0]):
            marks.append(cells.pop(0))                   # footnote marks after the label: "Adjusted EBITDA ... (non-GAAP) (2) (3)"
        if label and len(cells) >= ncols:
            take = ncols + (1 if yoy and len(cells) > ncols and _CHANGE_TOK.fullmatch(cells[ncols]) else 0)
            rest = cells[take:]
            page = rest[:1] if rest and re.fullmatch(r"\d{1,3}", rest[0]) else []
            if rest and not (all(_MARK_TOK.fullmatch(x) for x in rest[len(page):]) and (m >= len(toks) or re.match(r"[A-Z]", toks[m]))):
                return None                              # more figures than columns, and not the slide number
            _slide_row(lines, label + marks, cells[:take])
            label, k = [], m
            if rest:
                lines += page                            # the slide number, alone, as a printed page number
                k = m - (len(rest) - len(page))          # a footnote mark after the last row starts the footnotes
                break
            continue
        if m < len(toks) and all(c in ("\u2014", "\u2013", "-") for c in cells):
            label += toks[k:m]                           # a dash inside a label: "operating activities - TTM"
            k = m
            continue
        label += marks + toks[k:m]                       # fewer figures than columns ("Total GAAP gross margin 160 bp"): a line of text
        if m < len(toks) and _UNIT_TOK.fullmatch(toks[m]):
            label.append(toks[m])
            m += 1
        lines.append(" ".join(label))
        label, k = [], m
    if label:
        lines.append(" ".join(label))
    return lines, k


def _slide_lines(raw: str) -> str:
    text, title = raw, ""
    runs = list(_SPACED.finditer(text))
    if runs:
        m = runs[0]
        after = text[m.end():]
        un = re.match(r"\s*(\((?:unaudited|[^)]{0,80}millions[^)]{0,80})\))", after, re.I)
        title = _unspace(m.group(0)) + (" " + un.group(1) if un else "")
        text = text[:m.start()] + " " + (after[un.end():] if un else after)
        text = _SPACED.sub(lambda r: _unspace(r.group(0)), text)
    toks = text.split()
    out = [title] if title else []
    words: list = []
    i = 0
    while i < len(toks):
        j = i
        while j < len(toks) and _PERIOD_TOK.fullmatch(toks[j]):
            j += 1
        table = _slide_table(toks, i, j) if j - i >= 2 else None
        if table is None:
            words += toks[i:max(j, i + 1)]               # text, or a header its rows do not fit: left as printed
            i = max(j, i + 1)
            continue
        if words:
            out.append(" ".join(words))                  # the caption, or the text before the table
            words = []
        out += table[0]
        i = table[1]
    if words:
        out.append(" ".join(words))
    return "\n".join(x for x in out if x.strip())


def _tag(el) -> str:
    t = el.tag if isinstance(el.tag, str) else ""
    return t.lower().split("}")[-1] if "}" in t else t.lower()


def _clean(s: str) -> str:
    return re.sub(r"[ \t    ​]+", " ", s or "")


def _cell_join(el) -> str:
    """The cell's text. A line break or block inside the cell separates words ("Three Months Ended<br/>June 30,");
    inline tags do not, since they can split one number ("202" + "5")."""
    out = [el.text or ""]
    for ch in el:
        if isinstance(ch.tag, str):
            block = _tag(ch) in _BLOCK
            out.append(" " if block else "")
            out.append(_cell_join(ch))
            out.append(" " if block else "")
        out.append(ch.tail or "")
    return "".join(out)


def _cell_text(td) -> str:
    t = _clean(_cell_join(td)).strip()
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
    body = root.find(".//body")
    top = body if body is not None else root
    tiny = [e for e in top.iter() if isinstance(e.tag, str) and _TINY.search(e.get("style") or "")]
    outer = [e for e in tiny if not any(_TINY.search(a.get("style") or "") for a in e.iterancestors() if isinstance(a.tag, str))]
    chars = lambda e: len(re.sub(r"\s+", "", "".join(e.itertext())))
    slides = set(outer) if outer and sum(chars(e) for e in outer) > 0.5 * max(1, chars(top)) else set()

    def emit(s: str):
        if s:
            buf.append(_clean(s))

    def walk(el):
        tag = _tag(el)
        style = el.get("style") or "" if hasattr(el, "get") else ""
        if tag in _SKIP or _HIDDEN.search(style):
            return
        if el in slides:
            buf.append("\n" + _slide_lines("".join(el.itertext())) + "\n")
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

    walk(top)
    text = "".join(buf)
    pages = []
    for chunk in text.split(PAGE):
        lines = [l.strip() for l in chunk.split("\n")]
        t = "\n".join(l for l in lines if l)
        if t.strip():
            pages.append({"text": t})
    return pages or [{"text": ""}]
