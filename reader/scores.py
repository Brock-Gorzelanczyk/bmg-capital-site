"""Accounting quality scores from the filing's own statements: Altman Z, Piotroski F and Beneish M.

Every input is a printed statement line of this filing (the current and prior-year columns of an annual
report), kept with its page and the line it was read from. A score whose inputs are not all found is not
computed; the missing lines are listed instead. Nothing is estimated.

Formulas (as published):
  Altman Z (1968, public manufacturers):  1.2 X1 + 1.4 X2 + 3.3 X3 + 0.6 X4 + 1.0 X5
      X1 working capital / total assets, X2 retained earnings / total assets, X3 EBIT / total assets,
      X4 market value of equity / total liabilities, X5 sales / total assets. Zones: above 2.99 safe,
      1.81 to 2.99 grey, below 1.81 distress. The market value used is the cover page's equity held by
      non-affiliates (a historical figure the filing prints), so X4 is labelled as such.
  Altman Z'' (1995, non-manufacturers, book equity): 6.56 X1 + 3.26 X2 + 6.72 X3 + 1.05 X4'
      X4' book equity / total liabilities. Zones: above 2.60 safe, 1.10 to 2.60 grey, below 1.10 distress.
  Piotroski F (2000): nine tests, one point each (profitability, leverage and liquidity, efficiency).
      Year-end total assets are used where the paper uses beginning or average assets, because an annual
      report prints two balance sheets; this is stated with the score.
  Beneish M (1999, eight variables): -4.84 + 0.920 DSRI + 0.528 GMI + 0.404 AQI + 0.892 SGI + 0.115 DEPI
      - 0.172 SGAI + 4.679 TATA - 0.327 LVGI. Above -1.78 is the paper's flag for likely manipulation.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Optional

from .facts import fmt_value
from .xbrl import page_of

SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}

# statement lines that the fact book does not extract, by statement, in order of preference
LINES = {
    "current_assets": ("balance", [r"^total current assets$"]),
    "current_liabilities": ("balance", [r"^total current liabilities$"]),
    "receivables": ("balance", [r"^(accounts|trade) receivables?( net)?( of (an |the )?allowances?[a-z0-9 ,.$]*)?$", r"^receivables?( net)?$",
                                r"^(accounts|trade) receivables? less allowances?[a-z0-9 ,.$]*$", r"^accounts and notes receivable( net)?$"]),
    "ppe": ("balance", [r"^(total )?propert(y|ies) plant and equipment( net)?( of accumulated depreciation[a-z0-9 ,.$]*)?$",   # Chevron: "Properties, plant ..."
                        r"^(total )?property and equipment( net)?( of accumulated depreciation[a-z0-9 ,.$]*)?$",
                        r"^net property plant and equipment$", r"^net property and equipment$", r"^property plant and equipment at cost net$"]),
    "retained_earnings": ("balance", [r"^retained earnings( accumulated deficit)?$", r"^accumulated deficit$", r"^(accumulated deficit|retained earnings) (accumulated deficit|retained earnings)$",
                                      r"^reinvested earnings$", r"^earnings retained in the business$"]),
    "long_term_debt": ("balance", [r"^long term debt( net)?( of current (portion|maturities))?( less current (portion|maturities))?( excluding current (portion|maturities))?$",
                                   r"^long term debt and finance leases?( net of current portion)?$", r"^long term borrowings$",
                                   r"^debt and finance leases net of current portion$",
                                   # names that do not say long-term: only under a non-current heading
                                   r"^(term debt|notes payable|senior notes|borrowings|debt)$!"]),
    "sga": ("income", [r"^(selling|sales) general and administrative( expenses?)?$", r"^selling general and administrative expense$",
                       r"^selling general administrative and other( expenses?)?$", r"^marketing general and administrative( expenses?)?$"]),
    "depreciation": ("cashflow", [r"^depreciation and amortization( expense)?$", r"^depreciation( expense)?$", r"^depreciation depletion and amortization$",
                                  r"^depreciation amortization and other$", r"^depreciation and amortization of property[a-z ,]*$",
                                  r"^depreciation amortization and impairment$", r"^depreciation and amortization including[a-z ,]*$"]),
}
# the same lines as inline XBRL tags, for an HTML filing whose table rows did not give them
XBRL = {
    "current_assets": ["us-gaap:AssetsCurrent"],
    "current_liabilities": ["us-gaap:LiabilitiesCurrent"],
    "receivables": ["us-gaap:AccountsReceivableNetCurrent", "us-gaap:ReceivablesNetCurrent", "us-gaap:AccountsNotesAndLoansReceivableNetCurrent"],
    "ppe": ["us-gaap:PropertyPlantAndEquipmentNet"],
    "retained_earnings": ["us-gaap:RetainedEarningsAccumulatedDeficit"],
    "long_term_debt": ["us-gaap:LongTermDebtNoncurrent", "us-gaap:LongTermDebtAndCapitalLeaseObligations", "us-gaap:LongTermDebtAndFinanceLeaseObligationsNoncurrent"],
    "sga": ["us-gaap:SellingGeneralAndAdministrativeExpense"],
    "depreciation": ["us-gaap:DepreciationDepletionAndAmortization", "us-gaap:DepreciationAndAmortization", "us-gaap:DepreciationAmortizationAndAccretionNet", "us-gaap:Depreciation"],
}
NAMES = {"revenue": "Revenue (sales)", "cost_of_revenue": "Cost of revenue", "gross_profit": "Gross profit", "operating_income": "Operating income (EBIT)",
         "net_income": "Net income", "cfo": "Operating cash flow", "total_assets": "Total assets", "total_liabilities": "Total liabilities",
         "equity": "Shareholders' equity", "shares_diluted": "Diluted weighted-average shares", "market_value_nonaffiliates": "Market value held by non-affiliates (cover page)",
         "current_assets": "Total current assets", "current_liabilities": "Total current liabilities", "receivables": "Receivables",
         "ppe": "Property, plant and equipment, net", "retained_earnings": "Retained earnings (accumulated deficit)",
         "long_term_debt": "Long-term debt (non-current)", "sga": "Selling, general and administrative", "depreciation": "Depreciation and amortization",
         "total_debt": "Total debt"}


def _point(f: dict) -> dict:
    src = f.get("source") or {}
    out = {"value": f["value"], "display": f.get("display") or fmt_value(f["value"], f.get("unit", "USD")), "end": (f.get("period") or {}).get("end"),
           "period": (f.get("period") or {}).get("label"), "page": src.get("pdf_page"), "printed_page": src.get("printed_page"),
           "raw": src.get("raw"), "quote": (src.get("excerpt") or "")[:240], "fact": f.get("id")}
    if not src and (f.get("formula") or f.get("definition") or f.get("inputs")):
        out["calc"] = True                   # a total the reader added up from cited lines (total debt): its formula, not a page
        out["formula"] = f.get("definition") or f.get("formula") or ""
    return out


def _row_point(r: dict, k: int) -> dict:
    v = r["values"][k] * SCALE.get(r.get("scale") or "", 1.0)
    return {"value": v, "display": fmt_value(v, "USD"), "end": r["cols"][k]["end"], "period": r["cols"][k].get("label"),
            "page": r.get("pdf_page"), "printed_page": r.get("printed_page"), "raw": (r.get("raw") or [""] * (k + 1))[k],
            "quote": (r.get("line") or r.get("full_label") or "")[:240], "row": r.get("full_label") or r.get("label")}


def _near(a: Optional[str], b: Optional[str], days: int = 7) -> bool:
    """Two period ends within a week (a 52/53-week year can end a day apart in two statements)."""
    if not a or not b:
        return False
    if a == b:
        return True
    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days) <= days
    except ValueError:
        return False


def _noncurrent(r: dict) -> bool:
    h = (r.get("heading") or "").lower()
    return not re.search(r"\bcurrent (assets|liabilities)\b", h) or bool(re.search(r"non ?-?current|long[- ]term", h))


def _line(rows: list, key: str, end: Optional[str]) -> Optional[dict]:
    """The first statement line that matches, for the column that ends on `end`."""
    stmt, pats = LINES[key]
    if not end:
        return None
    for pat in pats:
        strict = pat.endswith("!")
        rx = re.compile(pat.rstrip("!"))
        for r in rows:
            if r.get("statement") != stmt or r.get("footnote") or r.get("scope") in ("affiliate", "segment", "subsidiary", "supplemental") or r.get("unit") not in ("USD", None):
                continue
            if not rx.match(r.get("key") or ""):
                continue
            if key == "long_term_debt" and not _noncurrent(r):
                continue
            if strict and not re.search(r"non ?-?current|long[- ]term", (r.get("heading") or "").lower()):
                continue
            cols = r.get("cols") or []
            for exact in (True, False):
                for k, c in enumerate(cols):
                    if (c.get("end") == end if exact else _near(c.get("end"), end)) and k < len(r["values"]) and r["values"][k] is not None:
                        return _row_point(r, k)
    return None


def _xbrl_line(doc, key: str, end: Optional[str], instant: bool, near_page: Optional[int] = None, concept_only: Optional[str] = None) -> Optional[dict]:
    """The line as the filer tagged it (consolidated, no dimensions), with the page where its table row is printed."""
    if not doc or not getattr(doc, "xbrl", None) or not end:
        return None
    for concept in ([concept_only] if concept_only else XBRL.get(key, [])):
        cands = [f for f in doc.xbrl if f["concept"] == concept and not f["dims"] and f["instant"] == instant and _near(f["end"], end)]
        cands.sort(key=lambda f: f["end"] != end)          # the exact date first
        for f in cands:
            if not instant and f.get("start"):
                days = (int(f["end"][:4]) - int(f["start"][:4])) * 365 + (int(f["end"][5:7]) - int(f["start"][5:7])) * 30
                if not 330 <= days <= 400:
                    continue
            ex = re.sub(r"\s+", " ", f.get("excerpt") or "").strip()
            page = page_of(doc, ex, near_page)
            return {"value": f["value"], "display": fmt_value(f["value"], "USD"), "end": f["end"], "page": page, "printed_page": doc.printed_page(page) if page else None,
                    "raw": f.get("raw"), "quote": ex[:240], "xbrl": concept, "anchor": f.get("anchor")}
    return None


def _xbrl_pair(doc, key: str, now_end: Optional[str], before_end: Optional[str], instant: bool, near_page: Optional[int] = None):
    """This year's and last year's line from the same tag, the first tagged for both: Chevron tags its trade receivables (a
    note's sentence) for 2025 only, so 2025 came from that tag and 2024 from "accounts and notes receivable", two definitions
    in one index. A tag for this year only when none has both."""
    first = None
    for concept in XBRL.get(key, []):
        cn = _xbrl_line(doc, key, now_end, instant, near_page, concept)
        cb = _xbrl_line(doc, key, before_end, instant, near_page, concept) if cn else None
        if cn and cb:
            return cn, cb
        first = first or cn
    return first, None


def _div(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b in (None, 0):
        return None
    return a / b


def build(fb, rows: list, profile: str = "", doc=None) -> dict:
    """Scores for an annual report; for anything else, the reason they are not computed."""
    rev_c, rev_p = fb.comparable("revenue")
    if not rev_c or (rev_c.get("period") or {}).get("type") != "FY":
        return {"status": "not computed", "reason": "Quality scores use annual figures: they are computed for a 10-K (or 20-F, 40-F) whose income statement was read."}
    if not rev_p:
        return {"status": "not computed", "reason": "The prior-year column of the income statement was not read, so year-over-year tests cannot be run."}
    cur, pri = rev_c["period"]["end"], rev_p["period"]["end"]
    ta_c, ta_p = fb.comparable("total_assets")
    bs_cur = (ta_c or {}).get("period", {}).get("end") or cur
    bs_pri = (ta_p or {}).get("period", {}).get("end") or pri

    inputs: dict[str, dict] = {}

    def fact(m: str) -> None:
        c, p = fb.comparable(m)
        if p and p.get("status") == "Unavailable":
            p = None
        if c and c.get("value") is not None and c.get("status") != "Unavailable":
            inputs[m] = {"name": NAMES.get(m, m), "now": _point(c), "before": _point(p) if p and p.get("value") is not None else None, "basis": "statement figure"}

    for m in ("revenue", "cost_of_revenue", "gross_profit", "operating_income", "net_income", "cfo", "total_assets", "total_liabilities", "equity", "shares_diluted", "total_debt"):
        fact(m)
    mv = fb.facts.get("market_value_nonaffiliates")
    if mv and mv.get("value") and mv.get("status") != "Unavailable":
        inputs["market_value_nonaffiliates"] = {"name": NAMES["market_value_nonaffiliates"], "now": _point(mv), "before": None, "basis": "cover page"}
    for key, (stmt, _) in LINES.items():
        a, b = (bs_cur, bs_pri) if stmt == "balance" else (cur, pri)
        now, before = _line(rows, key, a), _line(rows, key, b)
        basis = "statement line"
        if not now or not before:                 # an HTML filing's own tags fill a line its table rows did not give
            anchor = {"balance": "total_assets", "income": "revenue", "cashflow": "cfo"}[stmt]
            hint = (((inputs.get(anchor) or {}).get("now")) or {}).get("page")
            xn, xb = _xbrl_pair(doc, key, a, b, stmt == "balance", hint)
            if xn and (not now or (xb and not before)):
                now, before, basis = xn, xb, "inline XBRL tag"
        if now:
            inputs[key] = {"name": NAMES[key], "now": now, "before": before, "basis": basis}

    def v(key: str, which: str = "now") -> Optional[float]:
        x = (inputs.get(key) or {}).get(which)
        return x["value"] if x else None

    # gross profit from revenue minus cost of revenue when the filing prints only those two lines (a calculation, labelled)
    if "gross_profit" not in inputs and "cost_of_revenue" in inputs:
        inputs["gross_profit"] = {"name": "Gross profit (revenue minus cost of revenue)", "basis": "calculated",
                                  "now": {"value": v("revenue") - abs(v("cost_of_revenue")), "display": fmt_value(v("revenue") - abs(v("cost_of_revenue")), "USD"), "end": cur, "calc": True},
                                  "before": ({"value": v("revenue", "before") - abs(v("cost_of_revenue", "before")), "end": pri, "calc": True,
                                              "display": fmt_value(v("revenue", "before") - abs(v("cost_of_revenue", "before")), "USD")}
                                             if v("revenue", "before") is not None and v("cost_of_revenue", "before") is not None else None)}
    # total liabilities from total assets minus equity when not printed (a calculation, labelled)
    if "total_liabilities" not in inputs and "total_assets" in inputs and "equity" in inputs:
        tl = lambda w: (v("total_assets", w) - v("equity", w)) if v("total_assets", w) is not None and v("equity", w) is not None else None
        inputs["total_liabilities"] = {"name": "Total liabilities (total assets minus equity)", "basis": "calculated",
                                       "now": {"value": tl("now"), "display": fmt_value(tl("now"), "USD"), "end": bs_cur, "calc": True},
                                       "before": {"value": tl("before"), "display": fmt_value(tl("before"), "USD"), "end": bs_pri, "calc": True} if tl("before") is not None else None}
    # Piotroski's leverage test and Beneish's LVGI use long-term debt; total debt stands in only when no long-term line is printed, and says so
    if "long_term_debt" not in inputs and "total_debt" in inputs:
        inputs["long_term_debt"] = dict(inputs["total_debt"], name="Total debt (no separate long-term debt line was found)", basis="substitute")

    bank = profile == "Banking"
    out = {"status": "computed", "period": {"current": rev_c["period"]["label"], "prior": rev_p["period"]["label"]}, "inputs": inputs,
           "note": "Inputs are this filing's own statement lines (current and prior-year columns), each with its page. "
                   "A score with any input missing is not computed. Scores are screening aids from published formulas, not BMG ratings."}

    def need(keys: list, which=("now",)) -> list:
        miss = []
        for k in keys:
            for w in which:
                if v(k, w) is None:
                    miss.append(f"{NAMES.get(k, k)}{' (prior year)' if w == 'before' else ''}")
        return miss

    # ---- Altman Z and Z''
    if bank:
        out["altman"] = {"status": "not meaningful", "reason": "Altman Z was built on manufacturers' balance sheets; a bank has no current assets or liabilities in that sense."}
        out["altman_book"] = dict(out["altman"])
    else:
        base = ["current_assets", "current_liabilities", "retained_earnings", "operating_income", "total_assets", "total_liabilities", "revenue"]
        miss = need(base)
        if miss:
            out["altman"] = {"status": "not computed", "missing": miss}
            out["altman_book"] = {"status": "not computed", "missing": miss}
        else:
            ta, tl = v("total_assets"), v("total_liabilities")
            x1, x2, x3, x5 = (v("current_assets") - v("current_liabilities")) / ta, v("retained_earnings") / ta, v("operating_income") / ta, v("revenue") / ta
            terms = [("Working capital / total assets", x1, ["current_assets", "current_liabilities", "total_assets"]),
                     ("Retained earnings / total assets", x2, ["retained_earnings", "total_assets"]),
                     ("EBIT / total assets", x3, ["operating_income", "total_assets"])]
            if v("market_value_nonaffiliates") is not None:
                x4 = v("market_value_nonaffiliates") / tl
                w = [1.2, 1.4, 3.3, 0.6, 1.0]
                tt = terms + [("Market value of equity (non-affiliates, cover page) / total liabilities", x4, ["market_value_nonaffiliates", "total_liabilities"]),
                              ("Sales / total assets", x5, ["revenue", "total_assets"])]
                z = sum(wi * t[1] for wi, t in zip(w, tt))
                out["altman"] = {"status": "computed", "model": "Altman Z (1968, public manufacturers)", "value": round(z, 2),
                                 "zone": "safe" if z > 2.99 else "grey" if z >= 1.81 else "distress", "cutoffs": "above 2.99 safe; 1.81 to 2.99 grey; below 1.81 distress",
                                 "terms": [{"name": n, "value": round(x, 4), "weight": wi, "inputs": ins} for wi, (n, x, ins) in zip(w, tt)],
                                 "caveat": "The cover page's market value is equity held by non-affiliates at the end of the second fiscal quarter, not today's market value."}
            else:
                out["altman"] = {"status": "not computed", "missing": [NAMES["market_value_nonaffiliates"]]}
            x4b = v("equity") / tl if v("equity") is not None else None
            if x4b is None:
                out["altman_book"] = {"status": "not computed", "missing": [NAMES["equity"]]}
            else:
                w = [6.56, 3.26, 6.72, 1.05]
                tt = terms + [("Book equity / total liabilities", x4b, ["equity", "total_liabilities"])]
                z = sum(wi * t[1] for wi, t in zip(w, tt))
                out["altman_book"] = {"status": "computed", "model": "Altman Z'' (1995, non-manufacturers, book equity)", "value": round(z, 2),
                                      "zone": "safe" if z > 2.60 else "grey" if z >= 1.10 else "distress", "cutoffs": "above 2.60 safe; 1.10 to 2.60 grey; below 1.10 distress",
                                      "terms": [{"name": n, "value": round(x, 4), "weight": wi, "inputs": ins} for wi, (n, x, ins) in zip(w, tt)]}

    # ---- Piotroski F: each test that has its inputs is scored; the score counts only when all nine ran
    tests = []

    def test(name: str, keys: list, fn, detail) -> None:
        # a key ending in "~" is needed for both years; the others for the current year only
        ks = [k.rstrip("~") for k in keys]
        miss = need([k for k, raw in zip(ks, keys) if raw.endswith("~")], ("now", "before")) + need([k for k, raw in zip(ks, keys) if not raw.endswith("~")])
        if miss:
            tests.append({"name": name, "passed": None, "missing": miss, "inputs": ks})
            return
        ok = fn()
        tests.append({"name": name, "passed": bool(ok), "detail": detail(), "inputs": ks})

    pct = lambda x: f"{x * 100:.1f}%" if x is not None else "n/a"
    roa = lambda w: _div(v("net_income", w), v("total_assets", w))
    test("Return on assets is positive", ["net_income", "total_assets"], lambda: roa("now") > 0, lambda: f"Net income / total assets = {pct(roa('now'))}")
    test("Operating cash flow is positive", ["cfo"], lambda: v("cfo") > 0, lambda: f"Operating cash flow {inputs['cfo']['now']['display']}")
    test("Return on assets improved", ["net_income~", "total_assets~"], lambda: roa("now") > roa("before"), lambda: f"{pct(roa('before'))} to {pct(roa('now'))}")
    test("Cash flow exceeds net income (accruals)", ["cfo", "net_income"], lambda: v("cfo") > v("net_income"),
         lambda: f"Operating cash flow {inputs['cfo']['now']['display']} vs net income {inputs['net_income']['now']['display']}")
    lev = lambda w: _div(v("long_term_debt", w), v("total_assets", w))
    test("Leverage fell (long-term debt / assets)", ["long_term_debt~", "total_assets~"], lambda: lev("now") < lev("before"), lambda: f"{pct(lev('before'))} to {pct(lev('now'))}")
    cr = lambda w: _div(v("current_assets", w), v("current_liabilities", w))
    test("Current ratio rose", ["current_assets~", "current_liabilities~"], lambda: cr("now") > cr("before"), lambda: f"{cr('before'):.2f} to {cr('now'):.2f}")
    test("No new shares (diluted share count did not rise)", ["shares_diluted~"], lambda: v("shares_diluted") <= v("shares_diluted", "before"),
         lambda: f"{inputs['shares_diluted']['before']['display']} to {inputs['shares_diluted']['now']['display']}")
    gm = lambda w: _div(v("gross_profit", w), v("revenue", w))
    test("Gross margin rose", ["gross_profit~", "revenue~"], lambda: gm("now") > gm("before"), lambda: f"{pct(gm('before'))} to {pct(gm('now'))}")
    at = lambda w: _div(v("revenue", w), v("total_assets", w))
    test("Asset turnover rose", ["revenue~", "total_assets~"], lambda: at("now") > at("before"), lambda: f"{at('before'):.3f} to {at('now'):.3f}")
    ran = [t for t in tests if t["passed"] is not None]
    out["piotroski"] = {"status": "computed" if len(ran) == 9 else "partial" if ran else "not computed", "value": sum(1 for t in ran if t["passed"]),
                        "max": len(ran), "tests": tests, "basis": "Year-end total assets are used for return on assets, leverage and turnover (the filing prints two balance sheets)."}
    if bank:
        out["piotroski"]["caveat"] = "Built for industrial companies; for a bank the margin and current-ratio tests are not meaningful."

    # ---- Beneish M
    if bank:
        out["beneish"] = {"status": "not meaningful", "reason": "Beneish's variables (receivables, gross margin, PP&E) do not describe a bank's accounts."}
    else:
        keys = ["receivables", "revenue", "gross_profit", "current_assets", "ppe", "total_assets", "depreciation", "sga", "current_liabilities", "long_term_debt"]
        miss = need(keys, ("now", "before")) + need(["net_income", "cfo"])
        if miss:
            out["beneish"] = {"status": "not computed", "missing": sorted(set(miss))}
        else:
            gmr = lambda w: v("gross_profit", w) / v("revenue", w)
            aq = lambda w: 1 - (v("current_assets", w) + v("ppe", w)) / v("total_assets", w)
            dep = lambda w: abs(v("depreciation", w)) / (abs(v("depreciation", w)) + v("ppe", w))
            parts = [
                ("DSRI", "Days' sales in receivables index", (v("receivables") / v("revenue")) / (v("receivables", "before") / v("revenue", "before")), 0.920, ["receivables", "revenue"]),
                ("GMI", "Gross margin index", gmr("before") / gmr("now"), 0.528, ["gross_profit", "revenue"]),
                ("AQI", "Asset quality index", aq("now") / aq("before") if aq("before") else None, 0.404, ["current_assets", "ppe", "total_assets"]),
                ("SGI", "Sales growth index", v("revenue") / v("revenue", "before"), 0.892, ["revenue"]),
                ("DEPI", "Depreciation index", dep("before") / dep("now") if dep("now") else None, 0.115, ["depreciation", "ppe"]),
                ("SGAI", "SG&A index", (v("sga") / v("revenue")) / (v("sga", "before") / v("revenue", "before")), -0.172, ["sga", "revenue"]),
                ("LVGI", "Leverage index", ((v("current_liabilities") + v("long_term_debt")) / v("total_assets")) /
                                           ((v("current_liabilities", "before") + v("long_term_debt", "before")) / v("total_assets", "before")), -0.327,
                 ["current_liabilities", "long_term_debt", "total_assets"]),
                ("TATA", "Total accruals to total assets", (v("net_income") - v("cfo")) / v("total_assets"), 4.679, ["net_income", "cfo", "total_assets"]),
            ]
            if any(p[2] is None for p in parts):
                out["beneish"] = {"status": "not computed", "missing": [f"{p[1]} (a zero denominator)" for p in parts if p[2] is None]}
            else:
                m = -4.84 + sum(p[2] * p[3] for p in parts)
                out["beneish"] = {"status": "computed", "model": "Beneish M (1999, eight variables)", "value": round(m, 2), "threshold": -1.78,
                                  "flag": m > -1.78, "terms": [{"code": c, "name": n, "value": round(x, 3), "weight": w, "inputs": ins} for c, n, x, w, ins in parts],
                                  "reading": ("Above -1.78: the paper's flag for a higher likelihood of earnings manipulation. A flag is a reason to read the notes, not a finding."
                                              if m > -1.78 else "At or below -1.78: not flagged by the paper's cutoff.")}
    return out
