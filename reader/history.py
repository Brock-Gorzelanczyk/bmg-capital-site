"""Ten years of annual figures from SEC EDGAR's XBRL company facts, and a peer table built the same way.

Every point is a fact the company filed: its value, period, the tag it was filed under, and the filing
(accession number, form, filing date) it comes from. For each fiscal year the most recently filed value is
used (a later 10-K can restate a year); when that differs from the value first reported, both are kept.
Margins and free cash flow are calculations of two filed facts of the same year and say so.
Source: https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json (no key needed).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "10-KT/A", "20-F", "20-F/A", "40-F", "40-F/A"}

# key, label, unit, kind (flow over a year, or a year-end instant), tags in order of preference
SERIES = [
    ("revenue", "Revenue", "USD", "flow", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
                                           "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense", "SalesRevenueGoodsNet"]),
    ("gross_profit", "Gross profit", "USD", "flow", ["GrossProfit"]),
    ("operating_income", "Operating income", "USD", "flow", ["OperatingIncomeLoss"]),
    ("net_income", "Net income", "USD", "flow", ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"]),
    ("eps_diluted", "Diluted EPS", "USD/shares", "flow", ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted"]),
    ("cfo", "Operating cash flow", "USD", "flow", ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"]),
    ("capex", "Capital expenditures", "USD", "flow", ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets",
                                                     "PaymentsForCapitalImprovements", "PaymentsToAcquireOtherPropertyPlantAndEquipment"]),
    ("dividends_per_share", "Dividends declared per share", "USD/shares", "flow", ["CommonStockDividendsPerShareDeclared", "CommonStockDividendsPerShareCashPaid"]),
    ("shares_diluted", "Diluted shares", "shares", "flow", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    ("total_assets", "Total assets", "USD", "instant", ["Assets"]),
    ("equity", "Shareholders' equity", "USD", "instant", ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"]),
    ("cash", "Cash and cash equivalents", "USD", "instant", ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"]),
    ("long_term_debt", "Long-term debt", "USD", "instant", ["LongTermDebt", "LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations",
                                                            "DebtInstrumentCarryingAmount"]),
    ("current_assets", "Current assets", "USD", "instant", ["AssetsCurrent"]),
    ("current_liabilities", "Current liabilities", "USD", "instant", ["LiabilitiesCurrent"]),
]
LABEL = {k: l for k, l, *_ in SERIES}


def filing_url(cik: int, accn: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accn.replace('-', '')}/{accn}-index.htm"


def _days(a: str, b: str) -> Optional[int]:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def _facts(cf: dict, tag: str, unit: str) -> list:
    node = ((cf.get("facts") or {}).get("us-gaap") or {}).get(tag) or ((cf.get("facts") or {}).get("ifrs-full") or {}).get(tag)
    if not node:
        return []
    units = node.get("units") or {}
    return units.get(unit) or (units.get("USD/shares") if unit == "USD/shares" else []) or []


def annual(cf: dict, tags: list, unit: str, kind: str) -> dict:
    """{period end: point} for the annual facts under the first tag that reports each year."""
    out: dict = {}
    for tag in tags:
        by_end: dict = {}
        for f in _facts(cf, tag, unit):
            if f.get("form") not in ANNUAL_FORMS or f.get("val") is None or not f.get("end"):
                continue
            if kind == "flow":
                d = _days(f.get("start"), f["end"])
                if d is None or not 350 <= d <= 380:
                    continue
            elif f.get("start"):
                continue
            by_end.setdefault(f["end"], []).append(f)
        for end, fs in by_end.items():
            if end in out:
                continue                              # an earlier tag already gave this year
            latest = max(fs, key=lambda f: (f.get("filed") or "", f.get("accn") or ""))
            first = min(fs, key=lambda f: (f.get("filed") or "", f.get("accn") or ""))
            fy = min((f.get("fy") for f in fs if f.get("fy")), default=None)
            pt = {"end": end, "start": latest.get("start"), "value": latest["val"], "tag": tag, "accn": latest["accn"], "form": latest["form"],
                  "filed": latest["filed"], "fy": fy}
            if first["accn"] != latest["accn"] and first["val"] != latest["val"]:
                pt["first_reported"] = {"value": first["val"], "accn": first["accn"], "filed": first["filed"], "form": first["form"]}
            out[end] = pt
    return out


def _calc(a: Optional[dict], b: Optional[dict], fn, formula: str) -> Optional[dict]:
    if not a or not b:
        return None
    v = fn(a["value"], b["value"])
    if v is None:
        return None
    return {"end": a["end"], "value": v, "calc": formula, "inputs": [{"tag": a["tag"], "accn": a["accn"], "value": a["value"]},
                                                                   {"tag": b["tag"], "accn": b["accn"], "value": b["value"]}], "fy": a.get("fy")}


def build(cf: dict, cik: int, years: int = 10) -> dict:
    """The company's last `years` fiscal years, newest last (the order a chart reads)."""
    series = {}
    for key, label, unit, kind, tags in SERIES:
        pts = annual(cf, tags, unit, kind)
        if pts:
            series[key] = pts
    # the fiscal years: the year ends that the revenue (or net income, or assets) series reports
    spine = series.get("revenue") or series.get("net_income") or series.get("total_assets") or {}
    ends = sorted(spine)[-years:]
    if not ends:
        return {"cik": cik, "company": cf.get("entityName"), "years": [], "series": [], "calcs": [],
                "note": "No annual facts were found in the company's XBRL filings (a foreign filer may report under IFRS tags, or the company may not file 10-Ks)."}

    def at(key: str, end: str) -> Optional[dict]:
        pts = series.get(key) or {}
        if end in pts:
            return pts[end]
        # a 52/53-week year can end a few days apart from one statement to another
        near = [e for e in pts if (_days(e, end) is not None and abs(_days(e, end)) <= 7)]
        return pts[near[0]] if near else None

    out_series = []
    for key, label, unit, kind, tags in SERIES:
        if key not in series:
            continue
        pts = [at(key, e) for e in ends]
        if not any(pts):
            continue
        out_series.append({"key": key, "label": label, "unit": unit, "points": pts,
                           "tags": sorted({p["tag"] for p in pts if p})})
    calcs = []
    div = lambda a, b: (a / b) if b else None
    for key, label, num, den in (("gross_margin", "Gross margin", "gross_profit", "revenue"), ("operating_margin", "Operating margin", "operating_income", "revenue"),
                                 ("net_margin", "Net margin", "net_income", "revenue")):
        pts = [_calc(at(num, e), at(den, e), div, f"{LABEL[num]} / {LABEL[den]}") for e in ends]
        if any(pts):
            calcs.append({"key": key, "label": label, "unit": "ratio", "points": pts, "formula": f"{LABEL[num]} / {LABEL[den]}"})
    pts = [_calc(at("cfo", e), at("capex", e), lambda a, b: a - abs(b), "Operating cash flow - |Capital expenditures|") for e in ends]
    if any(pts):
        calcs.append({"key": "fcf", "label": "Free cash flow", "unit": "USD", "points": pts, "formula": "Operating cash flow - |Capital expenditures|"})
    # columns are named by the year their period ends (the filer's own fiscal-year tag is sometimes wrong: GATX's FY2022
    # 10-K tags its year 2021), with the end date shown beside it
    return {"cik": cik, "company": cf.get("entityName"), "years": [{"end": e, "label": e[:4]} for e in ends],
            "series": out_series, "calcs": calcs,
            "note": "Annual facts from the company's own 10-K (or 20-F, 40-F) XBRL, most recently filed value for each year; a value later restated keeps its first-reported figure beside it. "
                    "Margins and free cash flow are calculations of two filed facts for the same year."}


