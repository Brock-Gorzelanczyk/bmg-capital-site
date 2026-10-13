"""BMG Capital company report from one filing.

Built from the reader's extracted text (one string, pages folded into rows,
page start offsets known). Everything here is deterministic: a figure is
only reported when a table row or a sentence in the filing carries it, with
its period, scale and page. Missing items say "Not found in the reviewed
sources"; calculated items carry their formula.

    build_report(body, page_starts, title, ticker) -> dict
"""
from __future__ import annotations

import re
from typing import Optional

NOT_FOUND = "Not found in the reviewed sources"

_YEAR = re.compile(r"\b(19|20)\d\d\b")
_DATE = re.compile(r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+((?:19|20)\d\d)\b")
_NUM = re.compile(r"\(?-?\$?\s?\d[\d,]*(?:\.\d+)?\)?%?|—|(?<!\w)-(?!\w)")
_CELL = re.compile(r"\(?-?\$?\d[\d,]*(?:\.\d+)?\)?%?|\$|%|\u2014|\u2013|-")
_SCALE = re.compile(r"\(\s*(?:dollars |amounts |\$ )?in (millions|billions|thousands)", re.I)


def _page_of(page_starts: list, off: int) -> Optional[int]:
    pg = None
    for ps, pn in page_starts:
        if ps <= off:
            pg = pn
        else:
            break
    return pg


def _num(tok: str) -> Optional[float]:
    t = tok.strip()
    if t in ("—", "-", "–"):
        return 0.0
    neg = t.startswith("(") and t.endswith(")") or t.startswith("-")
    t = re.sub(r"[^\d.]", "", t)
    if not t or t == ".":
        return None
    try:
        v = float(t)
    except ValueError:
        return None
    return -v if neg else v


def _scale_before(body: str, off: int) -> str:
    hits = list(_SCALE.finditer(body[max(0, off - 5000):off]))
    return {"millions": "M", "billions": "B", "thousands": "K"}[hits[-1].group(1).lower()] if hits else ""


# ----------------------------------------------------------------------
# Tables: header lines with periods, then rows "label  v1 v2 v3"
# ----------------------------------------------------------------------
_PHRASE = re.compile(r"\b(three|six|nine|twelve|3|6|9|12)[ -]months? ended\s+([A-Z][a-z]+\.? \d{1,2})|\b(years?|fiscal years?) ended\s+([A-Z][a-z]+\.? \d{1,2})", re.I)
_KIND = {"three": "Q", "3": "Q", "six": "6M", "6": "6M", "nine": "9M", "9": "9M", "twelve": "FY", "12": "FY"}


def _header_periods(line: str, context: str) -> Optional[dict]:
    """A column header: two or more period columns on one line. `context`
    is the line(s) just above, which may carry "Three months ended May 31"."""
    years = [m.group(0) for m in _YEAR.finditer(line)]
    if len(years) < 2:
        return None
    other = [t for t in _NUM.findall(line) if re.sub(r"[^\d]", "", t) and not _YEAR.fullmatch(re.sub(r"[^\d]", "", t))]
    other = [t for t in other if not re.fullmatch(r"\d{1,2},?", t.strip())]
    if len(other) > 1:
        return None
    # the header must be mostly dates and period words, not a sentence
    words = re.findall(r"[A-Za-z]+", line)
    if len([w for w in words if w.lower() not in {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december", "change", "years", "year", "ended", "months", "three", "six", "nine", "twelve", "fiscal", "as", "of", "and", "at", "net", "sales"}]) > 4:
        return None
    low = line.lower()
    has_change = "change" in low
    dates = [(m.group(1)[:3] + " " + m.group(2) + ", " + m.group(3)) for m in _DATE.finditer(line)]
    if len(dates) >= 2:
        k = ("Q" if "three months" in low else "9M" if "nine months" in low else "6M" if "six months" in low
             else "FY" if ("year" in low or "fiscal" in low) else "")
        return {"periods": dates, "years": [d[-4:] for d in dates], "kinds": [k] * len(dates), "change_cols": has_change}
    phrases = [(_KIND.get((m.group(1) or "").lower(), "FY") if m.group(1) else "FY", (m.group(2) or m.group(4) or "").replace(".", "")) for m in _PHRASE.finditer(context + " " + line)]
    if phrases and len(years) % len(phrases) == 0:
        per = len(years) // len(phrases)
        labels, kinds = [], []
        for g, (k, md) in enumerate(phrases):
            for y in years[g * per:(g + 1) * per]:
                labels.append(f"{md[:3]} {md.split()[-1]}, {y}" if md else y)
                kinds.append(k)
        return {"periods": labels, "years": years, "kinds": kinds, "change_cols": has_change}
    k = "FY" if ("year" in low or "fiscal" in low or "year" in context.lower()) else ""
    return {"periods": years, "years": years, "kinds": [k] * len(years), "change_cols": has_change}


def _cells(parts: list, n: int, change_cols: bool) -> list:
    toks = []
    for j, t in enumerate(parts):
        if t in ("$", "%"):
            continue
        if change_cols and (t.endswith("%") or (j + 1 < len(parts) and parts[j + 1] == "%")):
            continue
        toks.append(t)
    return toks


def parse_tables(body: str, page_starts: list) -> list[dict]:
    rows: list[dict] = []
    hdr = None
    hdr_age = 0
    heading = ""
    prev_lines: list[str] = []
    pos = 0
    for line in body.split("\n"):
        start = pos
        pos += len(line) + 1
        s = line.strip()
        if not s:
            continue
        ctx = " ".join(prev_lines[-2:])
        prev_lines.append(s)
        h = _header_periods(s, ctx) if len(s) < 260 else None
        if h:
            hdr, hdr_age, heading = h, 0, ""
            tail = re.sub(r"^.*\b(19|20)\d\d\b", "", s).strip(" :")
            if tail and re.match(r"[A-Za-z]", tail):
                heading = tail
            continue
        hdr_age += 1
        if hdr is None or hdr_age > 90 or len(s) > 320:
            if hdr_age > 90:
                hdr = None
            continue
        n = len(hdr["periods"])
        parts = s.replace("$", " $ ").split()
        # numbers on the right: "label  v1 v2 v3"
        k = len(parts)
        while k > 0 and _CELL.fullmatch(parts[k - 1]):
            k -= 1
        label = " ".join(parts[:k]).strip(" :")
        toks = _cells(parts[k:], n, hdr["change_cols"])
        if k == 0 or not re.match(r"[A-Za-z(]", label or "x"):
            # numbers first: an unlabeled total ("576.5 842.7 ..."), maybe
            # followed by the next section's heading on the same line
            j = 0
            while j < len(parts) and _CELL.fullmatch(parts[j]):
                j += 1
            lead = _cells(parts[:j], n, hdr["change_cols"])
            if len(lead) == n and heading:
                label, toks = "Total " + heading, lead
                rest = " ".join(parts[j:]).strip(" :")
                if rest and re.match(r"[A-Za-z]", rest):
                    heading = rest
            else:
                continue
        elif not toks:
            # a text line inside a table: a section heading ("Revenue", "Assets")
            if len(s) <= 70 and re.match(r"[A-Z]", s) and not s.endswith("."):
                heading = s.strip(" :")
            continue
        if len(toks) < 2 or len(toks) != n:
            continue
        vals = [_num(t) for t in toks]
        if any(v is None for v in vals):
            continue
        rows.append({
            "label": label, "key": re.sub(r"[^a-z0-9]+", " ", label.lower()).strip(),
            "values": vals, "raw": toks, "heading": heading,
            "periods": hdr["periods"], "years": hdr["years"], "kinds": hdr["kinds"], "kind": hdr["kinds"][0] if hdr["kinds"] else "",
            "scale": _scale_before(body, start), "offset": start, "page": _page_of(page_starts, start),
            "line": s[:200],
        })
    return rows


# ----------------------------------------------------------------------
# Core financial metrics
# ----------------------------------------------------------------------
CORE = [
    ("revenue", "Revenue", r"^(total net sales|total revenues?|total net revenues?|net sales|revenues?|net revenues?|total revenue)$"),
    ("gross_profit", "Gross profit", r"^(gross margin|gross profit|margin)$"),
    ("operating_income", "Operating income", r"^(operating income|income from operations|operating income loss|earnings from operations)$"),
    ("net_income", "Net income", r"^(net income|net earnings)( attributable to [a-z .]+)?$"),
    ("eps_diluted", "Diluted EPS", r"^(diluted|diluted earnings per (common )?share|diluted net income per share)$"),
    ("cash", "Cash and cash equivalents", r"^cash and cash equivalents$"),
    ("total_assets", "Total assets", r"^total assets$"),
    ("total_liabilities", "Total liabilities", r"^total liabilities$"),
    ("equity", "Shareholders' equity", r"^total (share|stock)holders( s)? equity$|^total equity$"),
    ("cfo", "Operating cash flow", r"^(cash generated by operating activities|net cash (provided by|from|generated by) operating activities)$"),
    ("capex", "Capital expenditures", r"^(payments for acquisition of property plant and equipment|capital expenditures|purchases of property( plant)? and equipment|capital expenditures for property and equipment)$"),
    ("buybacks", "Share repurchases", r"^(repurchases of common stock|repurchase of common stock|purchases of treasury stock)$"),
    ("dividends", "Dividends paid", r"^(payments for dividends and dividend equivalents|dividends paid|cash dividends paid|payments of dividends)$"),
]
DEBT_ROWS = r"^(commercial paper|term debt|long term debt|current portion of long term debt|short term debt|notes payable|short term borrowings|revolving notes|senior notes)$"


def _pick(rows: list, pat: str, prefer_kind: str = "") -> Optional[dict]:
    rx = re.compile(pat)
    hits = [r for r in rows if rx.search(r["key"])]
    if not hits:
        return None
    if prefer_kind:
        k = [r for r in hits if r["kind"] == prefer_kind]
        if k:
            hits = k
    # statements first: the row with the most columns, then earliest in the filing
    hits.sort(key=lambda r: (-len(r["values"]), r["offset"]))
    return hits[0]


def _pick_near_bs(rows: list, pat: str) -> Optional[dict]:
    """A balance-sheet line: same columns as Total assets and close to it."""
    ta = _pick(rows, r"^total assets$")
    rx = re.compile(pat)
    if ta:
        near = [r for r in rows if rx.search(r["key"]) and r["periods"] == ta["periods"] and abs(r["offset"] - ta["offset"]) < 6000]
        if near:
            return near[0]
    return _pick(rows, pat)


def _fmt(v: Optional[float], scale: str, kind: str = "amount") -> str:
    if v is None:
        return NOT_FOUND
    if kind == "pct":
        return f"{v:.1f}%"
    if kind == "eps":
        return f"${v:,.2f}"
    mult = {"K": 1e3, "M": 1e6, "B": 1e9}.get(scale, 1)
    x = v * mult
    a = abs(x)
    s = (f"${a / 1e12:.2f}T" if a >= 1e12 else f"${a / 1e9:.1f}B" if a >= 1e9 else f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a:,.0f}")
    return f"({s})" if x < 0 else s


def _chg(cur: Optional[float], prev: Optional[float]) -> Optional[float]:
    if cur is None or prev in (None, 0):
        return None
    return (cur - prev) / abs(prev) * 100


def _period_label(row: dict, i: int) -> str:
    p = row["periods"][i] if i < len(row["periods"]) else ""
    ks = row.get("kinds") or []
    k = ks[i] if i < len(ks) else (row.get("kind") or "")
    if k == "FY":
        return f"FY{row['years'][i]}"
    if k in ("Q", "9M", "6M"):
        return f"{ {'Q': '3 months', '9M': '9 months', '6M': '6 months'}[k]} ended {p}"
    return p


def financials(rows: list) -> dict:
    out: dict = {}
    for key, name, pat in CORE:
        r = _pick(rows, pat, "")
        if not r:
            out[key] = {"name": name, "found": False}
            continue
        kind = "eps" if key == "eps_diluted" else "amount"
        cur, prev = r["values"][0], (r["values"][1] if len(r["values"]) > 1 else None)
        out[key] = {"name": name, "found": True, "cur": cur, "prev": prev, "scale": "" if kind == "eps" else r["scale"],
                    "kind": kind, "cur_txt": _fmt(cur, r["scale"], kind), "prev_txt": _fmt(prev, r["scale"], kind) if prev is not None else "",
                    "chg": _chg(cur, prev), "period": _period_label(r, 0), "prev_period": _period_label(r, 1) if prev is not None else "",
                    "page": r["page"], "row": r["label"], "raw": r["raw"][0], "line": r["line"]}
    # debt: a "Total debt" line if the balance sheet has one, else the sum
    # of the debt lines in the balance sheet (same columns as total assets)
    ta = _pick(rows, CORE[6][2])
    td = _pick_near_bs(rows, r"^total debt( net)?$")
    if td:
        out["debt"] = {"name": "Total debt", "found": True, "cur": td["values"][0], "prev": td["values"][1] if len(td["values"]) > 1 else None,
                       "scale": td["scale"], "kind": "amount", "cur_txt": _fmt(td["values"][0], td["scale"]),
                       "prev_txt": _fmt(td["values"][1], td["scale"]) if len(td["values"]) > 1 else "",
                       "chg": _chg(td["values"][0], td["values"][1] if len(td["values"]) > 1 else None),
                       "period": _period_label(td, 0), "prev_period": _period_label(td, 1), "page": td["page"], "row": td["label"]}
    elif ta:
        drows = [r for r in rows if re.search(DEBT_ROWS, r["key"]) and r["periods"] == ta["periods"] and abs(r["offset"] - ta["offset"]) < 6000]
        if drows:
            cur = sum(r["values"][0] for r in drows)
            prev = sum(r["values"][1] for r in drows) if all(len(r["values"]) > 1 for r in drows) else None
            out["debt"] = {"name": "Total debt", "found": True, "cur": cur, "prev": prev, "scale": ta["scale"], "kind": "amount",
                           "cur_txt": _fmt(cur, ta["scale"]), "prev_txt": _fmt(prev, ta["scale"]) if prev is not None else "",
                           "chg": _chg(cur, prev), "period": _period_label(ta, 0), "prev_period": _period_label(ta, 1),
                           "page": drows[0]["page"], "formula": " + ".join(r["label"] for r in drows), "calc": True}
    if "debt" not in out:
        out["debt"] = {"name": "Total debt", "found": False}
    # calculated
    def ratio(num_k, den_k, name):
        a, b = out.get(num_k, {}), out.get(den_k, {})
        if not (a.get("found") and b.get("found")) or not b.get("cur"):
            return {"name": name, "found": False}
        cur = a["cur"] / b["cur"] * 100
        prev = a["prev"] / b["prev"] * 100 if a.get("prev") is not None and b.get("prev") else None
        return {"name": name, "found": True, "calc": True, "kind": "pct", "cur": cur, "prev": prev, "cur_txt": f"{cur:.1f}%",
                "prev_txt": f"{prev:.1f}%" if prev is not None else "", "chg_pp": (cur - prev) if prev is not None else None,
                "period": a["period"], "prev_period": a.get("prev_period", ""), "page": a["page"],
                "formula": f"{a['name']} / {b['name']}"}
    out["gross_margin_pct"] = ratio("gross_profit", "revenue", "Gross margin")
    out["operating_margin_pct"] = ratio("operating_income", "revenue", "Operating margin")
    out["net_margin_pct"] = ratio("net_income", "revenue", "Net margin")
    c, x = out.get("cfo", {}), out.get("capex", {})
    if c.get("found") and x.get("found"):
        cur = c["cur"] - abs(x["cur"])
        prev = c["prev"] - abs(x["prev"]) if c.get("prev") is not None and x.get("prev") is not None else None
        out["fcf"] = {"name": "Free cash flow", "found": True, "calc": True, "kind": "amount", "cur": cur, "prev": prev, "scale": c["scale"],
                      "cur_txt": _fmt(cur, c["scale"]), "prev_txt": _fmt(prev, c["scale"]) if prev is not None else "",
                      "chg": _chg(cur, prev), "period": c["period"], "prev_period": c.get("prev_period", ""), "page": c["page"],
                      "formula": "Operating cash flow - Capital expenditures"}
    else:
        out["fcf"] = {"name": "Free cash flow", "found": False}
    return out


# ----------------------------------------------------------------------
# Breakdown tables: rows above a "Total net sales / Total revenue" row
# ----------------------------------------------------------------------
_GEO = re.compile(r"americas|europe|greater china|china|japan|asia|pacific|emea|united states|\bu\.?s\.?\b|international|north america|latin america|rest of|other countries|foreign", re.I)


def breakdowns(rows: list) -> list[dict]:
    out, seen = [], set()
    for i, r in enumerate(rows):
        if not re.search(r"^(total net sales|total revenues?|total net revenues?)$", r["key"]):
            continue
        parts = []
        j = i - 1
        while j >= 0 and rows[j]["periods"] == r["periods"] and r["offset"] - rows[j]["offset"] < 3000 and len(parts) < 12:
            if re.search(r"^total|cost of|^net sales$", rows[j]["key"]):
                break
            parts.append(rows[j]); j -= 1
        parts.reverse()
        if len(parts) < 2:
            continue
        tot = sum(p["values"][0] for p in parts)
        if r["values"][0] and abs(tot - r["values"][0]) / abs(r["values"][0]) > 0.02:
            continue                     # rows do not add up to the total: not a breakdown
        sig = tuple(p["key"] for p in parts)
        if sig in seen:
            continue
        seen.add(sig)
        geo = sum(1 for p in parts if _GEO.search(p["label"])) >= len(parts) / 2
        out.append({
            "title": "Net sales by region" if geo else "Net sales by product or segment",
            "period": _period_label(r, 0), "prev_period": _period_label(r, 1) if len(r["values"]) > 1 else "",
            "page": parts[0]["page"], "scale": r["scale"],
            "items": [{"name": p["label"], "cur": p["values"][0], "prev": p["values"][1] if len(p["values"]) > 1 else None,
                       "cur_txt": _fmt(p["values"][0], r["scale"]), "share": p["values"][0] / r["values"][0] * 100 if r["values"][0] else None,
                       "chg": _chg(p["values"][0], p["values"][1] if len(p["values"]) > 1 else None), "page": p["page"]} for p in parts],
            "total_txt": _fmt(r["values"][0], r["scale"]),
        })
    best: dict = {}
    for b in out:
        k = b["title"]
        if k not in best or len(b["items"]) > len(best[k]["items"]):
            best[k] = b
    return [best[k] for k in ("Net sales by product or segment", "Net sales by region") if k in best]


# ----------------------------------------------------------------------
# Industry metrics (when disclosed), from rows and sentences
# ----------------------------------------------------------------------
PROFILES = {
    "Railcar leasing and manufacturing": (r"railcar|tank car|freight car", [
        ("Fleet size", r"(?:wholly owned |total )?fleet (?:of|was|totaled|consisted of|size)[^.]{0,80}?([\d,]{3,}) (?:railcars|cars)"),
        ("Fleet utilization", r"utilization[^.%]{0,60}?(\d{2}(?:\.\d)?)%"),
        ("Lease price index", r"lease price index[^.%]{0,80}?(-?\d+(?:\.\d)?)%"),
        ("Deliveries", r"deliver(?:ies|ed)[^.]{0,60}?([\d,]{3,}) (?:units|railcars)"),
        ("New orders", r"orders? (?:for|of|totaling|received)[^.]{0,40}?([\d,]{3,}) (?:units|railcars)"),
        ("Backlog", r"backlog[^.]{0,80}?([\d,]{3,}) (?:units|railcars)"),
        ("Backlog value", r"backlog[^.]{0,120}?\$\s?([\d.,]+)\s?(billion|million)"),
    ]),
    "Software and subscriptions": (r"subscription|software|cloud|saas", [
        ("Remaining performance obligations", r"remaining performance obligations?[^.]{0,120}?\$\s?([\d.,]+)\s?(billion|million)"),
        ("Annual recurring revenue", r"annual(?:ized)? recurring revenue[^.]{0,80}?\$\s?([\d.,]+)\s?(billion|million)"),
        ("Net revenue retention", r"net (?:revenue|dollar) retention[^.%]{0,60}?(\d{2,3})%"),
        ("Customers", r"([\d,]{3,}) (?:paying )?customers"),
        ("Stock-based compensation", r"share-based compensation expense[^.]{0,60}?\$\s?([\d.,]+)\s?(billion|million)"),
    ]),
    "Consumer products and services": (r"iphone|consumer|retail stores|devices", [
        ("Inventories", r"^inventories$"),
        ("Gross margin percentage", r"^(total )?gross margin percentage$"),
        ("Installed base / active devices", r"active (?:devices|installed base)[^.]{0,80}?([\d.,]+)\s?(billion|million)"),
        ("Share-based compensation", r"^share based compensation expense$"),
    ]),
    "Banking": (r"net interest income|deposits|loans held", [
        ("Net interest margin", r"net interest margin[^.%]{0,60}?(\d(?:\.\d+)?)%"),
        ("Total deposits", r"^total deposits$"),
        ("CET1 ratio", r"common equity tier 1[^.%]{0,80}?(\d{1,2}(?:\.\d)?)%"),
    ]),
}


def industry_metrics(body: str, rows: list, page_starts: list) -> dict:
    low = body.lower()
    scores = {name: len(re.findall(rx, low)) for name, (rx, _) in PROFILES.items()}
    prof = max(scores, key=scores.get)
    if scores[prof] < 8:
        return {"profile": "General", "items": []}
    items = []
    for name, pat in PROFILES[prof][1]:
        if pat.startswith("^"):
            r = _pick_near_bs(rows, pat) if name in ("Inventories", "Total deposits") else _pick(rows, pat)
            if r:
                pct = r["raw"][0].endswith("%")
                f = (lambda v: f"{v:.1f}%") if pct else (lambda v: _fmt(v, r["scale"]))
                items.append({"name": name, "value": f(r["values"][0]), "period": _period_label(r, 0),
                              "prev": f(r["values"][1]) if len(r["values"]) > 1 else "", "page": r["page"], "source": r["line"]})
            else:
                items.append({"name": name, "value": NOT_FOUND})
            continue
        m = re.search(pat, body, re.I)
        if not m or not m.groups():
            items.append({"name": name, "value": NOT_FOUND})
            continue
        v = m.group(1)
        unit = m.group(2) if m.lastindex and m.lastindex >= 2 else ""
        txt = (f"${v} {unit}" if unit else (v + "%" if "%" in m.group(0)[m.end(1) - m.start(0):m.end(1) - m.start(0) + 1] or pat.endswith("%)") else v)).strip()
        s0 = body.rfind(". ", 0, m.start()) + 2
        s1 = body.find(". ", m.end())
        items.append({"name": name, "value": txt, "page": _page_of(page_starts, m.start()),
                      "source": re.sub(r"\s+", " ", body[s0:(s1 if s1 > 0 else m.end()) + 1])[:260]})
    return {"profile": prof, "items": items}


# ----------------------------------------------------------------------
# Company snapshot, risks
# ----------------------------------------------------------------------
def snapshot(body: str, page_starts: list, ticker: Optional[str]) -> dict:
    head = body[:12000]
    name = ""
    m = re.search(r"\n([^\n]{2,80})\n\s*\(Exact name of (?:registrant|Registrant)", head)
    if m:
        name = m.group(1).strip()
    form = ""
    m = re.search(r"FORM\s+(10-K|10-Q|20-F|40-F|8-K)", head, re.I)
    if m:
        form = m.group(1).upper()
    period = ""
    m = re.search(r"for the (fiscal year|quarterly period) ended\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", head, re.I)
    if m:
        period = ("Fiscal year ended " if m.group(1).lower() == "fiscal year" else "Quarter ended ") + re.sub(r"\s+", " ", m.group(2))
    exch = ""
    m = re.search(r"(The Nasdaq Stock Market LLC|New York Stock Exchange|NYSE|Nasdaq Global Select Market)", head)
    if m:
        exch = m.group(1)
    # business description: the first sentences after the Item 1 heading
    # (second occurrence; the first is the table of contents)
    desc, desc_page = "", None
    starts = [m.start() for m in re.finditer(r"Item 1\.\s+Business", body)]
    for st in (starts[1:] + starts[:1]):
        seg = re.sub(r"\s+", " ", body[st:st + 5000])
        seg = re.sub(r"^Item 1\.\s+Business\s*", "", seg)
        sents = [re.sub(r"^(Company Background|Business Overview|Overview|General|Background|Our Company|Company Overview)\s+", "", x.strip()) for x in re.split(r"(?<=[.])\s+(?=[A-Z])", seg)]
        good = [x for x in sents if 40 <= len(x) <= 400 and re.match(r"(The Company|We |Our |[A-Z][a-z]+ (Inc|Corp|Corporation|Company)\b)", x) and not re.search(r"forward-looking|fiscal year is", x, re.I)]
        if good:
            i0 = sents.index(good[0])
            nxt = [x for x in sents[i0 + 1:i0 + 3] if x in good]
            desc = " ".join([good[0]] + nxt[:1])[:800]
            desc_page = _page_of(page_starts, st)
            break
    segs, seg_page = "", None
    flat = re.sub(r"\s+", " ", body)
    for pat in (r"[^.]{0,300}\b(?:reportable|operating) segments (?:consist of|are|include|:)[^.]{0,300}\.",
                r"[^.]{0,200}\bmanages its business primarily on a [a-z]+ basis[^.]{0,300}\.",
                r"[^.]{0,200}\boperates? in (?:two|three|four|five|\d) (?:reportable )?segments[^.]{0,300}\.",
                r"\bthe ((?:[A-Z][\w&,'\u2019-]*\s?)+(?:and (?:[A-Z][\w&,'\u2019-]*\s?)+)?)reportable segments\b"):
        m = next((x for x in re.finditer(pat, flat) if not (x.groups() and re.search(r"Company|Registrant", x.group(1)))), None)
        if m:
            segs = (f"Reportable segments: {m.group(1).strip()}." if m.groups() else m.group(0).strip())[:450]
            k = body.find(segs[:30].strip())
            seg_page = _page_of(page_starts, k) if k >= 0 else None
            break
    return {"name": name or NOT_FOUND, "ticker": ticker or NOT_FOUND, "form": form or NOT_FOUND, "period": period or NOT_FOUND,
            "exchange": exch or NOT_FOUND, "description": desc or NOT_FOUND, "description_page": desc_page,
            "segments": segs or NOT_FOUND, "segments_page": seg_page}


def risks(body: str, page_starts: list) -> list[dict]:
    out = []
    starts = [m.start() for m in re.finditer(r"Item 1A\.\s+Risk Factors", body)]
    if starts:
        st = starts[1] if len(starts) > 1 else starts[0]
        en = body.find("Item 1B", st + 100)
        en = en if en > 0 else st + 60000
        sec = body[st:en]
        pos = 0
        prev = ""
        for line in sec.split("\n"):
            s = line.strip()
            o = st + pos
            pos += len(line) + 1
            starts_para = (not prev) or prev.endswith((".", ":")) or len(prev) < 60
            ok = (starts_para and 50 <= len(s) <= 240 and s.endswith(".") and s[0].isupper() and s.count(". ") == 0
                  and not s.lower().startswith(("the following", "this section", "see ", "as a result", "references"))
                  and re.search(r"\b(could|may|might|depend|depends|risk|risks|adversely|subject to|exposed|impact|impacted|uncertain|volatil|harm|fail|compet)", s, re.I))
            prev = s
            if ok:
                out.append({"kind": "Risk factor", "text": s, "page": _page_of(page_starts, o)})
            if len([x for x in out if x["kind"] == "Risk factor"]) >= 6:
                break
    sents = [(m.start(), m.group(0)) for m in re.finditer(r"[^.\n]{20,600}\.", body)]
    for kind, key, pat in (("Concentration", "% of", r"(?:accounted for|represented) (?:approximately )?\d{1,2}% of (?:the Company.s )?(?:net sales|total revenue|revenues?|trade receivables)"),
                           ("Debt maturities", "matur", r"matur\w+.*\$\s?[\d.,]+\s?(?:billion|million)"),
                           ("Single source", "single", r"single(?:-| )source"),
                           ("Going concern / weakness", "", r"going concern|material weakness")):
        for off, sent in sents:
            if key and key not in sent.lower():
                continue
            if re.search(pat, sent, re.I):
                out.append({"kind": kind, "text": re.sub(r"\s+", " ", sent).strip()[:320], "page": _page_of(page_starts, off)})
                break
    return out


def takeaways(fin: dict, bds: list) -> list[dict]:
    t = []
    rv = fin.get("revenue", {})
    if rv.get("found") and rv.get("chg") is not None:
        txt = f"Revenue {'rose' if rv['chg'] >= 0 else 'fell'} {abs(rv['chg']):.1f}% to {rv['cur_txt']} ({rv['period']} vs {rv['prev_period']})"
        prod = next((b for b in bds if b["title"].endswith("segment")), None) or (bds[0] if bds else None)
        if prod:
            movers = [i for i in prod["items"] if i.get("chg") is not None and i.get("prev")]
            if movers:
                # the line that moved revenue most in the same direction
                if rv["chg"] >= 0:
                    top = max(movers, key=lambda i: i["cur"] - i["prev"])
                    if top["cur"] > top["prev"]:
                        txt += f", led by {top['name']} ({top['chg']:+.0f}%)"
                else:
                    top = min(movers, key=lambda i: i["cur"] - i["prev"])
                    if top["cur"] < top["prev"]:
                        txt += f", mainly {top['name']} ({top['chg']:+.0f}%)"
        t.append({"text": txt + ".", "page": rv.get("page")})
    om = fin.get("operating_margin_pct", {})
    if om.get("found") and om.get("chg_pp") is not None:
        t.append({"text": f"Operating margin {'widened' if om['chg_pp'] >= 0 else 'narrowed'} {abs(om['chg_pp']):.1f} points to {om['cur_txt']} (formula: {om['formula']}).", "page": om.get("page")})
    ni, eps = fin.get("net_income", {}), fin.get("eps_diluted", {})
    if ni.get("found") and ni.get("chg") is not None:
        s = f"Net income {'grew' if ni['chg'] >= 0 else 'declined'} {abs(ni['chg']):.1f}% to {ni['cur_txt']}"
        if eps.get("found") and eps.get("chg") is not None:
            s += f"; diluted EPS {eps['cur_txt']} ({eps['chg']:+.1f}%)"
        t.append({"text": s + ".", "page": ni.get("page")})
    f, bb, dv = fin.get("fcf", {}), fin.get("buybacks", {}), fin.get("dividends", {})
    if f.get("found"):
        s = f"Free cash flow was {f['cur_txt']} (operating cash flow less capital expenditures)"
        ret = sum(abs(x["cur"]) for x in (bb, dv) if x.get("found"))
        if ret and (bb.get("found") or dv.get("found")):
            s += f"; {_fmt(ret, (bb if bb.get('found') else dv)['scale'])} returned through repurchases and dividends"
        t.append({"text": s + ".", "page": f.get("page")})
    c, d = fin.get("cash", {}), fin.get("debt", {})
    if c.get("found") and d.get("found"):
        net = c["cur"] - d["cur"]
        t.append({"text": f"Cash of {c['cur_txt']} against total debt of {d['cur_txt']}: net {'cash' if net >= 0 else 'debt'} of {_fmt(abs(net), c['scale'])} (formula: Cash and cash equivalents - Total debt).", "page": d.get("page")})
    return t[:5]


def build_report(body: str, page_starts: list, title: str, ticker: Optional[str]) -> dict:
    rows = parse_tables(body, page_starts)
    snap = snapshot(body, page_starts, ticker)
    if snap["form"] in ("10-K", "20-F", "40-F"):
        # an annual report: a table without its own period words is fiscal years
        for r in rows:
            r["kinds"] = [k or "FY" for k in r["kinds"]]
            r["kind"] = r["kinds"][0] if r["kinds"] else "FY"
    fin = financials(rows)
    bds = breakdowns(rows)
    if snap["segments"] == NOT_FOUND:
        seg_bd = next((b for b in bds if b["title"].endswith("segment")), None)
        if seg_bd:
            snap["segments"] = "Revenue lines reported in the filing: " + ", ".join(i["name"] for i in seg_bd["items"]) + "."
            snap["segments_page"] = seg_bd["page"]
    return {
        "brand": "BMG Capital",
        "title": title,
        "snapshot": snap,
        "takeaways": takeaways(fin, bds),
        "financials": fin,
        "breakdowns": bds,
        "industry": industry_metrics(body, rows, page_starts),
        "risks": risks(body, page_starts),
        "rows_parsed": len(rows),
        "not_found": NOT_FOUND,
    }


def report_markdown(rep: dict, source_name: str, read_at: str) -> str:
    """The same report as Markdown, saved to the vault with the run."""
    sn, fin = rep["snapshot"], rep["financials"]
    L = [f"# BMG Capital company report: {sn['name']} ({sn['ticker']})", "",
         f"{sn['form']} | {sn['period']} | {sn['exchange']}", "",
         f"Source: `{source_name}` (PRIMARY, document as uploaded). Read {read_at} by the BMG Capital reader.", "",
         "## Company snapshot", "", sn["description"] + (f" (p. {sn['description_page']})" if sn.get("description_page") else ""), "",
         "Segments: " + sn["segments"] + (f" (p. {sn['segments_page']})" if sn.get("segments_page") else ""), "",
         "## Key takeaways", ""]
    L += [f"- {t['text']} (p. {t['page']})" for t in rep["takeaways"]] or ["- " + NOT_FOUND]
    L += ["", "## Financial overview", "", "| Metric | Current | Prior | Change | Source |", "|---|---|---|---|---|"]
    order = ["revenue", "gross_profit", "gross_margin_pct", "operating_income", "operating_margin_pct", "net_income", "net_margin_pct",
             "eps_diluted", "cash", "debt", "total_assets", "total_liabilities", "equity", "cfo", "capex", "fcf", "buybacks", "dividends"]
    for k in order:
        f = fin.get(k, {})
        if not f.get("found"):
            L.append(f"| {f.get('name', k)} | {NOT_FOUND} |  |  |  |")
            continue
        chg = f"{f['chg']:+.1f}%" if f.get("chg") is not None else (f"{f['chg_pp']:+.1f} pts" if f.get("chg_pp") is not None else "")
        src = f"p. {f['page']}" + (f"; formula: {f['formula']}" if f.get("formula") else "")
        L.append(f"| {f['name']} ({f['period']}) | {f['cur_txt']} | {f.get('prev_txt', '')} | {chg} | {src} |")
    for b in rep["breakdowns"]:
        L += ["", f"## {b['title']} ({b['period']})", "", "| Line | Value | Share | Change |", "|---|---|---|---|"]
        for i in b["items"]:
            L.append(f"| {i['name']} | {i['cur_txt']} | {i['share']:.0f}% | {('%+.1f%%' % i['chg']) if i.get('chg') is not None else ''} |")
    ind = rep["industry"]
    L += ["", f"## Company-specific metrics ({ind['profile']})", ""]
    L += [f"- {i['name']}: {i['value']}" + (f" (p. {i['page']})" if i.get("page") else "") for i in ind["items"]] or ["- " + NOT_FOUND]
    L += ["", "## Risks and watch items", ""]
    L += [f"- {x['kind']}: {x['text']} (p. {x['page']})" for x in rep["risks"]] or ["- " + NOT_FOUND]
    L += ["", "Figures are copied as printed in the filing. Line items, periods and categories are read automatically; check a figure against its page before using it in a model or note."]
    return "\n".join(L)
