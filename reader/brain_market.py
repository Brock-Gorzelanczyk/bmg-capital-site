"""Market data for the brain's pages on the local Crawler: quotes and the earnings calendar.

Quotes: Finnhub's free quote API (real-time US prices) when FINNHUB_API_KEY is set, kept under its free limit of 60
calls a minute; otherwise Yahoo's public chart data (no key, may be delayed). Earnings dates: Finnhub's free earnings
calendar with the key, otherwise Nasdaq's public earnings calendar. Both are SECONDARY: a price or a date shown here is
context for the page, never a source for a figure in a note or a model (M39); a report date is confirmed on the
company's own investor-relations page.

Everything is cached in memory (quotes for a minute, the calendar for six hours) and the calendar also on disk, so a
page refresh does not call out again. Named brain_* so a change here does not make My reads read every saved file again.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

import httpx

UA = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*"}
TIMEOUT = 6.0
QUOTE_TTL = 60
CAL_TTL = 6 * 3600
FINNHUB_PER_MIN = 55

_quotes: dict = {}                 # ticker -> (fetched at, quote dict)
_cal: dict = {}                    # (from, to) -> (fetched at, calendar dict)
_calls: deque = deque()            # times of recent Finnhub calls
_lock = threading.Lock()


def key() -> Optional[str]:
    """FINNHUB_API_KEY, else the first line of ~/.bmg-finnhub-key (the owner's free key, kept out of the vault)."""
    k = os.environ.get("FINNHUB_API_KEY")
    if not k:
        try:
            k = (Path.home() / ".bmg-finnhub-key").read_text().strip().splitlines()[0].strip()
        except (OSError, IndexError):
            k = None
    return k or None


def status() -> dict:
    k = bool(key())
    return {"finnhub_key": k,
            "quotes_source": "Finnhub, real-time (free key)" if k else "Yahoo public data, may be delayed (no key set)",
            "earnings_source": "Finnhub earnings calendar" if k else "Nasdaq public earnings calendar",
            "how_to_go_live": None if k else "Get a free key at finnhub.io, put it in ~/.bmg-finnhub-key, restart the reader."}


def _finnhub_slot() -> bool:
    """True when a Finnhub call fits under the free limit right now."""
    now = time.time()
    with _lock:
        while _calls and now - _calls[0] > 60:
            _calls.popleft()
        if len(_calls) >= FINNHUB_PER_MIN:
            return False
        _calls.append(now)
        return True


async def _finnhub_quote(client: httpx.AsyncClient, t: str) -> Optional[dict]:
    if not key() or not _finnhub_slot():
        return None
    r = await client.get("https://finnhub.io/api/v1/quote", params={"symbol": t.replace("-", "."), "token": key()}, timeout=TIMEOUT)
    if r.status_code != 200:
        return None
    d = r.json()
    if not d or not d.get("c"):
        return None
    return {"price": d["c"], "change": d.get("d"), "change_pct": d.get("dp"), "prev_close": d.get("pc"),
            "at": _dt.datetime.fromtimestamp(d["t"], _dt.timezone.utc).isoformat() if d.get("t") else None, "source": "Finnhub"}


async def _yahoo_quote(client: httpx.AsyncClient, t: str) -> Optional[dict]:
    r = None
    for host in ("query1", "query2"):                       # the second host when the first one is busy (429)
        r = await client.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}", params={"interval": "1d", "range": "5d"},
                             headers=UA, timeout=TIMEOUT)
        if r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        return None
    meta = ((r.json().get("chart") or {}).get("result") or [{}])[0].get("meta") or {}
    p, pc = meta.get("regularMarketPrice"), meta.get("chartPreviousClose") or meta.get("previousClose")
    if not p:
        return None
    ch = (p - pc) if pc else None
    return {"price": p, "change": round(ch, 4) if ch is not None else None, "change_pct": round(ch / pc * 100, 3) if ch is not None and pc else None,
            "prev_close": pc, "at": _dt.datetime.fromtimestamp(meta["regularMarketTime"], _dt.timezone.utc).isoformat() if meta.get("regularMarketTime") else None,
            "source": "Yahoo"}


async def quotes(tickers: list[str], client: Optional[httpx.AsyncClient] = None) -> dict:
    """{ticker: quote or {"error": ...}} for up to 60 tickers; a quote younger than a minute comes from memory."""
    want = []
    for t in tickers:
        t = (t or "").strip().upper()
        if t and t not in want:
            want.append(t)
    want = want[:60]
    out, todo, now = {}, [], time.time()
    for t in want:
        hit = _quotes.get(t)
        if hit and now - hit[0] < QUOTE_TTL:
            out[t] = hit[1]
        else:
            todo.append(t)
    if todo:
        own = client is None
        client = client or httpx.AsyncClient(follow_redirects=True)
        sem = asyncio.Semaphore(6)

        async def one(t):
            async with sem:
                q = None
                for f in (_finnhub_quote, _yahoo_quote):
                    try:
                        q = await f(client, t)
                    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                        q = None
                    if q:
                        break
                if q:
                    _quotes[t] = (time.time(), q)
                out[t] = q or {"error": "no quote"}
        try:
            await asyncio.gather(*(one(t) for t in todo))
        finally:
            if own:
                await client.aclose()
    return {t: out.get(t) for t in want}


