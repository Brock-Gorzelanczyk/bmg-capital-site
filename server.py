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
TTL_PRICES = 1 * 3600         # 1h — success
TTL_PRICES_FAIL = 10 * 60     # 10 min — negative cache for failed source
PRICE_TIMEOUT_S = 4.0         # per-call price fetch timeout (hard cap)

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


# Negative cache for failed price sources. Keyed by a synthetic
# "neg:<url>" string so success and failure cache rows do not collide.
def _neg_cache_read(url: str, ttl: int) -> Optional[str]:
    p = _cache_path("neg:" + url)
    if not p.exists():
        return None
    try:
        obj = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    fetched = obj.get("__fetched_at__", 0)
    if time.time() - fetched > ttl:
        return None
    reason = obj.get("reason")
    return reason if isinstance(reason, str) else None


def _neg_cache_write(url: str, reason: str) -> None:
    p = _cache_path("neg:" + url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"__fetched_at__": time.time(),
                              "reason": reason}))


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


BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/537.36 (KHTML, like Gecko) "
               "Chrome/126.0.0.0 Safari/537.36")


async def _finnhub_fetch_price(ticker: str,
                                client: httpx.AsyncClient
                                ) -> tuple[Optional[dict], Optional[str]]:
    """Finnhub free-tier quote endpoint. Needs FINNHUB_API_KEY env var.
    Never commit, never log, never send to the browser. If the env var
    is missing, skip Finnhub silently without constructing the URL and
    return a 'finnhub key missing' reason.
    Cache successes 15 minutes; cache failures 10 minutes.
    """
    key = os.environ.get("FINNHUB_API_KEY", "").strip()
    if not key:
        # Do NOT construct the URL; do NOT contact the service. Return
        # a reason containing the exact phrase 'finnhub key missing' so
        # operators detect WAITING-ON-KEY state from the response body
        # (which never contains the key itself).
        return None, "finnhub key missing"
    # Cache key is keyed on ticker only — URL contains the secret and
    # must never be persisted to disk.
    cache_key = f"finnhub-quote::{ticker.upper()}"
    cached = _cache_read(cache_key, 15 * 60)
    if cached is not None:
        log.info("CACHE HIT finnhub %s", ticker)
        return cached, None
    neg = _neg_cache_read(cache_key, TTL_PRICES_FAIL)
    if neg is not None:
        log.info("NEG-CACHE HIT finnhub %s (%s)", ticker, neg)
        return None, neg
    log.info("CACHE MISS finnhub %s", ticker)
    url = f"https://finnhub.io/api/v1/quote?symbol={ticker}&token={key}"
    try:
        r = await client.get(url, headers={"User-Agent": BROWSER_UA})
    except httpx.HTTPError as e:
        # Scrub the key out of any error string in case httpx echoed
        # the URL — belt-and-braces so the key never reaches a log.
        msg = str(e).replace(key, "<redacted>") if key else str(e)
        reason = f"finnhub fetch failed: {e.__class__.__name__}: {msg}" \
            if msg else f"finnhub timeout after {PRICE_TIMEOUT_S}s"
        _neg_cache_write(cache_key, reason)
        return None, reason
    if r.status_code != 200:
        reason = f"finnhub http {r.status_code}"
        _neg_cache_write(cache_key, reason)
        return None, reason
    try:
        obj = r.json()
    except Exception as e:
        reason = f"finnhub json parse error: {e}"
        _neg_cache_write(cache_key, reason)
        return None, reason
    price_val = obj.get("c")
    ts = obj.get("t")
    if price_val is None or price_val == 0:
        # Finnhub returns c=0 for unknown tickers instead of 404.
        reason = "finnhub returned no price (c=0 or missing)"
        _neg_cache_write(cache_key, reason)
        return None, reason
    import datetime as _dt
    try:
        if ts:
            date_s = _dt.datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
        else:
            date_s = _dt.date.today().isoformat()
    except (ValueError, TypeError, OSError):
        date_s = _dt.date.today().isoformat()
    body = {"last": float(price_val), "date": date_s,
             "source": "Finnhub (delayed)"}
    _cache_write(cache_key, body)
    return body, None


