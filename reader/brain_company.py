"""One company's report page in the brain: everything the Crawler found in its annual filing, with where it sits on the
map and who it is compared with.

Built from three things the Mac already holds, never fetched:
- the brain's map (brain/universe.json, brain/comp-sets.json): sector, sub-industry, comp set (BMG judgment);
- the company's brain memory (brain/companies/<T>/memory.json): figures across every read, each with its page;
- the latest annual read saved in My reads: the headline and takeaways, what the business is (Item 1), the red-flag
  checks, the risk factors and the debt maturities, each with its page.
Every figure keeps its filing page; nothing here is computed beyond what the reader already did. Named brain_* so a
change here does not make My reads read every saved file again.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import brain_universe as _bu

METRICS = [("revenue", "Revenue"), ("gross_profit", "Gross profit"), ("operating_income", "Operating income"),
           ("net_income", "Net income"), ("eps_diluted", "Diluted EPS"), ("cfo", "Cash from operations"), ("capex", "Capital spending"),
           ("buybacks", "Share buybacks"), ("dividends", "Dividends paid"), ("interest_expense", "Interest expense")]


def _memory(vault: Path, t: str) -> dict:
    from . import brain_peers
    for m in brain_peers.memories(Path(vault)):
        if (m.get("ticker") or "").upper() == t:
            return m
    return {}


def history(mem: dict, n: int = 5) -> list[dict]:
    """Each fiscal-year figure the brain holds, oldest to newest, last n years, with the page it came from."""
    out = []
    for metric, label in METRICS:
        s = (mem.get("series") or {}).get(f"fact:{metric}:FY") or {}
        pts = s.get("points") or {}
        if not pts:
            continue
        rows = []
        for end in sorted(pts)[-n:]:
            p = pts[end]
            src = p.get("src") or {}
            rows.append({"period": end, "value": p.get("value"), "display": p.get("display"), "form": src.get("form"),
                         "page": src.get("printed_page") or src.get("pdf_page"), "url": src.get("url")})
        out.append({"metric": metric, "label": label, "unit": s.get("unit"), "points": rows})
    return out


def latest_annual(library, t: str) -> Optional[dict]:
    """The newest saved read of the company's annual filing (10-K first, then any read the reader called annual)."""
    if library is None:
        return None
    es = [e for e in library.entries() if (e.get("ticker") or e.get("job_ticker") or "").upper() == t]
    for want in (lambda e: str(e.get("form") or "").upper().startswith("10-K"), lambda e: True):
        for e in es:
            if not want(e):
                continue
            got = library.load_any(e["sha256"], e.get("job_ticker") or None) or {}
            res = got.get("result") or {}
            if want is not None and (res.get("insights") or {}).get("kind") not in ("annual", None) and not str(e.get("form") or "").upper().startswith("10-K"):
                continue
            if res:
                return {"entry": e, "result": res}
    return None


def _filing(found: dict) -> dict:
    e, res = found["entry"], found["result"]
    rep = res.get("report") or {}
    ins = res.get("insights") or {}
    tk = rep.get("takeaways") or ins.get("takeaways") or {}
    biz = ((rep.get("business") or {}).get("description")) or {}
    bsrc = biz.get("source") or {}
    facts = (rep.get("evidence") or {}).get("facts") or {}
    figs = []
    for metric, label in METRICS:
        fy = sorted((f for f in facts.values() if f.get("metric") == metric and (f.get("period") or {}).get("type") == "FY"),
                    key=lambda f: (f.get("period") or {}).get("end") or "", reverse=True)
        if fy:
            f = fy[0]
            src = f.get("source") or {}
            figs.append({"metric": metric, "label": label, "display": f.get("display"), "value": f.get("value"),
                         "period": (f.get("period") or {}).get("short") or (f.get("period") or {}).get("end"),
                         "page": src.get("printed_page") or src.get("pdf_page"), "table": src.get("table_title")})
    flags = [{"title": x.get("title"), "why": x.get("why"), "status": x.get("status"), "count": x.get("count"),
              "quotes": (x.get("quotes") or [])[:2]} for x in ins.get("red_flags") or []]
    risks = [{"title": x.get("title"), "category": x.get("category"), "page": x.get("printed_page") or x.get("page")}
             for x in (ins.get("risk_items") or [])[:10]]
    mat = ((rep.get("capital") or {}).get("maturities")) or {}
    co = rep.get("company") or {}
    return {"form": co.get("form") or e.get("form"), "period": co.get("period") or e.get("period"), "title": e.get("title"),
            "saved_at": e.get("saved_at"), "sha256": e.get("sha256"), "pages": (rep.get("source") or {}).get("pages"),
            "headline": tk.get("headline"),
            "takeaways": [{k: x.get(k) for k in ("title", "detail", "means", "watch", "tone")} for x in tk.get("items") or []],
            "business": {"text": biz.get("text"), "page": bsrc.get("printed_page") or bsrc.get("pdf_page")} if biz.get("text") else None,
            "figures": figs, "red_flags": flags, "risks": risks,
            "maturities": {"text": mat.get("text"), "page": ((mat.get("source") or {}).get("printed_page") or (mat.get("source") or {}).get("pdf_page"))} if mat.get("text") else None}


def scores(row: dict) -> Optional[dict]:
    """Altman Z, Piotroski F (out of the tests it could run) and Beneish M from the latest year the brain scored, or None."""
    if not row or not row.get("period_end"):
        return None
    def val(k, nd=2):
        x = row.get(k)
        x = x.get("value") if isinstance(x, dict) else x
        return round(x, nd) if isinstance(x, (int, float)) else None
    pf = row.get("piotroski") if isinstance(row.get("piotroski"), dict) else {}
    out = {"period_end": row["period_end"], "altman": val("altman"), "beneish": val("beneish"),
           "piotroski": f"{pf['value']}/{pf.get('max') or 9}" if isinstance(pf.get("value"), (int, float)) else None}
    return out if any(out[k] is not None for k in ("altman", "beneish", "piotroski")) else None


def report(vault, library, ticker: str) -> Optional[dict]:
    """Everything the company page shows; None when the company is not on the brain's map."""
    t = _bu.canonical(vault, ticker)
    cl = _bu.classification(vault, t)
    if not cl:
        return None
    co = _bu.company(vault, t) or {}
    mem = _memory(vault, t)
    sc = max(mem.get("scores") or [{}], key=lambda r: r.get("period_end") or "")
    found = latest_annual(library, t)
    reads = mem.get("reads") or []
    return {"ticker": t, "name": co.get("name") or cl.get("name"), "classification": cl,
            "hq": co.get("hq"), "added_to_index": co.get("added_to_index"),
            "comps": _bu.comps(vault, t), "comp_notes": _bu.comp_entry(vault, t), "comped_by": _bu.comped_by(vault, t),
            "same_industry": [x for x in _bu.members(vault, cl["sector"], cl["industry"]) if x != t],
            "brain": {"reads": len(reads), "last": {k: reads[-1].get(k) for k in ("form", "period", "at")} if reads else None,
                      "scores": scores(sc)},
            "history": history(mem),
            "filing": _filing(found) if found else None,
            "note": "Figures are the filings' own, each with its page. The comp set is BMG judgment; it says who to compare with, "
                    "never that two figures are comparable (M03). The brain is not a source (M39): cite the filing page."}
