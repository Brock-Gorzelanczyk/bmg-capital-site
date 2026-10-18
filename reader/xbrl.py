"""Inline XBRL facts from an HTML filing (EDGAR iXBRL).

Each tagged number gives a concept (us-gaap:Revenues), a context (period,
dimensions), a unit, a scale and a sign. Facts with dimensions are segment
or member values and are kept apart from consolidated totals.
"""
from __future__ import annotations

import re
from typing import Optional

CONCEPTS = {
    "revenue": ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:SalesRevenueNet"],
    "cost_of_revenue": ["us-gaap:CostOfGoodsAndServicesSold", "us-gaap:CostOfRevenue", "us-gaap:CostOfGoodsSold"],
    "gross_profit": ["us-gaap:GrossProfit"],
    "operating_income": ["us-gaap:OperatingIncomeLoss"],
    "net_income": ["us-gaap:NetIncomeLoss", "us-gaap:NetIncomeLossAvailableToCommonStockholdersBasic"],
    "pretax_income": ["us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
                      "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"],
    "interest_expense": ["us-gaap:InterestExpense", "us-gaap:InterestExpenseNonoperating", "us-gaap:InterestExpenseNet"],
    "eps_basic": ["us-gaap:EarningsPerShareBasic"],
    "eps_diluted": ["us-gaap:EarningsPerShareDiluted"],
    "shares_diluted": ["us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding"],
    "cash": ["us-gaap:CashAndCashEquivalentsAtCarryingValue"],
    "inventories": ["us-gaap:InventoryNet"],
    "total_assets": ["us-gaap:Assets"],
    "total_liabilities": ["us-gaap:Liabilities"],
    "equity": ["us-gaap:StockholdersEquity", "us-gaap:StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "liab_and_equity": ["us-gaap:LiabilitiesAndStockholdersEquity"],
    "cfo": ["us-gaap:NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["us-gaap:PaymentsToAcquirePropertyPlantAndEquipment"],
    "buybacks": ["us-gaap:PaymentsForRepurchaseOfCommonStock"],
    "dividends": ["us-gaap:PaymentsOfDividends", "us-gaap:PaymentsOfDividendsCommonStock"],
}


def _num(text: str) -> Optional[float]:
    t = re.sub(r"[^\d.]", "", text or "")
    if not t or t == "." or t.count(".") > 1:
        return None
    return float(t)


def parse(data: bytes) -> list[dict]:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(data, "html.parser")
    contexts: dict[str, dict] = {}
    for c in soup.find_all(re.compile(r"^xbrli:context$", re.I)):
        cid = c.get("id")
        inst = c.find(re.compile(r"^xbrli:instant$", re.I))
        start = c.find(re.compile(r"^xbrli:startdate$", re.I))
        end = c.find(re.compile(r"^xbrli:enddate$", re.I))
        dims = [m.get("dimension", "") + "=" + m.get_text(strip=True) for m in c.find_all(re.compile(r"^xbrldi:explicitmember$", re.I))]
        contexts[cid] = {"instant": inst.get_text(strip=True) if inst else None,
                         "start": start.get_text(strip=True) if start else None,
                         "end": end.get_text(strip=True) if end else (inst.get_text(strip=True) if inst else None),
                         "dims": dims}
    out = []
    for el in soup.find_all(re.compile(r"^ix:nonfraction$", re.I)):
        name = el.get("name") or ""
        ctx = contexts.get(el.get("contextref") or "")
        if not ctx:
            continue
        raw = el.get_text(strip=True)
        v = _num(raw)
        if v is None:
            if (el.get("format") or "").endswith("fixed-zero") or raw in ("-", "\u2014"):
                v = 0.0
            else:
                continue
        try:
            v *= 10 ** int(el.get("scale") or 0)
        except ValueError:
            pass
        if (el.get("sign") or "") == "-":
            v = -v
        row = el.find_parent("tr")
        out.append({"concept": name, "value": v, "raw": raw, "unit": el.get("unitref") or "", "decimals": el.get("decimals"),
                    "start": ctx["start"], "end": ctx["end"], "instant": ctx["instant"] is not None, "dims": ctx["dims"],
                    "anchor": el.get("id") or (row.get("id") if row else None),
                    "excerpt": re.sub(r"[\s\u200b-\u200d\u2060\ufeff]+", " ", row.get_text(" ", strip=True)).strip()[:240] if row else raw})   # no zero-width spaces (Simon Property)
    return out


def find(xfacts: list, metric: str, end: Optional[str], instant: bool, months: Optional[int] = None) -> list:
    """Consolidated (no dimensions) facts for a metric at a period end."""
    names = CONCEPTS.get(metric, [])
    hits = []
    for f in xfacts:
        if f["concept"] not in names or f["dims"] or f["end"] != end or f["instant"] != instant:
            continue
        if months and f["start"] and f["end"]:
            y0, m0 = int(f["start"][:4]), int(f["start"][5:7])
            y1, m1 = int(f["end"][:4]), int(f["end"][5:7])
            span = (y1 - y0) * 12 + (m1 - m0)
            if abs(span - months) > 1:
                continue
        hits.append(f)
    return hits


def dei(data: bytes) -> dict:
    """Cover-page facts the filer tagged (dei:EntityRegistrantName,
    dei:TradingSymbol, dei:DocumentType, dei:DocumentPeriodEndDate)."""
    out = {}
    for m in re.finditer(rb'<ix:nonNumeric[^>]*name="dei:(EntityRegistrantName|TradingSymbol|DocumentType|DocumentPeriodEndDate|SecurityExchangeName)"[^>]*>(.*?)</ix:nonNumeric>',
                         data[:3_000_000], re.I | re.S):
        k = m.group(1).decode()
        if k in out:
            continue
        txt = re.sub(rb"<[^>]+>", b" ", m.group(2)).decode("utf-8", "replace")
        out[k] = re.sub(r"\s+", " ", txt.replace("&amp;", "&").replace("&#160;", " ").replace("&nbsp;", " ")).strip()
    return out


def page_of(doc, excerpt: str, near=None):
    """The page where a fact's table row is printed: the row's first words found in the extracted text (spacing
    may differ between the tagged HTML and the text), nearest to page `near` when there are several."""
    toks = re.sub(r"\s+", " ", excerpt or "").strip().split(" ")[:6]
    if len(toks) < 2 or sum(len(t) for t in toks) < 8:
        return None
    cache = doc.__dict__.setdefault("_row_pages", {})
    key = " ".join(toks)
    if key not in cache:
        rx = re.compile(r"\s*".join(re.escape(t) for t in toks))
        hits = []
        for m in rx.finditer(doc.body):
            pg = doc.page_at(m.start())
            if pg:
                hits.append(pg)
            if len(hits) >= 40:
                break
        cache[key] = hits
    hits = cache[key]
    if not hits:
        return None
    return min(hits, key=lambda h: abs(h - near)) if near else hits[0]
