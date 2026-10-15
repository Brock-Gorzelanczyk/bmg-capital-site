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

# Optional: /desk intake bot. Import is wrapped so the main site still deploys
# if the extra dependencies (aiolimiter, tenacity, edgartools, feedparser,
# trafilatura, pdfplumber, APScheduler, PyYAML) are not yet in requirements.txt.
_INTAKE_AVAILABLE = False
try:
    from intake.api import router as _desk_router  # type: ignore
    from intake.scheduler import start_scheduler as _start_scheduler, stop_scheduler as _stop_scheduler  # type: ignore
    app.include_router(_desk_router)
    _INTAKE_AVAILABLE = True
    log.info("intake package mounted at /desk")
except Exception as _ie:  # pragma: no cover - defensive
    log.info("intake package not mounted: %s", _ie)


@app.on_event("startup")
async def _on_startup() -> None:
    global HTTP_CLIENT
    HTTP_CLIENT = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    log.info("startup: cache dir %s", CACHE_DIR)
    # BMG_LOCAL=1 (start-reader.command on the owner's Mac): no background intake jobs
    if _INTAKE_AVAILABLE and os.environ.get("BMG_LOCAL") != "1":
        try:
            _start_scheduler()
        except Exception as e:
            log.warning("intake scheduler failed to start: %s", e)


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    global HTTP_CLIENT
    if HTTP_CLIENT:
        await HTTP_CLIENT.aclose()
    if _INTAKE_AVAILABLE and os.environ.get("BMG_LOCAL") != "1":
        try:
            _stop_scheduler()
        except Exception:
            pass


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

# 10 MB on the public site; 50 MB on the owner's Mac (BMG_LOCAL=1), where JPMorgan's 12.9 MB
# and Prologis's 11.5 MB 10-Ks must fit (known issue #46).
CHECK_MAX_BYTES = (50 if os.environ.get("BMG_LOCAL") == "1" else 10) * 1024 * 1024
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
    if f.endswith(".txt") or f.endswith(".md") or "plain" in c or "markdown" in c:
        return "text"
    return None


