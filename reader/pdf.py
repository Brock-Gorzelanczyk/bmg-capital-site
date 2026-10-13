"""BMG Capital Company Report as a PDF, rendered from the report dict.

The on-screen report and this PDF read the same validated data, so they
show the same figures, statuses and sources.
"""
from __future__ import annotations

import io
from datetime import datetime
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.platypus import (KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

GREEN = colors.HexColor("#0f7d47")
LIME = colors.HexColor("#2fdc74")
INK = colors.HexColor("#141414")
MUTED = colors.HexColor("#5f646b")
RULE = colors.HexColor("#d9d9d2")
SOFT = colors.HexColor("#f4f6f4")
AMBER = colors.HexColor("#9a5b00")
RED = colors.HexColor("#b42318")

S = {
    "h1": ParagraphStyle("h1", fontName="Times-Bold", fontSize=24, leading=28, textColor=INK, spaceAfter=2),
    "h2": ParagraphStyle("h2", fontName="Times-Bold", fontSize=15, leading=19, textColor=INK, spaceBefore=12, spaceAfter=5, keepWithNext=1),
    "h3": ParagraphStyle("h3", fontName="Helvetica-Bold", fontSize=10, leading=13, textColor=INK, spaceBefore=6, spaceAfter=3, keepWithNext=1),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=INK, spaceAfter=4),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=8, leading=10.5, textColor=MUTED, spaceAfter=3),
    "cell": ParagraphStyle("cell", fontName="Helvetica", fontSize=8.3, leading=10.5, textColor=INK),
    "cellb": ParagraphStyle("cellb", fontName="Helvetica-Bold", fontSize=8.3, leading=10.5, textColor=INK),
    "celln": ParagraphStyle("celln", fontName="Helvetica", fontSize=8.3, leading=10.5, textColor=INK, alignment=TA_RIGHT),
    "cellm": ParagraphStyle("cellm", fontName="Helvetica", fontSize=7.4, leading=9.4, textColor=MUTED),
    "th": ParagraphStyle("th", fontName="Helvetica-Bold", fontSize=7.6, leading=9.5, textColor=INK),
    "thn": ParagraphStyle("thn", fontName="Helvetica-Bold", fontSize=7.6, leading=9.5, textColor=INK, alignment=TA_RIGHT),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=9.5, leading=13.5, textColor=INK, leftIndent=12, bulletIndent=0, spaceAfter=4),
}


def P(text, style="body"):
    return Paragraph(text, S[style])


def e(x) -> str:
    return escape(str(x if x is not None else ""))


_KIND = {"pdf": True}


def _src(f: dict) -> str:
    s = (f or {}).get("source") or {}
    if s.get("pdf_page"):
        same = str(s.get("printed_page")) == str(s["pdf_page"])
        return (f"PDF p. {s['pdf_page']}" if _KIND["pdf"] else f"p. {s['pdf_page']}") + (f" (printed {s['printed_page']})" if s.get("printed_page") and not same else "")
    if s.get("anchor"):
        return f"XBRL {s['anchor']}"
    return ""


