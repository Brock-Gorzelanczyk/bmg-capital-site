"""Business and operating metrics: revenue mix and industry KPIs.

Profiles are picked by how often their vocabulary appears in the filing.
A KPI is published only with its evidence (the table row or the sentence
that states it). Company-defined KPIs keep the company's definition when the
filing states one near the KPI ("... is defined as ..."). Similarly named
metrics from different companies are never treated as the same measure.
"""
from __future__ import annotations

import re
from typing import Optional

from .facts import SCALE, fmt_value

_GEO = re.compile(r"americas|europe|greater china|china|japan|asia|pacific|emea|united states|\bu\.?s\.?\b|international|north america|"
                  r"latin america|rest of|other countries|foreign|canada|mexico", re.I)

# name, vocabulary (profile score), KPI specs: (label, row pattern, sentence pattern with value group, unit)
PROFILES = {
    "Railcar leasing": (r"lease fleet|railcar leasing|fleet utilization|lease price index|renewal success|railcars? (?:on|for) lease|lease revenue|as lessor|"
                        r"lease rates?|lease terms?|utilization|operating lease", [
        ("Fleet utilization", r"^(fleet )?utilization( rate)?( at (year|period|quarter) end)?$", r"utilization (?:rate )?(?:was|of|at|remained)[^.%]{0,40}?(\d{2}(?:\.\d)?)%", "percent"),
        ("Lease price index", r"^lease price index$", r"(?:lease price index|\bLPI\b)[^.%]{0,60}?(?:was |of )?((?:positive |negative |-)?\d+(?:\.\d)?)%", "percent"),
        ("Renewal success rate", r"^renewal success rate$", r"renewal success rate[^.%]{0,60}?(\d{2}(?:\.\d)?)%", "percent"),
        ("Fleet size (cars)", r"^(total )?(railcars|cars) (owned|in fleet)$|^fleet size$|fleet data ending balance$", r"fleet (?:of|consisting of|totaled)[^.]{0,60}?([\d,]{4,}) (?:railcars|cars)", "count"),
        ("Investment volume", r"^investment volume$", r"investment volume (?:was|of|totaled)[^.]{0,40}?\$\s?([\d.,]+)\s?(billion|million)", "USD"),
    ]),
    "Railcar manufacturing": (r"railcar deliveries|new railcar orders|railcar backlog|manufacturing backlog|deliveries of|\bbacklog\b|\bdeliveries\b|"
                              r"manufacturing facilit|production rates?|new orders", [
        ("Deliveries (units)", r"^(new railcar )?deliveries( units)?$", r"deliver(?:ies|ed)[^.]{0,40}?([\d,]{3,}) (?:units|railcars|new railcars)", "count"),
        ("New orders (units)", r"^(new railcar )?orders( units)?$", r"orders? (?:for|of|totaling|received)[^.]{0,40}?([\d,]{3,}) (?:units|railcars|new railcars)", "count"),
        ("Backlog (units)", r"^backlog( units)?$", r"backlog (?:of|was|totaled)[^.]{0,60}?([\d,]{3,}) (?:units|railcars|new railcars)", "count"),
        ("Backlog value", r"^backlog value$|^backlog \$$", r"backlog[^.]{0,80}?(?:valued at|with a value of|of)\s?(?:approximately )?\$\s?([\d.,]+)\s?(billion|million)", "USD"),
    ]),
    # Microsoft's MD&A prints "Commercial remaining performance obligation ... $678 billion" before the note's total
    # ($684 billion); the commercial part is a different measure, so the total is taken. A change before the level is
    # passed over: Oracle's releases print "RPO grew $85 billion in Q4 from $553 billion to $638 billion" and "RPO up
    # $209 billion year-over-year to $664 billion" (the increase was read as the level and put its kill criterion on the
    # trigger side). Its 10-Q's "Remaining performance obligations, as defined in Note 1 of ... May 31, 2024, were $130.2
    # billion" runs past 140 characters (2026-10-26)
    "Software and subscriptions": (r"subscription revenue|software license|remaining performance obligation|annual recurring revenue|saas", [
        ("Remaining performance obligations", None, r"(?<!commercial )remaining performance obligations?(?:[^.$]{0,40}?\b(?:grew|up|increased|rose)\s+(?:by\s+)?\$\s?[\d.,]+\s?(?:billion|million)"
                                                    r"(?:[^.]|\.\d){0,80}?\bto\b)?(?:[^.]|\.\d){0,200}?\$\s?([\d.,]+)\s?(billion|million)", "USD"),
        ("Annual recurring revenue", None, r"annual(?:ized)? recurring revenue[^.]{0,80}?\$\s?([\d.,]+)\s?(billion|million)", "USD"),
        ("Net revenue retention", None, r"net (?:revenue|dollar) retention(?: rate)?[^.%]{0,60}?(\d{2,3})%", "percent"),
        ("Customers", None, r"(?:approximately |over |more than )?(?<![\d,$.])(?!(?:19|20)\d\d(?![\d,]))(\d{1,3}(?:,\d{3})+|\d{3,})(?![\d,]) (?:paying )?customers", "count"),
        ("Stock-based compensation", r"^(share|stock) based compensation( expense)?$", None, "USD"),
    ]),
    "Consumer products": (r"iphone|retail stores|consumer products|net sales by category|our stores", [
        ("Retail stores", None, r"(?:operated|had|operates)\s+([\d,]{2,}) (?:retail )?stores", "count"),
        ("Share-based compensation", r"^share based compensation expense$", None, "USD"),
    ]),
    "Banking": (r"net interest income|deposits|loans held for investment|allowance for credit losses|tier 1", [
        ("Net interest income", r"^net interest income$", None, "USD"),
        ("Net interest margin", r"^net interest margin$", r"net interest margin[^.%]{0,60}?(\d(?:\.\d+)?)%", "percent"),
        ("Total deposits", r"^total deposits$", None, "USD"),
        ("Total loans", r"^(total loans|loans net|total loans and leases)$", None, "USD"),
        ("Allowance for credit losses", r"^allowance for (credit|loan) losses$", None, "USD"),
        ("CET1 capital ratio", None, r"common equity tier 1(?: capital)? ratio[^.%]{0,80}?(\d{1,2}(?:\.\d+)?)%", "percent"),
    ]),
}
# generic operating vocabulary for KPIs outside the profiles (MD&A tables)
_OPERATING = re.compile(r"\b(units|customers|subscribers|stores|employees|backlog|orders|deliveries|utilization|members|users|shipments|"
                        r"vehicles|aircraft|rigs|barrels|production|occupancy|same store)\b", re.I)