def _sniff_ok(kind: str, data: bytes) -> bool:
    """The bytes look like the declared type (a renamed file is refused)."""
    head = data[:2048].lstrip()
    if kind == "pdf":
        return data[:1024].find(b"%PDF-") >= 0
    if kind in ("docx", "pptx"):
        return data[:2] == b"PK"
    if kind == "html":
        return b"<" in head
    if kind == "text":
        return b"\x00" not in data[:4096]
    return False


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
    # Railway's edge gives request.client.host different values per request,
    # so the limiter sees each POST as a different IP. Honor X-Forwarded-For
    # (first hop = real client) so the per-IP rule actually binds.
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip()
          if xff
          else (request.client.host if request.client else "unknown"))
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

    # Claim extraction (numeric + text)
    claims = fc.extract_claims(pages)
    text_claims = fc.extract_text_claims(pages)

    # Precompute derived metrics once per request.
    derived = fc.derived_metrics(companyfacts or {})

    # TOOL-3: load the latest 10-K and 10-Q full text up front for the
    # text-fact scanner. Already-cached files are near-free.
    text_10k = ""
    if last_10k:
        text_10k = await _filing_text(last_10k["accession"], cik_padded,
                                        last_10k["primary"], HTTP_CLIENT)
    text_10q_latest = ""
    if last_10q:
        text_10q_latest = await _filing_text(
            last_10q[0]["accession"], cik_padded,
            last_10q[0]["primary"], HTTP_CLIENT)

    # TOOL-3: if the doc mentions an LPI trough or 2017/2018 reference,
    # fetch the oldest in-submissions 10-K so we can match historical
    # trough percent values (FY2019 10-K says -28.2% for 2017). SEC's
    # recent block holds a sliding window; older filings sit in
    # submissions.filings.files pointing to extension JSONs.
    historical_10k_texts: list[str] = []
    all_text_sources = " ".join(pg.get("text") or "" for pg in pages)
    needs_history = bool(re.search(r"\btrough\b|LPI|2017|2018|2020",
                                    all_text_sources, re.IGNORECASE))
    if needs_history:
        historical_10k: list[dict] = []
        for i in range(len(forms)):
            if forms[i] == "10-K":
                try:
                    fd = _dt.date.fromisoformat(dates_[i])
                except Exception:
                    continue
                if fd < _dt.date(2022, 1, 1):
                    prim = primaries[i] if i < len(primaries) else ""
                    historical_10k.append({"accession": accs[i],
                                            "primary": prim,
                                            "filed": dates_[i]})
        # Also walk extension index files for older 10-Ks
        ext_files = (submissions.get("filings", {}).get("files") or [])
        for ef in ext_files[:3]:
            name = ef.get("name")
            if not name:
                continue
            ext_url = f"https://data.sec.gov/submissions/{name}"
            try:
                ext_body = await _sec_fetch_json(ext_url, TTL_SUBMISSIONS,
                                                   HTTP_CLIENT)
            except Exception:
                continue
            if not ext_body:
                continue
            ex_forms = ext_body.get("form", []) or []
            ex_accs = ext_body.get("accessionNumber", []) or []
            ex_dates = ext_body.get("filingDate", []) or []
            ex_prims = ext_body.get("primaryDocument", []) or []
            for i in range(len(ex_forms)):
                if ex_forms[i] != "10-K":
                    continue
                try:
                    fd = _dt.date.fromisoformat(ex_dates[i])
                except Exception:
                    continue
                if fd < _dt.date(2022, 1, 1) and fd >= _dt.date(2019, 1, 1):
                    historical_10k.append({
                        "accession": ex_accs[i],
                        "primary": ex_prims[i] if i < len(ex_prims) else "",
                        "filed": ex_dates[i]})
        # Dedupe by accession
        seen = set()
        uniq = []
        for h in historical_10k:
            if h["accession"] not in seen:
                seen.add(h["accession"])
                uniq.append(h)
        historical_10k = uniq
        # Fetch up to 3 oldest-first (FY2019 is highest priority for -28.2%)
        historical_10k.sort(key=lambda x: x["filed"])
        for h in historical_10k[:3]:
            txt = await _filing_text(h["accession"], cik_padded,
                                      h["primary"], HTTP_CLIENT)
            if txt:
                historical_10k_texts.append(txt)
    text_10k_full = text_10k + "\n" + "\n".join(historical_10k_texts)

    # Classify every claim via the new v3 pipeline:
    # 1. MODEL_OR_ADJUSTED guard; 2. number-first full-tag scan (0.5%);
    # 3. derived metrics (2%); 4. text facts (ratings, LPI); 5. legacy
    # mapped-line-item fallback.
    bucket_match: list[dict] = []
    bucket_mismatch: list[dict] = []
    bucket_not_in: list[dict] = []
    bucket_model: list[dict] = []
    for cm in claims:
        res = fc.classify_claim_v3(
            cm, companyfacts or {}, derived,
            text_10k_full, text_10q_latest, t)
        row = {**cm, **res}
        if res.get("status") in ("MATCH", "MISMATCH"):
            # Attach a uniform citation block so no row is uncited.
            row["filing_citation"] = {
                "basis": res.get("basis") or "SEC filing",
                "xbrl_tag": res.get("xbrl_tag"),
                "xbrl_unit": res.get("xbrl_unit"),
                "period_end": res.get("period_end") or res.get("filing_period_end"),
                "fp": res.get("fp"),
                "form": res.get("form"),
                "filed": res.get("filed"),
                "filing_value": res.get("filing_value"),
                "distance_rel": res.get("distance_rel"),
                "metric": res.get("metric"),
                "formula": res.get("formula"),
                "inputs": res.get("inputs"),
                "filing_sentence": res.get("filing_sentence"),
                "filing_label": res.get("filing_label"),
            }
        if res["status"] == "MATCH":
            bucket_match.append(row)
        elif res["status"] == "MISMATCH":
            bucket_mismatch.append(row)
        elif res["status"] == "MODEL_OR_ADJUSTED":
            bucket_model.append(row)
        else:
            bucket_not_in.append(row)

    # Text-claim classification (ratings, covenants, fleet counts).
    # These are extracted separately because they are not numeric claims.
    for tc in text_claims:
        if tc.get("estimate"):
            tc["status"] = "MODEL_OR_ADJUSTED"
            tc["basis"] = "model_or_adjusted guard"
            bucket_model.append(tc)
            continue
        tres = fc.match_text_claim(tc, text_10k_full, text_10q_latest)
        if not tres:
            tc["status"] = "NOT_IN_FILINGS"
            tc["basis"] = "unverified text claim"
            bucket_not_in.append(tc)
            continue
        row = {**tc, **tres}
        row["filing_citation"] = {
            "basis": tres.get("basis"),
            "filing_label": tres.get("filing_label"),
            "filing_sentence": tres.get("filing_sentence"),
            "filing_text_value": tres.get("filing_text_value"),
        }
        if tres["status"] == "MATCH":
            bucket_match.append(row)
        elif tres["status"] == "MISMATCH":
            bucket_mismatch.append(row)
        else:
            bucket_not_in.append(row)

    # Staleness: compare doc periods to filings
    all_doc_periods: list[str] = []
    for cm in claims:
        all_doc_periods.extend(cm.get("periods", []))
    stale_filings = fc.staleness(all_doc_periods, recent_filings)

    # Section diff: last 2 10-Qs (if we have both). text_10q_latest was
    # already fetched above for the text-fact scanner.
    diff_blob: dict = {}
    latest_10q_text = text_10q_latest
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
            "MODEL_OR_ADJUSTED": bucket_model,
            "NOT_IN_FILINGS": bucket_not_in,
            "STALE": stale_filings,
        },
        "counts": {
            "MATCH": len(bucket_match),
            "MISMATCH": len(bucket_mismatch),
            "MODEL_OR_ADJUSTED": len(bucket_model),
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
# ----------------------------------------------------------------------
# /api/read: upload a document, get its text plus labeled findings for the
# /desk/read/ spider reader, compared against the desk's stored facts for
# the ticker (the same store vault_sync copies into the vault).
# Uploads are read into memory and NEVER written to disk.
# ----------------------------------------------------------------------
READ_MAX_FINDINGS = 400
READ_MAX_CHARS = 400000


def _norm_num(v: str) -> str:
    v = (v or "").replace(",", "").replace("$", "").strip()
    try:
        return f"{float(v):.4g}"
    except ValueError:
        return v


# --- /api/read finding classifier -------------------------------------
# Every figure gets the line item it sits on, a category, and a scale, so
# the reader panel can say what it found instead of "Figure".
_READ_CATS = [
    ("Revenue", r"revenue|net sales|\bsales\b|deliveries|orders?\b|backlog"),
    ("Profitability", r"gross margin|gross profit|operating income|operating margin|net income|earnings|ebitda|per share|\beps\b|cost of sales|research and development|selling, general"),
    ("Cash flow", r"cash flows?|operating activities|investing activities|financing activities|capital expenditure|purchases? of property|repurchase|buyback|dividend"),
    ("Debt and financing", r"\bdebt\b|notes? due|senior notes|borrowing|credit facility|commercial paper|term loan|interest expense|leases?\b|maturit"),
    ("Hedging and FX", r"hedg|derivative|foreign (?:exchange|currency)|swap|forward contracts?|interest rate contracts?"),
    ("Balance sheet", r"inventor|receivable|payable|total assets|total liabilities|shareholders.? equity|stockholders.? equity|goodwill|intangible|property, plant|cash and cash equivalents|marketable securities|deferred revenue|accrued"),
    ("Equity", r"^balance (?:at |as of )?[A-Z]?[a-z]+|retained earnings|comprehensive (?:income|loss)|attributable to|noncontrolling|additional paid-in"),
    ("Tax", r"\btax"),
    ("Shares", r"shares? (?:of|outstanding|repurchased|issued)|common stock|par value|share-based|stock-based"),
    ("Segments", r"americas|europe|greater china|japan|rest of asia|segment|north america|international"),
    ("Fleet and operations", r"railcars?|fleet|utilization|units|deliveries|employees|headcount"),
]
_READ_CATS = [(n, re.compile(pat, re.I)) for n, pat in _READ_CATS]
_LABEL_RE = re.compile(r"([A-Za-z][A-Za-z ,'\u2019&()/\-]{1,80}?)[\s:$\u2014\-]*(?:\(?\$?\s?-?[\d,]+(?:\.\d+)?\)?%?\s*|\$\s*|—\s*)*$")


def _read_line_item(body: str, st: int) -> tuple[str, str]:
    """(line item, section heading) for the figure at char offset st."""
    ls = body.rfind("\n", 0, st) + 1
    le = body.find("\n", st)
    le = len(body) if le < 0 else le
    before = body[ls:st]
    # a folded table line keeps its cells two spaces apart: the row label is
    # the last text cell, and a bare title cell before it is its parent
    cells = before.split("  ")
    parent = ""
    for c in reversed(cells[:-1]):
        c = c.strip()
        if c and not re.search(r"\d", c) and len(c.split()) <= 5 and c[:1].isupper():
            parent = c.strip("()")
            break
        if re.search(r"[A-Za-z]{3,}", c):
            break
    seg = before
    while "  " in seg and not re.search(r"[A-Za-z]{3,}", seg[seg.rfind("  "):]):
        seg = seg[:seg.rfind("  ")]
    seg = seg[seg.rfind("  ") + 2:] if "  " in seg else seg
    m = _LABEL_RE.search(seg)
    item = (m.group(1) if m else "").strip(" ,-(")
    if not item and not re.search(r"[A-Za-z]{2,}", before):
        # a label right after a lone value on its line ("3.600% Notes due 2042")
        after = body[st:le]
        m3 = re.match(r"\S+\s+([A-Za-z][A-Za-z0-9 ,'\u2019&()/\-]{3,60}?)(?:\s+[\u2014\-]|\s*$)", after)
        if m3 and not re.match(r"[\s$\d,.%()\u00a0]+\d", after[len(after.split()[0]):][:12]):
            item = m3.group(1).strip(" ,-")
    if not item and not re.search(r"[A-Za-z]{2,}", before) and ls > 0:
        # EDGAR PDF layout: the row label prints at the end of the line
        # above its numbers ("... Net sales" / "$ 265,595 $ 229,234 ...")
        ps = body.rfind("\n", 0, ls - 1) + 1
        prev = body[ps:ls - 1]
        m2 = re.search(r"(?:^|[\d%)\s\u00a0])([A-Z][A-Za-z ,'\u2019&()/\-:]{2,80}?)[\s\u00a0:]*$", prev)
        if m2 and not re.search(r"\d", m2.group(1)):
            item = m2.group(1).strip(" ,:")
    prose = len(re.findall(r"\b[a-z]{2,}\b", before)) >= 8 or bool(re.search(r"\b(?:was|were|is|are|totaled|increased|decreased|approximately)\s*\$?\s*$", before))
    if (not item or prose) and len(before) > 40:
        # a number inside a sentence: its subject ("The aggregate market
        # value of ... was approximately $3.25 trillion")
        sent = re.split(r"(?<=[.;:])\s|\n(?=[A-Z])", body[max(0, st - 400):st])[-1]
        sent = re.sub(r"^(?:As of|In|During|For|At|On)\b[^,]{0,80},\s*", "", sent)
        subj = re.split(r"\b(?:was|were|is|are|increased|decreased|totaled|of approximately|approximately|had|rose|fell|grew|declined)\b|,", sent)[0]
        words = [w for w in re.sub(r"[$\d,.%()]+", " ", subj).split() if re.search(r"[A-Za-z]", w)]
        if 2 <= len(words):
            item = " ".join(words[:8])
    if not item:
        # a number inside a sentence: the words just before it
        sent = re.split(r"[.;:]\s", before)[-1]
        words = [w for w in re.sub(r"[$\d,.%()]+", " ", sent).split() if w]
        item = " ".join(words[-6:])
    item = re.sub(r"\s+", " ", item)
    if len(item.split()) > 7:
        item = " ".join(item.split()[-7:])
    # prose fragments ("million and", "respectively, and") are not labels
    junk = {"increased", "decreased", "totaled", "rose", "fell", "grew", "declined", "compared", "approximately", "were", "be", "and", "or", "of", "the", "million", "billion", "thousand", "respectively", "to", "in", "was", "were", "by", "for", "from", "as", "at", "approximately", "which", "with", "is", "a", "an"}
    toks = [w for w in re.findall(r"[A-Za-z0-9']+", item)]
    while toks and toks[-1].lower() in junk:
        toks.pop()
    while toks and toks[0].lower() in junk:
        toks.pop(0)
    if not toks or len(" ".join(toks)) < 4 or not any(w.lower() not in junk for w in toks):
        item = ""
    else:
        item = " ".join(toks)
    # section: nearest short title-like line above (no long numbers)
    sect = ""
    pos, hops = ls - 1, 0
    while pos > 0 and hops < 25:
        ps = body.rfind("\n", 0, pos) + 1
        ln = body[ps:pos].strip()
        pos, hops = ps - 1, hops + 1
        words = ln.split()
        caps = sum(1 for w in words if w[:1].isupper()) / max(1, len(words))
        if (3 <= len(ln) <= 70 and not re.search(r"\d", ln) and ln[:1].isupper() and not ln.endswith((".", ",", ";"))
                and (caps >= 0.6 or re.match(r"(?:Condensed|Consolidated|Notes?|Item|Part|Statements?)\b", ln))):
            sect = re.sub(r"\s+", " ", ln)
            break
    if parent and parent != item:
        sect = parent
    return item, sect


def _read_scale(body: str, st: int, raw: str) -> str:
    after = body[st:st + len(raw) + 14].lower()
    for w, v in (("billion", "B"), ("million", "M"), ("thousand", "K")):
        if re.match(r"[\d,.$\s]*" + w, after):
            return v
    low = (raw or "").lower()
    for w in ("billion", "million", "thousand"):
        if w in low:
            return ""
    back = body[max(0, st - 6000):st].lower()
    hits = [(m.start(), m.group(1)) for m in re.finditer(r"\(\s*(?:dollars |amounts |\$ )?in (millions|billions|thousands)", back)]
    return {"millions": "M", "billions": "B", "thousands": "K"}[hits[-1][1]] if hits else ""


def _read_classify(body: str, f: dict) -> None:
    st = f.get("char_offset")
    if st is None or st < 0:
        f.update(item="", section="", category="Other", scale="")
        return
    item, sect = _read_line_item(body, st)
    text = " ".join([f.get("label") if f.get("src") == "desk" else "", item, sect, f.get("snippet") or ""])
    # the line item decides first, then the section, then the sentence
    cat = "Other"
    for src in (item, sect, text):
        hit = next((name for name, rx in _READ_CATS if rx.search(src)), None)
        if hit:
            cat = hit
            break
    snip = re.sub(r"\s+", " ", f.get("snippet") or "").strip()
    if snip[:1].islower():
        snip = snip.split(" ", 1)[-1]   # drop a word cut in half
    f["snippet"] = snip
    raw = f.get("display") or f.get("value") or ""
    unit = f.get("unit") or ""
    kind = "percent" if "%" in raw or unit == "%" else "per share" if (re.search(r"per (?:\w+ )?share", item + " " + body[st:st + 40], re.I) or re.fullmatch(r"(?:basic|diluted)", item.strip(), re.I)) else "amount"
    # table position: which of the numbers that follow this row label it is
    ls = body.rfind("\n", 0, st) + 1
    le = body.find("\n", st); le = len(body) if le < 0 else le
    line = body[ls:le]
    col = ncols = 0
    k = line.rfind(item) if item else -1
    if k < 0 and item and not re.search(r"[A-Za-z]{2,}", body[ls:st]):
        k, item_len = 0, 0          # label sat on the line above: cells start the line
    else:
        item_len = len(item)
    if k >= 0 and len(line) < 220:
        rest = line[k + item_len:]
        nxt = re.search(r"[A-Za-z]{3,}", rest)       # next row label on a folded line
        rest = rest[:nxt.start()] if nxt else rest
        if not re.fullmatch(r"[\s$\d,.()%\u2014\-]*", rest):
            rest = ""
        base = ls + k + item_len
        cells = [base + m.start() for m in re.finditer(r"\d[\d,]*(?:\.\d+)?", rest)]
        hit = [i for i, c in enumerate(cells) if c <= st < c + 12 and body[c:st + 1].strip()]
        if len(cells) > 1 and hit:
            ncols = len(cells)
            col = 1 + min(hit, key=lambda i: abs(cells[i] - st))
    f.update(item=item, section=sect, category=cat, kind=kind, col=col, ncols=ncols,
             scale=_read_scale(body, st, raw) if kind == "amount" else "")


_TICKER_COVER_RE = re.compile(r"\b([A-Z]{1,5}(?:\.[A-Z])?)\s+(?:The\s+)?(?:Nasdaq|New York Stock Exchange|NYSE|Chicago Stock Exchange)", re.I)


def _read_detect_ticker(body: str) -> Optional[str]:
    head = body[:8000]
    for m in _TICKER_COVER_RE.finditer(head):
        t = m.group(1).upper()
        if t not in {"THE", "LLC", "INC", "NEW", "PAR"}:
            return t
    m = re.search(r"Trading Symbol\(?s?\)?[^\n]{0,80}?\n?[^\n]{0,80}?\b([A-Z]{2,5})\b", head)
    return m.group(1) if m else None


def _read_covered_tickers() -> set:
    try:
        from intake import store as _store
        with _store.connect() as conn:
            return {r[0] for r in conn.execute("SELECT DISTINCT d.ticker FROM facts f JOIN documents d ON d.id = f.document_id").fetchall() if r[0]}
    except Exception:
        return set()


def _stored_facts_for_ticker(ticker: str) -> list[dict]:
    try:
        from intake import store as _store
        with _store.connect() as conn:
            rows = conn.execute(
                "SELECT f.label, f.value, f.unit, d.type, d.title, d.published_at, d.fetched_at "
                "FROM facts f JOIN documents d ON d.id = f.document_id "
                "WHERE d.ticker = ? ORDER BY COALESCE(d.published_at, d.fetched_at) DESC",
                (ticker,)).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning(f"/api/read stored facts lookup failed: {e}")
        return []


class _ReadError(Exception):
    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status, self.msg = status, msg


def _read_key(s: Optional[str]) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def _read_core(data: bytes, kind: str, title: str, ticker: Optional[str]) -> dict:
    try:
        pages = fc.extract(data, kind)
    except Exception as e:
        raise _ReadError(400, f"could not extract text: {e.__class__.__name__}: {e}")
    # Tables extract one cell per line; fold runs of short fragments back
    # into one row so the reader shows "Revenue $ 576.5 $ 842.7" on a line.
    # Folded page by page so every figure keeps its page number.
    def _fold(txt: str) -> str:
        folded, row = [], []
        for ln in txt.split("\n"):
            t = ln.strip()
            if t and len(t) < 28:
                row.append(t)
                continue
            if row:
                folded.append("  ".join(row)); row = []
            folded.append(t)
        if row:
            folded.append("  ".join(row))
        return re.sub(r"\n{3,}", "\n\n", "\n".join(folded)).strip()
    parts, page_starts, pos = [], [], 0
    for i, p in enumerate(pages):
        t = _fold((p.get("text") or "").strip())
        if not t:
            continue
        page_starts.append((pos, i + 1))
        parts.append(t)
        pos += len(t) + 2
    body = "\n\n".join(parts)[:READ_MAX_CHARS]
    if len(re.sub(r"\s+", "", body)) < 40:
        raise _ReadError(422, "no readable text in that file (scanned PDF or image only?)")

    # Labeled findings: the desk's own fact patterns (same labels the live
    # page uses), then every other money/percent claim the fact-checker sees.
    try:
        from intake.extract import _FACT_PATTERNS
    except Exception:
        _FACT_PATTERNS = []
    findings: list[dict] = []
    seen_spans: list[tuple[int, int]] = []
    for label, pat, grp, unit in _FACT_PATTERNS:
        for m in pat.finditer(body):
            try:
                val = m.group(grp)
            except IndexError:
                continue
            st, en = m.span(grp)
            if any(a <= st < b for a, b in seen_spans):
                continue
            seen_spans.append((st, en))
            findings.append({"label": label, "value": val, "unit": unit, "char_offset": st, "src": "desk",
                             "snippet": body[max(0, m.start() - 60): m.end() + 60].replace("\n", " ").strip()})
    try:
        claims = fc.extract_claims([{"locator": "doc", "text": body}])
    except Exception:
        claims = []
    for c in claims:
        raw = (c.get("raw") or "").strip()
        if not raw:
            continue
        num = re.search(r"[\d][\d,]*\.?\d*", raw)
        if not num:
            continue
        val = num.group(0).rstrip(".")
        pos = body.find(raw)
        st = pos + raw.find(val) if pos >= 0 else -1
        if st >= 0 and any(a <= st < b for a, b in seen_spans):
            continue
        if st >= 0:
            seen_spans.append((st, st + len(val)))
        ctx = c.get("context") or ""
        # Unlabeled: a context guess ("near EPS") was often wrong, so the
        # figure is shown exactly as the document writes it instead.
        findings.append({"label": "Figure", "src": "claim", "display": raw,
                         "value": val, "unit": c.get("unit"), "char_offset": st,
                         "snippet": re.sub(r"\s+", " ", c.get("sentence") or ctx)[:220]})
    # Keep every labeled finding first, then fill with bare numbers; the
    # reader walks them in document order.
    labeled = [f for f in findings if f.get("src") == "desk"]
    bare = [f for f in findings if f.get("src") != "desk"]
    findings = (labeled + bare)[:READ_MAX_FINDINGS]
    findings.sort(key=lambda f: (f["char_offset"] if f["char_offset"] is not None and f["char_offset"] >= 0 else 10**9))

    # Page number and classification for every figure.
    for f in findings:
        _read_classify(body, f)
        st = f.get("char_offset")
        f["page"] = None
        if st is not None and st >= 0:
            for ps, pn in page_starts:
                if ps <= st:
                    f["page"] = pn
                else:
                    break
    # No ticker picked: read it off the cover page and compare if the vault
    # covers it; otherwise every figure is simply new to the vault.
    t = _validate_ticker(ticker) if ticker else None
    detected = None
    if not t:
        detected = _read_detect_ticker(body)
        if detected and detected in _read_covered_tickers():
            t = detected
    stored = _stored_facts_for_ticker(t) if t else []
    # Earlier reads saved to the vault (research/reader-runs/<ticker>/):
    # a figure with the same line item and value is already known.
    run_ticker = t or detected
    prior = await _vault_prior_runs(run_ticker) if run_ticker else []
    prior_pairs = {(_read_key(f.get("item") or f.get("label")), _norm_num(f.get("value"))) for r in prior for f in r.get("findings", [])}
    if prior and not t:
        t = run_ticker
    covered = bool(t)
    known_vals: dict[str, set] = {}
    latest: dict[str, dict] = {}
    for r in stored:
        known_vals.setdefault(r["label"], set()).add(_norm_num(r["value"]))
        latest.setdefault(r["label"], r)
    for f in findings:
        if not t:
            f["status"] = "NOT_COVERED"
            continue
        lab = f["label"]
        if (_read_key(f.get("item") or lab), _norm_num(f["value"])) in prior_pairs:
            f["status"] = "KNOWN"
        elif _norm_num(f["value"]) in known_vals.get(lab, set()):
            f["status"] = "KNOWN"
        elif lab in latest and f.get("src") == "desk":
            # Only the desk's own labeled facts are compared value-to-value;
            # a context label ("revenue decreased $0.9 million") is a mention,
            # not a restatement of the line item.
            r = latest[lab]
            f["status"] = "CHANGED"
            f["prior"] = {"value": r["value"], "doc": f"{r['type']} {(r['published_at'] or r['fetched_at'] or '')[:10]}".strip()}
        else:
            f["status"] = "NEW"
    counts: dict[str, int] = {}
    for f in findings:
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    sha = hashlib.sha256(data).hexdigest()
    try:
        import report as _report
        company_report = _report.build_report(body, page_starts, title, t or detected)
    except Exception as e:      # the read still works if the report cannot be built
        log.warning(f"/api/read report failed: {e.__class__.__name__}: {e}")
        company_report = None
    return {
        "report": company_report,
        "doc_id": "upload-" + sha[:16], "sha256": sha, "title": title, "kind": kind, "ticker": t,
        "detected_ticker": detected, "covered": covered,
        "chars": len(body), "text": body, "findings": findings, "counts": counts,
        "stored_facts": len(stored), "prior_runs": len(prior),
        "last_run": (prior[0].get("read_at") or "")[:10] if prior else None,
        "already_saved": any(r.get("source_sha256") == sha for r in prior)}


# ----------------------------------------------------------------------
# Crawler: load a filing by ticker from SEC EDGAR (owner's Mac only)
# ----------------------------------------------------------------------
EDGAR_FORMS = ("10-K", "10-Q", "10-K/A", "10-Q/A", "20-F", "40-F")
TTL_SUBMISSIONS = 6 * 3600   # a company's filing list; new filings show within six hours
EDGAR_DOC_DIR = CACHE_DIR / "edgar"


def _edgar_denied() -> Optional[JSONResponse]:
    if os.environ.get("BMG_LOCAL") != "1":
        return JSONResponse(status_code=403, content={"error": "Loading filings from SEC EDGAR is available on the local crawler only."})
    return None


@app.get("/api/edgar/filings")
async def edgar_filings(ticker: str = Query(..., max_length=12)):
    """Recent 10-K and 10-Q filings for a ticker, newest first, from the SEC's submissions feed."""
    denied = _edgar_denied()
    if denied:
        return denied
    t = re.sub(r"[^A-Z0-9.\-]", "", ticker.upper())
    if not t:
        return JSONResponse(status_code=400, content={"error": "Enter a ticker, for example AMZN."})
    client = HTTP_CLIENT or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    try:
        tmap = await _ticker_map(client)
        row = tmap.get(t) or tmap.get(t.replace(".", "-"))
        if not row:
            return JSONResponse(status_code=404, content={"error": f"No SEC filer has the ticker {t}."})
        cik = int(row["cik_str"])
        sub = await _sec_fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", TTL_SUBMISSIONS, client)
    except httpx.HTTPError as e:
        return JSONResponse(status_code=502, content={"error": f"SEC EDGAR did not answer ({type(e).__name__}). Try again in a minute."})
    recent = ((sub or {}).get("filings") or {}).get("recent") or {}
    out = []
    for i, form in enumerate(recent.get("form", [])):
        if form not in EDGAR_FORMS:
            continue
        acc, doc = recent["accessionNumber"][i], recent["primaryDocument"][i]
        out.append({"form": form, "filed": recent["filingDate"][i], "period": (recent.get("reportDate") or [""] * (i + 1))[i],
                    "accession": acc, "document": doc,
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}"})
        if len(out) >= 12:
            break
    return {"ticker": t, "cik": cik, "company": (sub or {}).get("name") or row.get("title"), "filings": out}


@app.get("/api/edgar/document")
async def edgar_document(cik: int, accession: str, document: str):
    """The filing's primary document, fetched once from SEC EDGAR and then served from the local cache."""
    denied = _edgar_denied()
    if denied:
        return denied
    if not re.fullmatch(r"\d{10}-\d{2}-\d{6}", accession) or not re.fullmatch(r"[\w.\-]{1,120}\.(?:htm|html|txt|pdf)", document, re.I):
        return JSONResponse(status_code=400, content={"error": "That is not an EDGAR filing reference."})
    path = EDGAR_DOC_DIR / f"{cik}-{accession}-{document}"
    if path.exists():
        data = path.read_bytes()
    else:
        url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{document}"
        client = HTTP_CLIENT or httpx.AsyncClient(timeout=60.0, follow_redirects=True)
        try:
            await SEC_LIMITER.acquire()
            r = await client.get(url, headers={"User-Agent": SEC_USER_AGENT}, timeout=60.0)
            r.raise_for_status()
        except httpx.HTTPError as e:
            return JSONResponse(status_code=502, content={"error": f"SEC EDGAR did not return the filing ({type(e).__name__})."})
        data = r.content
        if len(data) > CHECK_MAX_BYTES:
            return JSONResponse(status_code=413, content={"error": f"The filing is {len(data) / 1e6:.1f} MB; the limit is {CHECK_MAX_BYTES // 1048576} MB."})
        EDGAR_DOC_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    kind = "application/pdf" if document.lower().endswith(".pdf") else "text/plain" if document.lower().endswith(".txt") else "text/html; charset=utf-8"
    return Response(content=data, media_type=kind, headers={"Cache-Control": "no-store"})


@app.get("/api/read/limits")
def read_limits():
    """What this server accepts, so the page states the real limit instead of a hard-coded one."""
    return {"max_bytes": CHECK_MAX_BYTES, "local": os.environ.get("BMG_LOCAL") == "1"}


@app.post("/api/read")
async def read_document(request: Request,
                        file: Optional[UploadFile] = File(None),
                        text: Optional[str] = Form(None),
                        ticker: Optional[str] = Form(None)) -> JSONResponse:
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip() if xff
          else (request.client.host if request.client else "unknown"))
    rl_err = _rate_limit_check(ip)
    if rl_err:
        return JSONResponse(status_code=429, content={"error": rl_err})
    if file is not None:
        data = await file.read()
        if len(data) > CHECK_MAX_BYTES:
            return JSONResponse(status_code=413, content={
                "error": f"file too large: {len(data)} bytes (cap {CHECK_MAX_BYTES})"})
        kind = _detect_kind(file.filename or "", file.content_type or "")
        if kind is None:
            return JSONResponse(status_code=400, content={
                "error": "unsupported file type; use PDF, DOCX, PPTX, HTML or text"})
        title = file.filename or "upload"
    elif text:
        data = text.encode("utf-8", errors="replace")
        kind, title = "text", "pasted text"
    else:
        return JSONResponse(status_code=400, content={"error": "upload a file or paste text"})
    try:
        res = await _read_core(data, kind, title, ticker)
    except _ReadError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg})
    return JSONResponse(content=res)


