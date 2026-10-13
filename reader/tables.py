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


_SCOPE_RX = [
    ("affiliate", r"\baffiliat|joint venture|equity[- ]method|investee|unconsolidated"),
    ("segment", r"\bsegments?\b|reportable|business group|division"),
    ("subsidiary", r"\bsubsidiar(y|ies)\b(?! and)|guarantor|parent company only|issuer"),
    ("supplemental", r"pro forma|supplemental|selected quarterly|quarterly financial data|unaudited quarterly|non-gaap|reconciliation of"),
]


# a label or heading that continues on the next line ends mid-phrase:
# "... attributable to noncontrolling interests and", "... net loss per"
_WRAPS = re.compile(r"\b(and|or|of|in|to|for|per|from|by|on|with|attributable|the|net|less)\s*$|,\s*$", re.I)


def _total_heading(groups: list, heading: str, toks: list) -> str:
    """Name an unlabeled total by the sections it actually sums. A total
    printed under a sub-heading can cover rows above that sub-heading too
    (Debt: Recourse; Non-recourse: two lines; then one unlabeled total of all
    three). Checked by arithmetic on every column; the nearest heading is
    kept when no run of sections adds up."""
    vals = [parse_number(t) for t in toks]
    if not groups or any(v is None for v in vals):
        return heading
    dec = max((len(t.split(".")[1]) if "." in t else 0) for t in toks)
    step = 10 ** -dec
    acc, nrows = [0.0] * len(vals), 0
    for g in range(len(groups) - 1, -1, -1):
        for row in groups[g][1]:
            if len(row) != len(vals):
                return heading
            acc = [a + x for a, x in zip(acc, row)]
            nrows += 1
        tol = step * (nrows + 1) * 0.5 + 1e-9
        if all(abs(a - v) <= tol for a, v in zip(acc, vals)):
            return groups[g][0] or heading
    return heading


