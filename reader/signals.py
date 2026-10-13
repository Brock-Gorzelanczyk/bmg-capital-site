"""What to look for, and what the numbers say.

brief(company, profile, why)  -> the analyst brief shown before extraction:
    the company, the industry it was read as (and why), and the measures
    that matter for that kind of business, each with the reason.
analyze(fb, profile, kpis)    -> signals: ratios computed from cited facts,
    each with a severity (risk, watch, strength, info), the rule of thumb
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

SEVERITIES = ("risk", "watch", "strength", "info")

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
# leverage reads differently for asset-heavy, debt-funded models
LEVERAGE = {
    "default": {"em": (2.5, 4.0), "de": (1.0, 2.0), "da": (0.35, 0.50)},
    "Railcar leasing": {"em": (4.0, 6.0), "de": (2.5, 3.5), "da": (0.55, 0.70)},
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


def _sig(sid, title, value, display, severity, headline, text, rule, inputs, focus, unit="ratio"):
    assert severity in SEVERITIES
    return {"id": sid, "title": title, "value": value, "display": display, "unit": unit, "severity": severity, "headline": headline,
            "text": text, "rule": rule, "inputs": [i["id"] for i in inputs if i], "focus": focus,
            "basis": "BMG interpretation of reported figures", "kind": "interpreted"}


def _x(v: float) -> str:
    return f"{v:.1f}x"


def _pct(v: float) -> str:
    return f"{v:.0f}%"


def analyze(fb, profile: str, kpis: list, watch: Optional[list] = None) -> list[dict]:
    """Signals from the current period's cited facts. A signal whose inputs
    are missing or of different periods is not produced."""
    g = lambda m, w=0: fb.get(m, w)
    same = lambda *xs: all(xs) and len({(x["period"]["type"], x["period"]["end"] or x["period"]["year"]) for x in xs}) == 1
    lessor = profile.split(" + ")[0] == "Railcar leasing"        # leasing is the main business
    lev = LEVERAGE["Railcar leasing"] if lessor else LEVERAGE["default"]
    out = []
    ta, eq, debt, cash = g("total_assets"), g("equity"), g("total_debt"), g("cash")
    tl = g("total_liabilities")

    # leverage: equity multiplier, debt to equity, debt to assets
    if same(ta, eq) and eq["value"] > 0:
        em = ta["value"] / eq["value"]
        lo, hi = lev["em"]
        da = debt["value"] / ta["value"] if same(debt, ta) and ta["value"] else None
        if em > hi:
            sev = "risk"
            text = f"Assets are {em:.1f} times shareholders' equity, so most of the balance sheet is funded by liabilities. Leverage magnifies returns on equity in good years and losses in bad ones."
            if da is not None and da < 0.35:
                sev = "watch"
                text += f" Debt is only {_pct(da * 100)} of assets, so the high multiple comes mostly from small equity (often the result of buybacks) and non-debt liabilities rather than heavy borrowing."
            elif lessor:
                text += " Lessors typically run high leverage against long-lived, contracted assets; compare with peers rather than with industrial companies."
        elif em > lo:
            sev, text = "watch", f"Assets are {em:.1f} times equity: meaningful leverage, worth tracking against peers and the debt maturity schedule."
        else:
            sev, text = "strength", f"Assets are {em:.1f} times equity: most assets are funded by owners' capital, which leaves room to borrow if needed."
        out.append(_sig("equity_multiplier", "Equity multiplier", em, _x(em), sev,
                        f"Equity multiplier {_x(em)}" + (" · high leverage" if sev == "risk" else " · leverage to watch" if sev == "watch" else " · conservative"),
                        text, f"Total assets / shareholders' equity. Rule of thumb{' for lessors' if lessor else ''}: above {hi:.1f}x high, {lo:.1f}-{hi:.1f}x moderate.",
                        [ta, eq], "leverage"))
    if same(debt, eq) and eq["value"] > 0:
        de = debt["value"] / eq["value"]
        lo, hi = lev["de"]
        sev = "risk" if de > hi else "watch" if de > lo else "strength" if de < 0.5 else "info"
        out.append(_sig("debt_to_equity", "Debt to equity", de, _x(de), sev,
                        f"Debt is {_x(de)} equity" + (" · heavy borrowing" if sev == "risk" else ""),
                        f"Total debt of {debt['display']} against shareholders' equity of {eq['display']}." +
                        (" Lenders, not owners, fund most of the capital base; rising rates or a downturn bite harder." if sev == "risk" else
                         " Borrowing is significant relative to the owners' stake." if sev == "watch" else " Borrowing is modest relative to the owners' stake." if sev == "strength" else ""),
                        f"Total debt / shareholders' equity. Rule of thumb{' for lessors' if lessor else ''}: above {hi:.1f}x high, {lo:.1f}-{hi:.1f}x elevated.",
                        [debt, eq], "debt_equity" if not lessor else "leverage"))
    if same(debt, ta) and ta["value"] > 0:
        da = debt["value"] / ta["value"]
        lo, hi = lev["da"]
        sev = "risk" if da > hi else "watch" if da > lo else "info"
        out.append(_sig("debt_to_assets", "Debt to assets", da * 100, f"{da * 100:.1f}%", sev, f"Debt funds {da * 100:.1f}% of assets",
                        f"{debt['display']} of debt against {ta['display']} of total assets." +
                        (" A large share of the asset base is financed with borrowed money." if sev != "info" else ""),
                        f"Total debt / total assets. Rule of thumb{' for lessors' if lessor else ''}: above {hi * 100:.0f}% high, {lo * 100:.0f}-{hi * 100:.0f}% elevated.",
                        [debt, ta], "debt_assets" if lessor else "leverage", unit="percent"))

    # interest coverage: (pretax income + interest) / interest
    pti, ie = g("pretax_income"), g("interest_expense")
    if same(pti, ie) and abs(ie["value"]) > 0:
        cov = (pti["value"] + abs(ie["value"])) / abs(ie["value"])
        sev = "risk" if cov < 2 else "watch" if cov < 4 else "strength" if cov > 8 else "info"
        out.append(_sig("interest_coverage", "Interest coverage", cov, _x(cov), sev, f"Earnings cover interest {_x(cov)}",
                        f"Earnings before interest and taxes are {cov:.1f} times interest expense of {fmt_value(abs(ie['value']), 'USD')}." +
                        (" Thin cover: a modest earnings decline or higher rates would strain it." if sev == "risk" else
                         " Adequate but not wide." if sev == "watch" else " Comfortable cover." if sev == "strength" else ""),
                        "(Income before income taxes + interest expense) / interest expense. Rule of thumb: below 2x thin, 2-4x adequate, above 8x comfortable.",
                        [pti, ie], "coverage"))

    # gains on asset sales as a share of pretax income (lessors and anyone with them)
    gn = g("gains_on_dispositions")
    if same(gn, pti) and pti["value"] > 0 and abs(gn["value"]) > 0:
        sh = abs(gn["value"]) / pti["value"] * 100
        if sh >= 10:
            sev = "watch" if sh >= 25 else "info"
            out.append(_sig("gains_share", "Asset-sale gains in pretax income", sh, _pct(sh), sev, f"{_pct(sh)} of pretax income is gains on asset sales",
                            f"Gains on asset dispositions of {fmt_value(abs(gn['value']), 'USD')} against pretax income of {pti['display']}. "
                            "These gains depend on the timing of sales and secondary-market prices, so they are less repeatable than lease income.",
                            "Gains on dispositions / income before income taxes. Above 25% flagged as a material, less recurring share.",
                            [gn, pti], "gains_share", unit="percent"))

    # cash: net debt, conversion, free cash flow, payouts
    nc = fb.calcs.get("net_cash:cur")
    if nc and nc.get("value") is not None and debt and cash:
        v = nc["value"]
        sev = "strength" if v > 0 else "info"
        out.append(_sig("net_cash", "Net cash or net debt", v, fmt_value(abs(v), "USD"), sev,
                        ("Net cash " if v > 0 else "Net debt ") + fmt_value(abs(v), "USD"),
                        ("Cash exceeds debt." if v > 0 else f"Debt exceeds cash and cash equivalents by {fmt_value(abs(v), 'USD')}.") + " Marketable securities are not included.",
                        "Cash and cash equivalents - total debt.", [cash, debt], "liquidity", unit="USD"))
    cfo, ni = g("cfo"), g("net_income")
    if same(cfo, ni) and ni["value"] > 0:
        cc = cfo["value"] / ni["value"]
        sev = "watch" if cc < 0.8 else "strength" if cc > 1.2 else "info"
        out.append(_sig("cash_conversion", "Cash conversion", cc, _x(cc), sev, f"Operating cash flow is {_x(cc)} net income",
                        f"Operating cash flow of {cfo['display']} against net income of {ni['display']}." +
                        (" Profit is not fully turning into cash; check working capital and non-cash gains." if sev == "watch" else
                         " Earnings are backed by cash; depreciation and other non-cash charges add back." if sev == "strength" else ""),
                        "Operating cash flow / net income. Below 0.8x flagged; above 1.2x strong.", [cfo, ni], "cash_conversion"))
    fcf = fb.calcs.get("fcf:cur")
    capex = g("capex")
    if fcf and fcf.get("value") is not None:
        v = fcf["value"]
        if v < 0:
            sev = "info" if lessor else "watch"
            out.append(_sig("fcf_negative", "Negative free cash flow", v, fmt_value(v, "USD"), sev, f"Free cash flow {fmt_value(v, 'USD')}",
                            f"Capital spending of {fmt_value(abs(capex['value']), 'USD') if capex else 'n/a'} exceeded operating cash flow." +
                            (" For a lessor this is usually fleet growth funded with debt, which is a choice, not a shortfall; watch the return on that investment." if lessor
                             else " The gap is funded by cash on hand or borrowing."),
                            "Operating cash flow - capital expenditures.", [fcf] + [x for x in (capex,) if x], "investment" if lessor else "cash_conversion", unit="USD"))
        cr = fb.calcs.get("capital_returned:cur")
        if cr and cr.get("value") and v > 0:
            ratio = cr["value"] / v * 100
            sev = "watch" if ratio > 100 else "info"
            out.append(_sig("payout_vs_fcf", "Shareholder returns vs free cash flow", ratio, _pct(ratio), sev,
                            f"Returned {_pct(ratio)} of free cash flow to shareholders",
                            f"Buybacks and dividends of {cr['display']} against free cash flow of {fcf['display']}." +
                            (" Returning more than the business generated means the difference came from cash or new debt; it also shrinks equity, which raises leverage ratios." if sev == "watch" else ""),
                            "(Share repurchases + dividends) / free cash flow. Above 100% flagged.", [cr, fcf], "payouts", unit="percent"))
        elif cr and cr.get("value") and v <= 0:
            out.append(_sig("payout_vs_fcf", "Shareholder returns with negative free cash flow", cr["value"], cr["display"], "watch",
                            f"Paid out {cr['display']} with negative free cash flow",
                            "Dividends and buybacks were paid in a year when free cash flow was negative, so they were funded by cash on hand or borrowing." +
                            (" Lessors commonly fund fleet growth with debt and pay dividends from operating cash flow; the question is whether the new assets earn more than their financing cost." if lessor else ""),
                            "Shareholder returns while operating cash flow - capex < 0.", [cr, fcf], "payouts", unit="USD"))

    # growth and margin trend
    rv0, rv1 = fb.comparable("revenue")
    if rv0 and rv1 and rv1["value"] > 0:
        gr = (rv0["value"] - rv1["value"]) / rv1["value"] * 100
        sev = "strength" if gr >= 10 else "risk" if gr <= -10 else "watch" if gr < 0 else "info"
        out.append(_sig("revenue_growth", "Revenue growth", gr, f"{gr:+.1f}%", sev, f"Revenue {gr:+.1f}%",
                        f"Revenue moved from {rv1['display']} to {rv0['display']}." + (" A double-digit decline; check whether it is volume, price or a divestiture." if sev == "risk" else ""),
                        "Change in revenue against the prior comparable period. +10% or more strong; -10% or worse flagged.", [rv0, rv1], "growth", unit="percent"))
    om0, om1 = fb.calcs.get("operating_margin:cur"), fb.calcs.get("operating_margin:prior")
    if om0 and om1 and om0.get("value") is not None and om1.get("value") is not None:
        d = om0["value"] - om1["value"]
        if abs(d) >= 1:
            sev = "strength" if d >= 2 else "watch" if d <= -2 else "info"
            text = f"Operating margin moved from {om1['display']} to {om0['display']}."
            ins = [om0, om1]
            # how much of the move is gains on asset sales (inside operating income for some filers)
            g0, g1, r0, r1 = g("gains_on_dispositions", 0), g("gains_on_dispositions", 1), g("revenue", 0), g("revenue", 1)
            if g0 and g1 and r0 and r1 and r0["value"] and r1["value"] and same(g0, r0) and same(g1, r1):
                d_ex = d - (abs(g0["value"]) / r0["value"] - abs(g1["value"]) / r1["value"]) * 100
                if abs(d_ex) < abs(d) / 2:
                    sev = "watch" if d > 0 else sev
                    text += (f" Most of the change comes from gains on asset sales and divestitures ({g0['display']} vs {g1['display']}); "
                             f"excluding them the margin moved about {d_ex:+.1f} points.")
                    ins += [g0, g1]
            out.append(_sig("margin_trend", "Operating margin trend", d, f"{d:+.1f} pts", sev, f"Operating margin {om0['display']} ({d:+.1f} pts)",
                            text, "Change in operating income / revenue. A move of 2 points or more flagged; gains on asset sales separated where reported.",
                            ins, "margins", unit="pts"))

    # inventory building faster than sales
    inv0, inv1 = fb.comparable("inventories")
    if inv0 and inv1 and rv0 and rv1 and inv1["value"] > 0 and rv1["value"] > 0:
        ig = (inv0["value"] - inv1["value"]) / inv1["value"] * 100
        rg = (rv0["value"] - rv1["value"]) / rv1["value"] * 100
        if ig - rg >= 10:
            out.append(_sig("inventory_build", "Inventory growing faster than revenue", ig - rg, f"{ig - rg:+.0f} pts", "watch",
                            f"Inventory {ig:+.0f}% vs revenue {rg:+.0f}%",
                            "Inventory grew much faster than sales, which can precede discounting or production cuts.",
                            "Inventory growth - revenue growth. 10 points or more flagged.", [inv0, inv1, rv0, rv1], "inventory", unit="pts"))

    # refinancing: one year carrying a large share of scheduled debt
    sched = next((w for w in watch or [] if w.get("title") == "Scheduled debt maturities"), None)
    if sched:
        import re as _re
        pairs = _re.findall(r"(20\d\d|Thereafter): \$([\d.]+)([KMBT])", sched["text"])
        mult = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}
        vals = [(y, float(v) * mult[u]) for y, v, u in pairs]
        tot = sum(v for _, v in vals)
        years = [(y, v) for y, v in vals if y != "Thereafter"]
        if tot > 0 and years:
            y, v = max(years, key=lambda x: x[1])
            sh = v / tot * 100
            if sh >= 20:
                out.append({**_sig("maturity_wall", "Debt maturity concentration", sh, _pct(sh), "watch" if sh >= 25 else "info",
                                   f"{fmt_value(v, 'USD')} of debt comes due in {y}",
                                   f"{_pct(sh)} of the scheduled principal falls in {y}. That debt must be repaid or refinanced at the rates of the day.",
                                   "Largest single-year maturity / total scheduled principal (as printed in the debt note). 25% or more flagged.",
                                   [], "liquidity", unit="percent"), "source": sched.get("source")})
    # industry KPIs with a read
    for k in kpis or []:
        if k.get("name") == "Fleet utilization" and k.get("value") is not None:
            u = k["value"]
            sev = "strength" if u >= 97 else "watch" if u < 90 else "info"
            out.append({**_sig("utilization", "Fleet utilization", u, f"{u:.1f}%", sev, f"Fleet utilization {u:.1f}%",
                                "Share of the fleet on lease." + (" Very high utilization usually gives the lessor pricing power on renewals." if sev == "strength" else
                                                                  " Idle cars earn nothing and still cost storage and maintenance." if sev == "watch" else ""),
                                "Rule of thumb: 97%+ tight market, below 90% soft.", [], "kpi:Fleet utilization", unit="percent"),
                        "inputs": [], "kpi": k["name"], "source": k.get("source")})
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
        sg = by_focus.get(key, []) + (by_focus.get("leverage", []) if key == "debt_equity" else [])
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