class _Numbered(rl_canvas.Canvas):
    """Header, footer and 'Page N of M' on every page."""

    def __init__(self, *a, meta=None, **k):
        super().__init__(*a, **k)
        self._saved = []
        self._meta = meta or {}

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        n = len(self._saved)
        for st in self._saved:
            self.__dict__.update(st)
            self._decorate(n)
            super().showPage()
        super().save()

    def _logo(self, x, y, size=11):
        self.setFont("Helvetica-Bold", size)
        self.setFillColor(INK)
        self.drawString(x, y, "BMG Capital")
        w = self.stringWidth("BMG Capital", "Helvetica-Bold", size)
        self.setStrokeColor(LIME)
        self.setLineWidth(size * 0.16)
        self.setLineCap(1)
        self.setLineJoin(1)
        sx, sy, k = x + w + 3, y - 1, size / 11.0
        p = self.beginPath()
        p.moveTo(sx, sy)
        p.lineTo(sx + 4 * k, sy + 4 * k)
        p.lineTo(sx + 6.5 * k, sy + 2 * k)
        p.lineTo(sx + 10 * k, sy + 7 * k)
        self.drawPath(p, stroke=1, fill=0)
        self.setFillColor(LIME)
        self.circle(sx + 10.4 * k, sy + 7.6 * k, 1.5 * k, stroke=0, fill=1)

    def _decorate(self, total):
        w, h = letter
        pn = self._pageNumber
        self.saveState()
        if pn == 1:
            self._logo(0.75 * inch + 4, h - 0.8 * inch - 17, 15)
        if pn > 1:
            self._logo(0.75 * inch, h - 0.5 * inch, 9)
            self.setFont("Helvetica", 7.5)
            self.setFillColor(MUTED)
            self.drawRightString(w - 0.75 * inch, h - 0.5 * inch, self._meta.get("running", ""))
            self.setStrokeColor(RULE)
            self.setLineWidth(0.5)
            self.line(0.75 * inch, h - 0.58 * inch, w - 0.75 * inch, h - 0.58 * inch)
        self.setStrokeColor(RULE)
        self.setLineWidth(0.5)
        self.line(0.75 * inch, 0.62 * inch, w - 0.75 * inch, 0.62 * inch)
        self.setFont("Helvetica", 7)
        self.setFillColor(MUTED)
        self.drawString(0.75 * inch, 0.45 * inch, "Automatically generated by the BMG Capital reader from the cited filing. Not reviewed by an analyst. Not investment advice.")
        self.drawRightString(w - 0.75 * inch, 0.45 * inch, f"Page {pn} of {total}")
        self.restoreState()


def _table(rows, widths, numeric_cols=(), header=True, zebra=True):
    t = Table(rows, colWidths=widths, repeatRows=1 if header else 0)
    st = [("VALIGN", (0, 0), (-1, -1), "TOP"), ("LEFTPADDING", (0, 0), (-1, -1), 4), ("RIGHTPADDING", (0, 0), (-1, -1), 4),
          ("TOPPADDING", (0, 0), (-1, -1), 3), ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
          ("LINEBELOW", (0, 0), (-1, 0), 1, INK) if header else ("LINEBELOW", (0, 0), (0, 0), 0, colors.white),
          ("LINEBELOW", (0, 1), (-1, -1), 0.4, RULE)]
    t.setStyle(TableStyle(st))
    return t


SEV_COL = {"flag": "#b42318", "watch": "#9a5b00", "observation": "#3b5bdb"}
SEV_LAB = {"flag": "FLAG", "watch": "WATCH", "observation": "OBSERVATION"}


