"""Segment tables from the filing's own inline XBRL: revenue, segment profit and the other lines a filer tags by
segment, for each period the filing reports, plus revenue by product and by region when those are tagged.

Every cell is a tagged fact of this filing (value, period, the row it is printed in and its page). A filing without
inline XBRL (a PDF) gets no segment tables from here; the report's revenue breakdowns still come from its tables.
"""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from typing import Optional

from .facts import fmt_value
from .xbrl import page_of

SEGMENT_AXIS = "us-gaap:StatementBusinessSegmentsAxis"
PRODUCT_AXIS = "srt:ProductOrServiceAxis"
REGION_AXIS = "srt:StatementGeographicalAxis"
# a second dimension that only says "this is the operating segments' figure" (ASU 2023-07 tagging)
NEUTRAL = {"srt:ConsolidationItemsAxis=us-gaap:OperatingSegmentsMember", "srt:ConsolidationItemsAxis=us-gaap:MaterialReconcilingItemsMember"}

REVENUE = ["us-gaap:Revenues", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "us-gaap:SalesRevenueNet",
           "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax", "us-gaap:RevenuesNetOfInterestExpense"]
PROFIT = ["us-gaap:OperatingIncomeLoss", "us-gaap:SegmentReportingInformationOperatingIncomeLoss",
          "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
          "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
          "us-gaap:GrossProfit"]
LABELS = {"us-gaap:Revenues": "Revenue", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax": "Revenue", "us-gaap:SalesRevenueNet": "Revenue",
          "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax": "Revenue", "us-gaap:RevenuesNetOfInterestExpense": "Net revenue",
          "us-gaap:OperatingIncomeLoss": "Operating income", "us-gaap:SegmentReportingInformationOperatingIncomeLoss": "Segment operating income",
          "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest": "Segment profit (before income taxes)",
          "us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments": "Segment profit (before income taxes)",
          "us-gaap:GrossProfit": "Gross profit", "us-gaap:Assets": "Assets", "us-gaap:DepreciationDepletionAndAmortization": "Depreciation and amortization",
          "us-gaap:SegmentExpenditureAdditionToLongLivedAssets": "Capital expenditures", "us-gaap:CostOfGoodsAndServicesSold": "Cost of revenue",
          "us-gaap:OperatingExpenses": "Operating expenses", "us-gaap:Goodwill": "Goodwill", "us-gaap:LeaseIncome": "Lease income"}


def _words(qname: str) -> str:
    """'msft:IntelligentCloudMember' -> 'Intelligent Cloud'."""
    local = qname.split(":")[-1]
    local = re.sub(r"(Segment)?Member$", "", local)
    local = re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", local)
    return local.replace(" And ", " and ").replace(" Of ", " of ").replace(" The ", " the ").strip() or qname


def _label(concept: str) -> str:
    return LABELS.get(concept) or _words(concept)


def _months(f: dict) -> Optional[int]:
    if f.get("instant") or not f.get("start"):
        return None
    try:
        d = (date.fromisoformat(f["end"]) - date.fromisoformat(f["start"])).days
    except ValueError:
        return None
    return round(d / 30.44)


def _period_label(end: str, months: Optional[int]) -> str:
    try:
        d = date.fromisoformat(end)
    except ValueError:
        return end
    when = d.strftime("%b %-d, %Y")
    if months is None:
        return f"At {when}"
    if 11 <= months <= 13:
        return f"Year ended {when}"
    return f"{months} months ended {when}"


def _member(f: dict, axis: str) -> Optional[str]:
    dims = [d for d in f["dims"] if d not in NEUTRAL]
    if len(dims) != 1 or not dims[0].startswith(axis + "="):
        return None
    return dims[0].split("=", 1)[1]


USEFUL = ["us-gaap:Assets", "us-gaap:DepreciationDepletionAndAmortization", "us-gaap:SegmentExpenditureAdditionToLongLivedAssets",
          "us-gaap:PaymentsToAcquirePropertyPlantAndEquipment", "us-gaap:CostOfGoodsAndServicesSold", "us-gaap:OperatingExpenses"]
_CUSTOM_PROFIT = re.compile(r"^(Segment)?(Profit|OperatingProfit|OperatingIncome|Income)(Loss)?$|^SegmentProfit|^SegmentOperatingIncome|^SegmentIncome")


