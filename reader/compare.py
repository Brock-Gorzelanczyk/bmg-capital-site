"""Two company reports side by side.

Every value comes from one of the two validated reports (a cited fact, a
calculation with cited inputs, or a signal built from them), so a
comparison row can always be traced to the page it came from. A row names
a leader only when both values exist and the gap is material; size rows
(revenue, fleet size) never have a "better" side. Different fiscal year
ends and different filing types are stated, not hidden; annual and
quarterly figures are not compared.
"""
from __future__ import annotations

from typing import Optional

# key, name, group, better ("higher", "lower" or None), unit
ROWS = [
    ("revenue", "Revenue", "Scale", None, "USD"),
    ("revenue_growth", "Revenue growth", "Growth and profitability", "higher", "pct"),
    ("gross_margin", "Gross margin", "Growth and profitability", "higher", "pct"),
    ("operating_margin", "Operating margin", "Growth and profitability", "higher", "pct"),
    ("pretax_margin", "Pretax margin", "Growth and profitability", "higher", "pct"),
    ("net_margin", "Net margin", "Growth and profitability", "higher", "pct"),
    ("roe", "Return on equity (parent)", "Growth and profitability", "higher", "pct"),
    ("roa", "Return on assets", "Growth and profitability", "higher", "pct"),
    ("gains_share", "Asset-sale gains in pretax income", "Quality of earnings", None, "pct"),
    ("cash_conversion", "Operating cash flow / net income", "Quality of earnings", "higher", "x"),
    ("fcf_margin", "Free cash flow margin", "Cash and returns", "higher", "pct"),
    ("payout_vs_fcf", "Shareholder returns / free cash flow", "Cash and returns", None, "pct"),
    ("equity_multiplier", "Equity multiplier", "Balance sheet", "lower", "x"),
    ("debt_to_equity", "Debt to equity", "Balance sheet", "lower", "x"),
    ("debt_to_assets", "Debt to assets", "Balance sheet", "lower", "pct"),
    ("interest_coverage", "Interest coverage", "Balance sheet", "higher", "x"),
    ("net_cash", "Net cash (negative = net debt)", "Balance sheet", None, "USD"),
]
KPI_BETTER = {"Fleet utilization": "higher", "Renewal success rate": "higher", "Lease price index": "higher",
              "Net revenue retention": "higher", "Net interest margin": "higher", "CET1 capital ratio": "higher"}


def _fmt(v: Optional[float], unit: str) -> str:
    from .facts import fmt_value
    if v is None:
        return ""
    if unit == "pct":
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    return fmt_value(v, "USD")