ANCHORS = {"Railcar leasing": r"railcar|rail car|tank car", "Railcar manufacturing": r"railcar|rail car|tank car",
           "Banking": r"net interest income|bank holding company|tier 1"}


def profile_for(doc) -> tuple[str, dict]:
    """The industry profile; two joined by " + " when the business clearly
    does both (a railcar builder that also leases, for example)."""
    low = doc.body.lower()
    scores = {name: len(re.findall(vocab, low)) for name, (vocab, _) in PROFILES.items()}
    # shared words ("operating lease", "utilization", "deliveries") score any company; a profile
    # with an anchor needs it (Amazon FY2025 scored 31 for railcar leasing on lease wording alone)
    for name, anchor in ANCHORS.items():
        if len(re.findall(anchor, low)) < 10:
            scores[name] = 0
    ranked = sorted(scores, key=scores.get, reverse=True)
    best = ranked[0]
    if scores[best] < 3:
        return "General", scores
    second = ranked[1]
    if scores[second] >= 20 and scores[second] >= 0.5 * scores[best] and "Banking" not in (best, second):
        return f"{best} + {second}", scores
    return best, scores


def parts(profile: str) -> list:
    return [p for p in (profile or "").split(" + ") if p in PROFILES]


def _definition(doc, label: str) -> Optional[dict]:
    words = re.escape(label.split(" (")[0].lower())
    m = re.search(rf"[^.\n]*\b{words}\b[^.\n]{{0,60}}\b(?:is|are) (?:defined|calculated|measured) as[^.\n]{{0,300}}\.", doc.body, re.I)
    if not m:
        m = re.search(rf"[^.\n]*\bwe define {words}\b[^.\n]{{0,300}}\.", doc.body, re.I)
    if not m:
        return None
    return {"text": re.sub(r"\s+", " ", m.group(0)).strip()[:400], **doc.location(m.start())}


def _plabel(r: dict, i: int) -> str:
    """The column's period stated the same way as the financial facts
    (FY2025, As of Sep 27, 2025, Nine months ended ...)."""
    from .facts import _period
    return _period(r["cols"][i], "instant" if r.get("statement") == "balance" else "")["label"]


def _cur_prior(r: dict) -> tuple:
    """The current and prior column of a table row. Most tables print the latest period first, but some operating
    tables run oldest first (GATX's five-quarter fleet table: Jun 30, 2025 ... Jun 30, 2026). With a dated column for
    every value, the current column is the latest end date (the first one on a tie, so three months beats six months)
    and the prior is the same kind of period a year earlier, else the latest one before it."""
    n = len(r["values"])
    cols = r.get("cols") or []
    ends = [c.get("end") for c in cols]
    if n < 2 or len(cols) != n or not all(ends):
        return 0, (1 if n > 1 else None)
    cur = max(range(n), key=lambda i: (ends[i], -i))
    from datetime import date
    def d(s):
        try:
            return date.fromisoformat(s)
        except ValueError:
            return None
    ce = d(ends[cur])
    same = [i for i in range(n) if i != cur and cols[i].get("type") == cols[cur].get("type") and d(ends[i]) and ce and d(ends[i]) < ce]
    year = [i for i in same if abs((ce - d(ends[i])).days - 365) <= 10]
    prior = year[0] if year else (max(same, key=lambda i: ends[i]) if same else None)
    return cur, prior