# ----------------------------------------------------------------------
# Reader runs saved to the vault on GitHub (free storage, no database).
#
# POST /api/read/save re-reads the uploaded file and commits one folder to
# the vault repo:  research/reader-runs/<TICKER>/<date>-<name>-<sha8>/
#   findings.json  every figure (page, line item, category, value, status)
#   summary.md     readable summary
#   source.<ext>   the document as uploaded (primary source)
# Only the owner can save: the request must carry READ_SAVE_KEY. The
# commit uses VAULT_GITHUB_TOKEN (a fine-grained token limited to the vault
# repo, Contents read/write). Earlier saved runs are read back by
# _vault_prior_runs() so the next read of the same company marks figures
# already in the vault as KNOWN.
# ----------------------------------------------------------------------
import base64 as _b64
import hmac as _hmac
from datetime import datetime as _dt, timezone as _tz

VAULT_REPO = os.environ.get("VAULT_REPO", "brock-gorzelanczyk/BMG-Capital-Vault")
VAULT_BRANCH = os.environ.get("VAULT_BRANCH", "main")
VAULT_TOKEN = os.environ.get("VAULT_GITHUB_TOKEN", "")
READ_SAVE_KEY = os.environ.get("READ_SAVE_KEY", "")
READ_RUNS_DIR = "research/reader-runs"
GITHUB_API = os.environ.get("VAULT_GITHUB_API", "https://api.github.com")
READ_SAVE_SOURCE_MAX = 25 * 1024 * 1024
_PRIOR_CACHE: dict[str, tuple[float, list]] = {}


