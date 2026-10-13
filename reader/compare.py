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
    ("roe", "Return on equity", "Growth and profitability", "higher", "pct"),
    ("roa", "Return on assets", "Growth and profitability", "higher", "pct"),
    ("gains_share", "Asset-sale gains in pretax income", "Quality of earnings", "lower", "pct"),
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
        if key == "roe" and ni and eq and eq["value"] > 0:
            return ni["value"] / eq["value"] * 100, [ni["id"], eq["id"]], "Net income / year-end shareholders' equity."
        if key == "roa" and ni and ta and ta["value"] > 0:
            return ni["value"] / ta["value"] * 100, [ni["id"], ta["id"]], "Net income / year-end total assets."
        if key == "fcf_margin":
            f = self.C.get("fcf:cur")
            if f and f.get("value") is not None and rv and rv["value"]:
                return f["value"] / rv["value"] * 100, [f["id"], rv["id"]], None
        return None, [], None

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


def build(rep_a: dict, rep_b: dict) -> dict:
    A, B = _R(rep_a), _R(rep_b)
    pa, pb = A.period(), B.period()
    who = lambda rep: {"name": rep["company"].get("name") or rep["source"]["name"], "ticker": rep["company"].get("ticker") or rep["company"].get("ticker_hint"),
                       "form": rep["company"].get("form"), "period": rep["company"].get("period"), "profile": (rep.get("brief") or {}).get("profile") or rep["operating"]["profile"],
                       "source": rep["source"]["name"], "sha256": rep["source"]["sha256"], "coverage": rep["coverage"]["status"]}
    out = {"a": who(rep_a), "b": who(rep_b), "rows": [], "caveats": [], "summary": {"a": [], "b": []}}
    flows_ok = True
    if pa.get("type") and pb.get("type") and pa["type"] != pb["type"]:
        flows_ok = False
        out["caveats"].append(f"{out['a']['name']} figures are {pa.get('label')} and {out['b']['name']} figures are {pb.get('label')}. "
                              "Income and cash flow measures cover different lengths of time, so only balance sheet ratios are compared.")
    elif pa.get("end") and pb.get("end") and pa["end"] != pb["end"]:
        out["caveats"].append(f"Fiscal periods end on different dates ({pa.get('label')} vs {pb.get('label')}); the comparison is of each company's latest reported year.")
    if out["a"]["profile"] != out["b"]["profile"]:
        out["caveats"].append(f"The filings read as different business mixes ({out['a']['profile']} vs {out['b']['profile']}); margins and leverage norms differ by business model.")
    if rep_a["source"]["sha256"] == rep_b["source"]["sha256"]:
        out["caveats"].append("Both sides are the same document.")
    balance = {"equity_multiplier", "debt_to_equity", "debt_to_assets", "net_cash"}
    for key, name, group, better, unit in ROWS:
        if not flows_ok and key not in balance:
            continue
        va, ra, na = A.get(key)
        vb, rb, nb = B.get(key)
        if va is None and vb is None:
            continue
        leader = None
        if better and va is not None and vb is not None and _material(va, vb, unit):
            leader = "a" if (va > vb) == (better == "higher") else "b"
        row = {"key": key, "name": name, "group": group, "better": better, "unit": unit, "leader": leader, "note": na or nb,
               "a": {"value": va, "display": _fmt(va, unit), "refs": ra, "sources": A.src(ra)},
               "b": {"value": vb, "display": _fmt(vb, unit), "refs": rb, "sources": B.src(rb)}}
        out["rows"].append(row)
        if leader:
            out["summary"][leader].append(name)
    ka, kb = A.kpis(), B.kpis()
    for name in [n for n in ka if n in kb]:
        x, y = ka[name], kb[name]
        if x.get("unit") != y.get("unit") and not (name in KPI_BETTER):
            continue
        better = KPI_BETTER.get(name)
        leader = None
        if better and _material(x["value"], y["value"], "pct"):
            leader = "a" if (x["value"] > y["value"]) == (better == "higher") else "b"
        out["rows"].append({"key": "kpi:" + name, "name": name, "group": "Industry measures", "better": better, "unit": "kpi", "leader": leader,
                            "note": "Each company defines and scopes this measure itself" + (f" ({x.get('scope')} / {y.get('scope')})" if x.get("scope") or y.get("scope") else "") + "; compare with care.",
                            "a": {"value": x["value"], "display": x["display"], "refs": [], "sources": [{"pdf_page": (x.get("source") or {}).get("pdf_page"), "printed_page": (x.get("source") or {}).get("printed_page"), "excerpt": ((x.get("source") or {}).get("excerpt") or "")[:160]}]},
                            "b": {"value": y["value"], "display": y["display"], "refs": [], "sources": [{"pdf_page": (y.get("source") or {}).get("pdf_page"), "printed_page": (y.get("source") or {}).get("printed_page"), "excerpt": ((y.get("source") or {}).get("excerpt") or "")[:160]}]}})
        if leader:
            out["summary"][leader].append(name)
    out["method"] = ("Values come from each company's own report: reported figures, calculations from cited inputs, and BMG signals. A side is marked as "
                     "ahead only when both values exist and the gap is material (half a percentage point, or 5%). Leading on a measure is not a recommendation.")
    return out