def render(rep: dict) -> bytes:
    co, src, cov = rep["company"], rep["source"], rep["coverage"]
    _KIND["pdf"] = src.get("kind") == "pdf"
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    get = lambda i: (facts.get(i) or calcs.get(i)) if i else None
    name = co.get("name") or src["name"]
    tick = co.get("ticker") or co.get("ticker_hint") or ""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.8 * inch, bottomMargin=0.85 * inch,
                            title=f"BMG Capital Company Report: {name}", author="BMG Capital", subject="Company Report")
    W = letter[0] - 1.5 * inch
    story = []
    used = set()

    def pages_of(fid, depth=0):
        x = get(fid)
        if not x or depth > 4:
            return set()
        used.add(fid)
        if _src(x):
            return {_src(x)}
        out = set()
        for i in x.get("inputs", []):
            out |= pages_of(i, depth + 1)
        return out

    def cite(ids, extra=None):
        pgs = sorted(set().union(*[pages_of(i) for i in ids]) if ids else set())
        if extra and _src(extra):
            pgs = sorted(set(pgs) | {_src(extra)})
        return f' <font size="7" color="#5f646b">[{e("; ".join(pgs))}]</font>' if pgs else ""

    # ---------------- header
    story.append(Table([[P('<font size="15"> </font>', "body"), P('<font color="#0f7d47" name="Helvetica-Bold" size="8">COMPANY REPORT</font>', "celln")]],
                       colWidths=[W * 0.6, W * 0.4], style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.5, INK), ("BOTTOMPADDING", (0, 0), (-1, 0), 6), ("VALIGN", (0, 0), (-1, -1), "BOTTOM")])))
    story.append(Spacer(1, 10))
    story.append(P(e(name) + (f' <font color="#0f7d47" size="13" name="Helvetica-Bold">{e(tick)}</font>' if tick else ""), "h1"))
    story.append(P(" &middot; ".join(e(x) for x in [co.get("form"), co.get("period"), co.get("exchange")] if x), "body"))
    gen = datetime.fromisoformat(rep["generated_at"]).strftime("%B %d, %Y %H:%M UTC")
    story.append(P(f"Source filing: <b>{e(src['name'])}</b> ({src['pages']} pages, sha256 {e(src['sha256'][:12])}). Generated {e(gen)}. "
                   f"Coverage: <b>{e(cov['status'])}</b>. Screens: {e(rep.get('screens_version') or 'n/a')}. {e(rep['review'])}", "small"))
    if co.get("amendment"):
        story.append(P("<b>This is an amended filing (/A).</b> Figures reflect the amendment as filed.", "body"))

    def tile(mkey, label):
        row = next((r for r in rep["financials"]["performance"] + rep["position"]["rows"] + rep["financials"]["calculated"] if r["metric"] == mkey), None)
        f = get(row.get("current")) if row else None
        val = f["display"] if f and f.get("display") else "n/a"
        chg = (row or {}).get("change", {}).get("display", "")
        per = ((f or {}).get("period", {}).get("short") or (f or {}).get("period", {}).get("label", "")) if f else "Not found"
        return [P(f'<font size="7" color="#5f646b">{e(label.upper())}</font>', "cell"), P(f'<font name="Helvetica-Bold" size="14">{e(val)}</font>', "cell"),
                P(f'<font size="7.5" color="#5f646b">{e(chg)}</font>', "cell"), P(f'<font size="6.8" color="#5f646b">{e(per)}</font>', "cell")]
    tiles = [tile("revenue", "Revenue"), tile("net_income", "Net income"), tile("eps_diluted", "Diluted EPS"), tile("cfo", "Operating cash flow")]
    tt = Table([[Table([[x] for x in t], colWidths=[W / 4 - 10]) for t in tiles]], colWidths=[W / 4] * 4)
    tt.setStyle(TableStyle([("BOX", (i, 0), (i, 0), 0.6, RULE) for i in range(4)] + [("VALIGN", (0, 0), (-1, -1), "TOP")]))
    story += [Spacer(1, 8), tt]

    # ---------------- 1. business overview
    bz = rep.get("business") or {}
    story.append(P("Business overview", "h2"))
    d = bz.get("description") or {}
    story.append(P((e(d["text"]) + cite([], d)) if d.get("text") else f"<i>{e(d.get('reason'))}</i>"))
    sg = bz.get("segments") or {}
    if sg.get("text"):
        story.append(P(f"<b>Segments.</b> {e(sg['text'])}" + cite([], sg)))
    if bz.get("revenue_mix"):
        story.append(P(f"<b>Revenue mix.</b> {e(bz['revenue_mix']['text'])}" + cite([], bz["revenue_mix"])))
    story.append(P(f"Read as <b>{e(bz.get('profile'))}</b>." + (f" Operating measures found: {e('; '.join(bz['drivers']))}." if bz.get("drivers") else ""), "small"))
    if rep["overview"].get("market_value_note"):
        mv = rep["overview"]["market_value_note"]
        story.append(P(e(mv["text"]) + cite([mv["fact"]]), "small"))

    # ---------------- 2. executive findings
    story.append(P("Executive findings", "h2"))
    if rep.get("exec_findings"):
        for x in rep["exec_findings"]:
            story.append(KeepTogether([
                P(f'<font name="Helvetica-Bold" size="7" color="{SEV_COL[x["severity"]]}">{SEV_LAB[x["severity"]]}</font>  <b>{e(x["headline"])}</b>' + cite(x["refs"], x if x.get("source") else None), "body"),
                P(e(x["text"]), "cell"), P(f"<i>Why it may matter (BMG interpretation): {e(x['why'])}</i>", "cellm"), Spacer(1, 5)]))
        story.append(P("Flag and Watch mean a value is beyond a stated screen for this kind of business; Observation means no screen applies. "
                       "None of these is a rating or a recommendation.", "small"))
    else:
        story.append(P("<i>No findings could be supported by the extracted figures.</i>"))

    # ---------------- 3. financial performance
    story.append(P("Financial performance", "h2"))
    p3 = (rep.get("performance3") or {}).get("rows") or []
    if p3:
        cols = [get(i)["period"]["short"] or get(i)["period"]["label"] for i in p3[0]["ids"]]
        data = [[P("Metric", "th")] + [P(e(c), "thn") for c in cols] + [P("Change (latest)", "thn"), P("Source", "th")]]
        for r in p3:
            vals = [get(i) for i in r["ids"]]
            ch = ""
            if len(vals) > 1 and vals[1]["value"] not in (None, 0):
                if vals[1]["value"] > 0 and vals[0]["value"] >= 0:
                    ch = f"{(vals[0]['value'] / vals[1]['value'] - 1) * 100:+.1f}%"
                else:
                    ch = "n.m. (negative base)"
            data.append([P(e(r["name"]), "cell")] + [P(e(v["display"]), "celln") for v in vals] + [P("", "celln")] * (len(cols) - len(vals)) +
                        [P(e(ch), "celln"), P(e("; ".join(sorted(pages_of(r["ids"][0])))), "cellm")])
        n = len(cols)
        story.append(_table(data, [W * 0.3] + [W * (0.42 / n)] * n + [W * 0.12, W * 0.16]))
        story.append(P("Growth is shown only between periods of the same length; a zero or negative base shows n.m. "
                       + e((rep.get("performance3") or {}).get("note") or ""), "small"))
    story.append(P("Margins and other calculations", "h3"))
    data = [[P("Measure", "th"), P("Current", "thn"), P("Prior", "thn"), P("Change", "thn"), P("Formula and source", "th")]]
    for r in rep["financials"]["calculated"]:
        c, pr = get(r.get("current")), get(r.get("prior"))
        if not c:
            data.append([P(e(r["name"]), "cell"), P("", "celln"), P("", "celln"), P("", "celln"), P(f"<i>{e(r.get('reason') or 'Not available.')}</i>", "cellm")])
            continue
        data.append([P(e(r["name"]), "cell"), P(e(c["display"]), "celln"), P(e((pr or {}).get("display", "")), "celln"), P(e(r["change"].get("display", "")), "celln"),
                     P(e(r["formula"]) + "; " + e("; ".join(sorted(pages_of(c["id"])))), "cellm")])
    story.append(_table(data, [W * 0.26, W * 0.12, W * 0.12, W * 0.12, W * 0.38]))
    for b in rep["operating"]["breakdowns"]:
        data = [[P(e(b["title"]), "th"), P("Value", "thn"), P("Share", "thn"), P("Prior", "thn"), P("Change", "thn"), P("Source", "th")]]
        for i in b["items"]:
            data.append([P(e(i["name"]), "cell"), P(e(i["display"]), "celln"), P(f"{i['share']:.0f}%", "celln"), P(e(i.get("prior_display", "")), "celln"),
                         P(f"{i['change_pct']:+.1f}%" if i.get("change_pct") is not None else "", "celln"), P(e(_src(i)), "cellm")])
        data.append([P("<b>Total</b>", "cell"), P(f"<b>{e(b['total_display'])}</b>", "celln"), P("", "cell"), P("", "cell"), P("", "cell"), P("", "cell")])
        story.append(KeepTogether([P(e(b["period"]), "small"), _table(data, [W * 0.32, W * 0.13, W * 0.09, W * 0.13, W * 0.1, W * 0.23]), P(e(b["check"]), "small")]))

    # ---------------- 4. earnings and cash generation
    ec = rep.get("earnings_cash") or {}
    story.append(P("Earnings and cash generation", "h2"))
    ni, cf = get(ec.get("net_income")), get(ec.get("cfo"))
    if ni and cf:
        story.append(P(f"Net income of {e(ni['display'])} ({e(ni['name'].lower())}) against operating cash flow of {e(cf['display'])}, {e(cf['period']['label'])}."
                       + cite([ni["id"], cf["id"]])))
        if ec.get("items"):
            data = [[P("Reconciling line (cash flow statement)", "th"), P("Amount", "thn"), P("Source", "th")]]
            for i in ec["items"]:
                f = get(i)
                if f:
                    data.append([P(e(f["name"]), "cell"), P(e(f["display"]), "celln"), P(e("; ".join(sorted(pages_of(i)))), "cellm")])
            story.append(_table(data, [W * 0.6, W * 0.15, W * 0.25]))
            story.append(P(e(ec["note"]), "small"))
        s = next((x for x in rep.get("signals") or [] if x["id"] == "cash_conversion"), None)
        if s:
            story.append(P(e(s["text"]) + " " + e(s["rule"]), "cellm"))
    else:
        story.append(P("<i>Net income or operating cash flow was not found, so earnings and cash cannot be compared.</i>"))

    # ---------------- 5. position and capital allocation
    cap = rep.get("capital") or {}
    story.append(P("Financial position and capital allocation", "h2"))
    def line(label, fid, note=""):
        f = get(fid)
        return [P(e(label), "cell"), P(e(f["display"]) if f else "", "celln"),
                P((e(f["period"]["label"]) + "; " + e("; ".join(sorted(pages_of(fid)))) + (f"<br/>{e(note)}" if note else "")) if f else "<i>Not found in the reviewed filing.</i>", "cellm")]
    data = [[P("Item", "th"), P("Value", "thn"), P("Period, source and definition", "th")],
            line("Cash and cash equivalents", cap.get("cash")), line("Total debt", cap.get("debt"), cap.get("debt_definition") or ""),
            line("Total assets", (next((r for r in rep["position"]["rows"] if r["metric"] == "total_assets"), {}) or {}).get("current")),
            line("Operating cash flow", (next((r for r in rep["position"]["rows"] if r["metric"] == "cfo"), {}) or {}).get("current")),
            line("Capital expenditures", cap.get("capex"))]
    for i in cap.get("capex_lines") or []:
        data.append(line("   of which: " + (get(i) or {}).get("name", ""), i))
    data += [line("Dividends paid", cap.get("dividends")), line("Share repurchases", cap.get("buybacks"))]
    story.append(_table(data, [W * 0.3, W * 0.15, W * 0.55]))
    for sid in (cap.get("fcf_signal"), cap.get("payout_signal")):
        s = next((x for x in rep.get("signals") or [] if x["id"] == sid), None)
        if s:
            story.append(P(f"<b>{e(s['headline'])}.</b> {e(s['text'])}" + cite(s["inputs"]), "cell"))
    if cap.get("maturities"):
        m = cap["maturities"]
        story.append(P(f"<b>Scheduled debt maturities (principal).</b> {e(m['text'])}" + cite([], m), "cell"))
    story.append(P("Equity and leverage", "h3"))
    data = [[P("Item", "th"), P("Value", "thn"), P("Period and source", "th")]]
    for r in rep["position"]["rows"]:
        if r["metric"] in ("total_liabilities", "equity"):
            data.append(line(r["name"], r.get("current"), (get(r.get("current")) or {}).get("definition", "")))
    for m in ("equity_total", "equity_nci"):
        f = next((x for x in facts.values() if x["metric"] == m and x["status"] != "Unavailable"), None)
        if f:
            data.append(line(f["name"], f["id"]))
    story.append(_table(data, [W * 0.3, W * 0.15, W * 0.55]))

    # ---------------- 6. business-specific metrics
    op = rep["operating"]
    story.append(P(f"Business-specific operating metrics ({e(op['profile'])})", "h2"))
    data = [[P("Metric", "th"), P("Value", "thn"), P("Period, scope, source and definition", "th")]]
    for k in op["kpis"]:
        if k.get("display"):
            meta = f"{e(k.get('period', ''))}" + (f"; scope: {e(k['scope'])}" if k.get("scope") else "") + f"; {e(_src(k))}"
            if k.get("definition"):
                meta += f"<br/>Company definition: {e(k['definition']['text'][:240])}"
            data.append([P(e(k["name"]), "cell"), P(e(k["display"]), "celln"), P(meta, "cellm")])
        else:
            data.append([P(e(k["name"]), "cell"), P("", "celln"), P(f"<i>{e(k.get('reason', 'Not found in the reviewed filing.'))}</i>", "cellm")])
    if len(data) > 1:
        story.append(_table(data, [W * 0.32, W * 0.16, W * 0.52]))
        story.append(P("Similarly named measures from different companies are defined by each company and are not directly comparable.", "small"))
    else:
        story.append(P("<i>No business-specific metrics were found in the reviewed filing.</i>"))

    # ---------------- 7. risks and watch items
    rk = rep["risks"]
    story.append(P("Risks and watch items", "h2"))
    if rk["watch"]:
        story.append(P("Disclosed items (from the notes, Item 3 and management's discussion)", "h3"))
        for w in rk["watch"]:
            story.append(KeepTogether([P(f"<b>{e(w['title'])}</b> <font size='7' color='#5f646b'>({e(w.get('basis', ''))}; {e(_src(w))})</font>", "body"),
                                       P(e(w["text"]), "cell"), P(f"<i>Why it may matter (BMG interpretation): {e(w['why_it_may_matter'])}</i>", "cellm"), Spacer(1, 4)]))
    if rk["items"]:
        story.append(P("Risk factors (possible events the company describes, not disclosed events)", "h3"))
        story.append(P(e(rk["note"]), "small"))
        for r in sorted(rk["items"], key=lambda r: r.get("specificity") != "Company-specific"):
            block = [P(f"<b>{e(r['category'])}</b> <font size='7' color='#5f646b'>({e(r['specificity'])}; {e(_src(r))})</font>", "body"),
                     P(f"&ldquo;{e(r['title'])}&rdquo;", "cell")]
            if r.get("magnitude"):
                block.append(P(f"Disclosed figure: {e(r['magnitude'])}", "cellm"))
            block.append(Spacer(1, 4))
            story.append(KeepTogether(block))
    elif rk.get("note"):
        story.append(P(e(rk["note"]), "small"))

    # ---------------- 8. questions
    if rep.get("questions"):
        story.append(P("Questions for further research", "h2"))
        for q in rep["questions"]:
            story.append(Paragraph(f"{e(q['q'])} <font size='7' color='#5f646b'>({e(q['basis'])})</font>" + cite(q.get("refs") or []), S["bullet"], bulletText="•"))
        story.append(P("One filing cannot answer everything; these are where this report stops.", "small"))

    # ---------------- 9. signals table
    sigs = rep.get("signals") or []
    if sigs:
        story.append(P("All screens and observations", "h2"))
        data = [[P("", "th"), P("Measure", "th"), P("Definition", "th"), P("Screen and source", "th")]]
        for s in sigs:
            dfn = s.get("definition") or {}
            data.append([P(f'<font name="Helvetica-Bold" size="6.5" color="{SEV_COL[s["severity"]]}">{SEV_LAB[s["severity"]]}</font>', "cell"),
                         P(f"<b>{e(s['headline'])}</b>", "cell"),
                         P(e(dfn.get("formula", "")) + (f"<br/>Numerator: {e(dfn.get('numerator'))}" if dfn.get("numerator") else "") +
                           (f"<br/>Denominator: {e(dfn.get('denominator'))}" if dfn.get("denominator") else ""), "cellm"),
                         P(e(s["rule"]) + cite(s.get("inputs") or [], s if s.get("source") else None), "cellm")])
        story.append(_table(data, [W * 0.13, W * 0.25, W * 0.34, W * 0.28]))

    # ---------------- 10. sources, coverage, method
    story.append(PageBreak())
    story.append(P("Sources and coverage", "h2"))
    sr = rep.get("sources_reviewed") or {}
    stm = cov["statements_found"]
    pg = lambda k: ", ".join(f"p.{x}" for x in (sr.get("statements") or {}).get(k, []))
    story.append(P(f"Statements read: income statement {pg('income') or 'not found'}; balance sheet {pg('balance') or 'not found'}; "
                   f"cash flow statement {pg('cashflow') or 'not found'}; statement of equity {pg('equity') or 'not found'}. "
                   f"Table rows parsed: {cov['tables_parsed']}. Arithmetic checks: {cov['checks_run']} ({cov['checks_failed']} failed)."))
    if sr.get("sections"):
        story.append(P("Sections located: " + e("; ".join(f"Item {s['item']} {s['title']} (p.{s['page']})" for s in sr["sections"])) + ".", "small"))
    story.append(P("Read: " + e("; ".join(sr.get("read", []))) + ".", "small"))
    story.append(P("Not read or out of scope: " + e("; ".join(sr.get("not_read", []))) + ".", "small"))
    for x in sr.get("failed", []):
        story.append(Paragraph("<b>Could not read:</b> " + e(x), S["bullet"], bulletText="•"))
    for w in cov["warnings"]:
        story.append(Paragraph(e(w["message"]), S["bullet"], bulletText="•"))
    for i in cov["issues"]:
        if i["type"] != "unreadable_statement":
            story.append(Paragraph("Unresolved: " + e(i["message"]), S["bullet"], bulletText="•"))
    story.append(P("Searched for: " + e("; ".join(cov["searched_for"])) + ". Not finding an item is not evidence that it does not exist.", "small"))
    for m in rep["methodology"]:
        story.append(Paragraph(e(m), S["bullet"], bulletText="•"))
    if rep["checks"]:
        story.append(P("Arithmetic checks", "h3"))
        data = [[P("Check", "th"), P("Period", "th"), P("Result", "th"), P("Detail", "th")]]
        for c in rep["checks"]:
            data.append([P(e(c["name"]), "cell"), P(e(c["period"]), "cellm"), P("Passed" if c["passed"] else "<font color='#b42318'>Failed</font>", "cell"), P(e(c["detail"]), "cellm")])
        story.append(_table(data, [W * 0.38, W * 0.27, W * 0.1, W * 0.25]))

    # ---------------- appendix: evidence for every figure used above
    for row in rep["financials"]["performance"] + rep["financials"]["calculated"] + rep["position"]["rows"]:
        pages_of(row.get("current")); pages_of(row.get("prior"))
    story.append(P("Evidence appendix", "h2"))
    data = [[P("Figure", "th"), P("Value", "thn"), P("Status", "th"), P("Location, scope and excerpt", "th")]]
    for f in facts.values():
        if f["status"] == "Unavailable" or f["id"] not in used:
            continue
        s = f["source"]
        loc = (f"{e(_src(f))}; {e(s.get('table_title') or s.get('section') or '')}; row \"{e(s.get('row_label', ''))}\", column {e(s.get('column', ''))}; "
               f"scope {e(f.get('scope', ''))}")
        data.append([P(f"{e(f['name'])}<br/><font size='6.8' color='#5f646b'>{e(f['period']['label'])}</font>", "cell"), P(e(f["display"]), "celln"),
                     P(e(f["status"]), "cellm"), P(f"{loc}<br/>&ldquo;{e((s.get('excerpt') or '')[:200])}&rdquo;", "cellm")])
    for c in calcs.values():
        if c.get("status") != "Calculation checked" or c["id"] not in used:
            continue
        data.append([P(f"{e(c['name'])}<br/><font size='6.8' color='#5f646b'>{e(c.get('period', {}).get('label', ''))}</font>", "cell"), P(e(c["display"]), "celln"),
                     P("Calculation checked", "cellm"), P("Formula: " + e(c["formula"]) + "<br/>Inputs: " + e(", ".join(
                         f"{(facts.get(i) or calcs.get(i) or {}).get('name', i)} {(facts.get(i) or calcs.get(i) or {}).get('display', '')}" for i in c["inputs"])), "cellm")])
    story.append(_table(data, [W * 0.22, W * 0.12, W * 0.13, W * 0.53]))

    running = f"{name}{' (' + tick + ')' if tick else ''} | {co.get('form') or ''} {co.get('period') or ''}"
    doc.build(story, canvasmaker=lambda *a, **k: _Numbered(*a, meta={"running": running}, **k))
    return buf.getvalue()