async def _gh(client: "httpx.AsyncClient", method: str, path: str, **kw):
    r = await client.request(method, GITHUB_API + path, **kw)
    if r.status_code >= 400:
        raise _ReadError(502, f"GitHub {method} {path.split('?')[0]} failed: {r.status_code} {r.text[:200]}")
    return r


def _gh_headers(raw: bool = False) -> dict:
    return {"Authorization": f"Bearer {VAULT_TOKEN}", "X-GitHub-Api-Version": "2022-11-28",
            "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json",
            "User-Agent": "bmg-desk-reader"}


async def _vault_prior_runs(ticker: Optional[str]) -> list[dict]:
    """Saved reader runs for a ticker, newest first (findings.json of up to 6)."""
    t = _validate_ticker(ticker or "")
    if not t or not VAULT_TOKEN:
        return []
    hit = _PRIOR_CACHE.get(t)
    if hit and time.time() - hit[0] < 300:
        return hit[1]
    runs: list[dict] = []
    try:
        async with httpx.AsyncClient(timeout=15, headers=_gh_headers()) as c:
            r = await c.get(f"{GITHUB_API}/repos/{VAULT_REPO}/contents/{READ_RUNS_DIR}/{t}", params={"ref": VAULT_BRANCH})
            if r.status_code == 200:
                dirs = sorted((x["name"] for x in r.json() if x.get("type") == "dir"), reverse=True)[:6]
                for d in dirs:
                    fr = await c.get(f"{GITHUB_API}/repos/{VAULT_REPO}/contents/{READ_RUNS_DIR}/{t}/{d}/findings.json",
                                     params={"ref": VAULT_BRANCH}, headers=_gh_headers(raw=True))
                    if fr.status_code == 200:
                        try:
                            runs.append(fr.json())
                        except ValueError:
                            pass
    except httpx.HTTPError as e:
        log.warning(f"vault prior runs lookup failed: {e}")
        return []
    _PRIOR_CACHE[t] = (time.time(), runs)
    return runs