async def _yahoo_fetch_price(ticker: str,
                              client: httpx.AsyncClient
                              ) -> tuple[Optional[dict], Optional[str]]:
    """Fetch latest regularMarketPrice from Yahoo chart endpoint. Delayed
    15 minutes but free and keyless. Negative-cached on failure for
    TTL_PRICES_FAIL so a dead source does not slow every subsequent
    request."""
    url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
            f"?range=5d&interval=1d")
    cached = _cache_read(url, TTL_PRICES)
    if cached is not None:
        log.info("CACHE HIT %s", url)
        return cached, None
    neg = _neg_cache_read(url, TTL_PRICES_FAIL)
    if neg is not None:
        log.info("NEG-CACHE HIT %s (%s)", url, neg)
        return None, neg
    log.info("CACHE MISS %s", url)
    try:
        r = await client.get(url, headers={"User-Agent": BROWSER_UA})
    except httpx.HTTPError as e:
        reason = f"yahoo fetch failed: {e.__class__.__name__}: {e}" \
            if str(e) else f"yahoo timeout after {PRICE_TIMEOUT_S}s"
        _neg_cache_write(url, reason)
        return None, reason
    if r.status_code != 200:
        reason = f"yahoo http {r.status_code}"
        _neg_cache_write(url, reason)
        return None, reason
    try:
        obj = r.json()
    except Exception as e:
        reason = f"yahoo json parse error: {e}"
        _neg_cache_write(url, reason)
        return None, reason
    try:
        result = obj["chart"]["result"][0]
        meta = result["meta"]
        price_val = meta.get("regularMarketPrice")
        timestamps = result.get("timestamp") or []
        if price_val is None:
            reason = "yahoo returned no regularMarketPrice"
            _neg_cache_write(url, reason)
            return None, reason
        import datetime as _dt
        if timestamps:
            ts = int(timestamps[-1])
            date_s = _dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        else:
            # Fallback: use regularMarketTime from meta
            mt = meta.get("regularMarketTime")
            if mt:
                date_s = _dt.datetime.utcfromtimestamp(int(mt)).strftime(
                    "%Y-%m-%d")
            else:
                date_s = _dt.date.today().isoformat()
    except (KeyError, IndexError, TypeError) as e:
        reason = f"yahoo payload shape error: {e}"
        _neg_cache_write(url, reason)
        return None, reason
    body = {"last": float(price_val), "date": date_s,
             "source": "Yahoo chart (delayed)"}
    _cache_write(url, body)
    return body, None


async def _stooq_fetch_price(ticker: str,
                              client: httpx.AsyncClient
                              ) -> tuple[Optional[dict], Optional[str]]:
    url = f"https://stooq.com/q/d/l/?s={ticker.lower()}.us&i=d"
    cached = _cache_read(url, TTL_PRICES)
    if cached is not None:
        log.info("CACHE HIT %s", url)
        return cached, None
    neg = _neg_cache_read(url, TTL_PRICES_FAIL)
    if neg is not None:
        log.info("NEG-CACHE HIT %s (%s)", url, neg)
        return None, neg
    log.info("CACHE MISS %s", url)
    try:
        r = await client.get(url, headers={
            "User-Agent": "Mozilla/5.0 (compatible; BMG Capital research)"
        })
    except httpx.HTTPError as e:
        reason = f"stooq fetch failed: {e.__class__.__name__}: {e}" \
            if str(e) else f"stooq timeout after {PRICE_TIMEOUT_S}s"
        _neg_cache_write(url, reason)
        return None, reason
    if r.status_code != 200:
        reason = f"stooq http {r.status_code}"
        _neg_cache_write(url, reason)
        return None, reason
    text = r.text
    # Stooq returns CSV; if it returns HTML (anti-bot), fail gracefully.
    if not text.startswith("Date,"):
        reason = "stooq returned non-CSV (anti-bot shield)"
        _neg_cache_write(url, reason)
        return None, reason
    rows = text.strip().splitlines()
    if len(rows) < 2:
        reason = "stooq returned empty CSV"
        _neg_cache_write(url, reason)
        return None, reason
    last = rows[-1].split(",")
    try:
        close = float(last[4])
        date = last[0]
    except (ValueError, IndexError):
        reason = "stooq CSV parse error"
        _neg_cache_write(url, reason)
        return None, reason
    body = {"last": close, "date": date, "source": "Stooq daily"}
    _cache_write(url, body)
    return body, None