def _table(doc, facts: list, axis: str, concepts: list, annual: bool, max_periods: int, near=None) -> list:
    """One table per concept: rows are members of the axis, columns are periods (newest first)."""
    out = []
    for concept in concepts:
        cells: dict = defaultdict(dict)
        periods: dict = {}
        for f in facts:
            if f["concept"] != concept:
                continue
            m = _member(f, axis)
            if not m:
                continue
            mo = _months(f)
            if f.get("instant"):
                key = (f["end"], None)
            elif mo is None or (annual and not 11 <= mo <= 13) or (not annual and mo not in (3, 6, 9, 12)):
                continue
            else:
                key = (f["end"], mo)
            periods[key] = _period_label(*key)
            if key not in cells[m]:
                cells[m][key] = f
        if len(cells) < 2:                       # one member is not a breakdown
            continue
        # newest first; for a quarter, the three-month columns before the year-to-date ones
        cols = sorted(periods, key=lambda k: (k[0], -(k[1] or 0)), reverse=True)
        if not annual:
            q = [k for k in cols if k[1] == 3][:2]
            ytd = [k for k in cols if k[1] and k[1] > 3][:2]
            cols = q + ytd
        cols = cols[:max_periods]
        rows = []
        for m, byp in cells.items():
            vals = []
            for k in cols:
                f = byp.get(k)
                if f is None:
                    vals.append(None)
                    continue
                pg = page_of(doc, f.get("excerpt") or "", near)
                vals.append({"value": f["value"], "display": fmt_value(f["value"], "USD" if f.get("unit", "").upper().startswith("USD") else ""),
                             "raw": f.get("raw"), "page": pg, "printed_page": doc.printed_page(pg) if pg else None,
                             "quote": re.sub(r"\s+", " ", f.get("excerpt") or "")[:240]})
            if any(vals):
                rows.append({"member": m, "name": _words(m), "values": vals})
        if len(rows) < 2:
            continue
        lead = cols[0] if cols else None
        rows.sort(key=lambda r: -(abs((r["values"][0] or {}).get("value") or 0)))
        out.append({"concept": concept, "label": _label(concept), "periods": [{"end": k[0], "months": k[1], "label": periods[k]} for k in cols],
                    "rows": rows, "lead_period": periods.get(lead)})
    return out


def build(doc, rep: dict) -> dict:
    """Segment, product and region tables, and the operating KPIs the report already found, by period."""
    form = ((rep.get("company") or {}).get("form") or "").upper()
    annual = not form.startswith("10-Q")
    xf = getattr(doc, "xbrl", None) or []
    out = {"source": "inline XBRL" if xf else None, "segments": [], "products": [], "regions": [], "kpis": []}
    if xf:
        on_axis = {f["concept"] for f in xf if _member(f, SEGMENT_AXIS)}
        # a filer's own segment profit line (GATX tags gmt:SegmentProfitLoss), after the standard ones
        custom = sorted(c for c in on_axis if not c.startswith("us-gaap:") and _CUSTOM_PROFIT.search(c.split(":")[-1]))
        profit = PROFIT + custom
        seg_concepts = REVENUE + profit
        # the other lines this filer tags by segment: assets, depreciation and capital spending first, then the most used
        counts: dict = defaultdict(int)
        for f in xf:
            if _member(f, SEGMENT_AXIS) and f["concept"] not in seg_concepts and (f.get("unit") or "").upper().startswith("USD"):
                counts[f["concept"]] += 1
        extra = sorted(counts, key=lambda c: (USEFUL.index(c) if c in USEFUL else len(USEFUL), -counts[c]))[:5]
        first = _table(doc, xf, SEGMENT_AXIS, REVENUE, annual, 3)
        # the segment note's page: later tables are looked for there first (a row can also be printed in MD&A)
        pages = [v["page"] for t in first[:1] for r in t["rows"] for v in r["values"] if v and v.get("page")]
        near = max(set(pages), key=pages.count) if pages else None
        tabs = _table(doc, xf, SEGMENT_AXIS, seg_concepts + extra, annual, 3, near)
        # one revenue table and one profit table first (a filer can tag the same thing under two names)
        seen_kind, ordered = set(), []
        for t in tabs:
            kind = "revenue" if t["concept"] in REVENUE else "profit" if t["concept"] in profit else t["concept"]
            if kind == "profit" and t["concept"] in custom:
                t["label"] = "Segment profit"           # the filer's own segment profit measure (its definition is in the segment note)
            if kind in seen_kind:
                continue
            seen_kind.add(kind)
            ordered.append(t)
        out["segments"] = ordered[:8]
        out["products"] = _table(doc, xf, PRODUCT_AXIS, REVENUE, annual, 3)[:1]
        out["regions"] = _table(doc, xf, REGION_AXIS, REVENUE, annual, 3)[:1]
        if not any(t["concept"] in profit for t in out["segments"]) and out["segments"]:
            out["profit_note"] = "This filing does not tag a segment profit line on its segment axis; segment revenue and the other tagged lines are shown."
    else:
        out["note"] = "Segment tables come from a filing's inline XBRL tags; this document has none (a PDF or plain text), so only the revenue breakdowns read from its tables are shown."
    for k in ((rep.get("operating") or {}).get("kpis") or []):
        if k.get("status") == "Unavailable" or k.get("value") is None:
            continue
        src = k.get("source") or {}
        out["kpis"].append({"name": k.get("name"), "now": k.get("display"), "now_period": k.get("period"), "before": k.get("prior_display"),
                            "before_period": k.get("prior_period"), "scope": k.get("scope"), "page": src.get("pdf_page"),
                            "printed_page": src.get("printed_page"), "raw": src.get("raw"), "quote": (src.get("excerpt") or "")[:240]})
    return out
