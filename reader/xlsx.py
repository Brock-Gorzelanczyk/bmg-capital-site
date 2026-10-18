"""The report as an Excel workbook: every number a real number, and every number linked to where it came from.

A figure from the document links to the reader at its page (the saved read opens and the page scrolls to the
printed value) and carries a comment with the page and the line it was read from. A figure from SEC XBRL links to
the filing on SEC EDGAR. Calculations show their formula. The Sources sheet lists every figure with its location.
"""
from __future__ import annotations

import io
import re
from typing import Optional
from urllib.parse import quote

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

BOLD = Font(bold=True)
HEAD = PatternFill("solid", fgColor="E8F3EC")
LINK = Font(color="0F7D47", underline="single")
NUM = "#,##0;(#,##0)"
NUM2 = "#,##0.00;(#,##0.00)"
PCT = "0.0%"


def _scale(unit: str) -> str:
    return NUM2 if unit in ("USD/share", "USD/shares") else NUM


class Book:
    def __init__(self, base: str, sha: str, ticker: str = ""):
        self.wb = Workbook()
        self.base, self.sha, self.ticker = base.rstrip("/"), sha, ticker
        self.sources = []
        self.wb.remove(self.wb.active)

    def link(self, page: Optional[int], raw: Optional[str]) -> Optional[str]:
        if not page or not self.sha:
            return None
        frag = f"src={self.sha}&p={page}" + (f"&v={quote(str(raw))}" if raw else "") + (f"&t={quote(self.ticker)}" if self.ticker else "")
        return f"{self.base}/desk/read/#{frag}"

    def sheet(self, title: str, header: list, widths: Optional[list] = None):
        ws = self.wb.create_sheet(title[:31])
        ws.append(header)
        for c in ws[1]:
            c.font, c.fill = BOLD, HEAD
        ws.freeze_panes = "A2"
        for i, w in enumerate(widths or [], 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        return ws

    def cite(self, cell, value, unit: str, page=None, raw=None, quote_=None, url=None, label: str = "", period: str = "", formula: str = ""):
        """Write a cited number into a cell: number format, link, comment; and list it on the Sources sheet."""
        if value is None:
            cell.value = "n/a"
            return
        if unit in ("USD", "shares"):
            cell.value = float(value)
            cell.number_format = NUM
        elif unit in ("USD/share", "USD/shares"):
            cell.value = float(value)
            cell.number_format = NUM2
        elif unit in ("pct", "ratio_pct"):
            cell.value = float(value)
            cell.number_format = PCT
        else:
            cell.value = value
        target = url or self.link(page, raw)
        if target:
            cell.hyperlink = target
            cell.font = LINK
        note = []
        if page:
            note.append(f"p.{page}")
        if quote_:
            note.append(f"\"{str(quote_)[:300]}\"")
        if formula:
            note.append(f"Formula: {formula}")
        if url and not page:
            note.append(url)
        if note:
            cell.comment = Comment(" ".join(note), "BMG Capital")
        self.sources.append([label, period, value, unit, page or "", quote_ or "", formula, target or ""])

    def save(self) -> bytes:
        ws = self.sheet("Sources", ["Figure", "Period", "Value", "Unit", "Page", "Line it was read from", "Formula", "Link"], [34, 26, 18, 10, 8, 80, 40, 60])
        for row in self.sources:
            ws.append(row)
            c = ws.cell(row=ws.max_row, column=8)
            if row[7]:
                c.hyperlink, c.font = row[7], LINK
        bio = io.BytesIO()
        self.wb.save(bio)
        return bio.getvalue()


def build(result: dict, base: str, sha: str, ticker: str = "", history: Optional[dict] = None, notes: Optional[list] = None) -> bytes:
    rep = result.get("report") or {}
    co = rep.get("company") or {}
    F, C = (rep.get("evidence") or {}).get("facts") or {}, (rep.get("evidence") or {}).get("calcs") or {}
    b = Book(base, sha, ticker)

    ws = b.sheet("Summary", ["BMG Capital report", ""], [34, 90])
    for k, v in (("Company", co.get("name") or co.get("name_hint") or ""), ("Ticker", co.get("ticker") or co.get("ticker_hint") or ticker or ""),
                 ("Form", co.get("form") or ""), ("Period", co.get("period") or ""), ("Document", (rep.get("source") or {}).get("name") or result.get("title") or ""),
                 ("Coverage", (rep.get("coverage") or {}).get("status") or ""), ("Generated", rep.get("generated_at") or ""),
                 ("How to read", "Every number links to its page in the document (opens in the reader on this computer) and its comment quotes the line it was read from. "
                                 "SEC XBRL figures link to the filing on EDGAR. Automatically generated; not reviewed by an analyst; not investment advice.")):
        ws.append([k, v])
    ws.column_dimensions["B"].width = 90
    for r in ws.iter_rows(min_row=2):
        r[0].font = BOLD
        r[1].alignment = Alignment(wrap_text=True, vertical="top")

    # statements: performance and position rows, current and prior
    ws = b.sheet("Statements", ["Line", "Current", "Period", "Prior", "Prior period", "Change", "Status"], [36, 18, 30, 18, 30, 14, 16])
    fin = rep.get("financials") or {}
    for row in (fin.get("performance") or []) + ((rep.get("position") or {}).get("rows") or []):
        cur, pri = F.get(row.get("current")) or C.get(row.get("current")), F.get(row.get("prior")) or C.get(row.get("prior"))
        ws.append([row.get("name"), None, (cur or {}).get("period", {}).get("label", ""), None, (pri or {}).get("period", {}).get("label", ""),
                   (row.get("change") or {}).get("display", ""), row.get("status")])
        r = ws.max_row
        for col, x in ((2, cur), (4, pri)):
            if not x:
                ws.cell(row=r, column=col).value = "n/a"
                continue
            src = x.get("source") or {}
            b.cite(ws.cell(row=r, column=col), x.get("value"), x.get("unit", "USD"), src.get("pdf_page"), src.get("raw"), src.get("excerpt"),
                   label=x.get("name") or row.get("name"), period=(x.get("period") or {}).get("label", ""), formula=x.get("formula") or x.get("definition") or "")

    ws = b.sheet("Calculated", ["Measure", "Current", "Prior", "Formula", "Status"], [34, 16, 16, 60, 20])
    for row in fin.get("calculated") or []:
        cur, pri = C.get(row.get("current")), C.get(row.get("prior"))
        ws.append([row.get("name"), None, None, row.get("formula"), row.get("status")])
        r = ws.max_row
        for col, x in ((2, cur), (3, pri)):
            if not x:
                ws.cell(row=r, column=col).value = "n/a"
                continue
            unit = "pct" if x.get("unit") == "percent" else x.get("unit", "USD")
            val = x["value"] / 100 if unit == "pct" and x.get("value") is not None else x.get("value")
            b.cite(ws.cell(row=r, column=col), val, unit, label=x.get("name"), period=(x.get("period") or {}).get("label", ""), formula=row.get("formula") or "")

    seg = rep.get("segments") or {}
    tables = [("Segment", t) for t in seg.get("segments") or []] + [("Product", t) for t in seg.get("products") or []] + [("Region", t) for t in seg.get("regions") or []]
    if tables:
        ws = b.sheet("Segments", ["Table", "Member"] + [""] * 3, [36, 40, 18, 18, 18])
        for kind, t in tables:
            ws.append([f"{kind}: {t['label']}", ""] + [p["label"] for p in t["periods"]])
            for c in ws[ws.max_row]:
                c.font = BOLD
            for row in t["rows"]:
                ws.append(["", row["name"]] + [None] * len(t["periods"]))
                r = ws.max_row
                for k, v in enumerate(row["values"]):
                    cell = ws.cell(row=r, column=3 + k)
                    if v:
                        b.cite(cell, v["value"], "USD", v.get("page"), v.get("raw"), v.get("quote"), label=f"{row['name']}: {t['label']}", period=t["periods"][k]["label"])
                    else:
                        cell.value = "n/a"
            ws.append([])
    if seg.get("kpis"):
        ws = b.sheet("KPIs", ["Measure", "Now", "Period", "Before", "Period", "Page", "Quote"], [34, 14, 30, 14, 30, 8, 90])
        for k in seg["kpis"]:
            ws.append([k["name"], k["now"], k.get("now_period") or "", k.get("before") or "", k.get("before_period") or "", k.get("page") or "", k.get("quote") or ""])
            link = b.link(k.get("page"), k.get("raw"))
            if link:
                ws.cell(row=ws.max_row, column=2).hyperlink = link
                ws.cell(row=ws.max_row, column=2).font = LINK

    sc = rep.get("scores") or {}
    if sc.get("status") == "computed":
        ws = b.sheet("Scores", ["Score", "Value", "Reading", "Model"], [30, 12, 70, 50])
        for key, name in (("altman", "Altman Z"), ("altman_book", "Altman Z''"), ("piotroski", "Piotroski F"), ("beneish", "Beneish M")):
            x = sc.get(key) or {}
            if x.get("status") in ("computed", "partial"):
                reading = x.get("zone") or (f"{x.get('value')} of {x.get('max')} tests" if key == "piotroski" else x.get("reading") or "")
                ws.append([name, x.get("value"), reading, x.get("model") or x.get("basis") or ""])
            else:
                ws.append([name, "not computed", "; ".join(x.get("missing") or []) or x.get("reason") or "", ""])
        ws.append([])
        ws.append(["Input", "This year", "Last year", "Basis"])
        for c in ws[ws.max_row]:
            c.font = BOLD
        for k, inp in (sc.get("inputs") or {}).items():
            ws.append([inp.get("name"), None, None, inp.get("basis")])
            r = ws.max_row
            for col, p in ((2, inp.get("now")), (3, inp.get("before"))):
                if not p:
                    ws.cell(row=r, column=col).value = "n/a"
                    continue
                unit = "shares" if k == "shares_diluted" else "USD"
                b.cite(ws.cell(row=r, column=col), p.get("value"), unit, p.get("page"), p.get("raw"), p.get("quote"), label=inp.get("name"), period=p.get("period") or p.get("end") or "",
                       formula=p.get("formula") or "")

    if history and history.get("years"):
        yrs = history["years"]
        ws = b.sheet("History (SEC XBRL)", ["Measure"] + [f"{y['label']} ({y['end']})" for y in yrs], [30] + [20] * len(yrs))
        for s in (history.get("series") or []) + (history.get("calcs") or []):
            ws.append([s["label"]] + [None] * len(yrs))
            r = ws.max_row
            for k, p in enumerate(s["points"]):
                cell = ws.cell(row=r, column=2 + k)
                if not p:
                    cell.value = "n/a"
                    continue
                unit = "pct" if s.get("unit") == "ratio" else ("USD/share" if s.get("unit") == "USD/shares" else s.get("unit", "USD"))
                url = f"https://www.sec.gov/Archives/edgar/data/{history['cik']}/{p['accn'].replace('-', '')}/{p['accn']}-index.htm" if p.get("accn") else None
                b.cite(cell, p["value"], unit, url=url, label=s["label"], period=p.get("end", ""),
                       quote_=(f"{p.get('form')} filed {p.get('filed')}, accession {p.get('accn')}, tag {p.get('tag')}" if p.get("accn") else None),
                       formula=p.get("calc") or "")

    if notes:
        ws = b.sheet("Notes", ["Page", "Highlighted text", "Note", "Color", "Saved"], [8, 70, 60, 10, 18])
        for n in notes:
            ws.append([n.get("page") or "", n.get("text") or "", n.get("note") or "", n.get("color") or "", n.get("at") or ""])
            link = b.link(n.get("page"), None)
            if link:
                ws.cell(row=ws.max_row, column=1).hyperlink = link
                ws.cell(row=ws.max_row, column=1).font = LINK
    return b.save()