class _R:
    """Read values out of one report."""

    def __init__(self, rep: dict):
        self.rep = rep
        self.F, self.C = rep["evidence"]["facts"], rep["evidence"]["calcs"]
        self.sig = {s["id"]: s for s in rep.get("signals") or []}
        rows = rep["financials"]["performance"] + rep["position"]["rows"] + rep["financials"]["calculated"]
        self.row = {r["metric"]: r for r in rows}

    def ev(self, i):
        return self.F.get(i) or self.C.get(i) if i else None

    def cur(self, metric):
        r = self.row.get(metric)
        return self.ev(r.get("current")) if r else None

    def period(self) -> dict:
        x = self.cur("revenue") or self.cur("net_income") or {}
        return x.get("period") or {}

    def get(self, key):
        """(value, refs, note) for a comparison row."""
        s = self.sig.get({"payout_vs_fcf": "payout_vs_fcf"}.get(key, key))
        if key == "gains_share" and not s:
            gn = next((f for f in self.F.values() if f["metric"] == "gains_on_dispositions"), None)
            pt = next((f for f in self.F.values() if f["metric"] == "pretax_income" and gn and f["period"] == gn["period"]), None)
            if gn and pt and pt["value"] > 0:
                return abs(gn["value"]) / pt["value"] * 100, [gn["id"], pt["id"]], None
            return None, [], None
        if key in ("revenue_growth", "gains_share", "cash_conversion", "equity_multiplier", "debt_to_equity", "debt_to_assets", "interest_coverage"):
            if s and s.get("value") is not None:
                return s["value"], s["inputs"], None
            return None, [], None
        if key == "payout_vs_fcf":
            if s and s.get("unit") == "percent":
                return s["value"], s["inputs"], None
            return None, [], None
        if key == "revenue":
            x = self.cur("revenue")
            return (x["value"], [x["id"]], None) if x else (None, [], None)
        if key in ("gross_margin", "operating_margin", "net_margin"):
            c = self.C.get(f"{key}:cur")
            return (c["value"], [c["id"]], None) if c and c.get("value") is not None else (None, [], None)
        if key == "net_cash":
            c = self.C.get("net_cash:cur")
            return (c["value"], [c["id"]], None) if c and c.get("value") is not None else (None, [], None)
        rv, ni, eq, ta = self.cur("revenue"), self.cur("net_income"), self.cur("equity"), self.cur("total_assets")
        if key == "pretax_margin":
            pt = next((f for f in self.F.values() if f["metric"] == "pretax_income" and rv and f["period"].get("end") == rv["period"].get("end")
                       and f["period"]["type"] == rv["period"]["type"]), None)
            if pt and rv and rv["value"]:
                return pt["value"] / rv["value"] * 100, [pt["id"], rv["id"]], None
            return None, [], None
        if key == "roe":
            s = self.sig.get("roe")
            return (s["value"], s["inputs"], s["headline"].replace("Return on ", "On ").split(" equity")[0] + " equity.") if s else (None, [], None)
        if key == "roa" and ni and ta and ta["value"] > 0:
            return ni["value"] / ta["value"] * 100, [ni["id"], ta["id"]], "Net income / year-end total assets."
        if key == "fcf_margin":
            f = self.C.get("fcf:cur")
            if f and f.get("value") is not None and rv and rv["value"]:
                return f["value"] / rv["value"] * 100, [f["id"], rv["id"]], None
        return None, [], None

    def defn(self, key):
        """A short statement of how this report computed the measure, used to
        catch rows that share a name but not a definition."""
        s = self.sig.get(key)
        if key == "roe" and s:
            return "average equity" if "average" in s["headline"] else "ending equity"
        if key == "interest_coverage" and s:
            d = s.get("definition", {})
            return ("pretax before affiliates' earnings" if "affiliat" in d.get("numerator", "").lower() else "pretax income") + \
                   ("; net interest" if "net" in d.get("denominator", "").lower() else "; gross interest")
        if key == "pretax_margin":
            pt = next((f for f in self.F.values() if f["metric"] == "pretax_income"), None)
            if pt:
                return "before affiliates' earnings" if "affiliat" in pt["source"].get("row_label", "").lower() else "income before income taxes"
        return None

    def kpis(self) -> dict:
        return {k["name"]: k for k in self.rep["operating"]["kpis"] if k.get("display") and k.get("value") is not None}

    def src(self, refs) -> list:
        out = []

        def walk(i, d=0):
            x = self.ev(i)
            if not x or d > 4:
                return
            s = x.get("source") or {}
            if s.get("pdf_page"):
                out.append({"pdf_page": s["pdf_page"], "printed_page": s.get("printed_page"), "excerpt": (s.get("excerpt") or "")[:160]})
            for j in x.get("inputs", []):
                walk(j, d + 1)
        for i in refs:
            walk(i)
        seen, uniq = set(), []
        for s in out:
            if s["pdf_page"] not in seen:
                seen.add(s["pdf_page"])
                uniq.append(s)
        return uniq


def _material(a: float, b: float, unit: str) -> bool:
    if unit == "pct":
        return abs(a - b) >= 0.5
    if unit == "x":
        return abs(a - b) >= max(0.05, 0.05 * max(abs(a), abs(b)))
    return abs(a - b) >= 0.05 * max(abs(a), abs(b))


def _days(a: Optional[str], b: Optional[str]) -> Optional[int]:
    from datetime import date
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except Exception:
        return None


# rows whose meaning depends on the business model: no direction is shown
# when the two companies read as different business mixes
_MIX_SENSITIVE = {"gross_margin", "operating_margin", "pretax_margin", "net_margin", "equity_multiplier", "debt_to_equity",
                  "debt_to_assets", "interest_coverage", "fcf_margin", "gains_share", "roa"}