async def _vault_commit(files: dict[str, bytes], message: str) -> str:
    """One commit with all files on VAULT_BRANCH (Git Data API). Returns the commit sha."""
    repo = f"/repos/{VAULT_REPO}/git"
    async with httpx.AsyncClient(timeout=60, headers=_gh_headers()) as c:
        for attempt in range(3):
            ref = (await _gh(c, "GET", f"{repo}/ref/heads/{VAULT_BRANCH}")).json()["object"]["sha"]
            base_tree = (await _gh(c, "GET", f"{repo}/commits/{ref}")).json()["tree"]["sha"]
            tree = []
            for path, content in files.items():
                blob = (await _gh(c, "POST", f"{repo}/blobs", json={"content": _b64.b64encode(content).decode(), "encoding": "base64"})).json()["sha"]
                tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob})
            new_tree = (await _gh(c, "POST", f"{repo}/trees", json={"base_tree": base_tree, "tree": tree})).json()["sha"]
            commit = (await _gh(c, "POST", f"{repo}/commits", json={"message": message, "tree": new_tree, "parents": [ref]})).json()["sha"]
            r = await c.patch(f"{GITHUB_API}{repo}/refs/heads/{VAULT_BRANCH}", json={"sha": commit, "force": False})
            if r.status_code < 400:
                return commit
            if r.status_code != 422:      # 422: the branch moved meanwhile; rebuild on the new tip
                raise _ReadError(502, f"GitHub ref update failed: {r.status_code} {r.text[:200]}")
        raise _ReadError(502, "GitHub ref update kept conflicting; try again")


