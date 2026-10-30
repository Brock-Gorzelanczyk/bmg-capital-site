"""The brain across companies: what one industry's filings taught the Crawler (its playbook), who a company names as
its competitors, how its figures sit beside its peers' in the brain, and how BMG values its industry.

Reads brain/ and writes nothing. Named "brain_*" so a change here does not make My reads read every saved file again
(library.VERSION leaves the brain's modules out).

Cross-company rule (M03, reference/method/M03.md): a figure is set beside another company's only with each company's
own printed line, period and page, and it is ranked only when both definitions are on record and match: the same brain
key (so the same kind of period), the same basis from research/facts/CONTROLLED-VOCABULARY.md and the same unit. GAAP
statement lines carry the basis "gaap_reported", so revenue sits against revenue; an operating KPI with no basis on
record is shown, not ranked, and says why. GATX's LPI against Trinity's FLRD (falsified.md FALS-02, FALS-10) is the case
this guards against: the two have different keys and are never put in one row.
"""
from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from .brain import _fmt, display_name, latest, root

_PERIOD_SUFFIX = re.compile(r":(?:Q|FY|3M|6M|9M|H1|YTD|TTM)$", re.I)


# ---------------------------------------------------------------------------------------------------- the companies
def memories(vault: Path) -> list[dict]:
    """Every company book in the brain (memory.json), in ticker order."""
    out = []
    for p in sorted((root(vault) / "companies").glob("*/memory.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if m.get("ticker") and m.get("generated_by") == "crawler-brain":
            out.append(m)
    return out


def profile_parts(profile: Optional[str]) -> set[str]:
    """"Railcar leasing + Railcar manufacturing" -> {"railcar leasing", "railcar manufacturing"}. "General" is the
    reader's catch-all, not an industry, so it never makes two companies peers."""
    return {p.strip().lower() for p in re.split(r"\+|,|/", profile or "") if p.strip()} - {"general"}


def same_profile(m: dict, profile: Optional[str]) -> bool:
    return bool(profile_parts(m.get("profile")) & profile_parts(profile))


def _short_src(src: Optional[dict]) -> dict:
    s = src or {}
    return {k: s.get(k) for k in ("form", "filing_period", "pdf_page", "printed_page", "url", "file") if s.get(k) is not None}


def _base(key: str) -> str:
    return _PERIOD_SUFFIX.sub("", key)


# ---------------------------------------------------------------------------------------------------- 1. the playbook
def playbook(vault: Path, profile: str, ticker: Optional[str] = None) -> dict:
    """What the brain learned about reading one industry: every KPI its companies print, grouped by the brain's key,
    with each company's own printed name, where in its filing it sits and the latest printed line. Grouping is by key
    only; no figure is compared here. With a ticker: the KPIs other companies of the profile print that the brain has
    not seen for this one (either its filings do not print them or the Crawler has not found them)."""
    t = (ticker or "").upper()
    ms = [m for m in memories(vault) if same_profile(m, profile)]
    groups: dict[str, dict] = {}
    for m in ms:
        where = m.get("where") or {}
        for key, s in (m.get("series") or {}).items():
            if s.get("kind") != "kpi":
                continue
            lt = latest(s)
            if not lt:
                continue
            g = groups.setdefault(_base(key), {"key": _base(key), "name": s.get("name") or key, "unit": s.get("unit"), "by": {}})
            w = where.get(key) or {}
            g["by"].setdefault(m["ticker"], []).append({
                "key": key, "printed": display_name(key, s.get("name") or key), "scope": s.get("scope"), "basis": s.get("basis"),
                "end": lt[0], "display": lt[1].get("display") or _fmt(lt[1]["value"], s.get("unit")),
                "quote": ((lt[1].get("src") or {}).get("quote") or "")[:240], "src": _short_src(lt[1].get("src")),
                "where": {k: w.get(k) for k in ("section", "table", "row", "how", "form", "pdf_page") if w.get(k)}})
    kpis = sorted(groups.values(), key=lambda g: (-len(g["by"]), g["name"].lower()))
    for g in kpis:
        g["companies"] = sorted(g["by"])
    out = {"profile": profile, "companies": [{"ticker": m["ticker"], "name": m.get("name"), "reads": len(m.get("reads") or [])} for m in ms],
           "kpis": kpis}
    if t:
        out["for"] = t
        out["not_seen_for"] = [{"key": g["key"], "name": g["name"], "printed_by": [x for x in g["companies"] if x != t]}
                               for g in kpis if t not in g["by"] and any(x != t for x in g["companies"])]
    return out


def brief_lines(vault: Path, profile: str, ticker: str, n: int = 8) -> list[dict]:
    """The playbook's KPIs for the analyst brief: what other companies of the profile print, and where."""
    pb = playbook(vault, profile, ticker)
    t = (ticker or "").upper()
    out = []
    for g in pb["kpis"]:
        others = [x for x in g["companies"] if x != t]
        if not others:
            continue
        ex = g["by"][others[0]][0]
        out.append({"name": g["name"], "printed_by": others, "seen_for_this": t in g["by"],
                    "example": {"ticker": others[0], "printed": ex["printed"], "where": ex["where"], "src": ex["src"]}})
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------------------------------- 3. side by side
FACTS = [("revenue", "FY", "Revenue (year)"), ("revenue", "Q", "Revenue (quarter)"), ("net_income", "FY", "Net income (year)"),
         ("net_income", "Q", "Net income (quarter)"), ("eps_diluted", "FY", "Diluted EPS (year)"), ("eps_diluted", "Q", "Diluted EPS (quarter)"),
         ("cfo", "FY", "Operating cash flow (year)"), ("total_assets", "instant", "Total assets"),
         ("own_equity", "instant", "Shareholders' equity, the company's own")]


def own_equity_key(m: dict) -> Optional[str]:
    """The series holding the company's own shareholders' equity. Filers print it differently: GATX's "Total GATX
    Shareholders' Equity" sits under the brain's "equity" key, Trinity's and Greenbrier's "equity" key holds total
    equity with the non-controlling interest and their own share is "equity_parent". None when the brain cannot tell."""
    ser = m.get("series") or {}
    parent = sorted(k for k in ser if k.startswith("fact:equity_parent:instant"))
    if parent:
        return parent[0]
    has_nci = any(k.startswith("fact:equity_nci:") for k in ser)
    has_total = any(k.startswith("fact:equity_total:") for k in ser)
    if "fact:equity:instant" in ser and (has_total or not has_nci):
        return "fact:equity:instant"
    return None


def _fact_key(m: dict, base: str, typ: str) -> Optional[str]:
    if base == "own_equity":
        return own_equity_key(m)
    return f"fact:{base}:{typ}"


def comparable(cells: list[dict]) -> tuple[str, str]:
    """(status, why) for one row of cells, each with "kind", "key", "basis" and "unit". Ranked only on "same"."""
    if len(cells) < 2:
        return "single", "Only one company has this figure in the brain."
    keys = {c["key"] for c in cells}
    units = {c.get("unit") for c in cells}
    bases = [c.get("basis") for c in cells]
    if len(keys) > 1 or len(units) > 1:
        return "differ", "Different measures or periods (different brain keys or units)."
    if any(b is None for b in bases):
        missing = sorted(c["ticker"] for c in cells if c.get("basis") is None)
        return "not_on_record", (f"No definition on record for {', '.join(missing)} (no basis in CONTROLLED-VOCABULARY.md). Each "
                                 "filing defines it in its own words: read both before comparing (M03).")
    if len(set(bases)) > 1:
        return "differ", f"Defined differently: {', '.join(sorted(set(bases)))} (M03)."
    if cells[0].get("kind") == "ratio":
        return "same", "Computed the same way for each company from its own GAAP lines."
    return "same", f"Same line and basis ({bases[0]})."


def _days(a: str, b: str) -> Optional[int]:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except (TypeError, ValueError):
        return None


def _rank(cells: list[dict]) -> None:
    """Position by value, largest first, written on each cell as "1 of 3". Says where a figure sits, not which is better."""
    vals = sorted((c["value"] for c in cells), reverse=True)
    for c in cells:
        c["rank"] = f"{vals.index(c['value']) + 1} of {len(vals)}"


def _point(m: dict, key: str) -> Optional[dict]:
    s = (m.get("series") or {}).get(key)
    lt = latest(s) if s else None
    if not lt:
        return None
    return {"ticker": m["ticker"], "key": key, "kind": s.get("kind"), "basis": s.get("basis"), "unit": s.get("unit"),
            "name": display_name(key, s.get("name") or key), "end": lt[0], "value": lt[1]["value"],
            "display": lt[1].get("display") or _fmt(lt[1]["value"], s.get("unit")),
            "quote": ((lt[1].get("src") or {}).get("quote") or "")[:240], "src": _short_src(lt[1].get("src"))}


def _at(m: dict, key: str, end: str) -> Optional[dict]:
    s = (m.get("series") or {}).get(key)
    p = ((s or {}).get("points") or {}).get(end)
    return {"value": p["value"], "src": _short_src(p.get("src"))} if p else None


def _ratios(m: dict) -> list[dict]:
    """Ratios computed the same way for every company from its own GAAP lines of one year end: net margin, return on
    year-end equity and total debt to equity. Each carries both inputs and their pages."""
    out = []
    ni = _point(m, "fact:net_income:FY")
    if ni:
        end = ni["end"]
        ek = own_equity_key(m)
        rev, eq = _at(m, "fact:revenue:FY", end), (_at(m, ek, end) if ek else None)
        defs = [("net_margin", "Net margin (year)", rev, lambda r: ni["value"] / r, "net income / revenue"),
                ("roe_year_end", "Return on year-end equity (year)", eq, lambda e: ni["value"] / e,
                 "net income attributable to the company / its own year-end equity")]
        for k, name, inp, f, how in defs:
            if inp and inp["value"]:
                v = 100 * f(inp["value"])
                out.append({"ticker": m["ticker"], "key": f"ratio:{k}", "kind": "ratio", "basis": "computed", "unit": "percent", "name": name,
                            "end": end, "value": v, "display": f"{v:.1f}%", "how": how, "src": ni["src"], "inputs": [ni["src"], inp["src"]]})
    return out


def side_by_side(vault: Path, ticker: str, peers: Optional[list[str]] = None, profile: Optional[str] = None, n: int = 6) -> dict:
    """The company's figures beside its peers' from the brain. Peers: the tickers given that the brain holds, else the
    companies of the same profile. One row per measure; a row is ranked only when comparable() says "same"."""
    t = ticker.upper()
    allm = {m["ticker"]: m for m in memories(vault)}
    me = allm.get(t)
    prof = profile or (me or {}).get("profile")
    asked = [p.upper() for p in (peers or []) if p and p.upper() != t]
    chosen = [p for p in asked if p in allm] or [x for x, m in allm.items() if x != t and same_profile(m, prof)]
    chosen = chosen[:n]
    group = ([me] if me else []) + [allm[p] for p in chosen]
    rows = []
    for base, typ, label in FACTS:
        cells = []
        for m in group:
            k = _fact_key(m, base, typ)
            c = _point(m, k) if k else None
            if c:
                c["key"] = f"fact:{base}:{typ}"          # one row key; each cell keeps its own printed line and page
                cells.append(c)
        if cells:
            rows.append({"measure": label, "kind": "fact", "cells": cells})
    by_ratio: dict[str, list] = {}
    for m in group:
        for r in _ratios(m):
            by_ratio.setdefault(r["key"], []).append(r)
    for key, cells in by_ratio.items():
        rows.append({"measure": cells[0]["name"], "kind": "ratio", "cells": cells})
    kp: dict[str, list] = {}
    for m in group:
        for key, s in (m.get("series") or {}).items():
            if s.get("kind") == "kpi":
                c = _point(m, key)
                if c:
                    kp.setdefault(key, []).append(c)     # by the full key: a quarter's rate is never set against a year's
    for base, cells in sorted(kp.items(), key=lambda x: (-len(x[1]), x[0])):
        if me and not any(c["ticker"] == t for c in cells):
            continue            # a peer's KPI the company does not print is in the playbook, not this table
        rows.append({"measure": cells[0]["name"], "kind": "kpi", "cells": cells})
    for r in rows:
        r["status"], r["why"] = comparable(r["cells"])
        ends = [c["end"] for c in r["cells"]]
        gap = max((_days(a, b) or 0) for a in ends for b in ends) if len(ends) > 1 else 0
        if gap > 31:
            r["periods"] = f"Periods end up to {gap} days apart; each cell shows its own."
        if r["status"] == "same":
            _rank(r["cells"])
    return {"ticker": t, "known": bool(me), "profile": prof, "peers": chosen,
            "not_in_brain": [p for p in asked if p not in allm], "rows": rows,
            "note": "Each figure is the company's own latest printed figure with its page. A rank says where a figure sits in "
                    "the group, largest first; it does not say which is better. Rows that are not comparable are shown, not ranked (M03)."}


# ---------------------------------------------------------------------------------------------------- 2. competitors
_SUFFIX = {"inc", "incorporated", "corp", "corporation", "co", "company", "companies", "ltd", "limited", "plc", "llc", "lp", "llp",
           "holdings", "holding", "group", "sa", "ag", "nv", "se", "the", "de", "cv", "spa", "gmbh", "bv", "kg", "ab", "asa", "oyj"}
_CUE = re.compile(r"(?i)\b(?:competitors?|compete[sd]?\s+(?:directly\s+|primarily\s+)?(?:with|against)|competition\s+(?:from|with|includes?))\b")
_LEAD = re.compile(r"(?i)\b(?:are|include|includes|including|is|such as|namely|consist of)\b|:")
_SPLIT = re.compile(r"\s*(?:,|;|\band\b|\bor\b|\bas well as\b|\bamong others\b)\s*")
_NOT_NAMES = {"we", "our", "us", "it", "they", "the company", "company", "north america", "europe", "united states", "asia", "china",
              "india", "japan", "canada", "mexico", "latin america", "others", "other", "many", "certain", "some", "these", "those"}
_SMALL = {"of", "and", "&", "de", "du", "la", "the", "for", "y", "e"}
_ABBR = re.compile(r"(?i)(?:\b(?:inc|corp|co|ltd|no|nos|jr|sr|st|mr|ms|dr|vs|etc|approx|u\.s|l\.p|n\.v|s\.a|plc)|\b[A-Z])$")


_STOP = re.compile(r"\.(?=\s|$)|\n")          # a period before any white space (a filing's HTML often has a no-break space)


def _sentence(body: str, at: int) -> tuple[int, int]:
    """Start and end offsets of the sentence around an offset; "Inc." and "U.S." do not end a sentence."""
    a = 0
    for m in _STOP.finditer(body, max(0, at - 1500), at):
        if m.group(0) == "\n" or not _ABBR.search(body[max(0, m.start() - 6):m.start()]):
            a = m.end()
    z = len(body)
    for m in _STOP.finditer(body, at):
        if m.group(0) == "\n" or not _ABBR.search(body[max(0, m.start() - 6):m.start()]):
            z = m.start() + (1 if m.group(0) == "." else 0)
            break
    return a, z


def norm_name(name: str) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return " ".join(w for w in s.split() if w not in _SUFFIX)


def _looks_like_name(chunk: str) -> bool:
    words = chunk.split()
    if not words or len(words) > 8 or not chunk[0].isupper():
        return False
    if norm_name(chunk) in _NOT_NAMES or chunk.lower() in _NOT_NAMES:
        return False
    big = [w for w in words if w.lower() not in _SMALL]
    return bool(big) and sum(1 for w in big if w[:1].isupper() or w[:1].isdigit()) >= max(1, round(0.75 * len(big)))


def named_competitors(body: str, location: Callable[[int], dict], own_names: tuple = ()) -> list[dict]:
    """Companies a filing names in a sentence about its competitors ("Rail North America's primary competitors in
    railcar leasing are Union Tank Car Company, CIT Rail, Trinity Industries Leasing Company, and American Industrial
    Transport."), each with the sentence and its page. The filer's own names are left out."""
    own = {norm_name(x) for x in own_names if x}
    seen, out = set(), []
    for cue in _CUE.finditer(body or ""):
        a0, z = _sentence(body, cue.start())
        a = a0 - 1
        sent = " ".join(body[a0:z].split())
        if len(sent) > 600:
            continue
        tail = body[cue.end():z]
        lead = _LEAD.search(tail[:90])
        if lead:
            tail = tail[lead.end():]
        elif not re.match(r"(?i)compete", cue.group(0)):
            continue
        tail = re.sub(r"\([^)]*\)", " ", tail).strip(" .:")
        for chunk in _SPLIT.split(tail):
            c = re.sub(r"^(?:the|other|certain|companies such as)\s+", "", chunk.strip(" .\"'"), flags=re.I).strip()
            c = re.sub(r"\s+(?:in|for|on|at|with|by)\s+.*$", "", c)
            if not _looks_like_name(c):
                continue
            k = norm_name(c)
            if not k or k in seen or k in own or any(k == o or k.startswith(o + " ") for o in own if o):
                continue
            seen.add(k)
            loc = location(a + 1) if location else {}
            out.append({"name": c, "quote": sent[:420], "pdf_page": loc.get("pdf_page"), "printed_page": loc.get("printed_page"),
                        "section": loc.get("section")})
    return out


def ticker_index(rows: list[tuple]) -> dict:
    """{normalized SEC company name: (ticker, title, cik)} from (ticker, title, cik) rows; the first ticker of a name wins."""
    idx: dict = {}
    for tk, title, cik in rows:
        k = norm_name(title)
        if k and k not in idx:
            idx[k] = (tk, title, cik)
    return idx


def match_ticker(name: str, idx: dict) -> Optional[dict]:
    """The SEC filer a named competitor is, or is part of ("Trinity Industries Leasing Company" is part of Trinity
    Industries, TRN). A part-of match needs at least two words of the filer's name."""
    k = norm_name(name)
    if not k:
        return None
    if k in idx:
        tk, title, cik = idx[k]
        return {"ticker": tk, "sec_name": title, "cik": cik, "match": "same name"}
    words = k.split()
    for n in range(len(words) - 1, 1, -1):
        p = " ".join(words[:n])
        if p in idx:
            tk, title, cik = idx[p]
            return {"ticker": tk, "sec_name": title, "cik": cik, "match": f"part of {title}"}
    return None


def in_brain(vault: Path) -> dict:
    """{ticker: (name, profile, reads)} for every company the brain holds."""
    return {m["ticker"]: (m.get("name"), m.get("profile"), len(m.get("reads") or [])) for m in memories(vault)}


# ---------------------------------------------------------------------------------------------------- 4. valuation
def valuation_methods(vault: Path, profile: Optional[str]) -> dict:
    """How BMG values the profile's companies, from brain/valuation-map.json (kept by hand; the brain never writes it)."""
    p = root(vault) / "valuation-map.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    parts = profile_parts(profile)
    methods, used = [], []
    for k, v in data.items():
        if k.startswith("_") or not isinstance(v, dict):
            continue
        if k.strip().lower() in parts:
            used.append(k)
            methods += [{**x, "profile": k} for x in v.get("methods") or []]
    return {"profile": profile, "matched": used, "methods": methods, "about": data.get("_about"),
            "note": None if methods else "No BMG valuation method is on record for this industry in brain/valuation-map.json yet."}
