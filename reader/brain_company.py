"""One company's report page in the brain: everything the Crawler found in its annual filing, with where it sits on the
map and who it is compared with.

Built from three things the Mac already holds, never fetched:
- the brain's map (brain/universe.json, brain/comp-sets.json): sector, sub-industry, comp set (BMG judgment);
- the company's brain memory (brain/companies/<T>/memory.json): figures across every read, each with its page;
- the latest annual read saved in My reads: the headline and takeaways, what the business is (Item 1), the red-flag
  checks, the risk factors and the debt maturities, each with its page.
Every figure keeps its filing page; nothing here is computed beyond what the reader already did. Named brain_* so a
change here does not make My reads read every saved file again.

For the company page's own sections the report also carries, straight from the brain's memory: the same figures by
quarter, the latest balance-sheet lines, the guidance the company last gave, the filings read (each with the saved
read it opens in the Crawler, when this computer has it) and the questions the reader left open.
"""
from __future__ import annotations

import re
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


BALANCE = [("cash", "Cash and equivalents"), ("total_assets", "Total assets"), ("total_liabilities", "Total liabilities"),
           ("equity", "Shareholders' equity"), ("inventories", "Inventories")]


def _point(end: str, p: dict, shas: Optional[dict] = None) -> dict:
    """One figure of a series as the page shows it: its value as printed, the filing page, the SEC link, and (when
    this computer has the saved read) the read and page that open it in the Crawler."""
    src = p.get("src") or {}
    return {"period": end, "value": p.get("value"), "display": p.get("display"), "label": p.get("label"), "form": src.get("form"),
            "page": src.get("printed_page") or src.get("pdf_page"), "pdf_page": src.get("pdf_page"), "url": src.get("url"),
            "read": _saved(shas, src.get("sha8") or p.get("read"), src.get("file"))}


def _saved(shas: Optional[dict], sha8: Optional[str], file: Optional[str]) -> Optional[str]:
    """The saved read on this computer that holds a figure's filing: the very read the brain took it from, else another
    saved read of the same filing (same file name, which carries the SEC accession number)."""
    shas = shas or {}
    return shas.get(sha8 or "") or shas.get("file:" + (file or "")) or None


def history(mem: dict, n: int = 5, span: str = "FY", shas: Optional[dict] = None) -> list[dict]:
    """Each figure the brain holds by fiscal year (span "FY") or by quarter ("Q"), oldest to newest, last n periods,
    with the page it came from."""
    out = []
    for metric, label in METRICS:
        s = (mem.get("series") or {}).get(f"fact:{metric}:{span}") or {}
        pts = s.get("points") or {}
        if not pts:
            continue
        out.append({"metric": metric, "label": label, "unit": s.get("unit"), "points": [_point(end, pts[end], shas) for end in sorted(pts)[-n:]]})
    return out


def balance(mem: dict, shas: Optional[dict] = None) -> Optional[dict]:
    """The latest balance-sheet lines the brain holds (cash, assets, liabilities, equity, inventories, and each debt
    line as the filing names it), all as of the same date, each with its page; None when it holds none."""
    series = mem.get("series") or {}
    ends = [max(pts) for pts in ((series.get(f"fact:{m}:instant") or {}).get("points") or {} for m, _l in BALANCE) if pts]
    if not ends:
        return None
    end = max(ends)
    rows = []
    for metric, label in BALANCE:
        p = ((series.get(f"fact:{metric}:instant") or {}).get("points") or {}).get(end)
        if p:
            rows.append({**_point(end, p, shas), "metric": metric, "label": label})
    for key in sorted(k for k in series if k.startswith("fact:debt_line:instant:")):
        p = (series[key].get("points") or {}).get(end)
        if p:
            rows.append({**_point(end, p, shas), "metric": "debt_line", "label": series[key].get("name") or "Debt"})
    return {"as_of": end, "rows": rows} if rows else None


