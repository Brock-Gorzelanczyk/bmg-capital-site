"""What to look for, and what the numbers say.

brief(company, profile, why)  -> the analyst brief shown before extraction:
    the company, the industry it was read as (and why), and the measures
    that matter for that kind of business, each with the reason.
analyze(fb, profile, kpis)    -> signals: ratios computed from cited facts,
    each with a severity (flag, watch, observation), the versioned screen
    behind it, and the facts it rests on. Signals are BMG interpretation of
    reported figures, never statements from the filing, and never advice.

Thresholds are rules of thumb for a first read, adjusted by industry where
the business model changes what is normal (a railcar lessor funds
long-lived assets with debt, so its leverage reads differently from a
software company's). They flag where to look; they do not grade the company.
"""
from __future__ import annotations

from typing import Optional

from .facts import fmt_value

SEVERITIES = ("flag", "watch", "observation")

# what to look for: key, title, why it matters for this kind of business
GENERAL = [
    ("growth", "Revenue growth", "Is the business getting bigger, and is growth speeding up or slowing?"),
    ("margins", "Operating margin and its trend", "How much of each dollar of revenue is left after running the business."),
    ("leverage", "Leverage: debt to equity and the equity multiplier", "How much of the balance sheet is funded by borrowing rather than owners' capital."),
    ("coverage", "Interest coverage", "Whether earnings comfortably cover the interest bill."),
    ("cash_conversion", "Cash conversion: operating cash flow vs net income", "Whether reported profit turns into cash."),
    ("payouts", "Buybacks and dividends vs free cash flow", "Whether shareholder returns are funded by the business or by the balance sheet."),
    ("liquidity", "Cash against debt", "How much debt sits above the cash on hand."),
]
FOCUS = {
    "Railcar leasing": [
        ("kpi:Fleet utilization", "Fleet utilization", "A lessor earns only on cars that are on lease; utilization is the first read on demand."),
        ("kpi:Lease price index", "Lease rates on renewal", "Whether renewals reprice up or down drives revenue for years."),
        ("debt_assets", "Debt to assets", "Lessors fund long-lived railcars largely with debt; this is the core leverage measure."),
        ("coverage", "Interest coverage", "Interest is a lessor's largest cost after depreciation and maintenance."),
        ("gains_share", "Earnings from asset sales", "Gains on selling railcars can be a large and lumpy part of pretax income."),
        ("investment", "Fleet investment vs operating cash flow", "Growth is bought with capex; heavy investment makes free cash flow negative by design."),
    ],
    "Railcar manufacturing": [
        ("kpi:Backlog (units)", "Backlog", "Orders already booked are next year's deliveries and revenue."),
        ("kpi:Deliveries (units)", "Deliveries", "Units shipped in the period."),
        ("margins", "Gross and operating margin", "Manufacturing margins swing with volume and steel and labor costs."),
        ("inventory", "Inventory vs revenue", "Inventory building faster than sales can signal slowing demand."),
        ("debt_equity", "Debt to equity", "Cyclical manufacturers need balance sheet room for downturns."),
    ],
    "Software and subscriptions": [
        ("kpi:Remaining performance obligations", "Remaining performance obligations", "Contracted revenue not yet recognized."),
        ("kpi:Net revenue retention", "Net revenue retention", "Whether existing customers spend more each year."),
        ("sbc", "Stock-based compensation vs revenue", "A real cost that does not show in cash flow."),
        ("fcf_margin", "Free cash flow margin", "Software economics show up in cash."),
    ],
    "Consumer products": [
        ("margins", "Gross margin", "Pricing power and product mix show up in gross margin."),
        ("inventory", "Inventory vs revenue", "Inventory building faster than sales can signal slowing demand."),
        ("payouts", "Buybacks and dividends vs free cash flow", "Large buybacks can shrink equity and inflate leverage ratios."),
    ],
    "Banking": [
        ("kpi:Net interest margin", "Net interest margin", "The spread between what the bank earns on loans and pays on deposits."),
        ("kpi:CET1 capital ratio", "CET1 capital ratio", "Regulatory capital cushion against losses."),
        ("kpi:Allowance for credit losses", "Allowance for credit losses", "Reserves set aside for loans that may not be repaid."),
    ],
}
def brief(company: dict, profile: str, scores: dict) -> dict:
    """The plan of what to look for, available right after parsing."""
    terms = sorted(((k, v) for k, v in (scores or {}).items() if v), key=lambda x: -x[1])
    why = (f"Read as {profile.lower()} because its industry vocabulary appears most often in the filing "
           f"({', '.join(f'{k.lower()} {v}' for k, v in terms[:3])} mentions)." if profile != "General"
           else "No industry vocabulary stood out, so the general checks apply.")
    items, seen = [], set()
    ind = [x for part in profile.split(" + ") for x in FOCUS.get(part, [])]
    for key, title, why_it in ind + ([] if profile == "Banking" else GENERAL):
        k = key.split(":")[0] if not key.startswith("kpi:") else key
        if k in seen or (key in ("margins", "payouts", "coverage") and key in seen):
            continue
        seen.add(k)
        items.append({"key": key, "title": title, "why": why_it, "industry": key in {x[0] for x in ind}})
    return {"company": company.get("name"), "ticker": company.get("ticker"), "form": company.get("form"), "period": company.get("period"),
            "profile": profile, "why": why, "items": items}