def _row_series(r: dict, mult: float) -> list:
    """Every dated column of the same kind of period as the current one, oldest first: one filing's whole run of a
    KPI (a five-quarter table gives five points)."""
    cols = r.get("cols") or []
    if len(cols) != len(r["values"]) or len(cols) < 3 or not all(c.get("end") for c in cols):
        return []
    cur, _ = _cur_prior(r)
    pts = [{"end": c["end"], "label": _plabel(r, i), "value": r["values"][i] * mult, "raw": r["raw"][i], "type": c.get("type")}
           for i, c in enumerate(cols) if c.get("type") == cols[cur].get("type")]
    return sorted(pts, key=lambda p: p["end"]) if len(pts) >= 3 else []


def _row_kpi(doc, rows: list, label: str, pat: str, unit: str) -> Optional[dict]:
    rx = re.compile(pat)
    for r in rows:
        # a row is matched by its label, or by its table block and label together ("Rail North America Fleet Data"
        # "Ending balance" is the fleet size; a bare "Ending balance" could be any roll-forward)
        if not (rx.search(r["key"]) or (r.get("block") and rx.search(f'{r["block"].lower()} {r["key"]}'))):
            continue
        ci, pi = _cur_prior(r)
        u = "percent" if r["raw"][ci].endswith("%") else unit
        mult = SCALE.get(r["scale"], 1.0) if u == "USD" else 1.0
        v = r["values"][ci] * mult
        prev = r["values"][pi] * mult if pi is not None else None
        k = {"name": label, "value": v, "display": fmt_value(v, u) if u in ("USD", "percent") else f"{v:,.0f}",
             "prior": prev, "prior_display": (fmt_value(prev, u) if u in ("USD", "percent") else f"{prev:,.0f}") if prev is not None else "",
             "period": _plabel(r, ci), "prior_period": _plabel(r, pi) if pi is not None else "",
             "period_type": ((r.get("cols") or [{}])[ci] if len(r.get("cols") or []) > ci else {}).get("type"),
             "kind": "reported", "status": "Source matched", "scope": r.get("block") or "",
             "source": {"pdf_page": r["pdf_page"], "printed_page": r["printed_page"], "section": r["section"], "table_title": r["table_title"] or r.get("block") or "",
                        "row_label": r["full_label"], "excerpt": r["line"], "raw": r["raw"][ci], "offset": r["offset"],
                        # the row group inside the table: GATX's release prints "Rail North America Statistics" and "Boxcar
                        # Fleet Rollforward" under one "SUPPLEMENTAL INFORMATION" caption
                        "heading": r.get("heading") or ""}}
        series = _row_series(r, mult)
        if series:
            k["series"] = series
        return k
    return None


def _sentence_kpi(doc, label: str, pat: str, unit: str) -> Optional[dict]:
    m = re.search(pat, doc.body, re.I)
    if not m or not m.groups():
        return None
    raw = m.group(1)
    if raw.lower().startswith(("positive ", "negative ")):
        raw = ("-" if raw.lower().startswith("negative") else "") + raw.split()[1]
    mult = {"billion": 1e9, "million": 1e6}.get((m.group(2) or "").lower(), 1.0) if (m.lastindex or 0) >= 2 else 1.0
    try:
        v = float(raw.replace(",", "")) * mult
    except ValueError:
        return None
    s0 = max(doc.body.rfind(". ", 0, m.start()) + 2, doc.body.rfind("\n", 0, m.start()) + 1)
    s1 = doc.body.find(".", m.end())
    sent = re.sub(r"\s+", " ", doc.body[s0:(s1 + 1 if s1 > 0 else m.end())])[:360]
    disp = fmt_value(v, unit) if unit in ("USD", "percent") else f"{v:,.0f}"
    return {"name": label, "value": v, "display": disp, "period": "As stated in the cited sentence", "kind": "reported", "status": "Source matched",
            "source": {**doc.location(m.start()), "excerpt": sent, "raw": raw, "offset": m.start()}}