def _read_summary_md(res: dict, source_name: str, read_at: str) -> str:
    fs = res["findings"]
    cats: dict[str, list] = {}
    for f in fs:
        cats.setdefault(f.get("category") or "Other", []).append(f)
    status_counts = ", ".join(f"{k} {v}" for k, v in sorted(res["counts"].items()))
    lines = [
        f"# Reader run: {res['title']}",
        "",
        f"- Ticker: {res.get('ticker') or res.get('detected_ticker') or 'not identified'}",
        f"- Source: `{source_name}` (PRIMARY: the document as uploaded), sha256 `{res['sha256']}`",
        f"- Read: {read_at} by the BMG desk reader (/desk/read)",
        f"- Text read: {res['chars']:,} characters; figures found: {len(fs)} ({status_counts})",
        f"- Compared with: {res.get('stored_facts', 0)} stored desk facts and {res.get('prior_runs', 0)} earlier saved runs",
        "",
        "Figures are copied as printed; the line item and category are the reader's automatic labels and should be checked against the page before use in a model or note.",
        "",
    ]
    for cat in sorted(cats, key=lambda c: (-len(cats[c]), c)):
        lines += [f"## {cat} ({len(cats[cat])})", "", "| Page | Line item | Value | Scale | Status |", "|---|---|---|---|---|"]
        for f in cats[cat]:
            item = (f.get("item") or (f.get("label") if f.get("label") != "Figure" else "") or f.get("section") or "").replace("|", "/")
            val = (f.get("display") or f.get("value") or "").replace("|", "/")
            lines.append(f"| {f.get('page') or ''} | {item} | {val} | {f.get('scale') or ''} | {f.get('status')} |")
        lines.append("")
    return "\n".join(lines)