def guidance(mem: dict, n: int = 8) -> Optional[dict]:
    """What the company last said it expects: the lines of the newest filing that gave guidance, each with its words
    and where they are. Guidance is the company's own statement, a check and never a target."""
    rows = [g for g in mem.get("guidance") or [] if g.get("text")]
    if not rows:
        return None
    newest = max((g.get("given_in") or {}).get("period_end") or "" for g in rows)
    out, seen = [], set()
    for g in rows:
        gi = g.get("given_in") or {}
        if (gi.get("period_end") or "") != newest or g["text"] in seen:
            continue
        seen.add(g["text"])
        src = g.get("src") or {}
        out.append({"text": g["text"], "topic": g.get("topic_label") or g.get("topic"), "covers": g.get("covers"),
                    "page": src.get("printed_page") or src.get("pdf_page"), "url": src.get("url"), "form": src.get("form") or gi.get("form")})
    said = [x for x in out if re.search(r"[.!?]$", x["text"].strip())]
    if len(said) >= 3:
        out = said                                   # the sentences; a bare table line ("GAAP gross margin: 74.0%") repeats one of them
    given = next((g.get("given_in") for g in rows if (g.get("given_in") or {}).get("period_end") == newest), None) or {}
    return {"given_in": {"form": given.get("form"), "period": given.get("period"), "period_end": newest or None}, "lines": out[:n], "more": max(0, len(out) - n)}


def reads(mem: dict, shas: Optional[dict] = None) -> list[dict]:
    """The filings the brain has read of the company, newest period first, one line per filing (a filing read twice
    is listed once, with its latest read)."""
    by = {}
    for r in mem.get("reads") or []:
        key = r.get("accession") or r.get("file") or (r.get("form"), r.get("period"))
        if key not in by or (r.get("at") or "") >= (by[key].get("at") or ""):
            by[key] = {**by.get(key, {}), **{k: v for k, v in r.items() if v is not None}}
    out = [{"form": r.get("form"), "period": r.get("period"), "period_end": r.get("period_end"), "kind": r.get("kind"), "pages": r.get("pages"),
            "read_at": r.get("at"), "url": r.get("url"), "read": _saved(shas, r.get("sha8"), r.get("file"))} for r in by.values()]
    out.sort(key=lambda r: (r.get("period_end") or "", r.get("read_at") or ""), reverse=True)
    return out


def questions(mem: dict) -> list[dict]:
    """The questions the reader raised that no later read has answered."""
    return [{"q": q.get("q"), "form": q.get("form"), "raised_in": q.get("raised_in")} for q in mem.get("questions") or []
            if q.get("q") and q.get("status") == "open"]


def _shas(library, t: str) -> dict:
    """The company's saved reads on this computer, to open a figure's page in the Crawler: {first 8 characters of a
    read's sha256: the whole sha256, "file:" + its file name: the whole sha256}."""
    if library is None:
        return {}
    out = {}
    for e in library.entries():
        if e.get("sha256") and (e.get("ticker") or e.get("job_ticker") or "").upper() == t:
            out[e["sha256"][:8]] = e["sha256"]
            if e.get("title"):
                out.setdefault("file:" + e["title"], e["sha256"])
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
    seen = mem.get("reads") or []
    shas = _shas(library, t)
    return {"ticker": t, "name": co.get("name") or cl.get("name"), "classification": cl,
            "hq": co.get("hq"), "added_to_index": co.get("added_to_index"),
            "comps": _bu.comps(vault, t), "comp_notes": _bu.comp_entry(vault, t), "comped_by": _bu.comped_by(vault, t),
            "same_industry": [x for x in _bu.members(vault, cl["sector"], cl["industry"]) if x != t],
            "brain": {"reads": len(seen), "last": {k: seen[-1].get(k) for k in ("form", "period", "at")} if seen else None,
                      "scores": scores(sc)},
            "history": history(mem, shas=shas), "quarters": history(mem, 8, "Q", shas), "balance": balance(mem, shas),
            "guidance": guidance(mem), "reads": reads(mem, shas), "questions": questions(mem),
            "filing": _filing(found) if found else None,
            "note": "Figures are the filings' own, each with its page. The comp set is BMG judgment; it says who to compare with, "
                    "never that two figures are comparable (M03). The brain is not a source (M39): cite the filing page."}