def kpis(doc, rows: list) -> dict:
    prof, scores = profile_for(doc)
    items = []
    for part in parts(prof):
        for label, rpat, spat, unit in PROFILES[part][1]:
            k = (_row_kpi(doc, rows, label, rpat, unit) if rpat else None) or (_sentence_kpi(doc, label, spat, unit) if spat else None)
            if k:
                d = _definition(doc, label)
                if d:
                    k["definition"] = d
                items.append(k)
            else:
                items.append({"name": label, "status": "Unavailable", "reason": "Not found in the reviewed filing."})
    # company-specific operating rows in MD&A tables, outside the profile list
    seen = {i["name"].lower() for i in items}
    extra = 0
    for r in rows:
        if extra >= 5 or r["statement"] != "other" or not (r["section"] or "").startswith("Item 7"):
            continue
        if not _OPERATING.search(r["full_label"]) or r["full_label"].lower() in seen:
            continue
        if re.search(r"external customers|intersegment|eliminations", r["full_label"], re.I):
            continue                     # revenue lines, not operating measures
        counted = re.search(r"\bunits\b|number of|\bcars\b|railcars|employees|customers|stores", r["full_label"], re.I)
        u = "percent" if r["raw"][0].endswith("%") else ("count" if counted or not r["scale"] else "USD")
        if u == "USD":
            continue                     # dollar rows belong to the financial statements, not operating KPIs
        if any((i.get("source") or {}).get("offset") == r["offset"] for i in items):
            continue                     # already shown as a profile KPI
        k = _row_kpi(doc, [r], r["full_label"] + (f" ({r['block']})" if r.get("block") else ""), "^" + re.escape(r["key"]) + "$", u)
        if k:
            k["company_specific"] = True
            items.append(k)
            seen.add(r["full_label"].lower())
            extra += 1
    return {"profile": prof, "scores": scores, "items": items,
            "not_applicable": ["Gross profit", "Gross margin", "Free cash flow"] if prof == "Banking" else []}


def breakdowns(fb, rows: list) -> list[dict]:
    """Revenue by product/segment and by region: table lines that sum to
    the consolidated revenue of the same fiscal period."""
    rev_cur = fb.get("revenue", 0)
    if not rev_cur:
        return []
    out = []
    for i, r in enumerate(rows):
        if not re.search(r"^(total net sales|total revenues?|total net revenues?|total revenue|net sales|revenues?)$", r["key"]) or r["unit"] != "USD":
            continue
        mult = SCALE.get(r["scale"], 1.0)
        if abs(r["values"][0] * mult - rev_cur["value"]) > abs(rev_cur["value"]) * 0.002:
            continue
        parts, j = [], i - 1
        while j >= 0 and rows[j]["cols"] == r["cols"] and r["offset"] - rows[j]["offset"] < 3000 and len(parts) < 14:
            if re.search(r"^total|cost of|^net sales$", rows[j]["key"]):
                break
            parts.append(rows[j])
            j -= 1
        parts.reverse()
        if len(parts) < 2:
            continue
        tot = sum(p["values"][0] for p in parts)
        if abs(tot - r["values"][0]) > abs(r["values"][0]) * 0.01:
            continue
        geo = sum(1 for p in parts if _GEO.search(p["label"])) >= len(parts) / 2
        kind = "region" if geo else "product or segment"
        if any(b["kind"] == kind and len(b["items"]) >= len(parts) for b in out):
            continue
        out = [b for b in out if b["kind"] != kind]
        items = []
        for p in parts:
            cur = p["values"][0] * mult
            prev = p["values"][1] * mult if len(p["values"]) > 1 else None
            chg = (cur - prev) / prev * 100 if prev and prev > 0 and cur >= 0 else None
            items.append({"name": p["label"], "value": cur, "display": fmt_value(cur, "USD"), "prior": prev,
                          "prior_display": fmt_value(prev, "USD") if prev is not None else "", "share": cur / rev_cur["value"] * 100,
                          "change_pct": chg, "change_abs": (cur - prev) if prev is not None else None,
                          "source": {"pdf_page": p["pdf_page"], "printed_page": p["printed_page"], "table_title": p["table_title"],
                                     "row_label": p["full_label"], "excerpt": p["line"], "raw": p["raw"][0], "offset": p["offset"]}})
        out.append({"kind": kind, "title": f"Net revenue by {kind}", "period": rev_cur["period"]["label"],
                    "prior_period": r["cols"][1]["label"] if len(r["cols"]) > 1 else "",
                    "total_display": fmt_value(r["values"][0] * mult, "USD"), "items": items,
                    "check": f"Lines sum to {fmt_value(tot * mult, 'USD')}, equal to consolidated revenue of {rev_cur['display']} within 1%.",
                    "source": {"pdf_page": r["pdf_page"], "printed_page": r["printed_page"], "table_title": r["table_title"], "excerpt": r["line"]}})
    return out