import json as _json
import os as _os
import re as _re

_SCREENS_PATH = _os.environ.get("READER_SCREENS") or _os.path.join(_os.path.dirname(__file__), "screens.json")


def screens() -> dict:
    with open(_SCREENS_PATH) as fh:
        return _json.load(fh)


def _screen_for(profile: str) -> tuple[dict, str]:
    cfg = screens()
    main = profile.split(" + ")[0]
    key = main if main in cfg["profiles"] else "default"
    return cfg["profiles"][key], key


def _sig(sid, title, value, display, severity, headline, text, rule, inputs, focus, unit="ratio", definition=None, followup=None):
    assert severity in SEVERITIES
    return {"id": sid, "title": title, "value": value, "display": display, "unit": unit, "severity": severity, "headline": headline,
            "text": text, "rule": rule, "inputs": [i["id"] for i in inputs if i], "focus": focus, "definition": definition or {},
            "followup": followup, "basis": "BMG interpretation of reported figures", "kind": "interpreted"}


def _x(v: float) -> str:
    return f"{v:.1f}x"


def _pct(v: float) -> str:
    return f"{v:.0f}%"


def _grade(v: float, scr: Optional[dict], higher_is_worse: bool = True) -> str:
    """flag / watch / observation against a screen ({"watch": a, "flag": b});
    no screen for this business model -> observation."""
    if not scr:
        return "observation"
    w, f = scr.get("watch"), scr.get("flag")
    if higher_is_worse:
        if f is not None and v > f:
            return "flag"
        if w is not None and v > w:
            return "watch"
    else:
        if f is not None and v < f:
            return "flag"
        if w is not None and v < w:
            return "watch"
    return "observation"


def _screen_text(name: str, scr: Optional[dict], fmt, version: str, prof_key: str, higher_is_worse: bool = True) -> str:
    if not scr or (scr.get("watch") is None and scr.get("flag") is None):
        return f"No screen applies to this measure for a {prof_key.lower()} profile ({version})."
    parts = []
    word = "above" if higher_is_worse else "below"
    if scr.get("watch") is not None:
        parts.append(f"watch {word} {fmt(scr['watch'])}")
    if scr.get("flag") is not None:
        parts.append(f"flag {word} {fmt(scr['flag'])}")
    return f"Screen ({'lessor' if prof_key == 'Railcar leasing' else prof_key.lower()} profile, {version}): " + ", ".join(parts) + "."


def _rows_like(fb, stmt: str, pat: str, table_id=None) -> list:
    from .facts import _core
    return [r for r in fb.rows if r["statement"] == stmt and _core(r) and r["unit"] == "USD" and _re.search(pat, r["key"])
            and (table_id is None or r.get("table_id") == table_id)]


COMBINED_INTEREST = r"foreign exchange|currency|\band other\b|other (income|expense)"


