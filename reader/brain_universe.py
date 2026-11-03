"""The brain's map of companies: every S&P 500 company (and the other companies the brain holds) with its GICS sector and
sub-industry (brain/universe.json), and each one's comp set, the companies an equity analyst would compare it with
(brain/comp-sets.json).

Both files are kept by hand (rebuilt with scripts/local/brain_universe_build.py and from BMG's comp-set work); the brain
reads them and never writes them. Neither is a source for a figure (M39): the GICS labels are SECONDARY (S&P's lists as
mirrored by Wikipedia, at a pinned revision), the SEC fields PRIMARY, and the comp sets are BMG's judgment, labeled so on
every page that shows them. A comp set says who a company is compared with; it never says a figure of one is comparable
with a figure of another (M03: that needs both definitions on record).

Standard library only, so scripts and other tools can import it without the server (scripts/local/brain_query.py).
Named "brain_*" so a change here does not make My reads read every saved file again (library.VERSION leaves it out).
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Optional

UNIVERSE = "universe.json"
COMPS = "comp-sets.json"
_CACHE: dict = {}
_GUARD = threading.Lock()


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def _brain(vault) -> Path:
    return Path(vault) / "brain"


def _load(path: Path) -> dict:
    """The file's JSON, read again only when it changes on disk; {} when it is missing or does not parse."""
    try:
        st = path.stat()
    except OSError:
        return {}
    sig = (st.st_mtime_ns, st.st_size)
    with _GUARD:
        hit = _CACHE.get(str(path))
        if hit and hit[0] == sig:
            return hit[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    with _GUARD:
        _CACHE[str(path)] = (sig, data)
    return data


def universe(vault) -> dict:
    return _load(_brain(vault) / UNIVERSE)


def comp_sets(vault) -> dict:
    return _load(_brain(vault) / COMPS)


def on(vault) -> bool:
    """True when the map is in the vault (the brain then sorts companies by it)."""
    return bool((universe(vault).get("companies") or {}))


def canonical(vault, ticker: Optional[str]) -> str:
    """The brain's name for a ticker: a second share class or a dotted class (GOOG, BRK.B) maps to the company's page
    (GOOGL, BRK-B). A ticker the map does not know is returned as given, upper case."""
    t = re.sub(r"\s+", "", (ticker or "").upper())
    u = universe(vault)
    al = u.get("aliases") or {}
    if t in (u.get("companies") or {}):
        return t
    return al.get(t) or al.get(t.replace("-", ".")) or t


def company(vault, ticker: Optional[str]) -> Optional[dict]:
    t = canonical(vault, ticker)
    c = (universe(vault).get("companies") or {}).get(t)
    return {"ticker": t, **c} if c else None


def classification(vault, ticker: Optional[str]) -> Optional[dict]:
    """{sector, industry, index, source, sector_slug, industry_slug, ...} from the map, or None."""
    c = company(vault, ticker)
    if not c or not c.get("gics_sector") or not c.get("gics_sub_industry"):
        return None
    return {"ticker": c["ticker"], "name": c.get("name"), "sector": c["gics_sector"], "industry": c["gics_sub_industry"],
            "sector_slug": slug(c["gics_sector"]), "industry_slug": slug(c["gics_sub_industry"]), "index": c.get("index"),
            "source": c.get("classification_source"), "cik": c.get("cik"), "tickers": c.get("tickers") or [c["ticker"]],
            "sic": c.get("sic"), "sic_description": c.get("sic_description"), "fiscal_year_end": c.get("fiscal_year_end")}


def sector_path(sector: str) -> str:
    """The sector page, without .md, from the vault root."""
    return f"brain/industries/{slug(sector)}"


def industry_path(sector: str, industry: str) -> str:
    """The GICS sub-industry page, without .md, from the vault root."""
    return f"brain/industries/{slug(sector)}/{slug(industry)}"


def members(vault, sector: str, industry: Optional[str] = None) -> list[str]:
    """The map's companies of a sector, or of one sub-industry of it, in ticker order."""
    s = (universe(vault).get("sectors") or {}).get(sector) or {}
    subs = s.get("sub_industries") or {}
    if industry is not None:
        return sorted((subs.get(industry) or {}).get("companies") or [])
    return sorted(t for v in subs.values() for t in v.get("companies") or [])


def comps(vault, ticker: Optional[str]) -> list[dict]:
    """The company's comp set, closest first: [{ticker, name, business, why, outside, in_map}]. A comp's ticker is
    mapped to the company's page (GOOG -> GOOGL)."""
    t = canonical(vault, ticker)
    entry = (comp_sets(vault).get("companies") or {}).get(t) or {}
    cos = universe(vault).get("companies") or {}
    out = []
    for x in entry.get("comps") or []:
        k = canonical(vault, x.get("ticker"))
        if not k or k == t:
            continue
        out.append({"ticker": k, "name": (cos.get(k) or {}).get("name") or x.get("name") or "", "business": x.get("business") or "",
                    "why": x.get("why") or "", "outside": bool(x.get("outside")) or k not in cos, "in_map": k in cos})
    return out


def comp_entry(vault, ticker: Optional[str]) -> dict:
    """The comp set's own notes for the company: analyst_view, confidence, note."""
    e = (comp_sets(vault).get("companies") or {}).get(canonical(vault, ticker)) or {}
    return {k: e.get(k) for k in ("analyst_view", "confidence", "note") if e.get(k)}


def comped_by(vault, ticker: Optional[str]) -> list[str]:
    """The companies whose comp set names this one (the other direction of the comp lines), in ticker order."""
    t = canonical(vault, ticker)
    return sorted(k for k, e in (comp_sets(vault).get("companies") or {}).items()
                  if any(canonical(vault, x.get("ticker")) == t for x in e.get("comps") or []) and k != t)


def about(vault) -> dict:
    """Where the map and the comp sets come from, as the files say."""
    u, c = universe(vault), comp_sets(vault)
    return {"universe": {k: u.get(k) for k in ("about", "as_of", "sources", "counts")},
            "comp_sets": {k: c.get(k) for k in ("about", "as_of", "method", "counts")}}


def listing(vault, read: Optional[dict] = None) -> dict:
    """Every company on the map, by sector and sub-industry, for the API and other tools: {sectors: [...]}, each company
    with its comp tickers and, when `read` is given ({ticker: {...}}), what the brain has read of it."""
    u = universe(vault)
    cos = u.get("companies") or {}
    read = read or {}
    out = []
    for sec, s in sorted((u.get("sectors") or {}).items()):
        subs = []
        for ind, v in sorted((s.get("sub_industries") or {}).items()):
            rows = []
            for t in sorted(v.get("companies") or []):
                c = cos.get(t) or {}
                rows.append({"ticker": t, "name": c.get("name"), "index": c.get("index"), "tickers": c.get("tickers"),
                             "comps": [x["ticker"] for x in comps(vault, t)], "brain": read.get(t)})
            subs.append({"industry": ind, "slug": slug(ind), "companies": rows})
        out.append({"sector": sec, "slug": slug(sec), "sub_industries": subs})
    return {"about": about(vault), "sectors": out}