def _nasdaq_rows(d: dict) -> list:
    return ((d.get("data") or {}).get("rows")) or []


async def earnings(universe: set, days: int = 14, cache_dir: Optional[Path] = None, client: Optional[httpx.AsyncClient] = None,
                   today: Optional[_dt.date] = None) -> dict:
    """The report dates of the map's companies over the next `days` days: [{ticker, date, hour}], hour "before open",
    "after close" or "" when not given."""
    today = today or _dt.date.today()
    end = today + _dt.timedelta(days=max(1, min(days, 31)))
    ck = (today.isoformat(), end.isoformat(), bool(key()))
    hit = _cal.get(ck)
    if hit and time.time() - hit[0] < CAL_TTL:
        return _only(hit[1], universe)
    disk = Path(cache_dir) / f"earnings-{ck[0]}-{ck[1]}-{'f' if ck[2] else 'n'}.json" if cache_dir else None
    if disk and disk.exists() and time.time() - disk.stat().st_mtime < CAL_TTL:
        try:
            cal = json.loads(disk.read_text())
            _cal[ck] = (time.time(), cal)
            return _only(cal, universe)
        except ValueError:
            pass
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    events, source, errors = [], None, []
    try:
        if key():
            try:
                r = await client.get("https://finnhub.io/api/v1/calendar/earnings",
                                     params={"from": today.isoformat(), "to": end.isoformat(), "token": key()}, timeout=15)
                if r.status_code == 200:
                    hour = {"bmo": "before open", "amc": "after close", "dmh": "during market"}
                    events = [{"ticker": (e.get("symbol") or "").upper().replace(".", "-"), "date": e.get("date"), "hour": hour.get(e.get("hour") or "", "")}
                              for e in (r.json().get("earningsCalendar") or []) if e.get("symbol") and e.get("date")]
                    source = "Finnhub earnings calendar"
                else:
                    errors.append(f"Finnhub answered {r.status_code}")
            except (httpx.HTTPError, ValueError) as e:
                errors.append(f"Finnhub: {e.__class__.__name__}")
        if source is None:
            hour = {"time-pre-market": "before open", "time-after-hours": "after close"}
            d = today
            while d <= end:
                if d.weekday() < 5:
                    try:
                        r = await client.get("https://api.nasdaq.com/api/calendar/earnings", params={"date": d.isoformat()}, headers=UA, timeout=15)
                        if r.status_code == 200:
                            for row in _nasdaq_rows(r.json()):
                                if row.get("symbol"):
                                    events.append({"ticker": row["symbol"].upper().replace(".", "-").replace("/", "-"), "date": d.isoformat(),
                                                   "hour": hour.get(row.get("time") or "", "")})
                            source = "Nasdaq earnings calendar"
                        else:
                            errors.append(f"Nasdaq answered {r.status_code} for {d}")
                    except (httpx.HTTPError, ValueError) as e:
                        errors.append(f"Nasdaq {d}: {e.__class__.__name__}")
                d += _dt.timedelta(days=1)
    finally:
        if own:
            await client.aclose()
    cal = {"from": today.isoformat(), "to": end.isoformat(), "source": source, "errors": errors[:5],
           "as_of": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "events": events}
    if source:
        _cal[ck] = (time.time(), cal)
        if disk:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                disk.write_text(json.dumps(cal))
            except OSError:
                pass
    return _only(cal, universe)


def _only(cal: dict, universe: set) -> dict:
    ev = [e for e in cal.get("events") or [] if not universe or e["ticker"] in universe]
    ev.sort(key=lambda e: (e["date"], {"before open": 0, "during market": 1, "after close": 2}.get(e["hour"], 3), e["ticker"]))
    return {**{k: v for k, v in cal.items() if k != "events"}, "events": ev,
            "note": "SECONDARY: report dates as listed by " + (cal.get("source") or "no calendar") + ". Confirm on the company's investor-relations page."}


def revenue(memories: list[dict]) -> dict:
    """{ticker: {value, display, period, page}}: the latest fiscal-year revenue in each company's brain memory (the
    10-K's own figure, with its page), used to size the company on the map."""
    out = {}
    for m in memories:
        s = (m.get("series") or {}).get("fact:revenue:FY") or {}
        pts = s.get("points") or {}
        if not pts:
            continue
        end = max(pts)
        p = pts[end]
        if isinstance(p.get("value"), (int, float)) and p["value"] > 0:
            src = p.get("src") or {}
            out[(m.get("ticker") or "").upper()] = {"value": p["value"], "display": p.get("display"), "period": end,
                                                    "page": src.get("printed_page") or src.get("pdf_page"), "form": src.get("form")}
    return out
