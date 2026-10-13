"""Financial tables out of filing text, one row at a time, with context.

The text comes from document.load(): each table row is one line such as
    Total net sales 416,161 391,035 383,285
with a period header above it such as
    Years ended  September 27,  2025  September 28,  2024  September 30,  2023
and a caption such as "(In millions, except ... per-share amounts)".

Each parsed row keeps its label, the section heading it sits under, every
column value with its period, the unit (USD, USD per share, shares,
percent), the scale that applies to it, the statement or table it belongs
to, the header line, and the source location.
"""
from __future__ import annotations

import re
from typing import Optional

_YEAR = re.compile(r"\b(19|20)\d\d\b")
_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")
_CELL = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?|\$|%|\u2014|\u2013|-")
_PHRASE = re.compile(r"\b(three|six|nine|twelve|3|6|9|12)[ -]months? ended\s+([A-Z][a-z]+\.? \d{1,2})|\b(years?|fiscal years?) ended\s+([A-Z][a-z]+\.? \d{1,2})", re.I)
_KIND = {"three": "Q", "3": "Q", "six": "6M", "6": "6M", "nine": "9M", "9": "9M", "twelve": "FY", "12": "FY"}
_CAPTION = re.compile(r"\(\s*(?:dollars |amounts |\$ )?in (millions|billions|thousands)([^)]{0,200})\)", re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

STATEMENTS = [
    ("comprehensive", re.compile(r"statements? of comprehensive (income|loss)", re.I)),
    ("income", re.compile(r"statements? of (operations|income|earnings)|income statements?", re.I)),
    ("balance", re.compile(r"balance sheets\b|consolidated balance sheet|statements? of financial (position|condition)", re.I)),
    ("cashflow", re.compile(r"statements? of cash flows?", re.I)),
    ("equity", re.compile(r"statements? of (changes in )?(shareholders|stockholders)['’]? equity|statements? of changes in equity", re.I)),
]


def statement_of(title: str) -> str:
    for name, rx in STATEMENTS:
        if rx.search(title or ""):
            return name
    return "other"


def parse_number(tok: str) -> Optional[float]:
    """'(1,234.5)' -> -1234.5; '-' or em dash -> 0.0 (reported as nil); '12%' -> 12.0."""
    t = tok.strip()
    if t in ("\u2014", "-", "\u2013"):
        return 0.0
    neg = (t.startswith("(") and t.rstrip("%").endswith(")")) or t.startswith("-")
    d = re.sub(r"[^\d.]", "", t)
    if not d or d == "." or d.count(".") > 1:
        return None
    try:
        v = float(d)
    except ValueError:
        return None
    return -v if neg else v


def _iso(month: str, day: str, year: str) -> Optional[str]:
    m = _MONTHS.get(month[:3].lower())
    if not m:
        return None
    return f"{year}-{m:02d}-{int(day):02d}"


def _kinds(text: str) -> list:
    out = []
    for m in re.finditer(r"\b(three|six|nine|twelve) months\b|\b(?:fiscal )?years?\b(?= ended| end)|\bfiscal year\b", text, re.I):
        w = (m.group(1) or "").lower()
        out.append({"three": "Q", "six": "6M", "nine": "9M", "twelve": "FY"}.get(w, "FY"))
    return out


def header_periods(line: str, context: str) -> Optional[dict]:
    """A column header: two or more period columns. `context` is the line(s)
    just above, which may carry "Three months ended May 31"."""
    years = [m.group(0) for m in _YEAR.finditer(line)]
    if len(years) < 2:
        return None
    nums = [t for t in re.findall(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?", line)]
    other = [t for t in nums if not _YEAR.fullmatch(re.sub(r"[^\d]", "", t)) and not re.fullmatch(r"\d{1,2},?", t.strip())]
    if len(other) > 1:
        return None
    words = re.findall(r"[A-Za-z]+", re.sub(r"\([^)]*\)", " ", line))      # a scale caption may share the line
    allowed = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
               "change", "years", "year", "ended", "months", "three", "six", "nine", "twelve", "fiscal", "as", "of", "and", "at", "net", "sales",
               "total", "unaudited", "restated", "q", "fy"}
    if len([w for w in words if w.lower() not in allowed]) > 4:
        return None
    low = line.lower()
    change_cols = "change" in low
    dates = list(_DATE.finditer(line))
    if len(dates) >= 2:
        # Period kinds in order, from this line or else the header line(s)
        # above ("Three Months Ended  Nine Months Ended" over the dates):
        # each kind takes an equal run of the date columns.
        kinds = _kinds(line) or _kinds(context[-300:])
        if len(kinds) > 1 and len(dates) % len(kinds) == 0:
            per = len(dates) // len(kinds)
            ks = [kinds[i // per] for i in range(len(dates))]
        elif len(set(kinds)) == 1:
            ks = [kinds[0]] * len(dates)
        else:
            ks = [""] * len(dates)
        cols = [{"label": f"{d.group(1)[:3]} {d.group(2)}, {d.group(3)}", "end": _iso(d.group(1), d.group(2), d.group(3)),
                 "year": d.group(3), "type": k} for d, k in zip(dates, ks)]
        return {"cols": cols, "change_cols": change_cols, "text": line[:200]}
    phrases = [((_KIND.get((m.group(1) or "").lower(), "FY") if m.group(1) else "FY"), (m.group(2) or m.group(4) or "").replace(".", ""))
               for m in _PHRASE.finditer(context + " " + line)]
    if phrases and len(years) % len(phrases) == 0:
        per = len(years) // len(phrases)
        cols = []
        for g, (k, md) in enumerate(phrases):
            for y in years[g * per:(g + 1) * per]:
                mon, day = (md.split() + ["", ""])[:2]
                cols.append({"label": f"{mon[:3]} {day}, {y}" if md else y, "end": _iso(mon, day, y) if md else None, "year": y, "type": k})
        return {"cols": cols, "change_cols": change_cols, "text": (context[-120:] + " | " + line)[:220]}
    k = "FY" if ("year" in low or "fiscal" in low or "year" in context.lower()) else ""
    # "December 31  2025 2024": one month and day for every year column
    md = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b", line) \
        or re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s*$", context.strip())
    if md:
        return {"cols": [{"label": f"{md.group(1)[:3]} {int(md.group(2))}, {y}", "end": _iso(md.group(1), md.group(2), y), "year": y, "type": k}
                         for y in years], "change_cols": change_cols, "text": line[:200]}
    return {"cols": [{"label": y, "end": None, "year": y, "type": k} for y in years], "change_cols": change_cols, "text": line[:200]}


def _caption(body: str, off: int) -> dict:
    """Scale and exceptions from the nearest "(In millions, except ...)" caption."""
    hits = list(_CAPTION.finditer(body[max(0, off - 6000):off]))
    if not hits:
        return {"scale": "", "shares_scale": "", "text": ""}
    m = hits[-1]
    scale = {"millions": "M", "billions": "B", "thousands": "K"}[m.group(1).lower()]
    rest = m.group(2).lower()
    shares_scale = scale
    sm = re.search(r"shares?[^.]{0,60}?(?:in|reflected in) (thousands|millions)", rest)
    if sm:
        shares_scale = {"thousands": "K", "millions": "M"}[sm.group(1)]
    elif "except" in rest and "share" in re.sub(r"per[ -]share[a-z ]*", "", rest):
        shares_scale = ""          # "except share data": share counts are actual ("except per share amounts" leaves them scaled)
    return {"scale": scale, "shares_scale": shares_scale, "text": m.group(0)[:200]}


_TITLE = re.compile(r"^(?:Condensed\s+)?(?:Consolidated\s+)?(?:Statements?|Balance Sheets?)\b[^\d]{0,80}$|^[A-Z][A-Z ,&'’()-]{8,90}$")


def _cells(parts: list, change_cols: bool) -> list:
    toks = []
    for j, t in enumerate(parts):
        if t in ("$", "%"):
            continue
        if change_cols and (t.endswith("%") or (j + 1 < len(parts) and parts[j + 1] == "%")):
            continue
        toks.append(t)
    return toks


_GENERIC = re.compile(r"^(basic|diluted|total|other|subtotal|products|services|current|non-current|noncurrent)$", re.I)


def _unit(label: str, heading: str, raw0: str) -> str:
    ctx = (heading + " " + label).lower()
    if raw0.endswith("%"):
        return "percent"
    if re.match(r"shares? (repurchased|withheld|purchased)|purchase of shares", label.lower()) and "activities" in heading.lower():
        return "USD"                    # a cash flow line, not a share count
    if re.match(r"(shares|weighted[- ]average|number of shares)", heading.lower()) or re.match(r"(shares|weighted[- ]average)", label.lower()):
        return "shares"
    if re.search(r"per (?:common |diluted |basic )?share|earnings per share|net income per share|loss per share", ctx) or (
            re.fullmatch(r"(basic|diluted)", label.strip(), re.I) and re.search(r"per share|earnings per", heading.lower())):
        return "USD/share"
    if re.search(r"\bshares\b|weighted[- ]average", ctx) and not re.search(r"repurchase|dividend|\$", ctx):
        return "shares"
    return "USD"


def parse(doc) -> list[dict]:
    """All table rows in the document, in order."""
    body = doc.body
    rows: list[dict] = []
    hdr = None
    hdr_age = 0
    heading = ""
    title = ""
    prev: list[str] = []
    block = ""
    pos = 0
    for line in body.split("\n"):
        start = pos
        pos += len(line) + 1
        s = line.strip()
        if not s:
            continue
        ctx = " ".join(prev[-2:])
        prev.append(s)
        if re.match(r"^(?:Note|NOTE)\s+\d{1,2}\b", s) and len(s) <= 120:
            title = s                                   # a note heading: tables below belong to the note
        elif re.match(r"^See accompanying notes", s, re.I):
            title = ""
        elif len(s) <= 110 and not s.endswith(".") and not re.search(r"\d{3}", s) and statement_of(s) != "other":
            title = re.sub(r"^.*?((?:Condensed\s+)?(?:Consolidated\s+)?(?:Statements?|Balance Sheets?)\b)", r"\1", s, flags=re.I)
        h = header_periods(s, ctx) if len(s) < 260 else None
        if h:
            hdr, hdr_age, heading = h, 0, ""
            # the table's own caption: a short heading-like line just above
            # ("Rail North America Fleet Data"), which says whose figures these are
            block = next((x for x in reversed(prev[-7:-1]) if 3 <= len(x) <= 70 and not re.search(r"\d", x) and x[:1].isupper()
                          and not x.endswith((".", ":", ",")) and statement_of(x) == "other" and not re.match(r"\(", x)), "")
            tail = re.sub(r"^.*\b(19|20)\d\d\b", "", s).strip(" :")
            if tail and re.match(r"[A-Za-z]", tail):
                heading = tail
            continue
        hdr_age += 1
        if hdr is None or hdr_age > 90 or len(s) > 320:
            if hdr_age > 90:
                hdr = None
            continue
        n = len(hdr["cols"])
        parts = re.sub(r"(\d)\s+%", r"\1%", s).replace("$", " $ ").split()       # "99.0 %" is one cell
        k = len(parts)
        while k > 0 and _CELL.fullmatch(parts[k - 1]):
            k -= 1
        label = " ".join(parts[:k]).strip(" :")
        toks = _cells(parts[k:], hdr["change_cols"])
        if len(toks) == n + 1 and re.fullmatch(r"\((?:\d|[a-z])\)", toks[0]) and label:
            toks = toks[1:]                 # a footnote marker after the label: "Utilization rate (1) 99.0%"
            label += f" {parts[k]}"
        unlabeled = False
        close_after, next_heading = False, None
        if k == 0 or not re.match(r"[A-Za-z(]", label or "x"):
            j = 0
            while j < len(parts) and _CELL.fullmatch(parts[j]):
                j += 1
            lead = _cells(parts[:j], hdr["change_cols"])
            if len(lead) == n and heading:
                label, toks, unlabeled = "Total " + heading, lead, True
                rest = " ".join(parts[j:]).strip(" :")
                close_after = True               # a total ends its section
                if rest and re.match(r"[A-Za-z]", rest):
                    next_heading = rest
            else:
                continue
        elif not toks:
            if len(s) <= 90 and re.match(r"[A-Z]", s) and not s.endswith("."):
                heading = s.strip(" :")
            continue
        if len(toks) < 2 or len(toks) != n:
            continue
        vals = [parse_number(t) for t in toks]
        if any(v is None for v in vals):
            continue
        label = re.sub(r"\s*\((?:\d|[a-z])\)$", "", label).strip()          # footnote marker
        footnote = bool(re.search(r"\((?:\d|[a-z])\)$", " ".join(parts[:k])))
        # "Earnings per share: Basic" -> heading "Earnings per share", label "Basic"
        if ":" in label:
            h2, label = [x.strip() for x in label.rsplit(":", 1)]
            if label:
                heading = h2
            else:
                label = h2
        cap = _caption(body, start)
        unit = _unit(label, heading, toks[0])
        scale = cap["scale"] if unit == "USD" else cap["shares_scale"] if unit == "shares" else ""
        full = (f"{heading}: {label}" if heading and label and len(label.split()) <= 3 and _GENERIC.match(label.split()[0])
                and not label.lower().startswith(heading.lower()) else label)
        loc = doc.location(start)
        statement = statement_of(title)
        cols = hdr["cols"]
        if statement == "balance":
            cols = [dict(c, type="instant") for c in cols]
        rows.append({
            "label": label, "full_label": full, "key": re.sub(r"[^a-z0-9]+", " ", label.lower()).strip(),
            "heading": heading, "values": vals, "raw": toks, "cols": cols, "header_text": hdr["text"],
            "unit": unit, "scale": scale, "caption": cap["text"], "table_title": title,
            "statement": statement, "unlabeled_total": unlabeled, "footnote": footnote, "block": block,
            "offset": start, "line": s[:240], **loc,
        })
        if close_after or label.lower().startswith("total "):
            heading = next_heading or ""
    return rows
