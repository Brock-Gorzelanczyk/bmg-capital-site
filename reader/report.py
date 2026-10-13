"""Assemble the BMG Capital Company Report (one dict) and the findings the
spider shows, from the same validated facts.

The report says what the filing establishes (reported), what was
calculated (formula and inputs), what is interpretation (labeled), and what
was not found. It never recommends buying or selling.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from . import document, facts as F, profiles, risks, tables
from .facts import fmt_value

SCHEMA = "bmg.company_report/1"
NF = "Not found in the reviewed filing"


def _company(doc) -> dict:
    head = doc.body[:15000]
    loc = lambda m: doc.location(m.start()) if m else {}
    name_m = re.search(r"\n([^\n]{2,90})\n\s*\(Exact name of (?:registrant|Registrant)", "\n" + head)
    form_m = re.search(r"FORM\s+(10-K(?:/A)?|10-Q(?:/A)?|20-F|40-F)", head, re.I)
    per_m = re.search(r"for the (fiscal year|quarterly period) ended\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", head, re.I)
    exch_m = re.search(r"(The Nasdaq Stock Market LLC|Nasdaq Global Select Market|New York Stock Exchange|NYSE American|NYSE)", head)
    tick = None
    for m in re.finditer(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\s+(?:The\s+)?(?:Nasdaq|New York Stock Exchange|NYSE)", head):
        if m.group(1) not in {"THE", "LLC", "INC", "NEW", "PAR", "LLC."}:
            tick = m.group(1)
            break
    sig = re.search(r"SIGNATURES[\s\S]{0,4000}?Date:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", doc.body)
    form = form_m.group(1).upper() if form_m else None
    return {
        "name": re.sub(r"\s+", " ", name_m.group(1)).strip() if name_m else None,
        "ticker": tick, "form": form, "amendment": bool(form and form.endswith("/A")),
        "period": ((("Fiscal year ended " if per_m.group(1).lower() == "fiscal year" else "Quarter ended ") + re.sub(r"\s+", " ", per_m.group(2))) if per_m else None),
        "exchange": exch_m.group(1) if exch_m else None,
        "signature_date": sig.group(1) if sig else None,
        "evidence": {"name": loc(name_m), "form": loc(form_m), "period": loc(per_m), "signature_date": loc(sig)},
    }


def _description(doc, form: Optional[str] = None) -> dict:
    sec = next((s for s in doc.sections if s.item == "1" and re.match(r"business", s.title, re.I)), None)
    if not sec:
        if (form or "").startswith("10-Q"):
            return {"text": None, "reason": "Quarterly reports (Form 10-Q) do not include a Business section; see the company's latest 10-K for a description."}
        return {"text": None, "reason": "Item 1 (Business) was not located in the extracted text."}
    seg = re.sub(r"\s+", " ", doc.body[sec.offset:sec.offset + 5000])
    seg = re.sub(r"^(?:PART\s+[IV]+\s+)?Item 1\.?\s+Business\s*", "", seg)
    sents = [re.sub(r"^(Company Background|Business Overview|Overview|General|Background|Our Company|Company Overview)\s+", "", x.strip())
             for x in re.split(r"(?<=[.])\s+(?=[A-Z])", seg)]
    good = [x for x in sents if 40 <= len(x) <= 400 and re.match(r"(The Company|We |Our |[A-Z][\w.&,' ]+ (Inc|Corp|Corporation|Company|Ltd)\b)", x)
            and not re.search(r"forward-looking|fiscal year is|refer to|herein", x, re.I)]
    if not good:
        return {"text": None, "reason": "No plain business description sentence was found at the start of Item 1."}
    i0 = sents.index(good[0])
    # a second sentence only when it directly follows and is clean prose
    nxt = sents[i0 + 1] if i0 + 1 < len(sents) else ""
    clean = nxt in good and re.match(r"(The Company|We |Our )", nxt) and not re.search(r"\b(\w+) \1\b", nxt)
    text = good[0] + (" " + nxt if clean else "")
    k = doc.body.find(good[0][:40])
    return {"text": text[:700], "kind": "reported", "source": {**doc.location(k if k >= 0 else sec.offset), "excerpt": good[0][:300]}}


def _segments_text(doc, bds: list) -> dict:
    flat = re.sub(r"\s+", " ", doc.body)
    for pat in (r"[^.]{0,300}\b(?:reportable|operating) segments (?:consist of|are|include|:)[^.]{0,300}\.",
                r"[^.]{0,200}\bmanages its business primarily on a [a-z]+ basis[^.]{0,300}\.",
                r"[^.]{0,200}\boperates? in (?:two|three|four|five|\d) (?:reportable )?segments[^.]{0,300}\."):
        m = re.search(pat, flat)
        if m:
            t = m.group(0).strip()
            k = doc.body.find(t[:30])
            return {"text": t[:450], "kind": "reported", "source": {**doc.location(k if k >= 0 else 0), "excerpt": t[:300]}}
    seg = next((b for b in bds if b["kind"] != "region"), None)
    if seg:
        return {"text": "Revenue lines shown in the filing: " + ", ".join(i["name"] for i in seg["items"]) + ".", "kind": "reported",
                "source": {**seg["source"]}}
    return {"text": None, "reason": "No segment description was found in the extracted text."}


def _change(cur: Optional[dict], prior: Optional[dict]) -> dict:
    if not cur or not prior or cur.get("value") is None or prior.get("value") is None:
        return {"display": "", "reason": "No comparable prior period in the filing." if cur and not prior else ""}
    if cur["period"]["type"] != prior["period"]["type"]:
        return {"display": "", "reason": "Periods are not comparable."}
    pct, why = F.growth(cur["value"], prior["value"])
    diff = cur["value"] - prior["value"]
    if pct is None:
        return {"pct": None, "abs": diff, "display": ("+" if diff >= 0 else "-") + fmt_value(abs(diff), cur["unit"]).lstrip("-"), "reason": why}
    return {"pct": pct, "abs": diff, "display": f"{pct:+.1f}%", "reason": ""}


ROWS_PERF = [("revenue", None), ("gross_profit", None), ("operating_income", None), ("net_income", None), ("eps_basic", None), ("eps_diluted", None)]
ROWS_POS = [("cash", None), ("total_debt", None), ("total_assets", None), ("total_liabilities", None), ("equity", None),
            ("cfo", None), ("capex", None), ("buybacks", None), ("dividends", None)]


def build(doc, rows: list, fb: "F.FactBook", claims: Optional[list] = None) -> dict:
    now = datetime.now(timezone.utc)
    co = _company(doc)
    prof = profiles.kpis(doc, rows)
    bank = prof["profile"] == "Banking"
    bds = profiles.breakdowns(fb, rows)
    rf, rf_note = risks.risk_factors(doc)
    watch, searched = risks.watch_items(doc, rows)

    def row(mkey: str) -> dict:
        cur, prior = fb.comparable(mkey)
        name = F.METRIC_NAMES.get(mkey, "Total debt" if mkey == "total_debt" else mkey)
        if not cur:
            return {"metric": mkey, "name": name, "current": None, "prior": None, "change": {"display": ""}, "status": "Unavailable", "reason": NF + "."}
        out = {"metric": mkey, "name": cur.get("name", name), "current": cur["id"], "prior": prior["id"] if prior else None,
               "change": _change(cur, prior), "status": cur["status"]}
        if mkey in ("capex", "buybacks", "dividends"):
            out["note"] = "Cash outflow; shown as printed in the cash flow statement (negative values are outflows)."
        if mkey == "total_debt":
            out["note"] = cur.get("definition", "")
        return out

    perf = [row(k) for k, _ in ROWS_PERF]
    pos = [row(k) for k, _ in ROWS_POS]

    # calculations (formula + cited inputs, same period only)
    calcs = []
    for which in (0, 1):
        sfx = "cur" if which == 0 else "prior"
        rev, gp, oi, ni = fb.get("revenue", which), fb.get("gross_profit", which), fb.get("operating_income", which), fb.get("net_income", which)
        if not bank:
            calcs.append(fb.calc(f"gross_margin:{sfx}", "Gross margin", "Gross profit / Revenue", [("Gross profit", gp), ("Revenue", rev)], F.ratio, "percent"))
        calcs.append(fb.calc(f"operating_margin:{sfx}", "Operating margin", "Operating income / Revenue", [("Operating income", oi), ("Revenue", rev)], F.ratio, "percent"))
        calcs.append(fb.calc(f"net_margin:{sfx}", "Net margin", "Net income / Revenue", [("Net income", ni), ("Revenue", rev)], F.ratio, "percent"))
        if not bank:
            cfo, capex = fb.get("cfo", which), fb.get("capex", which)
            calcs.append(fb.calc(f"fcf:{sfx}", "Free cash flow", "Operating cash flow - |Capital expenditures|",
                                 [("Operating cash flow", cfo), ("Capital expenditures", capex)], lambda a, b: (a - abs(b), ""), "USD"))
        bb, dv = fb.get("buybacks", which), fb.get("dividends", which)
        if bb or dv:
            ins = [(n, x) for n, x in (("Share repurchases", bb), ("Dividends paid", dv)) if x]
            calcs.append(fb.calc(f"capital_returned:{sfx}", "Capital returned to shareholders", " + ".join(f"|{n}|" for n, _ in ins), ins,
                                 lambda *v: (sum(abs(x) for x in v), ""), "USD"))
        cash, debt = fb.get("cash", which), fb.get("total_debt", which)
        calcs.append(fb.calc(f"net_cash:{sfx}", "Net cash (cash minus total debt)", "Cash and cash equivalents - Total debt",
                             [("Cash and cash equivalents", cash), ("Total debt", debt)], lambda a, b: (a - b, ""), "USD"))
    calc_rows = []
    for base in ("gross_margin", "operating_margin", "net_margin", "fcf", "capital_returned", "net_cash"):
        c, p = fb.calcs.get(f"{base}:cur"), fb.calcs.get(f"{base}:prior")
        if not c:
            continue
        ch = {"display": ""}
        if c.get("value") is not None and p and p.get("value") is not None:
            if c.get("unit") == "percent":
                ch = {"pp": c["value"] - p["value"], "display": f"{c['value'] - p['value']:+.1f} pts"}
            else:
                ch = _change(c, p)
        calc_rows.append({"metric": base, "name": c["name"], "current": c["id"], "prior": p["id"] if p else None, "change": ch,
                          "status": c["status"], "formula": c["formula"], "reason": "; ".join(c.get("reasons", []))})

    # takeaways: each sentence cites the facts it rests on
    take = []
    rv_c, rv_p = fb.comparable("revenue")
    if rv_c and rv_p:
        ch = _change(rv_c, rv_p)
        if ch.get("pct") is not None:
            t = f"Revenue {'rose' if ch['pct'] >= 0 else 'fell'} {abs(ch['pct']):.1f}% to {rv_c['display']} in {rv_c['period'].get('short') or rv_c['period']['label']} from {rv_p['display']} in {rv_p['period'].get('short') or rv_p['period']['label']}."
            refs = [rv_c["id"], rv_p["id"]]
            prod = next((b for b in bds if b["kind"] != "region"), None)
            if prod:
                mv = [i for i in prod["items"] if i.get("change_abs") is not None]
                if mv:
                    top = max(mv, key=lambda i: i["change_abs"]) if ch["pct"] >= 0 else min(mv, key=lambda i: i["change_abs"])
                    if (top["change_abs"] > 0) == (ch["pct"] >= 0):
                        t += f" The largest {'increase' if ch['pct'] >= 0 else 'decrease'} among the reported lines was {top['name']} ({'+' if top['change_abs'] >= 0 else '-'}{fmt_value(abs(top['change_abs']), 'USD')})."
            take.append({"text": t, "basis": "Calculated from reported figures", "refs": refs})
    om = fb.calcs.get("operating_margin:cur"), fb.calcs.get("operating_margin:prior")
    if om[0] and om[1] and om[0].get("value") is not None and om[1].get("value") is not None:
        d = om[0]["value"] - om[1]["value"]
        take.append({"text": f"Operating margin was {om[0]['display']}, {abs(d):.1f} points {'higher' if d >= 0 else 'lower'} than the prior period ({om[1]['display']}).",
                     "basis": "Calculated from reported figures", "refs": [om[0]["id"], om[1]["id"]]})
    ni_c, ni_p = fb.comparable("net_income")
    eps_c, eps_p = fb.comparable("eps_diluted")
    if ni_c and ni_p:
        ch = _change(ni_c, ni_p)
        t = f"Net income was {ni_c['display']} versus {ni_p['display']} ({ch['display'] or 'change not meaningful'})"
        refs = [ni_c["id"], ni_p["id"]]
        if eps_c and eps_p:
            t += f"; diluted EPS was {eps_c['display']} versus {eps_p['display']}"
            refs += [eps_c["id"], eps_p["id"]]
        take.append({"text": t + ".", "basis": "Reported", "refs": refs})
    fcf = fb.calcs.get("fcf:cur")
    if fcf and fcf.get("value") is not None:
        t = f"Free cash flow was {fcf['display']} in {fcf['period'].get('short') or fcf['period']['label']}, defined as operating cash flow minus capital expenditures."
        cr = fb.calcs.get("capital_returned:cur")
        refs = [fcf["id"]]
        if cr and cr.get("value") is not None:
            t += f" Share repurchases and dividends totalled {cr['display']}."
            refs.append(cr["id"])
        take.append({"text": t, "basis": "Calculated from reported figures", "refs": refs})
    nc = fb.calcs.get("net_cash:cur")
    if nc and nc.get("value") is not None:
        take.append({"text": f"Cash and cash equivalents less total debt: {'net cash' if nc['value'] >= 0 else 'net debt'} of {fmt_value(abs(nc['value']), 'USD')} "
                             f"at {nc['period'].get('short') or nc['period']['label']}. Marketable securities are not included.",
                     "basis": "Calculated from reported figures", "refs": [nc["id"]]})
    take = take[:5]

    # coverage
    stm = {k: any(r["statement"] == k for r in rows) for k in ("income", "balance", "cashflow")}
    published = [r for r in perf + pos if r.get("current")]
    issues = list(fb.issues)
    limited = len(published) < 4
    status = "Partial" if (not all(stm.values()) or limited or doc.warnings and any(w["code"] in ("truncated", "empty_pages", "incomplete") for w in doc.warnings)) else "Complete for the items listed"
    if limited:
        status = "Limited"
    followups = []
    if not stm["cashflow"]:
        followups.append("The cash flow statement was not found; cash generation and capital expenditures could not be reported.")
    if not any(w["category"] == "Liquidity and refinancing" for w in watch):
        followups.append("No debt maturity schedule was located in the extracted text; check the debt footnote for repayment timing.")
    if not rf:
        followups.append(rf_note + (" Quarterly reports usually refer back to the annual report's risk factors." if (co.get("form") or "").startswith("10-Q") else ""))
    for i in issues:
        followups.append(f"Review: {i['message']}")
    mv = fb.facts.get("market_value_nonaffiliates")

    rep = {
        "schema": SCHEMA, "generated_at": now.isoformat(timespec="seconds"), "brand": "BMG Capital", "title": "Company Report",
        "review": "Automatically generated by the BMG Capital reader. Not reviewed by an analyst. Not investment advice.",
        "company": co,
        "source": {"name": doc.title, "sha256": doc.sha256, "kind": doc.kind, "pages": len(doc.pages),
                   "printed_pages_detected": sum(1 for p in doc.pages if p.printed), "text_chars": len(doc.body),
                   "sections": [{"item": s.item, "title": s.title, "pdf_page": s.page} for s in doc.sections]},
        "coverage": {
            "status": status, "statements_found": stm, "tables_parsed": len(rows),
            "facts_published": len(published), "checks_run": len(fb.checks), "checks_failed": sum(1 for c in fb.checks if not c["passed"]),
            "warnings": doc.warnings, "issues": issues, "searched_for": searched,
            "note": ("Only figures matched to a printed table row, cover sentence or inline XBRL tag are published. "
                     "Figures that could not be matched are listed as not found rather than estimated."),
        },
        "overview": {"description": _description(doc, co.get("form")), "segments": _segments_text(doc, bds), "takeaways": take,
                     "limited": limited, "market_value_note": ({"fact": mv["id"], "text": f"The cover reports {mv['display']} of equity held by non-affiliates {mv['period']['label'][0].lower() + mv['period']['label'][1:]}. "
                                                                   "This is a historical, non-affiliate figure, not current market capitalization."} if mv else None)},
        "financials": {"performance": perf, "calculated": calc_rows,
                       "period_note": "Each figure states its own period. Income statement and cash flow figures are for the period shown; balance sheet figures are at the date shown. Quarterly, year-to-date and annual figures are never combined."},
        "operating": {"profile": prof["profile"], "kpis": prof["items"], "not_applicable": prof["not_applicable"], "breakdowns": bds},
        "position": {"rows": pos},
        "risks": {"items": rf, "note": rf_note, "watch": watch, "followups": followups},
        "checks": fb.checks,
        "methodology": [
            "The uploaded file was read once and not saved. Text was extracted page by page; running headers and footers were removed and printed page numbers recorded.",
            "Financial statement tables were parsed row by row with their column periods, scale caption and units. Values in parentheses are negative; a dash is reported as nil.",
            "Each metric is taken from its primary statement. The same label in other tables is used as a cross-check; differing values are flagged for review, not resolved silently.",
            "Arithmetic identities (gross profit, balance sheet totals, EPS against net income and diluted shares) are checked where the inputs exist.",
            "Calculations use only cited inputs of the same period. Percentage changes are withheld when the base is zero or negative.",
            "Risks come from the Risk Factors headings, with short quoted evidence; the 'why it may matter' lines are BMG interpretation, not statements from the filing.",
            "No language model is used, so instructions written inside a document cannot change the report.",
        ],
        "evidence": {"facts": fb.facts, "calcs": fb.calcs},
    }
    rep["findings"] = findings(rep, fb, claims or [])
    return rep


def findings(rep: dict, fb, claims: list) -> list[dict]:
    """What the spider shows: reported facts, KPIs, revenue lines and risks,
    deduplicated, each pointing to a page and the text to highlight."""
    out, seen = [], set()

    def add(group, label, display, period, src, ref=None, kind="reported", status=None, text=None):
        key = (label.lower(), display, period)
        if key in seen or not src:
            return
        seen.add(key)
        out.append({"id": f"fd{len(out)}", "group": group, "label": label, "value": display, "period": period, "kind": kind, "status": status,
                    "pdf_page": src.get("pdf_page"), "printed_page": src.get("printed_page"), "raw": src.get("raw"),
                    "text": text, "excerpt": (src.get("excerpt") or "")[:220], "offset": src.get("offset", -1), "ref": ref})

    for f in fb.facts.values():
        if f["metric"] == "debt_line":
            continue
        add("Financials", f["name"], f["display"], f["period"]["label"], f["source"], f["id"], status=f["status"])
    for k in rep["operating"]["kpis"]:
        if k.get("display"):
            add("Operating metrics", k["name"], k["display"], k.get("period", ""), k["source"], status=k.get("status"))
    for b in rep["operating"]["breakdowns"]:
        for i in b["items"]:
            add("Operating metrics", f"{i['name']} ({b['kind']})", i["display"], b["period"], i["source"])
    for r in rep["risks"]["items"]:
        add("Risks", r["category"], "", r["title"][:120], r["source"], kind="reported", text=r["source"]["excerpt"][:80])
    for w in rep["risks"]["watch"]:
        add("Risks", w["title"], "", w["category"], w["source"], text=(w["source"].get("excerpt") or "")[:80])
    # other numbers the text mentions, not validated: shown separately, capped.
    # A mention of a figure already listed above (same digits, same page) is
    # the same finding, not a new one.
    have = {(re.sub(r"\D", "", x["raw"] or ""), x["pdf_page"]) for x in out if x.get("raw")}
    n = 0
    for c in claims:
        if n >= 60:
            break
        raw = c.get("display") or c.get("value")
        if not raw or c.get("char_offset", -1) < 0:
            continue
        if (re.sub(r"\D", "", c.get("value") or ""), c.get("page")) in have:
            continue
        lbl = c.get("item") or c.get("label") or "Figure"
        before = len(out)
        add("Other figures", lbl if lbl != "Figure" else "Figure in text", raw, c.get("period") or "", {"pdf_page": c.get("page"), "raw": c.get("value"),
            "excerpt": c.get("snippet", ""), "offset": c.get("char_offset", -1)}, kind="mentioned", status="Unvalidated")
        n += len(out) - before
    out.sort(key=lambda x: (x["offset"] if x["offset"] is not None and x["offset"] >= 0 else 10**9))
    return out


def markdown(rep: dict, source_name: str, read_at: str) -> str:
    """The report as Markdown (saved to the vault next to report.json).
    Same data as the on-screen report and the PDF."""
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    co = rep["company"]

    def ev(fid):
        return facts.get(fid) or calcs.get(fid) or {}

    def loc(x):
        s = x.get("source") or {}
        if s.get("pdf_page"):
            return f"PDF p.{s['pdf_page']}" + (f" (printed {s['printed_page']})" if s.get("printed_page") else "")
        if s.get("anchor"):
            return f"XBRL {s['anchor']}"
        if x.get("inputs"):
            return "calculated from " + ", ".join(dict.fromkeys(loc(ev(i)) for i in x["inputs"] if ev(i)))
        return ""

    def cell(s):
        return str(s or "").replace("|", "/").replace("\n", " ")

    out = [f"# {rep['brand']} {rep['title']}: {co.get('name') or 'Unidentified company'}", "",
           f"- Ticker: {co.get('ticker') or co.get('ticker_hint') or 'not identified'}; form: {co.get('form') or 'not identified'}; period: {co.get('period') or 'not identified'}",
           f"- Source: `{source_name}` (PRIMARY: the document as uploaded), sha256 `{rep['source']['sha256']}`, {rep['source']['pages']} pages",
           f"- Generated: {read_at}. {rep['review']}",
           f"- Coverage: {rep['coverage']['status']}; validation {'passed' if rep['validation']['passed'] else 'withdrew ' + str(len(rep['validation']['withdrawn'])) + ' item(s)'}",
           "", "## Executive overview", ""]
    d = rep["overview"]["description"]
    if d.get("text"):
        out += [d["text"] + (f" ({loc(d)})" if loc(d) else ""), ""]
    for t in rep["overview"]["takeaways"]:
        out.append(f"- {t['text']} [{t['basis']}; " + "; ".join(dict.fromkeys(loc(ev(r)) for r in t["refs"])) + "]")
    if rep["overview"].get("market_value_note"):
        out += ["", rep["overview"]["market_value_note"]["text"]]
    out += ["", "## Financial overview", "", "| Metric | Current | Prior | Change | Status | Source |", "|---|---|---|---|---|---|"]
    for r in rep["financials"]["performance"] + rep["financials"]["calculated"] + rep["position"]["rows"]:
        c, p = ev(r.get("current")), ev(r.get("prior"))
        cur = f"{c['display']} ({c['period'].get('short') or c['period']['label']})" if c else "Not found"
        pri = f"{p['display']} ({p['period'].get('short') or p['period']['label']})" if p else ""
        out.append(f"| {cell(r['name'])} | {cell(cur)} | {cell(pri)} | {cell(r['change'].get('display'))} | {r['status']} | {cell(loc(c) or r.get('reason'))} |")
    out += ["", rep["financials"]["period_note"], ""]
    op = rep["operating"]
    out += ["## Business and operating metrics", "", f"Profile: {op['profile']}", ""]
    for k in op["kpis"]:
        out.append(f"- {k['name']}: " + (f"{k.get('display')} ({loc(k)})" if k.get("status") != "Unavailable" else k.get("reason", "Not found.")))
    for b in op["breakdowns"]:
        out += ["", f"{b.get('title') or b['kind']}:", ""]
        for i in b["items"]:
            out.append(f"- {i['name']}: {i.get('display', '')}")
    out += ["", "## Risks and watch items", ""]
    for r in rep["risks"]["items"]:
        out += [f"- **{r['category']}**: {r['title']} ({loc(r)})", f"  - Why it may matter (BMG interpretation): {r.get('why_it_may_matter', '')}"]
    if rep["risks"].get("note"):
        out.append(rep["risks"]["note"])
    for w in rep["risks"]["watch"]:
        out.append(f"- Watch, {w['category']}: {w['title']} ({loc(w)})")
    for f in rep["risks"]["followups"]:
        out.append(f"- Follow up: {f}")
    out += ["", "## Checks", ""]
    for c in rep["checks"]:
        out.append(f"- {'Pass' if c['passed'] else 'FAIL'}: {c['name']}, {c['period']} ({c['detail']})")
    out += ["", "## Methodology and limitations", ""] + [f"- {m}" for m in rep["methodology"]]
    if rep["coverage"]["issues"]:
        out += ["", "Unresolved issues:", ""] + [f"- {i['message']}" for i in rep["coverage"]["issues"]]
    return "\n".join(out) + "\n"
