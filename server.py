"""BMG Capital site + /api/snapshot — FastAPI replacement for the
previous Caddy static server.

Serves:
  - Every static file at /<path> (the previous Caddy root).
  - /api/snapshot?ticker=XXX — SEC XBRL + Stooq price snapshot.
  - /api/health — Railway healthcheck.

Cache-Control is set by middleware to preserve the Caddyfile semantics:
  - *.html, /, /site.css, /house-style.css → no-cache
  - hashed site.<hash>.css + house-style.<hash>.css → immutable
  - *.png, *.svg, *.webp, *.gif, *.ico, *.woff, *.woff2, *.js → immutable
  - *.pdf, *.xlsx, *.pptx, *.docx → no-cache

SEC compliance:
  - Every outbound SEC request carries User-Agent
    "BMG Capital research gorzela2@uwm.edu".
  - Rate limit ≤10 req/s across all SEC endpoints (asyncio semaphore
    + token bucket).
  - On-disk caches under /tmp/bmg-cache keyed by SHA256(url) with TTLs:
      ticker map 24h, submissions 6h, companyfacts 24h, prices 1h.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

SEC_USER_AGENT = "BMG Capital research gorzela2@uwm.edu"
CACHE_DIR = Path(os.environ.get("BMG_CACHE_DIR", "/tmp/bmg-cache"))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TTL_TICKER_MAP = 24 * 3600   # 24h
TTL_SUBMISSIONS = 6 * 3600   # 6h
TTL_COMPANYFACTS = 24 * 3600  # 24h
TTL_PRICES = 1 * 3600         # 1h

STATIC_ROOT = Path(__file__).parent

# Logging — a one-line record per outbound SEC request, with HIT/MISS
# cache status. Needed for Gate 3 proof (cache hit on repeat GATX).
logging.basicConfig(
    level=os.environ.get("BMG_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("bmg-snapshot")


# ----------------------------------------------------------------------
# Rate limiter: ≤10 req/s across SEC endpoints
# ----------------------------------------------------------------------

class SECRateLimiter:
    """Token bucket: 10 tokens max, refills at 10/sec. Combined with an
    asyncio semaphore (concurrency bound) and a global asyncio lock so
    multiple coroutines serialise their token consumption.
    """

    def __init__(self, rate_per_sec: float = 10.0, burst: int = 10):
        self.rate = rate_per_sec
        self.capacity = burst
        self.tokens = float(burst)
        self.updated = time.monotonic()
        self.sem = asyncio.Semaphore(burst)
        self.lock = asyncio.Lock()

    async def acquire(self) -> None:
        await self.sem.acquire()
        try:
            async with self.lock:
                now = time.monotonic()
                elapsed = now - self.updated
                self.tokens = min(self.capacity,
                                   self.tokens + elapsed * self.rate)
                self.updated = now
                if self.tokens < 1.0:
                    sleep_for = (1.0 - self.tokens) / self.rate
                else:
                    sleep_for = 0.0
                self.tokens = max(0.0, self.tokens - 1.0)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        finally:
            self.sem.release()


SEC_LIMITER = SECRateLimiter(rate_per_sec=10.0, burst=10)


# ----------------------------------------------------------------------
# File cache (SHA256 keyed)
# ----------------------------------------------------------------------

def _cache_path(url: str) -> Path:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return CACHE_DIR / f"{key}.json"


def _cache_read(url: str, ttl: int) -> Optional[Any]:
    p = _cache_path(url)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    fetched = obj.get("__fetched_at__", 0)
    if time.time() - fetched > ttl:
        return None
    return obj.get("body")


def _cache_write(url: str, body: Any) -> None:
    p = _cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"__fetched_at__": time.time(), "body": body}))


# ----------------------------------------------------------------------
# SEC/Stooq fetchers
# ----------------------------------------------------------------------

async def _sec_fetch_json(url: str, ttl: int,
                           client: httpx.AsyncClient) -> Optional[dict]:
    cached = _cache_read(url, ttl)
    if cached is not None:
        log.info("CACHE HIT %s", url)
        return cached
    log.info("CACHE MISS %s", url)
    await SEC_LIMITER.acquire()
    r = await client.get(url, headers={"User-Agent": SEC_USER_AGENT,
                                        "Accept": "application/json"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    _cache_write(url, body)
    return body


async def _stooq_fetch_price(ticker: str,
                              client: httpx.AsyncClient
                              ) -> tuple[Optional[dict], Optional[str]]:
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    cached = _cache_read(url, TTL_PRICES)
    if cached is not None:
        log.info("CACHE HIT %s", url)
        return cached, None
    log.info("CACHE MISS %s", url)
    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; BMG Capital research)"
        })
    except httpx.HTTPError as e:
        return None, f"stooq fetch failed: {e}"
    if r.status_code != 200:
        return None, f"stooq http {r.status_code}"
    text = r.text
    # Stooq returns CSV; if it returns HTML (anti-bot), fail gracefully.
    if not text.startswith("Date,"):
        return None, "stooq returned non-CSV (anti-bot shield)"
    rows = text.strip().splitlines()
    if len(rows) < 2:
        return None, "stooq returned empty CSV"
    last = rows[-1].split(",")
    try:
        close = float(last[4])
        date = last[0]
    except (ValueError, IndexError):
        return None, "stooq CSV parse error"
    body = {"last": close, "date": date, "source": "Stooq daily"}
    _cache_write(url, body)
    return body, None


# ----------------------------------------------------------------------
# XBRL tag resolution
# ----------------------------------------------------------------------

XBRL_TAGS = {
    "revenue": ["Revenues",
                 "RevenueFromContractWithCustomerExcludingAssessedTax",
                 "SalesRevenueNet"],
    "net_income": ["NetIncomeLoss"],
    "eps_diluted": ["EarningsPerShareDiluted"],
    "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "equity": ["StockholdersEquity"],
    "cash": ["CashAndCashEquivalentsAtCarryingValue"],
    "debt": ["LongTermDebt",
              "__LongTermDebtNoncurrent+LongTermDebtCurrent",
              "DebtInstrumentCarryingAmount"],
    "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
    "dps": ["CommonStockDividendsPerShareDeclared"],
    "interest_expense": ["InterestExpense"],
}


def _pick_unit(fact: dict) -> Optional[str]:
    units = fact.get("units", {})
    for k in ("USD", "USD/shares", "shares"):
        if k in units:
            return k
    for k in units.keys():
        return k
    return None


def _fy_span_days(r: dict) -> Optional[int]:
    """Days between start and end for an XBRL entry. Point-in-time
    balance-sheet facts have no start — return None."""
    import datetime as _dt
    s = r.get("start")
    e = r.get("end")
    if not s or not e:
        return None
    try:
        return (_dt.date.fromisoformat(e) - _dt.date.fromisoformat(s)).days
    except Exception:
        return None


def _end_year(r: dict) -> Optional[int]:
    e = r.get("end")
    if not e:
        return None
    try:
        return int(e[:4])
    except (ValueError, TypeError):
        return None


def _fy_series(fact: dict) -> list[dict]:
    """Return FY entries (fp == 'FY', form 10-K), one per fiscal year.

    The `fy` field in XBRL means "fiscal year of the filing in which
    this fact appears" — a 2026 10-K restates prior years using the
    SAME fy value. The real fiscal year of the data is the year of
    its `end` date. Key by end-year.

    Flow facts (revenue, net income) have start+end — a full-year
    fact spans ~365 days. Balance-sheet facts (equity, cash, debt)
    have only end — pick the row whose `end` is `YYYY-12-31` (or the
    latest end date in that calendar year)."""
    unit = _pick_unit(fact)
    if not unit:
        return []
    rows = fact["units"][unit]
    fy_rows = [r for r in rows
                if r.get("fp") == "FY" and r.get("form") == "10-K"]

    by_year: dict[int, dict] = {}
    for r in fy_rows:
        ey = _end_year(r)
        if ey is None:
            continue
        span = _fy_span_days(r)
        is_full_year = span is not None and span >= 350
        is_balance = span is None
        # Reject partial-year flow rows outright.
        if span is not None and span < 350:
            continue
        prev = by_year.get(ey)
        if prev is None:
            by_year[ey] = r
            continue
        prev_span = _fy_span_days(prev)
        prev_balance = prev_span is None
        if is_balance and prev_balance:
            if (r.get("end", ""), r.get("filed", "")) > \
               (prev.get("end", ""), prev.get("filed", "")):
                by_year[ey] = r
        else:
            # Both are full-year flow rows → prefer latest filed
            # (most-restated figure).
            if r.get("filed", "") > prev.get("filed", ""):
                by_year[ey] = r

    out = []
    for yr in sorted(by_year.keys()):
        r = by_year[yr]
        out.append({"fy": yr, "end": r.get("end"),
                    "val": r.get("val"), "unit": unit})
    return out[-10:]


def _q_series(fact: dict) -> list[dict]:
    """Return quarterly entries (fp in Q1/Q2/Q3, form 10-Q). Pick the
    row whose span is ~90d (quarter), preferring latest filing. Key
    by `end` date (not by fy/fp — those are the filing's labels and
    restated 10-Qs reuse the same fp for prior-year comparables)."""
    unit = _pick_unit(fact)
    if not unit:
        return []
    rows = fact["units"][unit]
    q_rows = [r for r in rows
               if r.get("fp") in ("Q1", "Q2", "Q3")
               and r.get("form") == "10-Q"]
    by_end: dict[str, dict] = {}
    for r in q_rows:
        span = _fy_span_days(r)
        is_q = span is not None and 60 <= span <= 120
        is_balance = span is None
        if span is not None and not is_q:
            continue  # skip 6mo/9mo cumulative rows
        end = r.get("end")
        if not end:
            continue
        prev = by_end.get(end)
        if prev is None:
            by_end[end] = r
            continue
        if r.get("filed", "") > prev.get("filed", ""):
            by_end[end] = r
    sorted_rows = sorted(by_end.values(), key=lambda r: r.get("end", ""))
    out = []
    for r in sorted_rows:
        ey = _end_year(r)
        # Infer the quarter label from the end-date month (approximate;
        # non-calendar-year filers may use a shifted calendar — labels
        # here are informational, keyed by end anyway).
        end = r.get("end", "")
        try:
            m = int(end[5:7])
        except (ValueError, TypeError):
            m = 0
        fp_label = {3: "Q1", 6: "Q2", 9: "Q3", 12: "Q4"}.get(m,
                                                               r.get("fp", ""))
        out.append({"fy": ey, "fp": fp_label,
                    "end": end, "val": r.get("val"), "unit": unit})
    return out[-8:]


def _resolve_metric(facts_gaap: dict, metric: str
                     ) -> tuple[Optional[list[dict]], Optional[list[dict]],
                                 Optional[str]]:
    """Return (fy_series, q_series, tag_used). For each tag in order,
    build its (fy, q) series; a tag qualifies when its FY series has
    at least one entry whose end date is within the last 2 years, so
    Apple's dormant `Revenues` tag doesn't win over the live
    `RevenueFromContractWithCustomerExcludingAssessedTax`.
    """
    import datetime as _dt
    today = _dt.date.today()
    best: Optional[tuple[list[dict], list[dict], str]] = None
    for tag in XBRL_TAGS[metric]:
        if tag.startswith("__"):
            if tag == "__LongTermDebtNoncurrent+LongTermDebtCurrent":
                nc = facts_gaap.get("LongTermDebtNoncurrent")
                cu = facts_gaap.get("LongTermDebtCurrent")
                if nc and cu:
                    fy_nc = {r["fy"]: r for r in _fy_series(nc)}
                    fy_cu = {r["fy"]: r for r in _fy_series(cu)}
                    fy_combined = []
                    for fy in sorted(set(fy_nc) | set(fy_cu)):
                        n = fy_nc.get(fy, {}).get("val") or 0
                        c = fy_cu.get(fy, {}).get("val") or 0
                        fy_combined.append({"fy": fy, "end":
                            fy_nc.get(fy, fy_cu[fy])["end"],
                            "val": n + c, "unit": "USD"})
                    if fy_combined:
                        return fy_combined[-10:], [], tag
            continue
        if tag not in facts_gaap:
            continue
        fy = _fy_series(facts_gaap[tag])
        q = _q_series(facts_gaap[tag])
        if not fy and not q:
            continue
        # Reject tags whose newest FY row is older than 2 years — a
        # dormant tag (Apple's deprecated Revenues) must not block
        # the live successor.
        latest_end = None
        if fy:
            latest_end = max(r.get("end", "") for r in fy)
        if q:
            latest_q = max(r.get("end", "") for r in q)
            if latest_end is None or latest_q > latest_end:
                latest_end = latest_q
        is_fresh = True
        if latest_end:
            try:
                le = _dt.date.fromisoformat(latest_end)
                is_fresh = (today - le).days <= 730
            except ValueError:
                is_fresh = True
        if is_fresh:
            return fy, q, tag
        if best is None:
            best = (fy, q, tag)
    if best is not None:
        return best
    return None, None, None


# ----------------------------------------------------------------------
# Ticker map
# ----------------------------------------------------------------------

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,6}(\.[A-Z0-9])?$")


def _validate_ticker(raw: str) -> Optional[str]:
    t = (raw or "").strip().upper()
    if not t:
        return None
    if not TICKER_RE.match(t):
        return None
    return t


async def _ticker_map(client: httpx.AsyncClient) -> dict:
    """Returns {TICKER: {cik_str, title}}."""
    url = "https://www.sec.gov/files/company_tickers.json"
    raw = await _sec_fetch_json(url, TTL_TICKER_MAP, client)
    if not raw:
        return {}
    out = {}
    for _, row in raw.items():
        out[row["ticker"].upper()] = row
    return out


# ----------------------------------------------------------------------
# Snapshot builder
# ----------------------------------------------------------------------

def _latest(series: list[dict]) -> Optional[dict]:
    return series[-1] if series else None


def _growth_rates(fy: list[dict]) -> dict:
    """Return revenue 1y, 3y CAGR, 5y CAGR using the FY series."""
    out: dict[str, Optional[float]] = {}
    vals = [(r["fy"], r["val"]) for r in fy if r.get("val") is not None]
    if len(vals) < 2:
        return out
    last_fy, last_v = vals[-1]
    # 1y
    for pfy, pv in reversed(vals[:-1]):
        if pv:
            out["revenue_1y"] = (last_v / pv) - 1
            break
    # 3y CAGR
    for pfy, pv in reversed(vals[:-1]):
        if last_fy - pfy == 3 and pv:
            out["revenue_3y_cagr"] = (last_v / pv) ** (1 / 3) - 1
            break
    # 5y CAGR
    for pfy, pv in reversed(vals[:-1]):
        if last_fy - pfy == 5 and pv:
            out["revenue_5y_cagr"] = (last_v / pv) ** (1 / 5) - 1
            break
    return out


def _build_snapshot(ticker: str, cik_padded: str, submissions: dict,
                     companyfacts: Optional[dict],
                     price: Optional[dict],
                     price_error: Optional[str]) -> dict:
    name = submissions.get("name", ticker)
    exchanges = submissions.get("exchanges", [])
    exchange = exchanges[0] if exchanges else None
    sic = submissions.get("sic")
    sic_desc = submissions.get("sicDescription")

    # Filings — pull first 10 from recent
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates = recent.get("filingDate", [])
    primaries = recent.get("primaryDocument", [])
    filings = []
    for i in range(min(10, len(forms))):
        acc = accs[i]
        acc_nodash = acc.replace("-", "")
        prim = primaries[i] if i < len(primaries) else ""
        url = (f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik_padded)}/{acc_nodash}/{prim}")
        filings.append({"form": forms[i], "filed": dates[i],
                         "accession": acc, "url": url})

    # Insider — count Form 4 within 90 days of latest filing date.
    import datetime as _dt
    def _parse(d: str) -> Optional[_dt.date]:
        try:
            return _dt.date.fromisoformat(d)
        except Exception:
            return None
    today = _dt.date.today()
    insider_count = 0
    for i, f in enumerate(forms):
        if f == "4":
            d = _parse(dates[i])
            if d and (today - d).days <= 90:
                insider_count += 1

    # XBRL metrics
    fy_series_out: dict[int, dict] = {}
    q_series_out: dict[tuple, dict] = {}
    tags_used: dict[str, Optional[str]] = {}
    warnings: list[str] = []

    if companyfacts is None:
        xbrl_block: Optional[dict] = None
        for m in XBRL_TAGS:
            tags_used[m] = None
        warnings.append("No XBRL filings — likely a foreign filer.")
    else:
        gaap = companyfacts.get("facts", {}).get("us-gaap", {})
        xbrl_block = {}
        for metric in XBRL_TAGS:
            fy, q, tag = _resolve_metric(gaap, metric)
            tags_used[metric] = tag
            xbrl_block[metric] = {"tag": tag, "fy": fy, "q": q}
            if fy:
                for r in fy:
                    y = r["fy"]
                    e = fy_series_out.setdefault(y, {"year": y})
                    e[metric] = r["val"]
            if q:
                for r in q:
                    k = r.get("end") or (r["fy"], r["fp"])
                    e = q_series_out.setdefault(k, {
                        "period": f"{r['fp']} {r['fy']}",
                        "end": r.get("end"),
                        "fy": r["fy"], "fp": r["fp"]})
                    e[metric] = r["val"]

    # Build FY list with derived fields (ROE, scaled values).
    fy_list = []
    sorted_years = sorted(fy_series_out.keys())
    for i, y in enumerate(sorted_years):
        row = fy_series_out[y]
        rev = row.get("revenue")
        ni = row.get("net_income")
        eq = row.get("equity")
        prev_eq = fy_series_out.get(y - 1, {}).get("equity") if (y - 1) in fy_series_out else None
        eq_avg = ((eq + prev_eq) / 2) if (eq is not None and prev_eq is not None) else eq
        roe = (ni / eq_avg) if (ni is not None and eq_avg) else None
        fy_list.append({
            "year": y,
            "revenue": _to_m(rev),
            "net_income": _to_m(ni),
            "eps_diluted": row.get("eps_diluted"),
            "shares_diluted": _to_m(row.get("shares_diluted")),
            "equity": _to_m(eq),
            "equity_avg": _to_m(eq_avg) if eq_avg else None,
            "debt": _to_m(row.get("debt")),
            "cash": _to_m(row.get("cash")),
            "cfo": _to_m(row.get("cfo")),
            "capex": _to_m(row.get("capex")),
            "dps": row.get("dps"),
            "interest_expense": _to_m(row.get("interest_expense")),
            "roe": round(roe, 4) if roe is not None else None,
        })

    # Quarterly list — last 8 periods, sorted by end date.
    q_list = []
    sorted_keys = sorted(q_series_out.keys(),
                          key=lambda k: q_series_out[k].get("end") or "")
    for k in sorted_keys:
        row = q_series_out[k]
        q_list.append({
            "period": row["period"],
            "end": row.get("end"),
            "revenue": _to_m(row.get("revenue")),
            "eps_diluted": row.get("eps_diluted"),
            "interest_expense": _to_m(row.get("interest_expense")),
        })
    q_list = q_list[-8:]

    # Latest diluted shares & market cap
    shares_latest = None
    if fy_list:
        for row in reversed(fy_list):
            if row.get("shares_diluted") is not None:
                shares_latest = row["shares_diluted"]
                break
    market_cap = None
    if shares_latest is not None and price and price.get("last"):
        market_cap = round(shares_latest * price["last"], 2)

    # Valuation metrics
    val: dict = {"tooltips": {
        "pe_ttm": "Price divided by last four quarters of diluted EPS",
        "pb": "Price divided by stockholders' equity per diluted share",
        "div_yield": "Trailing four-quarter DPS divided by price",
        "net_debt_to_equity":
            "(Long-term debt minus cash) divided by stockholders' equity",
    }}
    last_price = price.get("last") if price else None
    eps_ttm: Optional[float] = None
    if q_list:
        eps_vals = [q.get("eps_diluted") for q in q_list
                    if q.get("eps_diluted") is not None]
        if len(eps_vals) >= 4:
            eps_ttm = sum(eps_vals[-4:])
    if eps_ttm is None and fy_list:
        for row in reversed(fy_list):
            if row.get("eps_diluted") is not None:
                eps_ttm = row["eps_diluted"]
                break
    if last_price and eps_ttm:
        val["pe_ttm"] = round(last_price / eps_ttm, 2)
    else:
        val["pe_ttm"] = None

    latest_eq = fy_list[-1].get("equity") if fy_list else None
    if last_price and latest_eq and shares_latest:
        bvps = latest_eq / shares_latest
        if bvps:
            val["pb"] = round(last_price / bvps, 2)
    else:
        val["pb"] = None

    # Dividend yield — trailing 4-q DPS if present, else latest FY DPS.
    dps_sum = None
    if fy_list:
        last_dps = fy_list[-1].get("dps")
        if last_dps is not None:
            dps_sum = last_dps
    if last_price and dps_sum:
        val["div_yield"] = round(dps_sum / last_price, 4)
    else:
        val["div_yield"] = None

    latest_debt = fy_list[-1].get("debt") if fy_list else None
    latest_cash = fy_list[-1].get("cash") if fy_list else None
    if (latest_debt is not None and latest_eq):
        nd = (latest_debt or 0) - (latest_cash or 0)
        val["net_debt_to_equity"] = round(nd / latest_eq, 2) if latest_eq else None
    else:
        val["net_debt_to_equity"] = None

    growth = _growth_rates([{"fy": r["year"], "val": r.get("revenue")}
                            for r in fy_list if r.get("revenue") is not None])

    out = {
        "ticker": ticker,
        "cik": cik_padded,
        "name": name,
        "exchange": exchange,
        "sic": sic,
        "sic_description": sic_desc,
        "price": price,
        "price_error": price_error,
        "shares_diluted_latest": shares_latest,
        "market_cap": market_cap,
        "valuation": val,
        "fy_series": fy_list,
        "quarterly_series": q_list,
        "filings": filings,
        "insider_count_90d": insider_count,
        "tags_used": tags_used,
        "growth": growth,
        "warnings": warnings,
    }
    if companyfacts is None:
        out["xbrl"] = None
        out["message"] = "No XBRL filings — likely a foreign filer."
    return out


def _to_m(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return round(v / 1_000_000, 2)
    except (TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# FastAPI
# ----------------------------------------------------------------------

app = FastAPI(title="BMG Capital site + snapshot API")

HTTP_CLIENT: Optional[httpx.AsyncClient] = None


@app.on_event("startup")
async def _on_startup() -> None:
    global HTTP_CLIENT
    HTTP_CLIENT = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("startup: cache dir %s", CACHE_DIR)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global HTTP_CLIENT
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True}


@app.get("/api/snapshot")
async def snapshot(ticker: str = Query(..., min_length=1,
                                         max_length=10)) -> JSONResponse:
    assert HTTP_CLIENT is not None
    t = _validate_ticker(ticker)
    if not t:
        return JSONResponse(status_code=400,
                            content={"error": "invalid ticker"})

    tmap = await _ticker_map(HTTP_CLIENT)
    if t not in tmap:
        return JSONResponse(status_code=404, content={
            "error": "unknown ticker",
            "message": "Not in SEC US issuer map. Foreign filers and OTC "
                        "may be missing.",
        })
    row = tmap[t]
    cik_padded = f"{int(row['cik_str']):010d}"

    sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    cf_url = (f"https://data.sec.gov/api/xbrl/companyfacts/"
                f"CIK{cik_padded}.json")

    submissions = await _sec_fetch_json(sub_url, TTL_SUBMISSIONS,
                                          HTTP_CLIENT)
    if submissions is None:
        return JSONResponse(status_code=502, content={
            "error": "sec submissions fetch failed"})

    companyfacts = await _sec_fetch_json(cf_url, TTL_COMPANYFACTS,
                                           HTTP_CLIENT)

    # Price (never blocks the snapshot).
    price, price_error = await _stooq_fetch_price(t, HTTP_CLIENT)

    snap = _build_snapshot(t, cik_padded, submissions, companyfacts,
                            price, price_error)
    return JSONResponse(content=snap)


# ----------------------------------------------------------------------
# Cache-Control middleware (replace Caddy header logic)
# ----------------------------------------------------------------------

@app.middleware("http")
async def _cache_control(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    # Never set cache headers on API paths — those are JSON responses
    # that should always revalidate.
    if path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
        return response
    # HTML + bare directory URLs → no-cache
    if (path.endswith(".html") or path == "/" or path.endswith("/")):
        response.headers["Cache-Control"] = "no-cache"
        return response
    # Legacy stylesheet copies
    if path in ("/site.css", "/house-style.css"):
        response.headers["Cache-Control"] = "no-cache"
        return response
    # Hashed stylesheet names
    if re.match(r"^/(site|house-style)\.[0-9a-f]{8}\.css$", path):
        response.headers["Cache-Control"] = \
            "public, max-age=31536000, immutable"
        return response
    # Documents that rebuild in place
    if path.endswith((".pdf", ".xlsx", ".pptx", ".docx")):
        response.headers["Cache-Control"] = "no-cache"
        return response
    # Everything else static (images, fonts, hashed/non-hashed JS, SVG)
    if path.endswith((".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif",
                       ".ico", ".woff", ".woff2", ".js")):
        response.headers["Cache-Control"] = \
            "public, max-age=31536000, immutable"
        return response
    return response


# ----------------------------------------------------------------------
# StaticFiles mount — serves every current static URL at /
#
# Must come AFTER all /api routes so FastAPI dispatches /api/* before
# falling back to the static mount.
# ----------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_ROOT), html=True),
          name="static")
