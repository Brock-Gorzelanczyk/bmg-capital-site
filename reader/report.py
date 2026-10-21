"""Assemble the BMG Capital Company Report (one dict) and the findings the
spider shows, from the same validated facts.

The report says what the filing establishes (reported), what was
calculated (formula and inputs), what is interpretation (labeled), and what
was not found. It never recommends buying or selling.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Optional

from . import document, facts as F, profiles, risks, signals, tables
from .facts import fmt_value

SCHEMA = "bmg.company_report/1"
NF = "Not found in the reviewed filing"


_LISTED = re.compile(r"(?<![\w&'’.\-])((?:[A-Z][\w&'’.\-]*,?[ \t]+(?:&[ \t]+)?){0,5}[A-Z][\w&'’.\-]*)[ \t]*\("
                     r"(?:NYSE American|NYSE MKT|NYSE|NASDAQ|Nasdaq|AMEX|TSX)[ \t]*:[ \t]*([A-Z]{1,5}(?:\.[A-Z])?)\)")


# a release with no listing mark names itself in its dateline: "ATLANTA, July 28, 2026 – The Coca-Cola Company today reported",
# "INDIANAPOLIS, August 10, 2026 – Simon(R), a real estate investment trust engaged in ..., dining, ..., today reported"
_DATELINE_NAME = re.compile(r"(?m)^[ \t]*[A-Z][A-Za-z.' \-]{2,40},(?:[ \t][A-Z][A-Za-z.]+,)?[ \t](?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?[ \t]"
                            r"\d{1,2},?[ \t](?:19|20)\d\d[ \t]*(?:/[^/\n]{2,40}/)?[ \t]*[\u2013\u2014\-]+[ \t]*"
                            r"((?:The[ \t]+)?[A-Z][\w&'\u2019.\-]*(?:,?[ \t]+(?:&[ \t]+)?[A-Z][\w&'\u2019.\-]*){0,5})[\u00ae\u2122]?(?:,[^\n]{2,200}?,)?[ \t]+"
                            r"(?:today|reported|announced|reports|announces)\b")
# and its quarter in its own words: "Coca-Cola Reports Second Quarter 2026 Results", "Caterpillar Reports Second-Quarter 2026
# Results", "Fourth-Quarter and Full-Year 2025 Results" (never a quarter named later in the text, which may be the prior year's)
_RELEASE_PERIOD = re.compile(r"(?i)\b(?:reports?|reported|announces?|announced)\b[^.\n]{0,40}?\b(first|second|third|fourth|march|june|september|december)[ \t\-\u2010\u2011]+quarter(?P<fy>\s+and\s+(?P<fyw>full|fiscal)[ \t\-\u2010\u2011]+year)?\s+"
                             r"(?:of[ \t]+)?(fiscal[ \t]+(?:year[ \t]+)?|FY[ \t]?)?((?:19|20)\d\d)\b")
# "Adobe Reports Record Q3 Results ... its third quarter FY2026", "Q4 and FY2025"
_RELEASE_Q = re.compile(r"(?i)\b(?:reports?|reported|announces?|announced)\b[^.\n]{0,40}?\bQ([1-4])[ \t]+(?P<qfy>and[ \t]+)?(FY[ \t]?|fiscal[ \t]+)?((?:19|20)\d\d)\b")
# the year first: "GATX Corporation Reports 2026 Second-Quarter Results", Apple's "results for its fiscal 2026 third quarter"
_RELEASE_YQ = re.compile(r"(?i)\b(?:reports?|reported|announces?|announced)\b(?:[^.\n]|\.(?=\d)){0,60}?\b(fiscal[ \t]+)?((?:19|20)\d\d)[ \t\-\u2010\u2011]+"
                         r"(first|second|third|fourth)[ \t\-\u2010\u2011]+quarter\b")
# the quarter by its end: Goldman Sachs "today reported net revenues of $18.68 billion ... for the third quarter ended September 30, 2026"
_RELEASE_ENDED = re.compile(r"(?i)\b(?:reports?|reported|announces?|announced)\b(?:[^.\n]|\.(?=\d)|\n(?=[a-z$\d])){0,200}?\bfor (?:the|its) (first|second|third|fourth)[ \t]+quarter[ \t]+"
                            r"ended[ \t]+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?[ \t]+\d{1,2},[ \t]+(?:19|20)\d\d)")


def _release_period(head: str):
    """A release's quarter in its own words, from the earliest statement of it in the head: (label, match) or (None, None)."""
    found = [m for m in (_RELEASE_PERIOD.search(head), _RELEASE_Q.search(head), _RELEASE_YQ.search(head), _RELEASE_ENDED.search(head)) if m]
    if not found:
        return None, None
    m = min(found, key=lambda x: x.start())
    if m.re is _RELEASE_PERIOD:
        fy = f"and {m.group('fyw').lower()} year " if m.group("fy") else ""
        return f"{_ORDINAL.get(m.group(1), m.group(1).capitalize())} quarter {fy}{'fiscal ' if m.group(4) else ''}{m.group(5)}", m
    if m.re is _RELEASE_Q:
        both = "and fiscal year " if m.group("qfy") and m.group(3) else "fiscal " if m.group(3) else ""
        return f"{_ORDINAL[m.group(1)]} quarter {both}{m.group(4)}", m
    if m.re is _RELEASE_YQ:
        return f"{m.group(3).capitalize()} quarter {'fiscal ' if m.group(1) else ''}{m.group(2)}", m
    ended = " ".join(m.group(2).split())
    return f"{m.group(1).capitalize()} quarter ended {ended}", m