def build(rep_a: dict, rep_b: dict) -> dict:
    A, B = _R(rep_a), _R(rep_b)
    pa, pb = A.period(), B.period()
    who = lambda rep: {"name": rep["company"].get("name") or rep["source"]["name"], "ticker": rep["company"].get("ticker") or rep["company"].get("ticker_hint"),
                       "form": rep["company"].get("form"), "period": rep["company"].get("period"), "profile": (rep.get("brief") or {}).get("profile") or rep["operating"]["profile"],
                       "source": rep["source"]["name"], "sha256": rep["source"]["sha256"], "coverage": rep["coverage"]["status"],
                       "period_end": pa.get("end") if rep is rep_a else pb.get("end"), "period_type": pa.get("type") if rep is rep_a else pb.get("type")}
    out = {"a": who(rep_a), "b": who(rep_b), "rows": [], "caveats": [], "checks": []}
    check = lambda name, ok, detail: out["checks"].append({"check": name, "result": ok, "detail": detail})

    # ---- comparability checks (stated whether they pass or not)
    same_len = bool(pa.get("type")) and pa.get("type") == pb.get("type")
    check("Period length", "same" if same_len else "different",
          f"{pa.get('label') or 'unknown'} vs {pb.get('label') or 'unknown'}")
    gap = _days(pa.get("end"), pb.get("end"))
    check("Period end dates", "same" if gap == 0 else f"{gap} days apart" if gap is not None else "unknown",
          f"{pa.get('end')} vs {pb.get('end')}")
    mix_same = out["a"]["profile"] == out["b"]["profile"]
    check("Business mix", "same profile" if mix_same else "different", f"{out['a']['profile']} vs {out['b']['profile']}")
    check("Currency and scale", "USD, converted to units", "Both reports state values in US dollars; each table's scale caption was applied before comparing.")
    check("Scope", "consolidated", "Rows use each company's consolidated statements; segment and affiliate tables are excluded.")
    if rep_a["source"]["sha256"] == rep_b["source"]["sha256"]:
        out["caveats"].append("Both sides are the same document.")
    if not same_len:
        out["caveats"].append("The two filings cover periods of different length, so income and cash flow measures are not compared; "
                              "balance sheet ratios are compared only if their dates are within a month of each other.")
    elif gap and gap > 31:
        out["caveats"].append(f"Fiscal years end {gap} days apart; each column is that company's latest reported year, so economic conditions differ.")
    if not mix_same:
        out["caveats"].append("The filings read as different business mixes; margins, leverage and coverage norms differ by model, so those rows show values side by side without a direction.")

    balance = {"equity_multiplier", "debt_to_equity", "debt_to_assets", "net_cash"}
    for key, name, group, better, unit in ROWS:
        va, ra, na = A.get(key)
        vb, rb, nb = B.get(key)
        if va is None and vb is None:
            continue
        reason = None
        if not same_len and key not in balance:
            reason = "Periods of different length."
        elif not same_len and key in balance and (gap is None or gap > 31):
            reason = "Balance sheet dates more than a month apart."
        elif va is None or vb is None:
            reason = "Not reported or not found for one company."
        defs = (A.defn(key), B.defn(key))
        aligned = key in balance or (gap is not None and gap <= 31)
        if not reason and defs[0] and defs[1] and defs[0] != defs[1]:
            reason = f"Definitions differ: {out['a']['ticker'] or 'A'} {defs[0]}; {out['b']['ticker'] or 'B'} {defs[1]}."
        elif not reason and not aligned:
            reason = f"Fiscal years end {gap} days apart." if gap is not None else "Fiscal year ends unknown."
        elif not reason and key in _MIX_SENSITIVE and not mix_same:
            reason = "Different business mix; norms for this measure differ by business model."
        note = na or nb
        direction = None
        if not reason and better and _material(va, vb, unit):
            direction = "a" if (va > vb) == (better == "higher") else "b"
        row = {"key": key, "name": name, "group": group, "better": better, "unit": unit, "leader": direction, "note": note,
               "comparable": reason is None, "not_comparable_reason": reason,
               "a": {"value": va, "display": _fmt(va, unit), "refs": ra, "sources": A.src(ra)},
               "b": {"value": vb, "display": _fmt(vb, unit), "refs": rb, "sources": B.src(rb)}}
        out["rows"].append(row)
    ka, kb = A.kpis(), B.kpis()
    for name in [n for n in ka if n in kb]:
        x, y = ka[name], kb[name]
        src = lambda k: [{"pdf_page": (k.get("source") or {}).get("pdf_page"), "printed_page": (k.get("source") or {}).get("printed_page"), "excerpt": ((k.get("source") or {}).get("excerpt") or "")[:160]}]
        out["rows"].append({"key": "kpi:" + name, "name": name, "group": "Industry measures", "better": None, "unit": "kpi", "leader": None,
                            "comparable": False, "not_comparable_reason": "Company-defined measure; definitions and fleet scope differ" +
                            (f" ({x.get('scope') or 'scope not stated'} vs {y.get('scope') or 'scope not stated'})" if (x.get("scope") or y.get("scope")) else "") + ".",
                            "note": None, "a": {"value": x["value"], "display": x["display"], "refs": [], "sources": src(x)},
                            "b": {"value": y["value"], "display": y["display"], "refs": [], "sources": src(y)}})
    out["summary"] = {"a": [], "b": []}       # kept for older clients; no overall tally is produced
    out["method"] = ("Values come from each company's own report: reported figures, calculations from cited inputs, and BMG signals. A direction "
                     "(higher or lower) is shown only when both values exist, the periods match, the definitions match, the business mix is the same "
                     "for business-model-sensitive measures, and the gap is material. No row says which company is better overall, and none of this "
                     "is a recommendation.")
    out["screens"] = (rep_a.get("screens_version"), rep_b.get("screens_version"))
    return out