@app.post("/api/read/save")
async def read_save(request: Request,
                    file: Optional[UploadFile] = File(None),
                    text: Optional[str] = Form(None),
                    ticker: Optional[str] = Form(None),
                    key: Optional[str] = Form(None)) -> JSONResponse:
    if not READ_SAVE_KEY or not VAULT_TOKEN:
        return JSONResponse(status_code=503, content={"error": "saving is not set up on the server (READ_SAVE_KEY and VAULT_GITHUB_TOKEN)"})
    if not key or not _hmac.compare_digest(key.encode(), READ_SAVE_KEY.encode()):
        return JSONResponse(status_code=403, content={"error": "wrong save key"})
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown"))
    rl_err = _rate_limit_check(ip)
    if rl_err:
        return JSONResponse(status_code=429, content={"error": rl_err})
    if file is not None:
        data = await file.read()
        if len(data) > max(CHECK_MAX_BYTES, READ_SAVE_SOURCE_MAX):
            return JSONResponse(status_code=413, content={"error": "file too large"})
        kind = _detect_kind(file.filename or "", file.content_type or "")
        if kind is None:
            return JSONResponse(status_code=400, content={"error": "unsupported file type"})
        title = name = file.filename or "upload"
    elif text:
        data = text.encode("utf-8", errors="replace")
        kind, title, name = "text", "pasted text", "pasted.txt"
    else:
        return JSONResponse(status_code=400, content={"error": "upload a file or paste text"})
    try:
        res = await _read_core(data, kind, title, ticker)
        t = res.get("ticker") or res.get("detected_ticker") or "UNKNOWN"
        if res.get("already_saved"):
            return JSONResponse(content={"saved": False, "already_saved": True, "ticker": t})
        # first time in the vault: everything not already known is new
        for f in res["findings"]:
            if f.get("status") == "NOT_COVERED":
                f["status"] = "NEW"
        res["counts"] = {}
        for f in res["findings"]:
            res["counts"][f["status"]] = res["counts"].get(f["status"], 0) + 1
        now = _dt.now(_tz.utc)
        read_at = now.strftime("%Y-%m-%d %H:%M UTC")
        slug = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:48] or "document"
        folder = f"{READ_RUNS_DIR}/{t}/{now.strftime('%Y-%m-%d')}-{slug}-{res['sha256'][:8]}"
        ext = (Path(name).suffix.lower() or ".txt")[:6]
        keep = ("label", "item", "section", "category", "kind", "value", "display", "scale", "col", "ncols", "page", "status", "prior", "snippet")
        record = {
            "title": res["title"], "ticker": t, "kind": kind, "read_at": now.isoformat(timespec="seconds"),
            "source_file": f"source{ext}", "source_name": name, "source_sha256": res["sha256"],
            "source_label": "PRIMARY (document as uploaded)", "chars": res["chars"], "counts": res["counts"],
            "findings": [{k: f.get(k) for k in keep if f.get(k) not in (None, "")} for f in res["findings"]],
        }
        files = {
            f"{folder}/findings.json": json.dumps(record, indent=1, ensure_ascii=False).encode(),
            f"{folder}/summary.md": _read_summary_md(res, name, read_at).encode(),
        }
        if res.get("report"):
            import report as _report
            files[f"{folder}/report.json"] = json.dumps(res["report"], indent=1, ensure_ascii=False).encode()
            files[f"{folder}/report.md"] = _report.report_markdown(res["report"], name, read_at).encode()
        if len(data) <= READ_SAVE_SOURCE_MAX:
            files[f"{folder}/source{ext}"] = data
        commit = await _vault_commit(files, f"reader run: {t} {name} ({len(res['findings'])} figures)")
    except _ReadError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg})
    _PRIOR_CACHE.pop(t, None)
    return JSONResponse(content={"saved": True, "ticker": t, "path": folder, "commit": commit,
                                 "url": f"https://github.com/{VAULT_REPO}/tree/{VAULT_BRANCH}/{folder}",
                                 "figures": len(res["findings"])})



# ----------------------------------------------------------------------
# Reader jobs (reader/): upload -> parse -> extract -> validate -> report.
# The browser holds an owner token (random, kept in localStorage) and sends
# it as the X-Reader-Owner header (never in the URL, so it stays out of
# access logs). Job ids are random;
# a job is only visible to its owner.
# ----------------------------------------------------------------------
from fastapi.responses import StreamingResponse as _StreamingResponse
from reader import jobs as _reader_jobs
from reader.document import ReadError as _ReaderError


def _owner(request: Request, owner: Optional[str]) -> Optional[str]:
    tok = request.headers.get("x-reader-owner") or owner or ""
    return tok if 16 <= len(tok) <= 200 else None


@app.post("/api/read/jobs")
async def reader_job_create(request: Request,
                            file: Optional[UploadFile] = File(None),
                            text: Optional[str] = Form(None),
                            ticker: Optional[str] = Form(None),
                            owner: Optional[str] = Form(None)) -> JSONResponse:
    tok = _owner(request, owner)
    if not tok:
        return JSONResponse(status_code=400, content={"error": "missing reader owner token"})
    xff = request.headers.get("x-forwarded-for", "")
    ip = (xff.split(",")[0].strip() if xff else (request.client.host if request.client else "unknown"))
    rl_err = _rate_limit_check(ip)
    if rl_err:
        return JSONResponse(status_code=429, content={"error": rl_err})
    if file is not None:
        data = await file.read()
        kind = _detect_kind(file.filename or "", file.content_type or "")
        title = (file.filename or "upload")[:160]
        if kind is None:
            return JSONResponse(status_code=400, content={"error": "Unsupported file type. Use PDF, DOCX, PPTX, HTML or plain text."})
        if not _sniff_ok(kind, data):
            return JSONResponse(status_code=400, content={"error": f"The file does not look like a valid {kind.upper()} file."})
    elif text and text.strip():
        data, kind, title = text.encode("utf-8", errors="replace"), "text", "pasted text"
    else:
        return JSONResponse(status_code=400, content={"error": "Choose a file or paste text first."})
    if len(data) > CHECK_MAX_BYTES:
        return JSONResponse(status_code=413, content={"error": f"The file is {len(data) / 1e6:.1f} MB; the limit is {CHECK_MAX_BYTES // (1024 * 1024)} MB."})
    try:
        job, reused = _reader_jobs.MANAGER.submit(tok, data, kind, title, _validate_ticker(ticker) if ticker else None)
    except _ReaderError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg})
    return JSONResponse(status_code=200 if reused else 202, content={**job.public(), "reused": reused})


def _job_or_404(request: Request, job_id: str, owner: Optional[str]):
    tok = _owner(request, owner)
    j = _reader_jobs.MANAGER.get(tok or "", job_id) if tok else None
    if not j:
        raise HTTPException(status_code=404, detail="Job not found. It may have expired (results are kept for one hour) or the server restarted.")
    return j


@app.get("/api/read/jobs/{job_id}")
async def reader_job_status(request: Request, job_id: str) -> JSONResponse:
    return JSONResponse(content=_job_or_404(request, job_id, None).public())


@app.get("/api/read/jobs/{job_id}/result")
async def reader_job_result(request: Request, job_id: str) -> JSONResponse:
    j = _job_or_404(request, job_id, None)
    if j.state != "done":
        return JSONResponse(status_code=409, content={"error": f"The job is {j.state}.", **j.public()})
    r = j.result
    return JSONResponse(content={"job": j.public(), "report": r["report"], "findings": r["report"]["findings"],
                                 "text": r["text"], "title": r["title"], "kind": r["kind"], "sha256": r["sha256"], "pages": r["pages"]})


@app.get("/api/read/jobs/{job_id}/report.pdf")
async def reader_job_pdf(request: Request, job_id: str):
    j = _job_or_404(request, job_id, None)
    if j.state != "done":
        return JSONResponse(status_code=409, content={"error": f"The job is {j.state}; no report yet."})
    try:
        data = await asyncio.to_thread(_reader_jobs.MANAGER.pdf, j)
    except Exception as e:
        log.exception("report pdf failed")
        return JSONResponse(status_code=500, content={"error": f"The PDF could not be generated ({e.__class__.__name__}). The on-screen report is still available."})
    co = j.result["report"]["company"]
    base = re.sub(r"[^A-Za-z0-9]+", "-", f"BMG-Capital-{co.get('ticker') or co.get('name') or 'company'}-report").strip("-")
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{base}.pdf"', "Cache-Control": "private, no-store"})