def analyze(fb, profile: str, kpis: list, watch: Optional[list] = None) -> list[dict]:
    """Signals from the current period's cited facts. A signal whose inputs
    are missing or of different periods is not produced. Thresholds come from
    screens.json; where no screen applies the signal is an observation."""
    from .facts import SCALE
    g = lambda m, w=0: fb.get(m, w)
    def source_table(x):
        if not x:
            return None
        rows = [x] if x.get("source") else [fb.facts[i] for i in x.get("inputs", []) if i in fb.facts]
        ids = {(r.get("source") or {}).get("table_id") for r in rows}
        return next(iter(ids)) if len(ids) == 1 and None not in ids else None

    same = lambda *xs: all(xs) and len({(x["period"]["type"], x["period"]["end"] or x["period"]["year"]) for x in xs}) == 1
    scr_all, prof_key = _screen_for(profile)
    version = screens()["version"]
    lessor = prof_key == "Railcar leasing"
    bank = prof_key == "Banking"
    out = []
    ta, debt, cash = g("total_assets"), g("total_debt"), g("cash")
    eq_t, eq_p = g("equity_total") or g("equity"), g("equity_parent") or g("equity")
    eq_label = "total equity (including noncontrolling interests)" if g("equity_nci") else "total equity"
    debt_def = (debt or {}).get("definition") or "Total debt as printed on the balance sheet."

    # ---- leverage
    if same(ta, eq_t) and eq_t["value"] > 0 and not bank:
        em = ta["value"] / eq_t["value"]
        scr = scr_all.get("equity_multiplier")
        sev = _grade(em, scr)
        text = f"Total assets of {ta['display']} are {em:.1f} times {eq_label} of {eq_t['display']}."
        if same(debt, ta, eq_t):
            other_l = ta["value"] - eq_t["value"] - debt["value"]
            text += (f" Of the liabilities, {debt['display']} is debt ({debt['value'] / ta['value'] * 100:.0f}% of assets) and "
                     f"{fmt_value(other_l, 'USD')} is other liabilities (for example payables, deferred taxes, leases).")
        out.append(_sig("equity_multiplier", "Equity multiplier", em, _x(em), sev, f"Equity multiplier {_x(em)}", text,
                        _screen_text("equity multiplier", scr, _x, version, prof_key), [ta, eq_t], "leverage",
                        definition={"numerator": "Total assets (balance sheet)", "denominator": f"{eq_label.capitalize()} (balance sheet)",
                                    "period": ta["period"]["label"], "scope": "Consolidated", "formula": "Total assets / total equity",
                                    "balances": "Ending balances at the balance sheet date, not averages.",
                                    "missing": "Not computed if either input is missing, dated differently, or equity is zero or negative.",
                                    "applicability": "Not applied to banks." }))
    if same(debt, eq_t) and eq_t["value"] > 0 and not bank:
        de = debt["value"] / eq_t["value"]
        scr = scr_all.get("debt_to_equity")
        out.append(_sig("debt_to_equity", "Debt to equity", de, _x(de), _grade(de, scr), f"Debt is {_x(de)} total equity",
                        f"Total debt of {debt['display']} against {eq_label} of {eq_t['display']}. {debt_def}",
                        _screen_text("debt to equity", scr, _x, version, prof_key), [debt, eq_t], "debt_equity" if not lessor else "leverage",
                        definition={"numerator": "Total debt: " + debt_def, "denominator": eq_label.capitalize(), "period": debt["period"]["label"],
                                    "scope": "Consolidated", "formula": "Total debt / total equity", "balances": "Ending balances; gross debt (cash not netted).",
                                    "missing": "Not computed if debt is not found; debt is never assumed to be zero.",
                                    "applicability": "Not applied to banks."}))
    if same(debt, ta) and ta["value"] > 0 and not bank:
        da = debt["value"] / ta["value"]
        scr = scr_all.get("debt_to_assets")
        out.append(_sig("debt_to_assets", "Debt to assets", da * 100, f"{da * 100:.1f}%", _grade(da, scr), f"Debt funds {da * 100:.1f}% of assets",
                        f"{debt['display']} of debt against {ta['display']} of total assets.",
                        _screen_text("debt to assets", scr, lambda v: f"{v * 100:.0f}%", version, prof_key), [debt, ta],
                        "debt_assets" if lessor else "leverage", unit="percent",
                        definition={"numerator": "Total debt: " + debt_def, "denominator": "Total assets", "period": ta["period"]["label"],
                                    "scope": "Consolidated", "formula": "Total debt / total assets", "balances": "Ending balances; gross debt.",
                                    "missing": "Not computed if debt is not found.", "applicability": "Not applied to banks."}))

    # ---- interest coverage
    pti, ie = g("pretax_income"), g("interest_expense")
    # a line that mixes interest with other items is not interest expense; no ratio (the report asks instead)
    if ie and _re.search(COMBINED_INTEREST, ie["source"].get("row_label", ""), _re.I):
        ie = None
    if same(pti, ie) and abs(ie["value"]) > 0 and not bank:
        cov = (pti["value"] + abs(ie["value"])) / abs(ie["value"])
        scr = scr_all.get("interest_coverage")
        ie_lab, pt_lab = ie["source"].get("row_label", ""), pti["source"].get("row_label", "")
        notes = []
        if _re.search(r"\bnet\b", ie_lab, _re.I):
            notes.append(f"interest is \"{ie_lab}\", net of interest income as printed")
        if _re.search(r"affiliat|equity method|unconsolidated", pt_lab, _re.I):
            notes.append(f"pretax income is \"{pt_lab}\", which is before the share of affiliates' earnings")
        out.append(_sig("interest_coverage", "Interest coverage", cov, _x(cov), _grade(cov, scr, higher_is_worse=False),
                        f"Pretax income plus interest covers interest {_x(cov)}",
                        f"Income before income taxes of {pti['display']} plus interest of {fmt_value(abs(ie['value']), 'USD')}, divided by that interest."
                        + (" Note: " + "; ".join(notes) + "." if notes else ""),
                        _screen_text("interest coverage", scr, _x, version, prof_key, higher_is_worse=False), [pti, ie], "coverage",
                        definition={"numerator": f"\"{pt_lab}\" + |\"{ie_lab}\"|", "denominator": f"|\"{ie_lab}\"|", "period": pti["period"]["label"],
                                    "scope": "Consolidated", "formula": "(Income before income taxes + interest expense) / interest expense",
                                    "missing": "Not computed if interest expense is not a separate line.",
                                    "applicability": "Not applied to banks, where interest expense is an operating cost."}))

    # ---- gains on asset sales across every year shown
    gn0, pt0 = g("gains_on_dispositions"), g("pretax_income")
    if same(gn0, pt0) and pt0["value"] > 0 and abs(gn0["value"]) > 0:
        years, ins = [], []
        for w in range(3):
            gn, pt = g("gains_on_dispositions", w), g("pretax_income", w)
            if same(gn, pt) and pt["value"] > 0 and gn["period"].get("type") == gn0["period"].get("type"):    # same length as the latest
                years.append((gn["period"].get("short") or gn["period"]["label"], abs(gn["value"]) / pt["value"] * 100, gn, pt))
                ins += [gn, pt]
        sh = years[0][1]
        if sh >= ((scr_all.get("gains_share") or {}).get("observe") or 0.10) * 100:
            series = ", ".join(f"{y}: {v:.0f}%" for y, v, _, _ in years)
            shares = [v for _, v, _, _ in years]
            steady = len(shares) >= 2 and min(shares) >= 0.6 * max(shares)
            unit = "year" if gn0["period"].get("type") == "FY" else "period"
            text = (f"Gains on asset dispositions as a share of income before income taxes: {series}. " +
                    ("Only one period of this length is shown, so recurrence cannot be judged from this filing." if len(shares) < 2 else
                     f"The share is similar in every {unit} shown, so disposals appear to be a regular part of how the company earns, "
                     "though the amounts depend on the timing and pricing of sales." if steady else
                     f"The share varies across the {unit}s shown, so earnings depend partly on when sales happen."))
            gain_table = source_table(gn0)
            gain_label = gn0.get("formula") or (gn0.get("source") or {}).get("row_label", "")
            itemized = _rows_like(fb, "income", r"divestiture|sale of (business|subsidiary|interest)", gain_table) if gain_table is not None else []
            if itemized and itemized[0]["values"][0]:
                rr = itemized[0]
                v = rr["values"][0] * SCALE.get(rr["scale"], 1.0)
                text += f" The current year includes \"{rr['full_label']}\" of {fmt_value(v, 'USD')}, which the statement itemizes separately."
            out.append(_sig("gains_share", "Asset-sale gains in pretax income", sh, _pct(sh), "observation", f"Asset-sale gains {_pct(sh)} of pretax income", text,
                            "Observation, not screened: whether such gains recur depends on the business model, shown here across the years the filing presents.",
                            ins, "gains_share", unit="percent",
                            definition={"numerator": f"|\"{gain_label}\"|", "denominator": f"\"{pt0['source'].get('row_label', '')}\"",
                                        "period": ", ".join(y for y, *_ in years), "scope": "Consolidated", "formula": "Gains on dispositions / income before income taxes",
                                        "missing": "Years without both lines are left out.", "applicability": "Most relevant where asset sales are part of the business (lessors)."}))

    # ---- cash
    nc = fb.calcs.get("net_cash:cur")
    if nc and nc.get("value") is not None and debt and cash and not bank:
        v = nc["value"]
        out.append(_sig("net_cash", "Net cash or net debt", v, fmt_value(abs(v), "USD"), "observation", ("Net cash " if v > 0 else "Net debt ") + fmt_value(abs(v), "USD"),
                        ("Cash and cash equivalents exceed total debt." if v > 0 else f"Total debt exceeds cash and cash equivalents by {fmt_value(abs(v), 'USD')}.")
                        + " Marketable securities and restricted cash are not included.", "Observation, not screened.", [cash, debt], "liquidity", unit="USD",
                        definition={"numerator": "Cash and cash equivalents - total debt", "denominator": "", "period": cash["period"]["label"], "scope": "Consolidated",
                                    "formula": "Cash and cash equivalents - total debt", "missing": "Not computed if debt is not found.", "applicability": "Not applied to banks."}))
    cfo, nic = g("cfo"), g("net_income_consolidated") or g("net_income")
    if same(cfo, nic) and nic["value"] > 0:
        cc = cfo["value"] / nic["value"]
        evid = []
        for pat, lab in ((r"^depreciation( and amortization)?|^depreciation", "depreciation"), (r"gains?|losses on (disposition|sale)", "gains on sales")):
            rr = [x for x in _rows_like(fb, "cashflow", pat) if x["values"]]
            if rr:
                x = rr[0]
                evid.append(f"\"{x['full_label']}\" {fmt_value(x['values'][0] * SCALE.get(x['scale'], 1.0), 'USD')}")
        text = f"Operating cash flow of {cfo['display']} against consolidated net income of {nic['display']}."
        if evid:
            text += " Reconciling items in the cash flow statement include " + "; ".join(evid[:2]) + "."
        out.append(_sig("cash_conversion", "Operating cash flow / net income", cc, _x(cc), "observation", f"Operating cash flow {_x(cc)} net income", text,
                        "Observation, not screened. A low ratio is a question for the cash flow statement (working capital, noncash gains), not proof of weak earnings.",
                        [cfo, nic], "cash_conversion",
                        definition={"numerator": "Net cash provided by operating activities", "denominator": "Net income including noncontrolling interests (the figure the cash flow statement starts from)",
                                    "period": cfo["period"]["label"], "scope": "Consolidated", "formula": "Operating cash flow / consolidated net income",
                                    "missing": "Not computed when net income is zero or negative.", "applicability": "All nonbank profiles."},
                        followup=("Which working-capital or noncash items explain operating cash flow below net income?" if cc < 0.8 else None)))
    ni_p, e0, e1 = g("net_income"), g("equity_parent") or g("equity", 0), g("equity_parent", 1) or g("equity", 1)
    if ni_p and e0 and e0["value"] > 0:
        avg = e1 is not None and e1["value"] > 0 and e1["period"]["type"] == "instant"
        base = (e0["value"] + e1["value"]) / 2 if avg else e0["value"]
        roe = ni_p["value"] / base * 100
        lab = "average" if avg else "ending"
        per = ni_p["period"]
        interim = per.get("type") not in ("FY", None)
        out.append(_sig("roe", "Return on equity", roe, f"{roe:.1f}%", "observation",
                        f"Return on {lab} equity {roe:.1f}%" + (f" ({per.get('label', 'interim period')}, not annualized)" if interim else ""),
                        f"{ni_p['name']} of {ni_p['display']} over {lab} equity attributable to the company of {fmt_value(base, 'USD')}"
                        + (f" (average of {e1['display']} and {e0['display']})." if avg else ". Only the ending balance was available, so this is not average-equity ROE.")
                        + (f" This is one interim period and is not annualized; the average uses the two balance sheet dates the filing prints "
                           f"({e1['period'].get('end')} and {e0['period'].get('end')}), not the start of the period." if interim and avg else
                           " This is one interim period and is not annualized." if interim else ""),
                        "Observation, not screened.", [ni_p, e0] + ([e1] if avg else []), "returns", unit="percent",
                        definition={"numerator": ni_p["name"], "denominator": f"{lab.capitalize()} equity attributable to the company", "period": ni_p["period"]["label"],
                                    "scope": "Parent shareholders", "formula": "Net income attributable to the company / " + ("average of opening and closing parent equity" if avg else "ending parent equity"),
                                    "missing": "Not computed if equity is zero or negative.", "applicability": "All profiles."}))
    fcf = fb.calcs.get("fcf:cur")
    capex = g("capex")
    if fcf and fcf.get("value") is not None:
        v = fcf["value"]
        fleet = [fb.facts[i] for i in (capex or {}).get("inputs", []) if i in fb.facts and _re.search(r"fleet|lease|portfolio|railcar|equipment on operating", fb.facts[i]["name"], _re.I)]
        text = f"Free cash flow here is operating cash flow less all capital expenditure printed in the cash flow statement ({fmt_value(abs(capex['value']), 'USD') if capex else 'n/a'})."
        ins = [fcf] + ([capex] if capex else [])
        if fleet and capex:
            fl = sum(abs(x["value"]) for x in fleet)
            text += (f" Of that, {fmt_value(fl, 'USD')} is fleet investment ({', '.join(x['name'] for x in fleet)}); operating cash flow less only the other capital expenditure is "
                     f"{fmt_value(fcf['value'] + fl, 'USD')}.")
        elif lessor:
            text += " The filing prints fleet and other capital spending as one line, so fleet investment cannot be separated here."
        text += " Proceeds from asset sales are not netted."
        out.append(_sig("fcf", "Free cash flow", v, fmt_value(v, "USD"), "observation", f"Free cash flow {fmt_value(v, 'USD')}", text,
                        "Observation, not screened.", ins, "investment" if lessor else "cash_conversion", unit="USD",
                        definition={"numerator": "Net cash provided by operating activities - capital expenditures (all printed capex lines)", "denominator": "",
                                    "period": fcf["period"]["label"], "scope": "Consolidated", "formula": "Operating cash flow - |capital expenditures|",
                                    "missing": "Not computed if either line is missing.", "applicability": "All nonbank profiles; for lessors most capex is fleet growth."}))
        cr = fb.calcs.get("capital_returned:cur")
        scr = scr_all.get("payout_vs_fcf")
        if cr and cr.get("value"):
            if v > 0:
                ratio = cr["value"] / v
                sev = _grade(ratio, scr) if scr and scr.get("watch") is not None else "observation"
                out.append(_sig("payout_vs_fcf", "Shareholder returns vs free cash flow", ratio * 100, _pct(ratio * 100), sev,
                                f"Paid out {_pct(ratio * 100)} of free cash flow",
                                f"Buybacks and dividends of {cr['display']} against free cash flow of {fcf['display']}." +
                                (" Returning more than free cash flow means the difference came from cash on hand or borrowing." if ratio > 1 else ""),
                                _screen_text("payouts", scr, lambda x: f"{x * 100:.0f}% of free cash flow", version, prof_key), [cr, fcf], "payouts", unit="percent",
                                definition={"numerator": "|Share repurchases| + |dividends paid| (cash flow statement)", "denominator": "Free cash flow as defined above",
                                            "period": cr["period"]["label"], "scope": "Consolidated", "formula": "Capital returned / free cash flow",
                                            "missing": "A missing buyback or dividend line is left out, not counted as zero.", "applicability": "Not screened for lessors."}))
            else:
                out.append(_sig("payout_vs_fcf", "Shareholder returns with negative free cash flow", cr["value"], cr["display"], "observation",
                                f"Paid out {cr['display']} with negative free cash flow",
                                "Dividends and buybacks were paid in a year when free cash flow, as defined above, was negative; they were funded from cash on hand, asset-sale proceeds or borrowing."
                                + (" For a lessor, negative free cash flow usually reflects fleet growth." if lessor else ""),
                                "Observation, not screened.", [cr, fcf], "payouts", unit="USD",
                                definition={"numerator": "Capital returned", "denominator": "", "period": cr["period"]["label"], "scope": "Consolidated",
                                            "formula": "Shown when free cash flow < 0", "missing": "", "applicability": "All nonbank profiles."}))

    # ---- growth and margins
    rv0, rv1 = fb.comparable("revenue")
    if rv0 and rv1 and rv1["value"] > 0:
        gr = (rv0["value"] - rv1["value"]) / rv1["value"] * 100
        out.append(_sig("revenue_growth", "Revenue growth", gr, f"{gr:+.1f}%", "observation", f"Revenue {gr:+.1f}%",
                        f"Revenue moved from {rv1['display']} ({rv1['period'].get('short')}) to {rv0['display']} ({rv0['period'].get('short')}).",
                        "Observation, not screened. Growth is shown only for periods of the same length.", [rv0, rv1], "growth", unit="percent",
                        definition={"numerator": "Revenue (current) - revenue (prior comparable period)", "denominator": "Revenue (prior)", "period": f"{rv1['period']['label']} to {rv0['period']['label']}",
                                    "scope": "Consolidated", "formula": "(Current - prior) / prior", "missing": "Not computed if the prior value is zero or negative.", "applicability": "All profiles."}))
    om0, om1 = fb.calcs.get("operating_margin:cur"), fb.calcs.get("operating_margin:prior")
    if om0 and om1 and om0.get("value") is not None and om1.get("value") is not None:
        d = om0["value"] - om1["value"]
        if abs(d) >= 1:
            text = f"Operating margin moved from {om1['display']} to {om0['display']}."
            ins = [om0, om1]
            g0, g1, r0, r1 = g("gains_on_dispositions", 0), g("gains_on_dispositions", 1), g("revenue", 0), g("revenue", 1)
            if g0 and g1 and r0 and r1 and r0["value"] and r1["value"] and same(g0, r0) and same(g1, r1) and source_table(g0) is not None and source_table(g0) == source_table(fb.get("operating_income")):
                d_ex = d - (abs(g0["value"]) / r0["value"] - abs(g1["value"]) / r1["value"]) * 100
                text += (f" Operating income includes gains on dispositions ({g0['display']} vs {g1['display']}); excluding them the margin moved about {d_ex:+.1f} points.")
                ins += [g0, g1]
            out.append(_sig("margin_trend", "Operating margin trend", d, f"{d:+.1f} pts", "observation", f"Operating margin {om0['display']} ({d:+.1f} pts)",
                            text, "Observation, not screened.", ins, "margins", unit="pts",
                            definition={"numerator": "Operating income / revenue (each year)", "denominator": "", "period": f"{om1['period']['label']} to {om0['period']['label']}",
                                        "scope": "Consolidated", "formula": "Margin(current) - margin(prior)", "missing": "Not shown when operating income is not printed.",
                                        "applicability": "Companies that print operating income."}))
    inv0, inv1 = fb.comparable("inventories")
    if inv0 and inv1 and rv0 and rv1 and inv1["value"] > 0 and rv1["value"] > 0:
        ig = (inv0["value"] - inv1["value"]) / inv1["value"] * 100
        rg = (rv0["value"] - rv1["value"]) / rv1["value"] * 100
        if ig - rg >= 10:
            out.append(_sig("inventory_build", "Inventory growing faster than revenue", ig - rg, f"{ig - rg:+.0f} pts", "observation",
                            f"Inventory {ig:+.0f}% vs revenue {rg:+.0f}%",
                            "Inventory grew faster than revenue over the year; the filing's discussion of backlog or production plans would explain whether this is deliberate.",
                            "Observation, shown when inventory growth exceeds revenue growth by 10 points or more.", [inv0, inv1, rv0, rv1], "inventory", unit="pts",
                            followup="Is the inventory build tied to backlog or to slower sales?",
                            definition={"numerator": "Inventory growth (ending balances)", "denominator": "Revenue growth", "period": f"{rv1['period']['label']} to {rv0['period']['label']}",
                                        "scope": "Consolidated", "formula": "Inventory growth - revenue growth", "missing": "", "applicability": "Companies that hold inventory."}))

    # ---- refinancing: one year's share of scheduled principal
    sched = next((w for w in watch or [] if w.get("title") == "Scheduled debt maturities"), None)
    scr = scr_all.get("maturity_concentration")
    if sched and scr is not None:
        vals = [tuple(x) for x in sched.get("schedule") or []]
        if not vals:            # older reports: parse the rounded text
            pairs = _re.findall(r"(20\d\d|Thereafter): \$([\d.]+)([KMBT])", sched["text"])
            mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
            vals = [(y, float(v) * mult[u]) for y, v, u in pairs]
        tot = sum(v for _, v in vals)
        years = [(y, v) for y, v in vals if y != "Thereafter"]
        if tot > 0 and years:
            y, v = max(years, key=lambda x: x[1])
            sh = v / tot
            carry = (f" Total debt on the balance sheet is {debt['display']} (carrying value). The two can differ because the schedule may not cover every borrowing"
                     f" (for example commercial paper or credit lines) and because of discounts and issuance costs.") if debt else ""
            out.append({**_sig("maturity_wall", "Debt maturity concentration", sh * 100, _pct(sh * 100), _grade(sh, scr),
                                f"{fmt_value(v, 'USD')} of scheduled principal due in {y}",
                                f"{_pct(sh * 100)} of the {fmt_value(tot, 'USD')} of scheduled principal in the debt note falls due in {y}.{carry}",
                                _screen_text("maturity concentration", scr, lambda x: f"{x * 100:.0f}% of scheduled principal in one year", version, prof_key),
                                [], "liquidity", unit="percent",
                                definition={"numerator": f"Principal due in {y}", "denominator": "Sum of the scheduled principal payments in the debt maturity table (principal only; excludes interest, leases and unamortized discounts)",
                                            "period": "As printed in the debt note", "scope": "The company's own debt (affiliate schedules excluded)",
                                            "formula": "Largest single year / total scheduled principal", "missing": "Not computed without a maturity table.",
                                            "applicability": "All nonbank profiles."}), "source": sched.get("source")})

    # ---- industry KPIs: observations with the filing's own prior values
    for k in kpis or []:
        if k.get("name") == "Fleet utilization" and k.get("value") is not None:
            prior = f" ({k.get('prior_period') or 'prior'}: {k['prior_display']})" if k.get("prior_display") else ""
            out.append({**_sig("utilization", "Fleet utilization", k["value"], k["display"], "observation", f"Fleet utilization {k['display']}" + prior,
                                f"Share of the fleet on lease{(' (' + k['scope'] + ')') if k.get('scope') else ''}, as defined by the company." + prior,
                                "Observation, not screened. Utilization definitions differ between lessors.", [], "kpi:Fleet utilization", unit="percent",
                                definition={"numerator": "As defined in the filing", "denominator": "", "period": k.get("period") or "", "scope": k.get("scope") or "As stated",
                                            "formula": "Company-defined", "missing": "", "applicability": "Lessors."}),
                        "inputs": [], "kpi": k["name"], "source": k.get("source")})
    out.sort(key=lambda s: SEVERITIES.index(s["severity"]))
    return out