def scope_of(intro: str, title: str, block: str) -> str:
    """Whose figures a table holds, from the sentence that introduces it,
    its note title and its caption, in that order of weight. Running headers
    such as "Notes to consolidated financial statements" carry no scope."""
    clean = lambda x: re.sub(r"notes? to (the )?(condensed )?consolidated financial statements( \(continued\))?|and subsidiaries", "", (x or "").lower())
    for text in (clean(intro), clean(title), clean(block)):
        if not text.strip():
            continue
        for name, rx in _SCOPE_RX:
            if re.search(rx, text):
                # "the Company and its consolidated subsidiaries" is the whole group
                if name == "subsidiary" and re.search(r"consolidated subsidiaries|company and (its )?subsidiaries", text):
                    continue
                return name
        if re.search(r"\bconsolidated\b|\bthe company\b|\bour\b", text):
            return "consolidated"
    return "unknown"


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
    # change columns ("$ Change", "Increase (Decrease)", "% Change") follow the period columns
    m_ch = re.search(r"change|increase|decrease|variance", low)
    change_cols = bool(m_ch) and m_ch.start() > max(m.end() for m in _YEAR.finditer(line))
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
    # "September 29,  September 30," above "2018  2017": one month and day per year column, in order
    last = (context.strip().split("\n") or [""])[-1]
    mds = re.findall(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?", last)
    if len(mds) == len(years) and len(set(mds)) > 1 and len(set(_kinds(context[-300:]))) <= 1:
        kk = (_kinds(context[-300:]) or ["FY" if "year" in (low + context.lower()) else ""])[0]
        return {"cols": [{"label": f"{mo[:3]} {int(d)}, {y}", "end": _iso(mo, d, y), "year": y, "type": kk} for (mo, d), y in zip(mds, years)],
                "change_cols": change_cols, "text": (last[-120:] + " | " + line)[:220]}
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
    intro = ""
    restated = False
    table_id, row_index = 0, 0
    groups = []      # [heading, [values of each row]] for the open sections of this table, in order
    label_head, prev_heading = None, ""
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
        elif (len(s) <= 110 and not s.endswith((".", ":")) and not re.search(r"\d{3}", s) and statement_of(s) != "other"
              and re.match(r"^(?:[A-Z0-9][A-Z0-9 .,&'\u2019-]{2,70}\s+)?(?:condensed\s+)?(?:consolidated\s+)?(?:statements?|balance)\b", s, re.I)):
            # a statement title opens its line (after an all-caps company name at most); a sentence
            # that mentions "the statements of income:" introduces a note table instead
            title = re.sub(r"^.*?((?:Condensed\s+)?(?:Consolidated\s+)?(?:Statements?|Balance Sheets?)\b)", r"\1", s, flags=re.I)
        h = header_periods(s, ctx) if len(s) < 260 else None
        if h:
            hdr, hdr_age, heading = h, 0, ""
            table_id += 1
            row_index = 0
            groups = []
            # the sentence that introduces the table ("The following table shows
            # condensed income statements of the RRPF affiliates ... :")
            intro = next((x for x in reversed(prev[-6:-1]) if x.endswith(":") and len(x) > 30), "")
            restated = bool(re.search(r"as (adjusted|recast|revised|restated)|\brestated\b|\brecast\b", (ctx + " " + s).lower()))
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
        if (len(toks) == n + 1 or (len(toks) > n + 1 and re.search(r"[.,%]", toks[1]))) and re.fullmatch(r"\((?:\d|[a-z])\)", toks[0]) and label:
            toks = toks[1:]                 # a footnote marker after the label: "Utilization rate (1) 99.0%"
            label += f" {parts[k]}"
        if hdr["change_cols"] and len(toks) > n:
            toks = toks[:n]                      # the period columns come first; the rest are changes
        unlabeled = False
        close_after, next_heading = False, None
        if k == 0 or not re.match(r"[A-Za-z(]", label or "x"):
            j = 0
            while j < len(parts) and _CELL.fullmatch(parts[j]):
                j += 1
            lead = _cells(parts[:j], hdr["change_cols"])
            if hdr["change_cols"] and len(lead) > n:
                lead = lead[:n]
            if len(lead) == n and heading:
                label, toks, unlabeled = "Total " + _total_heading(groups, heading, lead), lead, True
                rest = " ".join(parts[j:]).strip(" :")
                close_after = True               # a total ends its section
                if rest and re.match(r"[A-Za-z]", rest):
                    next_heading = rest
            else:
                continue
        elif not toks:
            if len(s) <= 90 and re.match(r"[A-Z]", s) and not s.endswith("."):
                prev_heading, heading = heading, s.strip(" :")
                label_head = s if _WRAPS.search(s) else None
            elif len(s) <= 90 and re.match(r"[a-z]", s) and heading and label_head:
                heading = f"{heading} {s.strip(' :')}"          # a heading wrapped onto a second line
                label_head = f"{label_head.strip()} {s.strip()}" if _WRAPS.search(s) else None
            continue
        # "Recourse 6,431.5 5,887.5  6,448.8 5,905.6": a row and the unlabeled
        # total under it fused onto one text line. Split only when the second
        # half is the section's sum including the first half.
        fused = None
        if n >= 2 and len(toks) == 2 * n and not unlabeled and k > 0 and heading:
            v1, v2 = [parse_number(t) for t in toks[:n]], [parse_number(t) for t in toks[n:]]
            if None not in v1 and None not in v2 and _total_heading(groups + [[heading, [v1]]], "", toks[n:]):
                toks, fused = toks[:n], toks[n:]
        if len(toks) < 2 or len(toks) != n:
            continue
        vals = [parse_number(t) for t in toks]
        if any(v is None for v in vals):
            continue
        # a label wrapped onto a second line: "Net income (loss) attributable to noncontrolling
        # interests and" / "redeemable noncontrolling interests in subsidiaries  87  (87)"
        if label_head and re.match(r"[a-z]", label) and not unlabeled:
            segs = [x.strip() for x in re.split(r"\s{2,}", s) if x.strip()]
            if len(segs) >= 2 and re.match(r"[a-z]", segs[0]) and re.match(r"[A-Z]", segs[1]) and label.startswith(segs[0]):
                # "share of common stock  Basic  177": the end of a wrapped heading, then the row's own label
                heading = f"{label_head.strip()} {segs[0]}"
                label = label[len(segs[0]):].strip()
            else:
                label = f"{label_head.strip()} {label}"
                heading = prev_heading
        label_head = None
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
            "intro": intro[:240], "scope": scope_of(intro, title, block), "restated": restated,
            "table_id": table_id, "row_index": row_index,
            "offset": start, "line": s[:240], **loc,
        })
        row_index += 1
        if unlabeled or label.lower().startswith("total "):
            groups = []                      # a total closes the sections it sums
        elif groups and groups[-1][0] == heading:
            groups[-1][1].append(vals)
        else:
            groups.append([heading, [vals]])
        if fused:
            tl = "Total " + (_total_heading(groups, heading, fused) or heading)
            rows.append(dict(rows[-1], label=tl, full_label=tl, key=re.sub(r"[^a-z0-9]+", " ", tl.lower()).strip(),
                             values=[parse_number(t) for t in fused], raw=fused, unlabeled_total=True, row_index=row_index))
            row_index += 1
            groups = []
            close_after = True
        if close_after or label.lower().startswith("total "):
            heading = next_heading or ""
    # a combined statement of income and comprehensive income ("Consolidated
    # Statements of Comprehensive Income" running from revenue to EPS) is the
    # income statement
    combined = {r["table_id"] for r in rows if r["statement"] == "comprehensive"
                and re.match(r"^(total )?(net )?(revenues?|sales|net sales|operating revenues?)$", r["key"])}
    for r in rows:
        if r["table_id"] in combined:
            r["statement"] = "income"
    return rows