@app.post("/api/read/jobs/{job_id}/cancel")
async def reader_job_cancel(request: Request, job_id: str) -> JSONResponse:
    tok = _owner(request, None)
    j = _reader_jobs.MANAGER.cancel(tok or "", job_id) if tok else None
    if not j:
        raise HTTPException(status_code=404, detail="Job not found.")
    return JSONResponse(content=j.public())


@app.post("/api/read/jobs/{job_id}/save")
async def reader_job_save(request: Request, job_id: str,
                          file: Optional[UploadFile] = File(None),
                          text: Optional[str] = Form(None),
                          key: Optional[str] = Form(None),
                          owner: Optional[str] = Form(None)) -> JSONResponse:
    """Owner-only: commit the finished report to the vault on GitHub.
    The server does not keep the uploaded bytes, so the browser sends the
    same file again; it must match the job's sha256. Saved: report.json,
    report.md, findings.json and the source document."""
    if not READ_SAVE_KEY or not VAULT_TOKEN:
        return JSONResponse(status_code=503, content={"error": "Saving is not set up on the server (READ_SAVE_KEY and VAULT_GITHUB_TOKEN)."})
    if not key or not _hmac.compare_digest(key.encode(), READ_SAVE_KEY.encode()):
        return JSONResponse(status_code=403, content={"error": "Wrong save key."})
    j = _job_or_404(request, job_id, None)
    if j.state != "done":
        return JSONResponse(status_code=409, content={"error": f"The job is {j.state}; nothing to save."})
    if file is not None:
        data, name = await file.read(), (file.filename or "upload")
    elif text:
        data, name = text.encode("utf-8", errors="replace"), "pasted.txt"
    else:
        return JSONResponse(status_code=400, content={"error": "Send the same file again to save it with the report."})
    if hashlib.sha256(data).hexdigest() != j.sha256:
        return JSONResponse(status_code=400, content={"error": "This file is not the one the report was built from."})
    rep = j.result["report"]
    co = rep["company"]
    t = _validate_ticker(co.get("ticker") or co.get("ticker_hint") or "") or "UNKNOWN"
    prior = await _vault_prior_runs(t)
    if any(r.get("source_sha256") == j.sha256 for r in prior):
        return JSONResponse(content={"saved": False, "already_saved": True, "ticker": t})
    from reader import report as _rreport
    now = _dt.now(_tz.utc)
    read_at = now.strftime("%Y-%m-%d %H:%M UTC")
    slug = re.sub(r"[^a-z0-9]+", "-", Path(name).stem.lower()).strip("-")[:48] or "document"
    folder = f"{READ_RUNS_DIR}/{t}/{now.strftime('%Y-%m-%d')}-{slug}-{j.sha256[:8]}"
    ext = (Path(name).suffix.lower() or ".txt")[:6]
    record = {"title": j.title, "ticker": t, "kind": j.kind, "read_at": now.isoformat(timespec="seconds"),
              "source_file": f"source{ext}", "source_name": name, "source_sha256": j.sha256,
              "source_label": "PRIMARY (document as uploaded)", "schema": rep.get("schema"),
              "findings": rep["findings"]}
    files = {f"{folder}/findings.json": json.dumps(record, indent=1, ensure_ascii=False, default=str).encode(),
             f"{folder}/report.json": json.dumps(rep, indent=1, ensure_ascii=False, default=str).encode(),
             f"{folder}/report.md": _rreport.markdown(rep, name, read_at).encode()}
    if len(data) <= READ_SAVE_SOURCE_MAX:
        files[f"{folder}/source{ext}"] = data
    try:
        commit = await _vault_commit(files, f"reader run: {t} {name} ({len(rep['findings'])} findings)")
    except _ReadError as e:
        return JSONResponse(status_code=e.status, content={"error": e.msg})
    _PRIOR_CACHE.pop(t, None)
    log.info("reader job %s saved to vault %s", job_id[:8], folder)
    return JSONResponse(content={"saved": True, "ticker": t, "path": folder, "commit": commit,
                                 "url": f"https://github.com/{VAULT_REPO}/tree/{VAULT_BRANCH}/{folder}",
                                 "figures": len(rep["findings"])})


def _compare_jobs(request: Request, a: str, b: str):
    ja, jb = _job_or_404(request, a, None), _job_or_404(request, b, None)
    if ja.state != "done" or jb.state != "done":
        raise HTTPException(status_code=409, detail="Both reports must be finished before they can be compared.")
    from reader import compare as _rcompare
    return _rcompare.build(ja.result["report"], jb.result["report"])


@app.get("/api/read/compare")
async def reader_compare(request: Request, a: str, b: str) -> JSONResponse:
    """Two of the caller's finished reports side by side (owner-scoped)."""
    return JSONResponse(content=_compare_jobs(request, a, b))


@app.get("/api/read/compare.pdf")
async def reader_compare_pdf(request: Request, a: str, b: str):
    cmp = _compare_jobs(request, a, b)
    from reader import pdf as _rpdf
    try:
        data = await asyncio.to_thread(_rpdf.render_compare, cmp)
    except Exception as e:
        log.exception("compare pdf failed")
        return JSONResponse(status_code=500, content={"error": f"The comparison PDF could not be generated ({e.__class__.__name__})."})
    name = re.sub(r"[^A-Za-z0-9]+", "-", f"BMG-Capital-{cmp['a'].get('ticker') or 'A'}-vs-{cmp['b'].get('ticker') or 'B'}").strip("-")
    return Response(content=data, media_type="application/pdf", headers={"Content-Disposition": f'attachment; filename="{name}.pdf"', "Cache-Control": "private, no-store"})


@app.get("/api/read/jobs/{job_id}/events")
async def reader_job_events(request: Request, job_id: str, after: int = 0):
    """Server-Sent Events. Resumes after Last-Event-ID (or ?after=)."""
    j = _job_or_404(request, job_id, None)
    try:
        start = int(request.headers.get("last-event-id") or after or 0)
    except ValueError:
        start = 0

    async def gen():
        sent = start
        idle = 0.0
        yield "retry: 2000\n\n"
        while True:
            evs = j.events[sent:]
            for ev in evs:
                sent = ev["id"]
                yield f"id: {ev['id']}\nevent: {ev['type']}\ndata: {json.dumps({**ev['data'], 't': ev['t']})}\n\n"
            if j.state in ("done", "failed", "cancelled") and sent >= len(j.events):
                yield f"event: end\ndata: {json.dumps({'state': j.state})}\n\n"
                return
            if await request.is_disconnected():
                return
            await asyncio.sleep(0.25)
            idle += 0.25
            if idle >= 15:
                idle = 0
                yield ": keep-alive\n\n"

    return _StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

# StaticFiles mount — serves every current static URL at /
#
# Must come AFTER all /api routes so FastAPI dispatches /api/* before
# falling back to the static mount.
# ----------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_ROOT), html=True),
          name="static")
