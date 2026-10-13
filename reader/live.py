"""The plan for the live report panel.

Built after validation from the final report, so nothing withdrawn can
appear. The browser reveals each piece only when the spider reaches its
evidence: a fact when its finding is found, a calculation or signal only
when every fact it rests on has been revealed (or was read before the
section being replayed, which the panel says). The plan carries no timing.
"""
from __future__ import annotations

SECTIONS = [
    ("income", "Income statement", [("revenue", "Revenue"), ("gross_profit", "Gross profit"), ("operating_income", "Operating income"),
                                     ("pretax_income", "Income before income taxes"), ("net_income", "Net income"), ("eps_diluted", "Diluted EPS")]),
    ("balance", "Balance sheet", [("cash", "Cash and cash equivalents"), ("total_assets", "Total assets"), ("total_debt", "Total debt"),
                                  ("total_liabilities", "Total liabilities"), ("equity_parent", "Equity attributable to the company"),
                                  ("equity_total", "Total equity")]),
    ("cashflow", "Cash flow statement", [("cfo", "Operating cash flow"), ("capex", "Capital expenditures"),
                                         ("buybacks", "Share repurchases"), ("dividends", "Dividends paid")]),
]
CALCS = [("gross_margin:cur", "Gross margin"), ("operating_margin:cur", "Operating margin"), ("net_margin:cur", "Net margin"),
         ("fcf:cur", "Free cash flow"), ("capital_returned:cur", "Capital returned"), ("net_cash:cur", "Net cash (debt)")]


def plan(rep: dict) -> dict:
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    live = lambda i: (facts.get(i) or calcs.get(i)) and (facts.get(i) or calcs.get(i)).get("status") != "Unavailable"
    by_ref = {}
    for f in rep["findings"]:
        if f.get("ref"):
            by_ref.setdefault(f["ref"], f["id"])

    def leaves(i, depth=0):
        if depth > 5 or not live(i):
            return []
        if i in facts:
            return [i]
        return [x for j in calcs[i].get("inputs", []) for x in leaves(j, depth + 1)]

    rows_by_metric = {}
    for r in rep["financials"]["performance"] + rep["position"]["rows"]:
        rows_by_metric[r["metric"]] = r
    cells = {}

    def cell(i):
        x = facts.get(i) or calcs.get(i)
        src = x.get("source") or {}
        lv = leaves(i)
        cells[i] = {"id": i, "display": x["display"], "period": x["period"].get("short") or x["period"].get("label"),
                    "status": x["status"], "kind": x.get("kind", "reported"), "page": src.get("pdf_page"), "printed": src.get("printed_page"),
                    "excerpt": (src.get("excerpt") or "")[:160], "raw": src.get("raw"), "requires": lv, "findings": [by_ref[j] for j in lv if j in by_ref],
                    "formula": x.get("formula"),
                    "input_pages": sorted({(facts[j].get("source") or {}).get("pdf_page") for j in lv
                                           if (facts[j].get("source") or {}).get("pdf_page")})}
        return i

    sections = []
    for sid, title, metrics in SECTIONS:
        rows = []
        for m, label in metrics:
            ids = []
            if m in rows_by_metric:
                r = rows_by_metric[m]
                ids = [x for x in (r.get("current"), r.get("prior")) if x and live(x)]
                label = (facts.get(r.get("current")) or calcs.get(r.get("current")) or {}).get("name") or label
            else:
                cand = sorted([f for f in facts.values() if f["metric"] == m and f["status"] != "Unavailable"],
                              key=lambda f: f["period"].get("end") or "", reverse=True)
                cand = [c for c in cand if not cand or c["period"]["type"] == cand[0]["period"]["type"]][:2]
                ids = [c["id"] for c in cand] + [c["id"] for c in calcs.values() if c.get("metric") == m and c.get("status") != "Unavailable"][:2 - len(cand)]
                if ids:
                    label = (facts.get(ids[0]) or calcs.get(ids[0]))["name"]
            if ids:
                rows.append({"metric": m, "label": label, "cells": [cell(i) for i in ids]})
        if rows:
            sections.append({"id": sid, "title": title, "rows": rows})
    ratios = []
    for cid, label in CALCS:
        if live(cid) and calcs[cid].get("value") is not None:
            ratios.append({"id": cell(cid), "label": label})
    sigs = []
    for s in rep.get("signals") or []:
        req = [x for i in s.get("inputs", []) for x in leaves(i)]
        anchor = (s.get("anchor") or {}).get("finding")
        sigs.append({"id": s["id"], "severity": s["severity"], "headline": s["headline"], "text": s["text"], "rule": s["rule"],
                     "requires": sorted(set(req)), "findings": sorted({by_ref[j] for j in req if j in by_ref} | ({anchor} if anchor else set())),
                     "anchor": anchor})
    pages = sorted({c["page"] for c in cells.values() if c["page"]})
    leaf_ids = {x for c in cells.values() for x in c["requires"]} | {x for sg in sigs for x in sg["requires"]}
    fact_pages = {i: (facts[i].get("source") or {}).get("pdf_page") for i in leaf_ids if i in facts}
    fact_offsets = {i: (facts[i].get("source") or {}).get("offset") for i in leaf_ids if i in facts}
    return {"version": 1, "sections": sections, "ratios": ratios, "signals": sigs, "cells": cells,
            "fact_to_finding": {k: v for k, v in by_ref.items() if k in cells or any(k in c["requires"] for c in cells.values())},
            "pages": pages, "fact_pages": fact_pages, "fact_offsets": fact_offsets, "basis": "Replay of an analysis already completed on the server, revealed in the order the spider reaches each source line."}