async def _fetch_price(ticker: str
                        ) -> tuple[Optional[dict], Optional[str]]:
    """Fetch latest price. Finnhub first (needs FINNHUB_API_KEY env),
    Yahoo second, Stooq third. Uses its OWN httpx client with a 4s
    per-call timeout — never reuses the SEC client because SEC fetches
    may take much longer and we must not block the overall request on
    a dead price source. Negative-cached on failure for 10 minutes.
    """
    timeout = httpx.Timeout(PRICE_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout,
                                   follow_redirects=True) as client:
        price, finnhub_err = await _finnhub_fetch_price(ticker, client)
        if price is not None:
            return price, None
        price, yahoo_err = await _yahoo_fetch_price(ticker, client)
        if price is not None:
            return price, None
        price, stooq_err = await _stooq_fetch_price(ticker, client)
        if price is not None:
            return price, None
    # All failed — return a combined reason so callers can see which
    # sources were tried. The "finnhub key missing" phrase (if present)
    # is the detect-signal for operators that the env var is unset.
    finnhub_err = finnhub_err or "finnhub: no result"
    yahoo_err = yahoo_err or "yahoo: no result"
    stooq_err = stooq_err or "stooq: no result"
    return None, f"{finnhub_err}; {yahoo_err}; {stooq_err}"


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
                        if fy in fy_nc:
                            end_date = fy_nc[fy]["end"]
                        else:
                            end_date = fy_cu[fy]["end"]
                        fy_combined.append({"fy": fy, "end": end_date,
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

TICKER_RE = re.compile(r"^[A-Z][A-Z0-9]{0,6}([\.\-][A-Z0-9])?$")


def _validate_ticker(raw: str) -> Optional[str]:
    t = (raw or "").strip().upper()
    if not t:
        return None
    if not TICKER_RE.match(t):
        return None
    return t


def _ticker_variants(t: str) -> list[str]:
    """BRK.B and BRK-B both resolve to the same SEC CIK; try both forms."""
    variants = [t]
    if "." in t:
        variants.append(t.replace(".", "-"))
    if "-" in t:
        variants.append(t.replace("-", "."))
    seen = set()
    out = []
    for v in variants:
        if v not in seen:
            seen.add(v)
            out.append(v)
    return out


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

    # --- Inputs block for manual-price frontend fallback -----------------
    # When price.last is null the browser offers an "enter a price" input
    # and recomputes P/E / P/B / yield / market_cap client-side. These
    # fields are the raw inputs that math needs; they are also useful as
    # evidence for the live-data path.
    #
    # ttm_eps: sum of last 4 quarterly diluted EPS, or null if < 4.
    # bvps:    latest equity (dollars) / latest diluted shares (shares).
    #          Cites which quarter the equity came from.
    # ttm_dps: sum of last 4 quarterly DPS; else trailing-12mo-from-FY.
    inputs_ttm_eps: Optional[float] = None
    if q_list:
        eps_vals_full = [q.get("eps_diluted") for q in q_list
                         if q.get("eps_diluted") is not None]
        if len(eps_vals_full) >= 4:
            inputs_ttm_eps = round(sum(eps_vals_full[-4:]), 4)

    # BVPS: use raw (unscaled) equity + raw diluted shares from the
    # underlying q_series_out / fy_series_out so dollars-per-share is
    # computed correctly. fy_list stores both in millions, which cancels
    # fine arithmetically, but we also want to report the "basis quarter"
    # so cite the latest quarter equity if available, else latest FY.
    inputs_bvps: Optional[float] = None
    inputs_bvps_basis: Optional[str] = None
    # Prefer the newest quarter that has BOTH equity AND diluted shares.
    sorted_qkeys = sorted(q_series_out.keys(),
                           key=lambda k: q_series_out[k].get("end") or "")
    for k in reversed(sorted_qkeys):
        row = q_series_out[k]
        eq_q = row.get("equity")
        sh_q = row.get("shares_diluted")
        if eq_q is not None and sh_q:
            inputs_bvps = round(eq_q / sh_q, 4)
            inputs_bvps_basis = (f"{row.get('period','')} "
                                   f"(end {row.get('end','')})").strip()
            break
    if inputs_bvps is None and fy_list:
        # Fall back to latest FY from the raw dicts.
        last_year = sorted_years[-1] if sorted_years else None
        if last_year is not None:
            raw = fy_series_out.get(last_year, {})
            eq_fy = raw.get("equity")
            sh_fy = raw.get("shares_diluted")
            if eq_fy is not None and sh_fy:
                inputs_bvps = round(eq_fy / sh_fy, 4)
                inputs_bvps_basis = f"FY{last_year}"

    # TTM DPS: trailing 4 quarterly DPS if we have them; else latest FY
    # DPS as a 12-month proxy.
    inputs_ttm_dps: Optional[float] = None
    q_dps_vals = []
    for k in sorted_qkeys:
        row = q_series_out[k]
        dps_q = row.get("dps")
        if dps_q is not None:
            q_dps_vals.append(dps_q)
    if len(q_dps_vals) >= 4:
        inputs_ttm_dps = round(sum(q_dps_vals[-4:]), 4)
    elif fy_list:
        last_fy_dps = fy_list[-1].get("dps")
        if last_fy_dps is not None:
            inputs_ttm_dps = round(float(last_fy_dps), 4)

    inputs = {
        "ttm_eps": inputs_ttm_eps,
        "bvps": inputs_bvps,
        "bvps_basis": inputs_bvps_basis,
        "ttm_dps": inputs_ttm_dps,
        "shares_diluted_latest": shares_latest,
    }

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
        "inputs": inputs,
        "ttm_eps": inputs_ttm_eps,
        "bvps": inputs_bvps,
        "ttm_dps": inputs_ttm_dps,
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
    hit = None
    for variant in _ticker_variants(t):
        if variant in tmap:
            hit = tmap[variant]
            break
    if hit is None:
        return JSONResponse(status_code=404, content={
            "error": "unknown ticker",
            "message": "Not in SEC US issuer map. Foreign filers and OTC "
                        "may be missing.",
        })
    row = hit
    cik_padded = f"{int(row['cik_str']):010d}"

    sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    cf_url = (f"https://data.sec.gov/api/xbrl/companyfacts/"
                f"CIK{cik_padded}.json")

    # Parallel: SEC submissions + companyfacts + price. Price uses its
    # OWN httpx client (4s cap) so a dead price source does not slow
    # SEC fetches — see _fetch_price.
    sub_task = asyncio.create_task(
        _sec_fetch_json(sub_url, TTL_SUBMISSIONS, HTTP_CLIENT))
    cf_task = asyncio.create_task(
        _sec_fetch_json(cf_url, TTL_COMPANYFACTS, HTTP_CLIENT))
    price_task = asyncio.create_task(_fetch_price(t))

    submissions, companyfacts, price_result = await asyncio.gather(
        sub_task, cf_task, price_task)
    price, price_error = price_result

    if submissions is None:
        return JSONResponse(status_code=502, content={
            "error": "sec submissions fetch failed"})

    snap = _build_snapshot(t, cik_padded, submissions, companyfacts,
                            price, price_error)
    return JSONResponse(content=snap)


# ----------------------------------------------------------------------
# Fact-Checker (/api/check) — Tool-2 Parts 1 to 3
# ----------------------------------------------------------------------

from fastapi import UploadFile, File, Form
import fact_checker as fc

CHECK_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
CHECK_RL_WINDOW_S = 60
CHECK_RL_MAX = 6  # per IP per window
_check_rl_hits: dict[str, list[float]] = {}


def _rate_limit_check(ip: str) -> Optional[str]:
    now = time.monotonic()
    hits = _check_rl_hits.get(ip, [])
    hits = [t for t in hits if now - t < CHECK_RL_WINDOW_S]
    if len(hits) >= CHECK_RL_MAX:
        _check_rl_hits[ip] = hits
        return (f"rate limit: max {CHECK_RL_MAX} checks per "
                f"{CHECK_RL_WINDOW_S}s per IP; try again shortly")
    hits.append(now)
    _check_rl_hits[ip] = hits
    return None


def _detect_kind(filename: str, content_type: str) -> Optional[str]:
    f = (filename or "").lower()
    c = (content_type or "").lower()
    if f.endswith(".pdf") or "pdf" in c:
        return "pdf"
    if f.endswith(".docx") or "wordprocessingml" in c:
        return "docx"
    if f.endswith(".pptx") or "presentationml" in c:
        return "pptx"
    if f.endswith(".html") or f.endswith(".htm") or "html" in c:
        return "html"
    if f.endswith(".txt") or "plain" in c:
        return "text"
    return None


async def _filing_text(acc: str, cik_padded: str, primary: str,
                       client: httpx.AsyncClient) -> str:
    """Fetch and strip a filing's primary document. Cached per URL."""
    acc_nodash = acc.replace("-", "")
    url = (f"https://www.sec.gov/Archives/edgar/data/"
            f"{int(cik_padded)}/{acc_nodash}/{primary}")
    cache_key = f"filing-text::{url}"
    cached = _cache_read(cache_key, TTL_COMPANYFACTS)
    if cached is not None:
        log.info("CACHE HIT filing-text %s", url)
        return cached
    log.info("CACHE MISS filing-text %s", url)
    await SEC_LIMITER.acquire()
    try:
        r = await client.get(url, headers={"User-Agent": SEC_USER_AGENT},
                              timeout=30.0)
        if r.status_code != 200:
            return ""
        html_text = r.text
    except httpx.HTTPError:
        return ""
    from bs4 import BeautifulSoup
    try:
        s = BeautifulSoup(html_text, "html.parser")
        for t in s(["script", "style"]):
            t.decompose()
        text = s.get_text(separator=" ")
        text = re.sub(r"[ \t]+", " ", text)
    except Exception:
        text = html_text
    _cache_write(cache_key, text)
    return text


def _xbrl_series_for(companyfacts: Optional[dict]) -> dict:
    """Build {line_item: {fy:[...], q:[...]}} from companyfacts."""
    out: dict[str, dict] = {}
    if not companyfacts:
        return out
    gaap = companyfacts.get("facts", {}).get("us-gaap", {})
    for metric in XBRL_TAGS:
        fy, q, tag = _resolve_metric(gaap, metric)
        out[metric] = {"fy": fy or [], "q": q or [], "tag": tag}
    return out


@app.post("/api/check")
async def check_report(request: Request,
                        file: Optional[UploadFile] = File(None),
                        text: Optional[str] = Form(None),
                        ticker: str = Form(...)) -> JSONResponse:
    """Report Fact-Checker. Multipart form with either `file` upload or
    `text` paste, plus `ticker`. Returns four buckets + diff.
    Uploads are read into memory and NEVER written to disk."""
    assert HTTP_CLIENT is not None
    ip = request.client.host if request.client else "unknown"
    rl_err = _rate_limit_check(ip)
    if rl_err:
        return JSONResponse(status_code=429, content={"error": rl_err})

    t = _validate_ticker(ticker)
    if not t:
        return JSONResponse(status_code=400,
                            content={"error": "invalid ticker"})

    # Resolve ticker (BRK.B/BRK-B both OK)
    tmap = await _ticker_map(HTTP_CLIENT)
    hit = None
    for variant in _ticker_variants(t):
        if variant in tmap:
            hit = tmap[variant]
            break
    if hit is None:
        return JSONResponse(status_code=404, content={
            "error": "unknown ticker",
            "message": "Not in SEC US issuer map."})
    cik_padded = f"{int(hit['cik_str']):010d}"

    # Read bytes (file or pasted text). In-memory only, no persistence.
    kind: Optional[str] = None
    data: bytes = b""
    source_label = ""
    if file is not None:
        contents = await file.read()
        if len(contents) > CHECK_MAX_BYTES:
            return JSONResponse(status_code=413, content={
                "error": f"file too large: {len(contents)} bytes "
                         f"(cap {CHECK_MAX_BYTES})"})
        kind = _detect_kind(file.filename or "", file.content_type or "")
        if kind is None:
            return JSONResponse(status_code=400, content={
                "error": "unsupported file type; use PDF, DOCX, PPTX, HTML "
                         "or paste text"})
        data = contents
        source_label = file.filename or "upload"
    elif text:
        text_bytes = text.encode("utf-8", errors="replace")
        if len(text_bytes) > CHECK_MAX_BYTES:
            return JSONResponse(status_code=413, content={
                "error": f"pasted text too large: {len(text_bytes)} bytes"})
        kind = "text"
        data = text_bytes
        source_label = "pasted text"
    else:
        return JSONResponse(status_code=400, content={
            "error": "provide either a file upload or text form field"})

    # Extract (in-memory). The bytes are discarded as `data` goes out of scope.
    try:
        pages = fc.extract(data, kind)
    except Exception as e:
        return JSONResponse(status_code=400, content={
            "error": f"could not extract text: {e.__class__.__name__}: {e}"})

    # Filings + XBRL (reuse the snapshot code paths)
    sub_url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    cf_url = (f"https://data.sec.gov/api/xbrl/companyfacts/"
                f"CIK{cik_padded}.json")
    submissions, companyfacts = await asyncio.gather(
        _sec_fetch_json(sub_url, TTL_SUBMISSIONS, HTTP_CLIENT),
        _sec_fetch_json(cf_url, TTL_COMPANYFACTS, HTTP_CLIENT))
    if submissions is None:
        return JSONResponse(status_code=502, content={
            "error": "sec submissions fetch failed"})

    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs = recent.get("accessionNumber", [])
    dates_ = recent.get("filingDate", [])
    primaries = recent.get("primaryDocument", [])
    items_ = recent.get("items", [])
    # Build filings list, 12-month 8-K lookback plus last 2 10-Q and last 10-K
    import datetime as _dt
    today = _dt.date.today()
    twelve_mo_ago = today - _dt.timedelta(days=365)
    recent_filings: list[dict] = []
    last_10q = []
    last_10k = None
    for i in range(len(forms)):
        form = forms[i]
        try:
            fd = _dt.date.fromisoformat(dates_[i])
        except Exception:
            continue
        prim = primaries[i] if i < len(primaries) else ""
        acc = accs[i]
        acc_nodash = acc.replace("-", "")
        url = (f"https://www.sec.gov/Archives/edgar/data/"
                f"{int(cik_padded)}/{acc_nodash}/{prim}")
        row = {"form": form, "filed": dates_[i], "accession": acc,
               "primary": prim, "url": url,
               "items": items_[i] if i < len(items_) else ""}
        if form == "8-K" and fd >= twelve_mo_ago:
            recent_filings.append(row)
        if form == "10-Q" and len(last_10q) < 2:
            last_10q.append(row)
        if form == "10-K" and last_10k is None:
            last_10k = row
    if last_10k:
        recent_filings.append(last_10k)
    recent_filings.extend(last_10q)

    # Claim extraction
    claims = fc.extract_claims(pages)
    # Reported-line-item series
    xbrl_series = _xbrl_series_for(companyfacts)

    # Match each claim
    bucket_match: list[dict] = []
    bucket_mismatch: list[dict] = []
    bucket_not_in: list[dict] = []
    for cm in claims:
        res = fc.match_claim_to_filings(cm, xbrl_series)
        row = {**cm, **res}
        # Attach a citation for MATCH/MISMATCH so no row is uncited.
        if res.get("status") in ("MATCH", "MISMATCH"):
            line = res.get("line_item")
            tag = xbrl_series.get(line, {}).get("tag") if line else None
            row["filing_citation"] = {
                "basis": "SEC XBRL companyfacts (us-gaap)",
                "xbrl_tag": tag,
                "period": res.get("filing_period"),
                "end": res.get("filing_end"),
                "filing_value_scaled": res.get("filing_value"),
            }
        if res["status"] == "MATCH":
            bucket_match.append(row)
        elif res["status"] == "MISMATCH":
            bucket_mismatch.append(row)
        else:
            bucket_not_in.append(row)

    # Staleness: compare doc periods to filings
    all_doc_periods: list[str] = []
    for cm in claims:
        all_doc_periods.extend(cm.get("periods", []))
    stale_filings = fc.staleness(all_doc_periods, recent_filings)

    # Section diff: last 2 10-Qs (if we have both)
    diff_blob: dict = {}
    latest_10q_text = ""
    if last_10q:
        latest_10q_text = await _filing_text(
            last_10q[0]["accession"], cik_padded,
            last_10q[0]["primary"], HTTP_CLIENT)
    if len(last_10q) == 2:
        old_text = await _filing_text(last_10q[1]["accession"], cik_padded,
                                       last_10q[1]["primary"], HTTP_CLIENT)
        if latest_10q_text and old_text:
            diff_blob = fc.section_diff(latest_10q_text, old_text)

    # Attach up to 2 10-Q snippets to NOT_IN_FILINGS debt-mapped rows so
    # the user sees what the latest filing actually says about debt. This
    # is the "NOT IN FILINGS with the 10-Q snippet beside it" the WO
    # calls out for the GABX "$3B term loan" case.
    if latest_10q_text and last_10q:
        import re as _re
        def _snips(keyword: str) -> list[str]:
            sents = _re.split(r"(?<=[\.!?])\s+(?=[A-Z\$\(])",
                              latest_10q_text)
            out_s = []
            for s in sents:
                if keyword.lower() in s.lower():
                    s_norm = _re.sub(r"\s+", " ", s).strip()
                    if 40 < len(s_norm) < 400:
                        out_s.append(s_norm)
                        if len(out_s) >= 2:
                            break
            return out_s
        for r in bucket_not_in:
            ctx = (r.get("context") or "").lower()
            raw = (r.get("raw") or "")
            has_gabx = "gabx" in ctx
            has_term_loan = "term loan" in ctx
            has_floating = "floating" in ctx and ("debt" in ctx or "term loan" in ctx)
            if (r.get("line_item") == "debt"
                    or has_gabx or has_term_loan or has_floating):
                snips: list[str] = []
                if has_gabx or has_term_loan or has_floating:
                    snips = _snips("term loan")
                if not snips and r.get("line_item") == "debt":
                    snips = _snips("long-term debt")
                if snips:
                    r["filing_snippet"] = {
                        "source": f"{last_10q[0]['form']} filed "
                                   f"{last_10q[0]['filed']}",
                        "url": last_10q[0]["url"],
                        "sentences": snips,
                    }

    out = {
        "ticker": t,
        "source": source_label,
        "pages": len(pages),
        "claims_extracted": len(claims),
        "buckets": {
            "MATCH": bucket_match,
            "MISMATCH": bucket_mismatch,
            "NOT_IN_FILINGS": bucket_not_in,
            "STALE": stale_filings,
        },
        "counts": {
            "MATCH": len(bucket_match),
            "MISMATCH": len(bucket_mismatch),
            "NOT_IN_FILINGS": len(bucket_not_in),
            "STALE": len(stale_filings),
        },
        "diff": diff_blob,
        "notes": {
            "storage": "Uploaded bytes are processed in memory and not "
                        "stored on disk.",
            "size_cap_bytes": CHECK_MAX_BYTES,
            "rate_limit": f"{CHECK_RL_MAX} checks per {CHECK_RL_WINDOW_S}s "
                           "per IP.",
            "what_this_cannot_check": [
                "ratings and rating agency opinions",
                "FactSet consensus or any sell-side estimate",
                "stock prices and quote-derived ratios",
                "your own model outputs (BMG v32, v37, v38, etc)",
            ],
        },
    }
    return JSONResponse(content=out)


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