def render_compare(cmp: dict) -> bytes:
    """Side-by-side comparison of two company reports."""
    a, b = cmp["a"], cmp["b"]
    na, nb = a.get("ticker") or a["name"], b.get("ticker") or b["name"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, leftMargin=0.75 * inch, rightMargin=0.75 * inch, topMargin=0.8 * inch, bottomMargin=0.85 * inch,
                            title=f"BMG Capital Comparison: {na} vs {nb}", author="BMG Capital", subject="Company Comparison")
    W = letter[0] - 1.5 * inch
    story = [Table([[P('<font size="15"> </font>', "body"), P('<font color="#0f7d47" name="Helvetica-Bold" size="8">COMPANY COMPARISON</font>', "celln")]],
                   colWidths=[W * 0.6, W * 0.4], style=TableStyle([("LINEBELOW", (0, 0), (-1, 0), 1.5, INK), ("BOTTOMPADDING", (0, 0), (-1, 0), 6)])),
             Spacer(1, 10), P(f"{e(a['name'])} vs {e(b['name'])}", "h1"),
             P(f"{e(na)}: {e(a.get('form'))}, {e(a.get('period'))} ({e(a.get('profile'))}). {e(nb)}: {e(b.get('form'))}, {e(b.get('period'))} ({e(b.get('profile'))}).", "small"),
             P("Automatically generated by the BMG Capital reader from the two cited filings. Not reviewed by an analyst. Not investment advice.", "small")]
    for c in cmp["caveats"]:
        story.append(P(f"<b>Note.</b> {e(c)}", "body"))
    story.append(P("Comparability checks", "h2"))
    story.append(_table([[P("Check", "th"), P("Result", "th"), P("Detail", "th")]] +
                        [[P(e(c["check"]), "cell"), P(e(c["result"]), "cell"), P(e(c["detail"]), "cellm")] for c in cmp.get("checks") or []],
                        [W * 0.22, W * 0.22, W * 0.56]))
    story.append(P("No overall winner is declared. A row names the company with the more favourable value only when the periods, scope and "
                   "definitions line up; otherwise it says why the values are not comparable.", "small"))
    group = None
    data = []
    for r in cmp["rows"]:
        if r["group"] != group:
            if data:
                story.append(_table(data, [W * 0.34, W * 0.16, W * 0.16, W * 0.34], numeric_cols=(1, 2)))
            group = r["group"]
            story.append(P(e(group), "h3"))
            data = [[P("Measure", "th"), P(e(na), "thn"), P(e(nb), "thn"), P("Comparability and sources", "th")]]
        lead = {"a": na, "b": nb}.get(r["leader"], "")
        bold = lambda side: "<b>%s</b>" if r["leader"] == side else "%s"
        pg = lambda s: ", ".join(f"p.{x['pdf_page']}" for x in s if x.get("pdf_page"))
        meta = (f"More favourable: <b>{e(lead)}</b> ({'higher' if r['better'] == 'higher' else 'lower'} is better)<br/>" if lead else
                f"<i>Not comparable: {e(r['not_comparable_reason'])}</i><br/>" if r.get("not_comparable_reason") else
                ("Difference not material<br/>" if r["better"] else "Size or context, not ranked<br/>")) + f"{e(na)} {e(pg(r['a']['sources']))}; {e(nb)} {e(pg(r['b']['sources']))}"
        if r.get("note"):
            meta += f"<br/>{e(r['note'])}"
        data.append([P(e(r["name"]), "cell"), P(bold("a") % e(r["a"]["display"] or "n/a"), "celln"), P(bold("b") % e(r["b"]["display"] or "n/a"), "celln"), P(meta, "cellm")])
    if data:
        story.append(_table(data, [W * 0.34, W * 0.16, W * 0.16, W * 0.34], numeric_cols=(1, 2)))
    story.append(P("Method", "h2"))
    story.append(P(e(cmp["method"]), "small"))
    running = f"{na} vs {nb} | Company comparison"
    doc.build(story, canvasmaker=lambda *x, **k: _Numbered(*x, meta={"running": running}, **k))
    return buf.getvalue()
