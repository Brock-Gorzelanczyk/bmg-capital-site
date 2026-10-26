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
_DAY_YEAR = re.compile(r"\b(\d{1,2}),((?:19|20)\d\d)\b")
# "July 26, 2026", and NVIDIA's 10-Q header "Jul 26, 2026  Jul 27, 2025  Jul 26, 2026  Jul 27, 2025" (without the short
# names, a "Three Months Ended  Six Months Ended" header over those dates was read as four six-month columns ending Jul 26)
# "30-Sep-25", as Tesla prints its balance sheet columns, reads as "Sep 30, 2025"
_DMY = re.compile(r"\b(\d{1,2})-(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)-((?:19|20)?\d\d)\b", re.I)
# a fiscal quarter heading a column ("Q3-2026", "3Q-2026", "Q3'26"); its end date comes from the same document (_quarter_dates)
_QLABEL = re.compile(r"\b(?:Q([1-4])[\-\u2013' ]?((?:19|20)?\d\d)|([1-4])Q[\-\u2013' ]?((?:19|20)?\d\d))\b", re.I)


def _dmy(line: str) -> str:
    return _DMY.sub(lambda m: f"{m.group(2)} {int(m.group(1))}, {m.group(3) if len(m.group(3)) == 4 else '20' + m.group(3)}", line)


_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December"
                   r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2}),?\s+((?:19|20)\d\d)\b")
_CELL = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?|\$|%|\u2014|\u2013|-|N/M|n/m|NM|\*")     # N/M, *: a change not meaningful
# "Three months ended June 30", "Year ended December 31", and Wells Fargo's "Quarter ended June 30,  Six months ended June 30,"
_PHRASE = re.compile(r"\b(?:(?P<n>three|six|nine|twelve|3|6|9|12)[ -]months?|(?P<q>quarters?)|(?P<y>years?|fiscal years?)) ended\s+(?P<md>[A-Z][a-z]+\.? \d{1,2})", re.I)
_KIND = {"three": "Q", "3": "Q", "six": "6M", "6": "6M", "nine": "9M", "9": "9M", "twelve": "FY", "12": "FY"}
_CAPTION = re.compile(r"\(\s*(?:unaudited[;,]?\s*)?(?:dollars |amounts |\$ )?(?:in )?(millions|billions|thousands)\b([^)]{0,200})\)", re.I)   # "in" optional: NEE prints "(millions, except ...)"; Disney "(unaudited; in millions ...)"
# the same caption without brackets, at the start of a line (Home Depot: "in millions, except per share data  Fiscal 2025 ..."):
# only where it can be a caption, alone on its line, before an "except ..." clause or before the columns
_CAPTION_LINE = re.compile(r"(?im)^[ \t]*(?:(?:\$[ \t]*|dollars[ \t]+|amounts[ \t]+)?in[ \t]+(millions|billions|thousands)\b"
                           r"(?:[ \t]+of[ \t]+(?:USD|U\.?S\.?[ \t]+dollars)(?:[ \t]+or[ \t]+shares(?:[ \t]+as[ \t]+applicable)?)?)?"   # Tesla: "In millions of USD or shares as applicable"
                           r"|(millions|billions|thousands)[ \t]+of[ \t]+(?:u\.?s\.?[ \t]+)?dollars\b)"          # Chevron: "Millions of dollars  2025  2024"
                           r"((?:,?[ \t]+except\b[^\n]{0,120})?(?:[ \t]{2,}[^\n]*)?)[ \t]*$")
# the scale as the first column header of the year line (Union Pacific: "Millions  2025  2024  2023", "Millions, Except
# Per Share Amounts  2025 ..."): without it the income statement's $24,510 million read as $24,510, and an MD&A
# table under a chart's "(billions)" caption read $7,138 million as $7.1 trillion (2026-10-20)
_CAPTION_HEAD = re.compile(r"(?im)^[ \t]*(millions|billions|thousands)((?:,[^\n]{0,80}?)?)[ \t]{2,}(?:\$[ \t]*)?(?:19|20)\d\d\b")
_YEAR_LINE = re.compile(r"(?m)^[^\n]*\b(?:19|20)\d\d\b(?:[ \t]{2,}(?:\$[ \t]*)?(?:19|20)\d\d\b)+[ \t]*$")
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