_TITLE_NAME = re.compile(r"(?m)(?:^|\|)[ \t]*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?[ \t]+\d{1,2},[ \t]+(?:19|20)\d\d[ \t]+)?"
                         r"((?:[A-Z][\w&'\u2019.\-]*[ \t]+){0,4}?[A-Z][\w&'\u2019.\-]*)[\u00ae\u2122]?[ \t]+(?i:reports|announces|reported|announced)[ \t]+"
                         r"(?i:record[ \t]+)?(?i:(?:first|second|third|fourth)[ \t\-\u2010\u2011]+quarter|Q[1-4]\b|(?:march|june|september|december)[ \t]+quarter|(?:19|20)\d\d|fiscal|full[ \t\-]year|results|earnings)")
_EXCHANGE_NAME = re.compile(r"(?i)stock exchange|stock market|^(?:the )?(?:nyse|nasdaq)\b")
_ORDINAL = {"1": "First", "2": "Second", "3": "Third", "4": "Fourth"}


def _company(doc) -> dict:
    head = doc.body[:15000]
    dei = getattr(doc, "dei", None) or {}
    loc = lambda m: doc.location(m.start()) if m else {}
    name_m = re.search(r"\n([^\n]{2,90})\n\s*\(Exact name of (?:registrant|Registrant)", "\n" + head)
    if name_m and re.search(r"commission file|file number|^\s*\(|form 10|securities and exchange", name_m.group(1), re.I):
        name_m = None
    # the cover's own form line ("FORM 10-K", alone or folded in with the cover's other short lines), not a mention:
    # a call transcript or an earnings release refers to "our Form 10-K" in its safe-harbor text
    form_m = re.search(r"(?m)(?:^|(?<=  ))[ \t\u00a0]*(?:FORM[ \t\u00a0]+(10-K(?:/A)?|10-Q(?:/A)?|20-F(?:/A)?|40-F(?:/A)?)\b|Form[ \t\u00a0]+(10-K(?:/A)?|10-Q(?:/A)?|20-F(?:/A)?|40-F(?:/A)?)[ \t\u00a0]*$)", head)
    per_m = re.search(r"for the (fiscal year|quarterly period) ended\s+([A-Z][a-z]+\s+\d{1,2},\s+\d{4})", head, re.I)
    exch_m = re.search(r"(The Nasdaq Stock Market LLC|Nasdaq Global Select Market|New York Stock Exchange|NYSE American|NYSE)", head)
    tick = None
    for m in re.finditer(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\s+(?:The\s+)?(?:Nasdaq|New York Stock Exchange|NYSE)", head):
        if m.group(1) not in {"THE", "LLC", "INC", "NEW", "PAR", "LLC."}:
            tick = m.group(1)
            break
    sig = re.search(r"SIGNATURES[\s\S]{0,4000}?Date:\s*([A-Z][a-z]+ \d{1,2}, \d{4})", doc.body)
    form = (form_m.group(1) or form_m.group(2)).upper() if form_m else None
    # no registrant cover (an earnings release, a call): the name the document gives with its listing,
    # "GATX Corporation (NYSE: GATX) today reported", "PepsiCo, Inc. (NASDAQ: PEP)"
    # the company with its listing, never the exchange ("listed on the New York Stock Exchange (NYSE: HD)")
    listed_any = None if (name_m or dei.get("EntityRegistrantName")) else _LISTED.search(head[:6000])
    listed_m = listed_any and next((m for m in _LISTED.finditer(head[:6000]) if not _EXCHANGE_NAME.search(m.group(1).strip())), None)
    line_m = None if (name_m or dei.get("EntityRegistrantName") or listed_m) else _DATELINE_NAME.search(head[:6000])
    title_m = None if (name_m or dei.get("EntityRegistrantName") or listed_m or line_m) else next(
        (m for m in _TITLE_NAME.finditer(head[:6000]) if m.group(1).strip().lower() not in ("company", "the company")), None)
    rel_label, rel_m = (None, None) if (per_m or form_m) else _release_period(head[:4000])
    return {
        "name": dei.get("EntityRegistrantName") or (re.sub(r"\s+", " ", name_m.group(1)).strip() if name_m else None)
                or (re.sub(r"\s+", " ", listed_m.group(1)).strip(" ,") if listed_m else None)
                or (re.sub(r"\s+", " ", line_m.group(1)).strip(" ,") if line_m else None)
                or (re.sub(r"\s+", " ", title_m.group(1)).strip(" ,") if title_m else None),
        "ticker": tick or (dei.get("TradingSymbol") or None) or ((listed_m or listed_any).group(2) if (listed_m or listed_any) else None), "form": form or (dei.get("DocumentType") or None), "amendment": bool(form and form.endswith("/A")),
        "period": ((("Fiscal year ended " if per_m.group(1).lower() == "fiscal year" else "Quarter ended ") + re.sub(r"\s+", " ", per_m.group(2))) if per_m
                   else rel_label),
        "exchange": exch_m.group(1) if exch_m else None,
        "signature_date": sig.group(1) if sig else None,
        "evidence": {"name": loc(name_m or listed_m or line_m or title_m), "form": loc(form_m), "period": loc(per_m or rel_m), "signature_date": loc(sig)},
    }


def _description(doc, form: Optional[str] = None) -> dict:
    sec = next((s for s in doc.sections if s.item == "1" and re.match(r"business", s.title, re.I)), None)
    if not sec:
        if (form or "").startswith("10-Q"):
            return {"text": None, "reason": "Quarterly reports (Form 10-Q) do not include a Business section; see the company's latest 10-K for a description."}
        return {"text": None, "reason": "Item 1 (Business) was not located in the extracted text."}
    seg = re.sub(r"\s+", " ", doc.body[sec.offset:sec.offset + 5000])
    seg = re.sub(r"^(?:PART\s+[IV]+\s+)?Item 1\.?\s+Business\s*", "", seg)
    sents = [re.sub(r"(?i)^(Company Background|Business Overview|Overview|General|Background|Our Company|Company Overview)\s+(?=[A-Z])", "", x.strip())
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


def calculations(fb, bank: bool = False) -> None:
    """Margins, free cash flow, capital returned and net cash for the current
    and prior period: formula and cited inputs, same period only."""
    calcs = []
    for which in (0, 1):
        sfx = "cur" if which == 0 else "prior"
        rev, gp, oi, ni = fb.get("revenue", which), fb.get("gross_profit", which), fb.get("operating_income", which), fb.get("net_income", which)
        if not bank:
            cost = fb.get("cost_of_revenue", which)
            if not gp and cost and rev:          # no gross profit line (Pfizer): from the statement's own revenue and cost of sales
                calcs.append(fb.calc(f"gross_margin:{sfx}", "Gross margin", "(Revenue - |Cost of revenue|) / Revenue", [("Revenue", rev), ("Cost of revenue", cost)],
                                     lambda r, c: F.ratio(r - abs(c), r), "percent"))
            else:
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


def _body_pages(doc, pages: list) -> list:
    """Pages that belong to the report itself: anything after Item 15
    (exhibits, signatures, attached agreements) does not count against
    coverage, since the statements and notes come before it."""
    cut = max((sec.page for sec in getattr(doc, "sections", []) or [] if sec.item in ("15", "16") and sec.page), default=None)
    return [p for p in pages if cut is None or p < cut]


def _body_complete(doc) -> bool:
    """Text was cut at the size limit, but only after Item 15 (exhibits):
    the report itself was read in full."""
    return any(sec.item in ("15", "16") for sec in getattr(doc, "sections", []) or [])


def build(doc, rows: list, fb: "F.FactBook", claims: Optional[list] = None, analysis: Optional[dict] = None) -> dict:
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

    calculations(fb, bank)
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
        ok = lambda x: x is not None and x.get("value") is not None
        calc_rows.append({"metric": base, "name": c["name"], "current": c["id"] if ok(c) else None, "prior": p["id"] if ok(p) else None, "change": ch,
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
    # a statement whose title is in the filing but whose table could not be
    # read is a limitation, not an absence
    titles = {"income": r"statements? of (operations|income|earnings)", "balance": r"balance sheets?|statements? of financial (position|condition)",
              "cashflow": r"statements? of cash flows?"}
    for k, rx in titles.items():
        if stm[k]:
            continue
        m = None
        for mm in re.finditer(rf"(?im)^[ \t]*(?:[A-Z][A-Z .,&'\u2019-]{{2,60}}\s+)?(?:condensed\s+)?consolidated\s+(?:{rx})\b[^\n]{{0,60}}$", doc.body):
            if not re.search(r"\d{2,3}\s*$", mm.group(0)):         # not a contents-page line
                m = mm
                break
        if m:
            pg = doc.page_at(m.start())
            issues.append({"type": "unreadable_statement", "statement": k,
                           "message": f"The {'income statement' if k == 'income' else 'balance sheet' if k == 'balance' else 'cash flow statement'} "
                                      f"was located on {'PDF ' if doc.kind == 'pdf' else ''}page {pg} but its table could not be read; "
                                      "its figures are missing from this report rather than estimated."})
    # a 10-K that incorporates its statements by reference from its annual report (Wells Fargo: exhibit 13): say where they are
    if not any(stm.values()) and re.search(r"(?is)incorporated\s+(?:herein\s+)?by\s+reference.{0,600}?annual\s+report\s+to\s+(?:share|stock)holders"
                                           r"|annual\s+report\s+to\s+(?:share|stock)holders.{0,600}?incorporated\s+(?:herein\s+)?by\s+reference", doc.body[:60000]):
        issues.append({"type": "incorporated_by_reference",
                       "message": "This 10-K incorporates its financial statements by reference from the Annual Report to Shareholders, which is filed as "
                                  "a separate document (exhibit 13); they are not in this file. Load the filing by ticker in the Crawler, which reads exhibit 13 "
                                  "when it holds the annual report, or open exhibit 13 from the filing's index on SEC EDGAR."})
    limited = len(published) < 4
    status = "Partial" if (not all(stm.values()) or limited or any(i["type"] == "unreadable_statement" for i in issues) or doc.warnings and any(w["code"] == "incomplete" or (w["code"] == "truncated" and not _body_complete(doc)) or (w["code"] == "empty_pages" and len(_body_pages(doc, w.get("pages", []))) > max(3, len(doc.pages) * 0.05))
                                    for w in doc.warnings)) else "Complete for the items listed"
    if limited:
        status = "Limited"
    followups = []
    if not stm["cashflow"] and not any(i.get("statement") == "cashflow" for i in issues):
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
            ("The file and this report are kept on the owner's computer (My reads) so the read can be reopened; nothing is published. "
             if os.environ.get("BMG_LOCAL") == "1" else "The uploaded file was read once and not saved. ")
            + "Text was extracted page by page; running headers and footers were removed and printed page numbers recorded.",
            "Financial statement tables were parsed row by row with their column periods, scale caption and units. Values in parentheses are negative; a dash is reported as nil.",
            "Each metric is taken from its primary statement. The same label in other tables is used as a cross-check; differing values are flagged for review, not resolved silently.",
            "Arithmetic identities (gross profit, balance sheet totals, EPS against net income and diluted shares) are checked where the inputs exist.",
            "Calculations use only cited inputs of the same period. Percentage changes are withheld when the base is zero or negative.",
            "Risks come from the Risk Factors headings, with short quoted evidence; the 'why it may matter' lines are BMG interpretation, not statements from the filing.",
            "Figures are extracted by fixed rules, not a language model, so instructions written inside a document cannot change them.",
        ],
        "evidence": {"facts": fb.facts, "calcs": fb.calcs},
    }
    rep["findings"] = findings(rep, fb, claims or [])
    if analysis:
        rep["brief"] = analysis.get("brief")
        from . import signals as _sg
        rep["screens_version"] = _sg.screens()["version"]
        rep["signals"] = analysis.get("signals", [])
        _anchor_signals(rep)
    else:
        rep["brief"], rep["signals"] = None, []
    research_sections(doc, rows, fb, rep)
    return rep


def _anchor_signals(rep: dict) -> None:
    """Each signal pops where the spider finds the last of its inputs: the
    finding of the input fact that sits deepest in the document."""
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    by_ref = {f["ref"]: f for f in rep["findings"] if f.get("ref")}

    def leaves(fid, depth=0):
        if depth > 4:
            return []
        if fid in facts:
            return [fid]
        c = calcs.get(fid)
        return [x for i in (c or {}).get("inputs", []) for x in leaves(i, depth + 1)]

    for sg in rep["signals"]:
        cands = [by_ref[f] for i in sg["inputs"] for f in leaves(i) if f in by_ref]
        if not cands and sg.get("id") == "maturity_wall":
            cands = [f for f in rep["findings"] if f["group"] == "Risks" and f["label"] == "Scheduled debt maturities"]
        if not cands and sg.get("kpi"):
            cands = [f for f in rep["findings"] if f["group"] == "Operating metrics" and f["label"] == sg["kpi"]]
        cands = [c for c in cands if c.get("offset") is not None and c["offset"] >= 0]
        if not cands:
            sg["anchor"] = None
            continue
        a = max(cands, key=lambda c: c["offset"])
        sg["anchor"] = {"finding": a["id"], "pdf_page": a.get("pdf_page"), "offset": a["offset"]}
        a.setdefault("signals", []).append(sg["id"])


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
        # component lines (debt, capex) are printed rows too: the spider reaches
        # them, and the totals built from them are revealed only after they are
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
    ending_prefix = ""
    if rep.get("document_ending"):
        from .endings import markdown as ending_markdown
        ending_prefix = ending_markdown(rep, source_name, read_at) + "\n## Complete reader report\n\n"
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    co = rep["company"]

    def ev(fid):
        return facts.get(fid) or calcs.get(fid) or {}

    def loc(x):
        s = x.get("source") or {}
        if s.get("pdf_page"):
            same = str(s.get("printed_page")) == str(s["pdf_page"])
            return (f"PDF p.{s['pdf_page']}" if rep["source"].get("kind") == "pdf" else f"p.{s['pdf_page']}") + (f" (printed {s['printed_page']})" if s.get("printed_page") and not same else "")
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
    bz = rep.get("business") or {}
    if bz.get("revenue_mix"):
        out += ["", f"Revenue mix: {bz['revenue_mix']['text']} ({loc(bz['revenue_mix'])})"]
    if rep.get("exec_findings"):
        out += ["", "## Executive findings", ""]
        for x in rep["exec_findings"]:
            out.append(f"- **{x['severity'].upper()}** {x['headline']}. {x['text']} Why it may matter (BMG interpretation): {x['why']} ("
                       + "; ".join(dict.fromkeys(loc(ev(i)) for i in x["refs"])) + ")")
    p3 = (rep.get("performance3") or {}).get("rows") or []
    if p3:
        cols = [ev(i)["period"].get("short") for i in p3[0]["ids"]]
        out += ["", "## Financial performance", "", "| Metric | " + " | ".join(cols) + " | Source |", "|---|" + "---|" * len(cols) + "---|"]
        for r in p3:
            out.append(f"| {cell(r['name'])} | " + " | ".join(ev(i)["display"] for i in r["ids"]) + " |" * (len(cols) - len(r["ids"])) + f" | {cell(loc(ev(r['ids'][0])))} |")
    ec = rep.get("earnings_cash") or {}
    if ec.get("items"):
        out += ["", "## Earnings and cash generation", "", ec["note"], ""]
        out += [f"- {ev(i)['name']}: {ev(i)['display']} ({loc(ev(i))})" for i in ec["items"] if ev(i)]
    if rep.get("questions"):
        out += ["", "## Questions for further research", ""] + [f"- {q['q']} ({q['basis']})" for q in rep["questions"]]
    if rep.get("brief") or rep.get("signals"):
        out += ["", "## What the numbers say", ""]
        if rep.get("brief"):
            out += [f"Read as: {rep['brief']['profile']}. {rep['brief']['why']}", ""]
        for sg in rep.get("signals") or []:
            out.append(f"- **{sg['severity'].upper()}** {sg['headline']}: {sg['text']} ({sg['rule']}; " + "; ".join(dict.fromkeys(loc(ev(i)) for i in sg["inputs"])) + ")")
        if rep.get("brief"):
            out += ["", "What we looked for:", ""] + [f"- {it['title']}: {it.get('answer') or it.get('note') or 'Not found in the reviewed filing.'}" for it in rep["brief"]["items"]]
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
    return ending_prefix + "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Research brief sections (all built from facts, calculations and signals
# already in the report; every item carries the ids it rests on).

WHY_SIG = {
    "equity_multiplier": "Higher leverage magnifies the effect of any change in asset values or earnings on equity holders.",
    "debt_to_equity": "Lenders' claims rank ahead of shareholders; the more debt per dollar of equity, the less room for a bad year.",
    "debt_to_assets": "Shows how much of the asset base is financed by borrowing rather than owners and other liabilities.",
    "interest_coverage": "Thin coverage leaves less room for lower earnings or higher interest rates before interest strains cash flow.",
    "gains_share": "Earnings that depend on asset sales move with sale timing and resale prices.",
    "cash_conversion": "Shows how much of reported profit arrived as operating cash in the year.",
    "roe": "Profit relative to the owners' capital; leverage raises it as well as operating performance.",
    "fcf": "What is left for debt reduction, dividends or buybacks after capital spending.",
    "payout_vs_fcf": "Payouts above free cash flow are funded from the balance sheet.",
    "revenue_growth": "The direction and size of the top line set the base for everything else.",
    "margin_trend": "Margin changes show whether growth or decline reached profit.",
    "inventory_build": "Inventory built ahead of demand can lead to discounting or production cuts.",
    "maturity_wall": "Debt concentrated in one year has to be refinanced at that year's rates.",
    "utilization": "For a lessor, idle equipment earns nothing; utilization is the first read on demand.",
    "net_cash": "Net debt is the part of borrowing not covered by cash on hand.",
}


def _cf_items(fb, ni) -> list:
    """Reconciling lines between net income and operating cash flow, as
    printed in the operating section of the cash flow statement."""
    from .facts import _core
    cf = [r for r in fb.rows if r["statement"] == "cashflow" and _core(r) and r["unit"] == "USD"]
    if not cf:
        return []
    tid = cf[0].get("table_id")
    sect = []
    for r in cf:
        if r.get("table_id") != tid:
            continue
        if re.search(r"net cash (provided|used|generated|from)|cash generated by operating", r["key"]):
            break
        if re.match(r"^(net income|net earnings|net income loss)$", r["key"]) or not r["values"]:
            continue
        if re.search(r"beginning of (the )?(period|year)$", r["key"]):
            continue            # Amazon opens its cash flow statement with the beginning cash balance
        sect.append(r)
    # the column of the net income period (Amazon prints 2023 | 2024 | 2025, latest last)
    def col(r):
        if ni:
            for i, c in enumerate(r["cols"]):
                if c.get("end") == ni["period"].get("end") and (c.get("type") or "") == (ni["period"].get("type") or ""):
                    return i
        return 0
    sect = [r for r in sect if col(r) < len(r["values"]) and r["values"][col(r)] is not None]
    base = abs(ni["value"]) if ni else max((abs(r["values"][col(r)]) for r in sect), default=1) or 1
    out = []
    for r in sorted(sect, key=lambda r: -abs(r["values"][col(r)])):
        f = fb._fact_from("cf_item", r, col(r), "flow")
        if abs(f["value"]) < 0.04 * base or len(out) >= 6:
            continue
        f["id"] = f"cf_item:{r['offset']}:{col(r)}"
        f["name"] = r["full_label"]
        fb.facts[f["id"]] = f
        out.append(f["id"])
    return out


def research_sections(doc, rows, fb, rep: dict) -> None:
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    sigs = {s["id"]: s for s in rep.get("signals") or []}
    ev = lambda i: facts.get(i) or calcs.get(i)

    # business overview
    d = rep["overview"]["description"]
    mix = None
    for b in rep["operating"]["breakdowns"]:
        if b["kind"] != "region":
            top = sorted(b["items"], key=lambda i: -(i.get("value") or 0))[:3]
            mix = {"text": "Largest revenue lines: " + ", ".join(f"{i['name']} {i['display']} ({i['share']:.0f}%)" for i in top) + f" of {b['total_display']} ({b['period']}).",
                   "source": b["source"], "basis": "Calculated shares of reported revenue lines"}
            break
    kp = [k["name"] + (f" {k['display']}" if k.get("display") else "") for k in rep["operating"]["kpis"] if k.get("display")]
    rep["business"] = {"description": d, "segments": rep["overview"]["segments"], "revenue_mix": mix,
                       "profile": (rep.get("brief") or {}).get("profile") or rep["operating"]["profile"],
                       "drivers": kp[:6]}

    # executive findings: screened items first, then the most informative observations
    pri = ["maturity_wall", "interest_coverage", "debt_to_assets", "equity_multiplier", "debt_to_equity", "payout_vs_fcf", "gains_share",
           "margin_trend", "fcf", "cash_conversion", "inventory_build", "revenue_growth", "utilization", "roe"]
    lev = {"equity_multiplier", "debt_to_equity", "debt_to_assets"}
    chosen = []
    for s in [x for x in rep.get("signals") or [] if x["severity"] in ("flag", "watch")] + [sigs[i] for i in pri if i in sigs]:
        if len(chosen) >= 5:
            break
        if s in chosen or (s["id"] in lev and sum(1 for c in chosen if c["id"] in lev) >= 2):
            continue                      # at most two of the overlapping leverage measures
        sid = s["id"]
        if sid == "revenue_growth" and abs(s["value"]) < 5:
            continue
        if sid == "cash_conversion" and 0.8 <= s["value"] <= 1.5:
            continue
        chosen.append(s)
    rep["exec_findings"] = [{"id": s["id"], "severity": s["severity"], "headline": s["headline"], "text": s["text"],
                             "why": WHY_SIG.get(s["id"], ""), "refs": s.get("inputs") or [], "source": s.get("source")} for s in chosen[:5]]

    # three-year performance, same period type only
    perf3 = []
    for m in ("revenue", "operating_income", "pretax_income", "net_income", "eps_diluted"):
        ids = [i for i in fb.primary_periods.get(m, []) if ev(i)]
        if not ids:
            continue
        t0 = ev(ids[0])["period"]["type"]
        ids = [i for i in ids if ev(i)["period"]["type"] == t0][:3]
        perf3.append({"metric": m, "name": ev(ids[0])["name"], "ids": ids})
    restated = any((facts.get(i) or {}).get("period", {}).get("basis") for r in perf3 for i in r["ids"])
    rep["performance3"] = {"rows": perf3, "note": ("Some columns are presented as restated or recast in this filing." if restated else "")}

    # earnings and cash generation
    nic = fb.get("net_income_consolidated") or fb.get("net_income")
    cfo = fb.get("cfo")
    items = _cf_items(fb, nic)
    rep["evidence"]["facts"] = fb.facts            # the new reconciling-line facts
    facts = rep["evidence"]["facts"]
    rep["earnings_cash"] = {"net_income": nic["id"] if nic else None, "cfo": cfo["id"] if cfo else None, "items": items,
                            "signal": "cash_conversion" if "cash_conversion" in sigs else None,
                            "note": "Largest lines reconciling net income to operating cash flow, as printed in the cash flow statement. Positive lines added cash; negative lines used it."}

    # financial position and capital allocation
    cap_lines = [i for i in (fb.get("capex") or {}).get("inputs", []) if i in facts]
    mat = next((w for w in rep["risks"]["watch"] if w["title"] == "Scheduled debt maturities"), None)
    rep["capital"] = {"cash": (fb.get("cash") or {}).get("id"), "debt": (fb.get("total_debt") or {}).get("id"),
                      "debt_definition": (fb.get("total_debt") or {}).get("definition"),
                      "maturities": mat, "capex": (fb.get("capex") or {}).get("id"), "capex_lines": cap_lines,
                      "dividends": (fb.get("dividends") or {}).get("id"), "buybacks": (fb.get("buybacks") or {}).get("id"),
                      "fcf_signal": "fcf" if "fcf" in sigs else None, "payout_signal": "payout_vs_fcf" if "payout_vs_fcf" in sigs else None}

    # questions for further research, each tied to what is missing or unresolved
    qs = []
    for it in (rep.get("brief") or {}).get("items", []):
        if it.get("status") != "found" and it.get("industry"):
            if it["key"].startswith("kpi:"):
                qs.append({"q": f"{it['title']}: not found in this filing. Does the company report it elsewhere, for example in earnings materials?",
                           "basis": "Missing evidence", "refs": []})
            elif not (it["key"] == "coverage" and fb.get("interest_expense")):     # a combined interest line gets its own question
                qs.append({"q": f"{it['title']}: could not be computed from the figures read in this filing. Which inputs are missing, and are they in the notes?",
                           "basis": "Missing evidence", "refs": []})
    for s in rep.get("signals") or []:
        if s.get("followup"):
            qs.append({"q": s["followup"], "basis": f"Observation: {s['headline']}", "refs": s.get("inputs") or []})
    for i in rep["coverage"]["issues"]:
        if i["type"] == "conflict":
            qs.append({"q": f"Which figure is right for {i['metric']} ({i['period']})? The filing prints two different values.", "basis": "Unresolved conflict", "refs": []})
        if i["type"] == "unreadable_statement":
            qs.append({"q": f"What does the {i['statement']} statement show? It could not be read from this file.", "basis": "Unreadable source", "refs": []})
    ie = fb.get("interest_expense")
    if ie and re.search(signals.COMBINED_INTEREST, ie["source"].get("row_label", ""), re.I):
        qs.append({"q": f"How much of \"{ie['source']['row_label']}\" ({ie['display']}) is interest? The income statement prints interest together "
                        "with other items, so interest coverage was not computed.", "basis": "Coverage limit", "refs": [ie["id"]]})
    if not mat and fb.get("total_debt"):
        qs.append({"q": "When does the debt mature? No maturity schedule was read from this file.", "basis": "Missing evidence", "refs": [fb.get("total_debt")["id"]]})
    if "gains_share" in sigs:
        s = sigs["gains_share"]
        qs.append({"q": "What volume and pricing of asset sales does management expect, and how have resale prices moved?", "basis": f"Observation: {s['headline']}", "refs": s["inputs"]})
    if sigs.get("interest_coverage", {}).get("severity") in ("flag", "watch") or sigs.get("debt_to_assets", {}).get("severity") in ("flag", "watch"):
        qs.append({"q": "Which financial covenants apply to the debt, and how much headroom remains?", "basis": "Screened leverage or coverage",
                   "refs": (sigs.get("interest_coverage") or sigs.get("debt_to_assets"))["inputs"]})
    if rep["operating"]["breakdowns"]:
        qs.append({"q": "How profitable is each segment? This report reads revenue by segment but not segment profit.", "basis": "Coverage limit", "refs": []})
    rep["questions"] = qs[:7]

    # sources and coverage
    stm_pages = {}
    for r in rows:
        if r["statement"] in ("income", "balance", "cashflow", "equity") and r.get("scope") not in ("affiliate", "segment", "subsidiary", "supplemental"):
            stm_pages.setdefault(r["statement"], set()).add(r["pdf_page"])
    rep["sources_reviewed"] = {
        "sections": [{"item": s.item, "title": s.title, "page": s.page} for s in doc.sections],
        "statements": {k: sorted(p for p in v if p) for k, v in stm_pages.items()},
        "read": ["Financial statement tables (income statement, balance sheet, cash flow statement, statement of equity where present)",
                 "Notes tables for debt maturities and segment revenue", "Item 1A risk factor headings", "Item 3 legal proceedings",
                 "Item 7 management's discussion for narrative figures"],
        "not_read": ["Images and charts (no OCR)", "Exhibits and incorporated documents", "Narrative figures outside management's discussion",
                     "Segment profit, which is not extracted", "Market data, valuation and forecasts (out of scope)"],
        "failed": [i["message"] for i in rep["coverage"]["issues"] if i["type"] == "unreadable_statement"],
    }