# ---------------------------------------------------------------- peers
PEER_METRICS = [
    ("revenue", "Revenue", "USD"), ("revenue_growth", "Revenue growth", "pct"), ("gross_margin", "Gross margin", "pct"),
    ("operating_margin", "Operating margin", "pct"), ("net_margin", "Net margin", "pct"), ("fcf_margin", "Free cash flow margin", "pct"),
    ("roa", "Return on assets", "pct"), ("roe", "Return on equity", "pct"), ("debt_to_equity", "Long-term debt / equity", "x"),
    ("current_ratio", "Current ratio", "x"),
]


def peer_row(cf: dict, cik: int, ticker: str) -> dict:
    """The latest fiscal year's figures and ratios for one company, each with the filings it rests on."""
    h = build(cf, cik, years=2)
    row = {"ticker": ticker, "cik": cik, "company": h.get("company"), "metrics": {}}
    if not h["years"]:
        row["note"] = h["note"]
        return row
    s = {x["key"]: x["points"] for x in h["series"]}
    last = lambda k, i=-1: ((s.get(k) or [None, None])[i] if len(s.get(k) or []) >= abs(i) else None)
    row["fy_end"] = h["years"][-1]["end"]

    def put(key: str, value: Optional[float], refs: list, formula: str = "") -> None:
        if value is None:
            return
        row["metrics"][key] = {"value": value, "refs": [{"tag": r["tag"], "accn": r["accn"], "filed": r["filed"], "end": r["end"]} for r in refs if r],
                               **({"formula": formula} if formula else {})}

    rv, rv0 = last("revenue"), last("revenue", -2)
    ni, oi, gp, cfo, cx = last("net_income"), last("operating_income"), last("gross_profit"), last("cfo"), last("capex")
    ta, eq, ltd, ca, cl = last("total_assets"), last("equity"), last("long_term_debt"), last("current_assets"), last("current_liabilities")
    v = lambda p: p["value"] if p else None
    if rv:
        put("revenue", v(rv), [rv])
    if rv and rv0 and v(rv0) and v(rv0) > 0:
        put("revenue_growth", v(rv) / v(rv0) - 1, [rv, rv0], "Revenue / prior-year revenue - 1")
    for key, num, formula in (("gross_margin", gp, "Gross profit / Revenue"), ("operating_margin", oi, "Operating income / Revenue"), ("net_margin", ni, "Net income / Revenue")):
        if num and rv and v(rv):
            put(key, v(num) / v(rv), [num, rv], formula)
    if cfo and cx and rv and v(rv):
        put("fcf_margin", (v(cfo) - abs(v(cx))) / v(rv), [cfo, cx, rv], "(Operating cash flow - |Capital expenditures|) / Revenue")
    if ni and ta and v(ta):
        put("roa", v(ni) / v(ta), [ni, ta], "Net income / year-end total assets")
    if ni and eq and v(eq) and v(eq) > 0:
        put("roe", v(ni) / v(eq), [ni, eq], "Net income / year-end equity")
    if ltd and eq and v(eq) and v(eq) > 0:
        put("debt_to_equity", v(ltd) / v(eq), [ltd, eq], "Long-term debt / year-end equity")
    if ca and cl and v(cl):
        put("current_ratio", v(ca) / v(cl), [ca, cl], "Current assets / current liabilities")
    return row


def rank(rows: list) -> list:
    """Percentile of each company's value within the group for each metric (0 = lowest, 100 = highest)."""
    for key, _, _ in PEER_METRICS:
        vals = [(r, r["metrics"][key]["value"]) for r in rows if key in r["metrics"]]
        n = len(vals)
        for r, x in vals:
            below = sum(1 for _, y in vals if y < x)
            same = sum(1 for _, y in vals if y == x) - 1
            r["metrics"][key]["pct_rank"] = round(100 * (below + same / 2) / (n - 1)) if n > 1 else None
            r["metrics"][key]["of"] = n
    return rows