STATEMENTS = [
    ("comprehensive", re.compile(r"statements? of comprehensive (income|loss)|comprehensive (income|loss) statements?", re.I)),
    # Caterpillar: "Consolidated Results of Operations for the Years Ended December 31," and "Consolidated Financial Position at
    # December 31," (an MD&A heading "Consolidated Results of Operations" on its own is not the statement)
    ("income", re.compile(r"statements? of (results of )?(operations|income|earnings)|income statements?|"     # its 10-Q: "Statement of Results of Operations"
                          r"consolidated results of operations for the (?:years?|three|six|nine|twelve|quarters?|periods?)\b", re.I)),
    ("balance", re.compile(r"balance sheets?\b|consolidated balance sheet|statements? of financial (position|condition)|consolidated financial position (?:at|as of)\b", re.I)),
    ("cashflow", re.compile(r"statements? of cash flows?|cash flows? statements?", re.I)),     # Microsoft: "CASH FLOWS STATEMENTS"
    ("equity", re.compile(r"statements? of (changes in )?(shareholders|stockholders|shareowners)['’]? equity|statements? of changes in equity|"
                          r"(shareholders|stockholders|shareowners)['’]? equity statements?", re.I)),   # Coca-Cola: "SHAREOWNERS'"; Microsoft: "STOCKHOLDERS’ EQUITY STATEMENTS"
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
    # the last section subtracted from the ones above (Trinity's release: "Operating costs:" cost of revenues and SE&A,
    # then "Gains on dispositions of property:" printed positive, then 285.3 = 375.2 + 51.1 - 141.0, with no label). The
    # total is the first section's, never the gains' it was named after (2026-10-26)
    if len(groups) >= 2:
        last = [0.0] * len(vals)
        for row in groups[-1][1]:
            if len(row) != len(vals):
                return heading
            last = [a + x for a, x in zip(last, row)]
        acc, nrows = [-x for x in last], len(groups[-1][1])
        for g in range(len(groups) - 2, -1, -1):
            for row in groups[g][1]:
                if len(row) != len(vals):
                    return heading
                acc = [a + x for a, x in zip(acc, row)]
                nrows += 1
            tol = step * (nrows + 1) * 0.5 + 1e-9
            if all(abs(a - v) <= tol for a, v in zip(acc, vals)) and groups[g][0]:
                return groups[g][0]
    return heading


# another entity's figures, said in so many words: the parent alone, a guarantor or issuer group. Chevron's MD&A: "summary
# financial information for Chevron Corporation, as Guarantor, excluding its consolidated subsidiaries" (its net income
# of $40,967 million was read as Chevron's, whose statement prints $12,485 million); Wells Fargo's "Parent-Only" statements
_OTHER_ENTITY = re.compile(r"\bexcluding (?:its|their|the) (?:consolidated )?subsidiaries\b|\bobligor group\b|\bparent[- ](?:company[- ])?only\b"
                           r"|\bas (?:the )?guarantor\b|\bcondensed financial information of (?:the )?registrant\b", re.I)


# a paragraph that presents an affiliate's statements: Simon Property's "a summary of the combined balance sheets and
# statements of operations of our equity method investments" (their total assets were read against Simon's own)
_AFFILIATE_SUMMARY = re.compile(r"\b(?:summar\w*|condensed|combined)\b[^.]{0,120}\b(?:balance sheets?|statements? of (?:operations|income|earnings)"
                                r"|financial (?:information|statements|data|position))\b[^.]{0,120}\b(?:equity[- ]method|joint ventures?|unconsolidated"
                                r"|investees?|affiliates?|platform investments)\b", re.I)


def _net_of_less(rows: list, table_id: int, toks: list) -> str:
    """An unlabeled line right under a "Less" line is the line above it net of that one, not its section's total
    (Simon Property: "Investment properties, at cost" less "accumulated depreciation", then 30,244,557 with no label,
    which was read as Total assets)."""
    if len(rows) < 2 or rows[-1]["table_id"] != table_id or rows[-2]["table_id"] != table_id:
        return ""
    base, less = rows[-2], rows[-1]
    if not re.match(r"less\b", less["label"], re.I) or not len(base["values"]) == len(less["values"]) == len(toks):
        return ""
    vals = [parse_number(t) for t in toks]
    if any(v is None for v in vals) or not all(abs(b - abs(x) - v) <= max(1.0, abs(v) * 1e-6) for b, x, v in zip(base["values"], less["values"], vals)):
        return ""
    what = re.sub(r"^less\W*", "", less["label"], flags=re.I).strip()
    return f"{base['label']}, net of {what}" if what else f"{base['label']}, net"


def scope_of(intro: str, title: str, block: str) -> str:
    """Whose figures a table holds, from the sentence that introduces it,
    its note title and its caption, in that order of weight. Running headers
    such as "Notes to consolidated financial statements" carry no scope."""
    clean = lambda x: re.sub(r"notes? to (the )?(condensed )?consolidated financial statements( \(continued\))?|and subsidiaries", "", (x or "").lower())
    for text in (clean(intro), clean(title), clean(block)):
        if not text.strip():
            continue
        if _OTHER_ENTITY.search(text):
            return "subsidiary"
        for name, rx in _SCOPE_RX:
            if re.search(rx, text):
                # "the Company and its consolidated subsidiaries" is the whole group
                if name == "subsidiary" and re.search(r"consolidated subsidiaries|company and (its )?subsidiaries", text):
                    continue
                return name
        if re.search(r"\bconsolidated\b|\bthe company\b|\bour\b", text):
            return "consolidated"
    return "unknown"


def _statement_named(title: str) -> str:
    for name, rx in STATEMENTS:
        if rx.search(title or ""):
            return name
    return "other"


# a summary of a statement in the MD&A ("Selected income statement data", "Consolidated
# balance sheets analysis" in JPMorgan's 10-K) names the statement but is not it
_SUMMARY = re.compile(r"\b(selected|data|analysis|highlights|summary|discussion)\b", re.I)


_NUMBERED = {"level", "tier", "class", "series", "note", "notes", "phase", "stage", "item", "part", "type", "year", "years", "schedule",
             "exhibit", "statement", "unit", "units", "group", "step", "round", "option", "plan", "fund", "facility", "tranche", "lot",
             "block", "building", "line", "quarter", "week", "weeks", "day", "days", "month", "months", "segment", "region", "zone"}


def row_key(label: str, statement_row: bool = False) -> str:
    """A row label for matching: lower case, words only. A note in brackets that holds a number ("(included $ 134,559 ...
    at fair value)") is not the label, and neither is a footnote mark printed against the last word ("Basic Net Income
    Per Share1" is "basic net income per share"), nor are footnote letters ("Cost of sales(a), (b)"). On a statement's own rows a
    footnote mark set off by a space is not
    either ("Profit 1", "Diluted 2": Caterpillar), unless the word before numbers things ("Level 3", "Year 1")."""
    low = re.sub(r"\(\s*[a-z]{1,2}\s*\)", " ", label.lower())          # footnote letters: "Cost of sales(a), (b)" (Pfizer)
    k = re.sub(r"[^a-z0-9]+", " ", re.sub(r"\([^()]*\d[^()]*\)", " ", low)).strip()
    k = re.sub(r"(?<=[a-z]{3})\d{1,2}$", "", k).strip()
    m = re.search(r"\b([a-z]+) \d$", k) if statement_row else None
    return k[:-2].strip() if m and m.group(1) not in _NUMBERED else k


def statement_of(title: str) -> str:
    name = _statement_named(title)
    return "other" if name != "other" and _SUMMARY.search(title or "") else name


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
    for m in re.finditer(r"\b(three|six|nine|twelve) months\b|\b(quarters?)\s+ended\b|\b(?:first|second|third|fourth)[- ](quarter)\b"
                         r"|\b(?:fiscal )?years?\b(?= ended| end)|\bfiscal year\b", text, re.I):
        w = (m.group(1) or ("three" if m.group(2) or m.group(3) else "")).lower()      # Disney: "Quarter Ended  Nine Months Ended"
        out.append({"three": "Q", "six": "6M", "nine": "9M", "twelve": "FY"}.get(w, "FY"))
    return out


_LINE_CAPTION = re.compile(r"(?i)^\s*(?:(?:\$\s*|dollars\s+|amounts\s+)?in\s+(?:millions|thousands|billions)(?:\s+of\s+(?:u\.?s\.?\s+)?dollars)?"
                           r"|(?:millions|thousands|billions)\s+of\s+(?:u\.?s\.?\s+)?dollars)"
                           r"(?:,?\s+except\s+(?:per[ -]share|share|shares|percentages?)[\w ,()-]*?(?:data|amounts|figures|information|values))?")


_CHANGE_COL = re.compile(r"increase\s*\(decrease\)|\(decrease\)\s*increase|[%$]\s*change|\bchange\b|\bincrease\b|\bdecrease\b|\bvariance\b"
                         r"|\(decr\.?\)|%\s*incr\.?", re.I)


def header_periods(line: str, context: str) -> Optional[dict]:
    """A column header with, when a change column follows each group of periods, the "slots" of a row's cells."""
    h = _header_periods(line, context)
    if h and not h.get("slots"):
        line = _dmy(_DAY_YEAR.sub(r"\1, \2", line))
        dates = list(_DATE.finditer(line))
        ps = [d.start() for d in dates] if len(dates) == len(h["cols"]) else [m.start() for m in _YEAR.finditer(line)]
        marks = sorted([(p, "p") for p in ps] + [(m.start(), "c") for m in _CHANGE_COL.finditer(line)])
        slots = [k for _, k in marks]
        if len(ps) == len(h["cols"]) and "c" in slots and slots.index("c") < len(slots) - 1 - slots[::-1].index("p"):
            h = {**h, "change_cols": False, "slots": slots}
    return h


def _header_periods(line: str, context: str) -> Optional[dict]:
    """A column header: two or more period columns. `context` is the line(s)
    just above, which may carry "Three months ended May 31"."""
    # "December 27,2025" (no space after the comma, as some inline-XBRL filings print it)
    line = _dmy(_DAY_YEAR.sub(r"\1, \2", line))
    context = _dmy(_DAY_YEAR.sub(r"\1, \2", context))
    qs = list(_QLABEL.finditer(line))
    rest = _QLABEL.sub(" ", _LINE_CAPTION.sub(" ", line))
    if len(qs) >= 2 and not re.search(r"\d", rest) and len([w for w in re.findall(r"[A-Za-z]+", rest) if w.lower() not in ("yoy", "qoq", "change", "unaudited")]) <= 2:
        yr = lambda m: (m.group(2) or m.group(4)) if len(m.group(2) or m.group(4)) == 4 else "20" + (m.group(2) or m.group(4))
        cols = [{"label": f"Q{m.group(1) or m.group(3)} {yr(m)}", "end": None, "year": yr(m), "type": "Q", "fq": f"{yr(m)}-Q{m.group(1) or m.group(3)}"} for m in qs]
        return {"cols": cols, "change_cols": bool(re.search(r"(?i)\b(?:yoy|qoq|change)\b", rest)), "text": line[:200]}
    years = [m.group(0) for m in _YEAR.finditer(line)]
    if len(years) < 2:
        return None
    nums = [t for t in re.findall(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?", line)]
    other = [t for t in nums if not _YEAR.fullmatch(re.sub(r"[^\d]", "", t)) and not re.fullmatch(r"\d{1,2},?", t.strip())]
    if len(other) > 1:
        return None
    bare = _LINE_CAPTION.sub(" ", line)                 # "in millions, except per share data" before the dates, not in brackets
    words = re.findall(r"[A-Za-z]+", re.sub(r"\([^)]*\)", " ", bare))      # a scale caption may share the line
    allowed = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
               "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct", "nov", "dec",
               "change", "years", "year", "ended", "months", "three", "six", "nine", "twelve", "fiscal", "as", "of", "and", "at", "net", "sales",
               "total", "unaudited", "restated", "q", "fy"}
    if len([w for w in words if w.lower() not in allowed]) > 4:
        return None
    low = line.lower()
    # change columns ("$ Change", "Increase (Decrease)", "% Change") follow the period columns
    # Trinity's 10-Q backlog table: "2026  2025  2026  2025  Percent" over "(in units, $ in whole dollars)  Change" (the
    # word "Change" on the next line); its "Ending balance  11,340 ... (19.8)%" row was dropped for its fifth cell
    m_ch = re.search(r"change|increase|decrease|variance|\bpercent\s*$", low)
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
        # a change column after each group of periods (Home Depot's release: "August 2, 2026  August 3, 2025  % Change" twice):
        # which cells of a row are periods and which are changes
        marks = sorted([(d.start(), "p") for d in dates] + [(m.start(), "c") for m in _CHANGE_COL.finditer(line)])
        slots = [k for _, k in marks]
        if "c" in slots and slots.index("c") < len(slots) - 1 - slots[::-1].index("p"):
            return {"cols": cols, "change_cols": False, "slots": slots, "text": line[:200]}
        return {"cols": cols, "change_cols": change_cols, "text": line[:200]}
    # "September 29,  September 30," above "2018  2017": one month and day per year column, in order
    last = (context.strip().split("\n") or [""])[-1]
    mds = re.findall(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?", last)
    if len(mds) == len(years) and len(set(mds)) > 1 and len(set(_kinds(context[-300:]))) <= 1:
        kk = (_kinds(context[-300:]) or ["FY" if "year" in (low + context.lower()) else ""])[0]
        return {"cols": [{"label": f"{mo[:3]} {int(d)}, {y}", "end": _iso(mo, d, y), "year": y, "type": kk} for (mo, d), y in zip(mds, years)],
                "change_cols": change_cols, "text": (last[-120:] + " | " + line)[:220]}
    # "For the Three Months Ended  For the Six Months Ended" over "June 30,  June 30," over "2026  2025  2026  2025" (Simon
    # Property's 10-Q): one month and day per group of year columns, each group of its own length (all were six months)
    kinds = _kinds(context[-300:])
    # NVIDIA's earnings release: the same, with a month and day over every year column ("July 26,  July 27,  July 26,  July 27,"
    # over "2026  2025  2026  2025"; before 2026-10-19 the four columns were read as six months ending July 26)
    if len(kinds) > 1 and mds and len(years) % len(kinds) == 0 and len(mds) in (1, len(kinds), len(years)):
        per = len(years) // len(kinds)
        cols = []
        for g, k in enumerate(kinds):
            for j, y in enumerate(years[g * per:(g + 1) * per]):
                mo, d = mds[g * per + j] if len(mds) == len(years) else mds[g if len(mds) > 1 else 0]
                cols.append({"label": f"{mo[:3]} {int(d)}, {y}", "end": _iso(mo, d, y), "year": y, "type": k})
        return {"cols": cols, "change_cols": change_cols, "text": (last[-120:] + " | " + line)[:220]}
    phrases = [(_KIND.get(m.group("n").lower(), "FY") if m.group("n") else "Q" if m.group("q") else "FY", m.group("md").replace(".", ""))
               for m in _PHRASE.finditer(context + " " + line)]
    if phrases and len(years) % len(phrases) == 0:
        per = len(years) // len(phrases)
        cols = []
        for g, (k, md) in enumerate(phrases):
            for y in years[g * per:(g + 1) * per]:
                mon, day = (md.split() + ["", ""])[:2]
                cols.append({"label": f"{mon[:3]} {day}, {y}" if md else y, "end": _iso(mon, day, y) if md else None, "year": y, "type": k})
        return {"cols": cols, "change_cols": change_cols, "text": (context[-120:] + " | " + line)[:220]}
    if len(kinds) > 1 and len(years) % len(kinds) == 0:
        per = len(years) // len(kinds)
        return {"cols": [{"label": y, "end": None, "year": y, "type": kinds[i // per]} for i, y in enumerate(years)],
                "change_cols": change_cols, "text": (context[-120:] + " | " + line)[:220]}
    k = "FY" if ("year" in low or "fiscal" in low or "year" in context.lower() or "fiscal" in context[-200:].lower()) else ""
    # "December 31  2025 2024": one month and day for every year column
    md = re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})\b", line) \
        or re.search(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),?\s*$", context.strip())
    if md:
        return {"cols": [{"label": f"{md.group(1)[:3]} {int(md.group(2))}, {y}", "end": _iso(md.group(1), md.group(2), y), "year": y, "type": k}
                         for y in years], "change_cols": change_cols, "text": line[:200]}
    return {"cols": [{"label": y, "end": None, "year": y, "type": k} for y in years], "change_cols": change_cols, "text": line[:200]}


def _caption(body: str, off: int) -> dict:
    """Scale and exceptions from the nearest "(In millions, except ...)" caption."""
    window = body[max(0, off - 6000):off]
    heads = []
    for h in _CAPTION_HEAD.finditer(window):
        nl = window.find("\n", h.end())
        if nl < 0 or not _YEAR_LINE.search(window, nl + 1):
            heads.append(h)                     # a year line's scale belongs to its own table, not the next one
    hits = list(_CAPTION.finditer(window)) + list(_CAPTION_LINE.finditer(window)) + heads
    if not hits:
        return {"scale": "", "shares_scale": "", "text": ""}
    m = max(hits, key=lambda x: x.end())          # the caption nearest the table
    word, rest = ((m.group(1) or m.group(2)), m.group(3)) if m.re is _CAPTION_LINE else (m.group(1), m.group(2))   # _CAPTION and _CAPTION_HEAD
    scale = {"millions": "M", "billions": "B", "thousands": "K"}[word.lower()]
    rest = (rest or "").lower()
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


_ENTITY = re.compile(r"(?:\b(?:Inc|Corp|Corporation|Company|Co|LLC|Ltd|plc|N\.A)\.?|\bL\.?\s?P\.?)$", re.I)
_STATEMENT_NO = re.compile(r"^(?:statement|exhibit|schedule)\s+[0-9IVX]{1,4}\b\s*", re.I)
_PERIOD_LINE = re.compile(r"^(?:for the )?(?:(?:fiscal )?years?|three months|six months|nine months|twelve months|quarters?) ended\b[^\d]*(?:\d{1,2},?\s*)?(?:(?:19|20)\d\d)?$"
                          r"|^(?:as of |at )?(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+(?:19|20)\d\d$", re.I)


_ROW_SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}


def _quarter_dates(rows: list) -> None:
    """Columns headed by fiscal quarters ("Q3-2025 ... Q3-2026": Tesla's update) take the end dates the same document prints
    over its balance sheet for the same run of quarters ("30-Sep-25 ... 30-Sep-26"), column by column, when both runs step
    one quarter at a time. Quarters that cannot be dated that way keep their printed names when each falls in its own year
    (Q3 2026 against Q3 2025); otherwise, and for trailing-twelve-month rows printed under quarters, the rows are dropped:
    a quarter's date is never guessed."""
    from datetime import date
    dated = {}
    for r in rows:
        ends = [c.get("end") for c in r["cols"]]
        if r["statement"] == "balance" and len(ends) >= 2 and all(ends):
            dated.setdefault(len(ends), ends)            # the first dated balance sheet of each width

    def steps(fq, ends):
        q = [int(x[:4]) * 4 + int(x[-1]) for x in fq]
        d = [date.fromisoformat(e) for e in ends]
        s = q[1] - q[0]
        return s in (1, -1) and all(b - a == s for a, b in zip(q, q[1:])) and all(80 <= (b - a).days * s <= 100 for a, b in zip(d, d[1:]))
    keep = []
    for r in rows:
        fq = [c.get("fq") for c in r["cols"]]
        if not any(fq):
            keep.append(r)
            continue
        if not all(fq) or re.search(r"\bTTM\b|trailing[ -]twelve", r["label"] + " " + r["heading"], re.I):
            continue
        ends = dated.get(len(fq))
        if ends and steps(fq, ends):
            r["cols"] = [dict(c, end=e, label=f"{_MON_ABBR[int(e[5:7]) - 1]} {int(e[8:10])}, {e[:4]}") for c, e in zip(r["cols"], ends)]
            keep.append(r)
        elif len({c["year"] for c in r["cols"]}) == len(fq):
            keep.append(r)
    rows[:] = keep


def _date_year_columns(rows: list, doc) -> None:
    """Columns headed by a year alone ("2025  2024  2023": Disney, Home Depot) take the end date and the length of the
    inline XBRL facts printed in them, voted over the table's rows: Home Depot's "2025" ended February 1, 2026. Without
    inline XBRL (a PDF) the year alone stays."""
    from collections import Counter
    from .facts import _duration_kind
    xb = [x for x in getattr(doc, "xbrl", None) or [] if not x.get("dims") and x.get("value") is not None and x.get("end")]
    if not xb:
        return
    by_val = {}
    for x in xb:
        by_val.setdefault(round(abs(x["value"]), 2), []).append(x)
    groups = {}
    for r in rows:
        if r["cols"] and any(c.get("end") is None and c.get("year") for c in r["cols"]):
            groups.setdefault(r["table_id"], []).append(r)
    for rs in groups.values():
        ncol = len(rs[0]["cols"])
        votes = [Counter() for _ in range(ncol)]
        for r in rs:
            if len(r["cols"]) != ncol:
                continue
            for k, v in enumerate(r["values"][:ncol]):
                year = r["cols"][k].get("year")
                if v is None or not year:
                    continue
                for val in {round(abs(v) * _ROW_SCALE.get(r.get("scale") or "", 1.0), 2), round(abs(v), 2)}:
                    for x in by_val.get(val, []):
                        if x["end"][:4] in (year, str(int(year) + 1)):
                            votes[k][(x["end"], "instant" if x.get("instant") else _duration_kind(x.get("start"), x["end"]))] += 1
        for k in range(ncol):
            if not votes[k]:
                continue
            (end, kind), n = votes[k].most_common(1)[0]
            if n < 2 or n * 2 < sum(votes[k].values()):
                continue                   # too few rows agree on one period
            y, mo, d = end.split("-")
            for r in rs:
                c = r["cols"][k]
                if c.get("end") is None:
                    c.update(end=end, label=f"{_MON_ABBR[int(mo) - 1]} {int(d)}, {y}", type=c.get("type") or ("" if kind in ("instant", "unknown") else kind))


_MON_ABBR = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


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
    comment_col = False
    groups = []      # [heading, [values of each row]] for the open sections of this table, in order
    label_head, prev_heading = None, ""
    entity = ""
    other_at, other_text = None, ""      # a paragraph saying the tables below are another entity's or an affiliate's, and where
    prose = False            # running text since the last statement title: the title was an MD&A heading, not the statement's
    titled = True            # the open table sits right under its statement title
    pos = 0
    for line in body.split("\n"):
        start = pos
        pos += len(line) + 1
        s = line.strip()
        if not s:
            continue
        ctx = " ".join(prev[-2:])
        if len(prev) >= 3 and re.match(r"(?i)ended\b", prev[-2]) and re.search(r"(?i)\b(?:months|quarters?|weeks|years?)\s*$", prev[-3]):
            # a period phrase broken before "Ended" over three lines (Simon's release: "For the Three Months" / "Ended June 30,
            # For the Six Months" / "Ended June 30,"): the two lines above lost the first period, so every column was six months
            ctx = f"{prev[-3]} {ctx}"
        prev.append(s)
        if len(s) > 60 and (_OTHER_ENTITY.search(s) or _AFFILIATE_SUMMARY.search(s)):
            other_at, other_text = len(prev) - 1, s
        if len(s) > 200 and (s.endswith(".") or re.search(r"[a-z]{3}[.;] [A-Z]", s)):
            prose = True
        if re.match(r"^(?:Note|NOTE)\s+\d{1,2}\b", s) and len(s) <= 120:
            title = s                                   # a note heading: tables below belong to the note
            entity = ""
            other_at, prose = None, False
        elif re.match(r"^See accompanying notes", s, re.I):
            title = ""
        elif (len(s) <= 110 and not s.endswith((".", ":")) and not re.search(r"\d{3}", s) and _statement_named(s) != "other"
              and re.match(r"^(?:(?:(?-i:[A-Z0-9][A-Z0-9 .,&'\u2019-]{2,70})|[A-Z][A-Za-z0-9 .,&'\u2019-]{1,60}?\b(?:Inc|Corp|Corporation|Company|Co|LLC|Ltd|plc|L\.\s?P|LP)\.?)\s+)?(?:unaudited\s+)?(?:joint\s+ventures?\s+)?(?:combined\s+)?(?:condensed\s+)?(?:consolidated\s+)?"
                           r"(?:statements?|balance|results of operations for the|financial position (?:at|as of))\b", s, re.I)):
            # a statement title opens its line (after an all-caps company name at most, so an MD&A heading
            # "Other Items Impacting FPL's Consolidated Statements of Income" is not one); a sentence
            # that mentions "the statements of income:" introduces a note table instead
            t2 = re.sub(r"^.*?((?:Condensed\s+)?(?:Consolidated\s+)?(?:Statements?|Balance Sheets?)\b)", r"\1", s, flags=re.I)
            # Simon Property's 10-Q: "Unaudited Consolidated Balance Sheets" (the word before is not a registrant's name)
            title = t2 if _statement_named(t2) != "other" else s     # "INCOME STATEMENTS": the words before are the title
            prose = False
            # the company's own statements end such a paragraph's reach; an affiliate's "COMBINED BALANCE SHEETS" carry it on,
            # and an affiliate's statement says so itself (Simon's release: "Unaudited Joint Venture Combined Statements of Operations")
            if re.search(r"\bjoint ventures?\b|\bequity[- ]method\b|\bunconsolidated\b", s, re.I):
                other_at, other_text = len(prev) - 1, s
            elif other_at is not None and re.search(r"\b(?:combined|condensed)\b", s, re.I) and not re.search(r"\bconsolidated\b", s, re.I):
                other_at = len(prev) - 1
            else:
                other_at = None
            # the registrant named just above ("FLORIDA POWER & LIGHT COMPANY"): a combined 10-K prints a
            # full set of statements for each registrant
            pre = s[:len(s) - len(t2)].strip()                # "Apple Inc.  CONSOLIDATED BALANCE SHEETS"
            entity = (pre if pre and _ENTITY.search(pre) else
                      prev[-2] if len(prev) > 1 and len(prev[-2]) <= 70 and _ENTITY.search(prev[-2]) else "")
            entity = _STATEMENT_NO.sub("", entity)          # "STATEMENT 3  Caterpillar Inc.": the number is not the registrant
        h = header_periods(s, ctx) if len(s) < 260 else None
        if h is None and re.fullmatch(r"(?:19|20)\d\d", s) and _YEAR.search(ctx):
            h = header_periods(f"{ctx}  {s}", "")     # Simon's release: "June 30," / "2026  December 31," / "2025", the dates broken across lines
        if h:
            hdr, hdr_age, heading = h, 0, ""
            table_id += 1
            # Chevron's MD&A has a heading "Consolidated Statement of Income" over paragraphs and small tables: a table after
            # running text under such a heading is not the statement (its first rows were cited instead of the statement's)
            titled = not prose
            row_index = 0
            groups = []
            # the sentence that introduces the table ("The following table shows
            # condensed income statements of the RRPF affiliates ... :")
            intro = next((x for x in reversed(prev[-6:-1]) if x.endswith(":") and len(x) > 30), "")
            if not intro and other_at is not None and len(prev) - other_at <= 30:
                intro = other_text
            restated = bool(re.search(r"as (adjusted|recast|revised|restated)|\brestated\b|\brecast\b", (ctx + " " + s).lower()))
            # the table's own caption: a short heading-like line just above
            # ("Rail North America Fleet Data"), which says whose figures these are
            block = next((x for x in reversed(prev[-7:-1]) if 3 <= len(x) <= 70 and not re.search(r"\d", x) and x[:1].isupper()
                          and not x.endswith((".", ":", ",")) and statement_of(x) == "other" and not re.match(r"\(", x)), "")
            tail = re.sub(r"^.*\b(19|20)\d\d\b", "", s).strip(" :")
            if tail and re.match(r"[A-Za-z]", tail):
                heading = tail
            # a text column after the periods ("2025  2024  Year over Year - Comparison"): a row may end in a comment
            comment_col = bool(tail and re.match(r"[A-Za-z]", tail) and len(tail.split()) >= 2)
            continue
        if hdr is not None and len(s) <= 60 and _PERIOD_LINE.match(s):
            hdr = None              # a new period line this reader cannot use as a header (NEE's issuer/guarantor
            continue                # table has entity columns): the rows below are not the previous table's
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
        label = re.sub(r"^[-\u2013\u2014\u2022]\s+(?=[A-Za-z])", "", " ".join(parts[:k]).strip(" :"))     # "- Diluted": a list dash
        toks = _cells(parts[k:], hdr["change_cols"])
        if (len(toks) == n + 1 or (len(toks) > n + 1 and re.search(r"[.,%]", toks[1]))) and re.fullmatch(r"\((?:\d|[a-z])\)", toks[0]) and label:
            toks = toks[1:]                 # a footnote marker after the label: "Utilization rate (1) 99.0%"
            label += f" {parts[k]}"
        elif (len(toks) == n + 1 and label and parts[k] == toks[0] and re.fullmatch(r"[1-9]", toks[0])
              and (parts[k + 1] == "$" or re.search(r"[.,]", toks[1]))):
            toks = toks[1:]                 # a bare footnote digit: "Profit 1  $ 8,884", "Diluted 2  472.3" (Caterpillar)
            label += f" {parts[k]}"
        if len(toks) > n and label and not hdr.get("slots") and not hdr["change_cols"]:
            # amounts inside the label, set off from the columns by the wide gap: "Property and equipment, net of accumulated
            # depreciation and amortization of $24,719 and $23,228  39,743  37,595" (Delta): the row was dropped
            segs = [x for x in re.split(r"\s{2,}", s.strip()) if x]
            vals = _cells(" ".join(segs[1:]).replace("$", " $ ").split(), False) if len(segs) > 1 else []
            if (len(vals) == n and (re.search(r"(?i)\b(?:of|and)\s+\$?\s?\d[\d,.]*", segs[0]) or re.search(r"\([^()]*\d[^()]*\)\s*$", segs[0]))
                    and all(_CELL.fullmatch(x) for x in " ".join(segs[1:]).replace("$", " $ ").split())):
                label, toks = segs[0].strip(" :"), vals       # also a closing bracket of amounts: Chevron's "Accounts and notes
                                                             # receivable (less allowance: 2025 - $176; 2024 - $259)  18,075  20,684"
        if comment_col and len(toks) != n and k > 0:
            # "Backlog value  $ 1,661.6  $ 2,145.5  Expect to deliver approximately 49% of our railcar backlog value during
            # 2026": the row's figures, then its comment (whose "2026" looked like a cell). The row was dropped (or, when short,
            # taken for a heading), and
            # the "Backlog value" of the sustainable conversions below it was read as Trinity's backlog (2026-10-26)
            segs = [x for x in re.split(r"\s{2,}", s.strip()) if x]
            for j in range(2, len(segs)):
                cells = " ".join(segs[1:j]).replace("$", " $ ").split()
                if re.match(r"[A-Za-z]", segs[j]) and cells and all(_CELL.fullmatch(x) for x in cells) and len(_cells(cells, False)) == n:
                    label, toks = re.sub(r"^[-\u2013\u2014\u2022]\s+(?=[A-Za-z])", "", segs[0]).strip(" :"), _cells(cells, False)
                    break
        if hdr.get("slots") and len(toks) == len(hdr["slots"]):
            toks = [t for t, kind in zip(toks, hdr["slots"]) if kind == "p"]       # a change column after each group of periods
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
            net = _net_of_less(rows, table_id, lead) if len(lead) == n else ""
            if net:
                label, toks = net, lead                # the line above net of the "Less" line; its section goes on
            elif len(lead) == n and heading:
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
        # "Earnings per share: Basic" -> heading "Earnings per share", label "Basic"; not a colon inside brackets
        # (Chevron: "Accounts and notes receivable (less allowance: 2025 - $176; 2024 - $259)")
        if ":" in label and label.rsplit(":", 1)[0].count("(") <= label.rsplit(":", 1)[0].count(")"):
            h2, label = [x.strip() for x in label.rsplit(":", 1)]
            if label:
                heading = h2
            else:
                label = h2
        cap = _caption(body, start)
        unit = _unit(label, heading, toks[0])
        scale = cap["scale"] if unit == "USD" else cap["shares_scale"] if unit == "shares" else ""
        if unit == "shares" and re.search(r"\(000['\u2019]?s\)|\(in thousands\)", heading + " " + label, re.I):
            scale = "K"                  # Costco: "Shares used in calculation (000's)" under a millions caption
        full = (f"{heading}: {label}" if heading and label and len(label.split()) <= 3 and _GENERIC.match(label.split()[0])
                and not label.lower().startswith(heading.lower()) else label)
        if unlabeled:
            # The arithmetic-resolved section can precede the nearest subheading.
            # Keep the extraction label/key intact; name the displayed total by it.
            sect = label[len("Total "):].strip() if label.startswith("Total ") else ""
            full = f"{sect}: Total" if sect else label
        loc = doc.location(start)
        statement = statement_of(title) if titled else "other"
        cols = hdr["cols"]
        if statement == "balance":
            cols = [dict(c, type="instant") for c in cols]
        rows.append({
            "label": label, "full_label": full, "key": row_key(label, statement in ("income", "balance", "cashflow", "comprehensive", "equity")),
            "heading": heading, "values": vals, "raw": toks, "cols": cols, "header_text": hdr["text"],
            "unit": unit, "scale": scale, "caption": cap["text"], "table_title": title,
            "statement": statement, "unlabeled_total": unlabeled, "footnote": footnote, "block": block,
            "intro": intro[:240], "scope": scope_of(intro, title, block), "restated": restated, "entity": entity,
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
            rows.append(dict(rows[-1], label=tl, full_label=(f"{tl[len('Total '):].strip()}: Total" if tl[len('Total '):].strip() else tl), key=row_key(tl),
                             values=[parse_number(t) for t in fused], raw=fused, unlabeled_total=True, row_index=row_index))
            row_index += 1
            groups = []
            close_after = True
        # A total closes its section; so does a subtotal named like the heading it closes
        # ("Finance lease cost:" ... "Finance lease cost  3,596"), or the rows after it inherit
        # that heading (Amazon FY2025 p.55: "Total lease cost" read as a finance lease line).
        names_heading = heading and label.strip(" :").lower() == heading.strip(" :").lower()
        if close_after or label.lower().startswith("total ") or names_heading:
            heading = next_heading or ""
    # a combined statement of income and comprehensive income ("Consolidated
    # Statements of Comprehensive Income" running from revenue to EPS) is the
    # income statement
    combined = {r["table_id"] for r in rows if r["statement"] == "comprehensive"
                and re.match(r"^(total )?(net )?(revenues?|sales|net sales|operating revenues?)$", r["key"])}
    for r in rows:
        if r["table_id"] in combined:
            r["statement"] = "income"
    _quarter_dates(rows)
    _date_year_columns(rows, doc)
    # the first registrant whose statements are printed is the filer; another registrant's
    # statements (NEE's FPL, Prologis Inc.'s operating partnership L.P.) are a subsidiary's
    key = lambda e: re.sub(r"[^a-z0-9]", "", e.lower())
    first = next((key(r["entity"]) for r in rows if r["statement"] != "other" and r["entity"]), "")
    first_stmt = next((r for r in rows if r["statement"] != "other"), None)
    filer = key((getattr(doc, "dei", None) or {}).get("EntityRegistrantName") or "")
    if first_stmt is not None and not first_stmt["entity"] and filer:
        first = filer        # the filer's own statements name no entity (Simon Property Group, Inc.); a later set that does is another's
    for r in rows:
        if first and r["statement"] != "other" and r["entity"] and key(r["entity"]) != first and r["scope"] not in ("affiliate", "segment", "supplemental"):
            r["scope"], r["block"], r["other_registrant"] = "subsidiary", r["entity"], True
    return rows