def focus_status(br: dict, signals: list, kpis: list, fb=None) -> dict:
    """Mark each brief item found or not found, with the figure that
    answered it and the signals it produced."""
    by_focus: dict = {}
    for sg in signals:
        by_focus.setdefault(sg["focus"], []).append(sg)
    kp = {k["name"]: k for k in kpis or []}
    calc = (lambda cid: (fb.calcs.get(cid) or {}) if fb else {})
    fact = (lambda m: fb.get(m) if fb else None)
    for it in br["items"]:
        key = it["key"]
        alias = {"debt_assets": ("debt_to_assets",), "debt_equity": ("debt_to_equity", "equity_multiplier"),
                 "coverage": ("interest_coverage",), "investment": ("fcf",), "gains_share": ("gains_share",)}.get(key, ())
        sg = by_focus.get(key, []) + [x for x in signals if x["id"] in alias and x["focus"] != key]
        it["signals"] = [x["id"] for x in sg]
        ans = None
        if key.startswith("kpi:"):
            k = kp.get(key[4:])
            ans = k.get("display") if k and k.get("display") else None
        elif sg:
            ans = " · ".join(x["headline"] for x in sg[:2])
        elif key == "margins":
            gm, om = calc("gross_margin:cur"), calc("operating_margin:cur")
            ans = " · ".join(x for x in ((f"Gross margin {gm['display']}" if gm.get("value") is not None else ""),
                                          (f"Operating margin {om['display']}" if om.get("value") is not None else "")) if x) or None
            if not ans and fact("pretax_income") and fact("revenue"):
                p, rv = fact("pretax_income"), fact("revenue")
                if p["period"]["end"] == rv["period"]["end"] and rv["value"]:
                    ans = f"No operating income line; pretax margin {p['value'] / rv['value'] * 100:.1f}%"
        elif key == "inventory":
            i0, i1 = fb.comparable("inventories") if fb else (None, None)
            r0, r1 = fb.comparable("revenue") if fb else (None, None)
            if i0 and i1 and r0 and r1 and i1["value"] and r1["value"]:
                ans = f"Inventory {(i0['value'] / i1['value'] - 1) * 100:+.0f}% vs revenue {(r0['value'] / r1['value'] - 1) * 100:+.0f}%"
        elif key == "coverage" and fb and not fact("interest_expense"):
            ans = None
        elif key == "gains_share" and fb and fact("gains_on_dispositions") and fact("pretax_income") and fact("pretax_income")["value"] > 0:
            ans = f"Gains on asset sales {abs(fact('gains_on_dispositions')['value']) / fact('pretax_income')['value'] * 100:.0f}% of pretax income"
        elif key == "payouts":
            cr = calc("capital_returned:cur")
            ans = f"Returned {cr['display']}" if cr.get("value") else None
        it["answer"] = ans
        it["status"] = "found" if ans else "not found"
        if not ans and key == "coverage":
            it["note"] = "Interest expense is not shown as its own line in the income statement."
    return br
