"""SEC EDGAR full-text search (efts.sec.gov): the query URL and the hits as filings the reader can open.

The service searches the text of every document filed since 2001 (filings and their exhibits). Each hit is one
document of one filing: company, form, filing date, period, accession number and file name. The search has no
text snippets; the reader opens the document and finds the phrase in it.
"""
from __future__ import annotations

import html
import re
from urllib.parse import urlencode

FTS = "https://efts.sec.gov/LATEST/search-index"


def url(q: str, forms: str = "", start: str = "", end: str = "", sics: str = "", ciks: str = "", offset: int = 0) -> str:
    p = {"q": q}
    if forms:
        p["forms"] = forms
    if start or end:
        p.update({"dateRange": "custom", "startdt": start or "2001-01-01", "enddt": end or start})
    if sics:
        p["sics"] = sics
    if ciks:
        p["ciks"] = ciks
    if offset:
        p["from"] = str(offset)
    return FTS + "?" + urlencode(p)


_NAME = re.compile(r"^(.*?)\s*(?:\(([A-Z0-9.\-, ]+)\))?\s*\(CIK (\d+)\)\s*$")


def parse(data: dict) -> dict:
    hits = ((data or {}).get("hits") or {})
    out = []
    for h in hits.get("hits") or []:
        src = h.get("_source") or {}
        acc, _, doc = (h.get("_id") or "").partition(":")
        names = [html.unescape(n) for n in src.get("display_names") or []]
        m = _NAME.match(names[0]) if names else None
        ciks = src.get("ciks") or []
        cik = int(ciks[0]) if ciks else (int(m.group(3)) if m else None)
        tickers = [t.strip() for t in (m.group(2) or "").split(",") if t.strip()] if m else []
        if not acc or not doc or cik is None:
            continue
        out.append({"cik": cik, "company": (m.group(1) if m else (names[0] if names else "")).strip(), "ticker": tickers[0] if tickers else "",
                    "form": src.get("form") or src.get("file_type") or "", "filed": src.get("file_date") or "", "period": src.get("period_ending") or "",
                    "accession": acc, "document": doc, "description": html.unescape(src.get("file_description") or ""), "sics": src.get("sics") or [],
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"})
    total = hits.get("total") or {}
    return {"total": total.get("value", len(out)), "total_relation": total.get("relation", "eq"), "hits": out}
