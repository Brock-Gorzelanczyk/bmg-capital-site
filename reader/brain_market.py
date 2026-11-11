"""Market data for the brain's pages on the local Crawler: quotes, the price chart and the earnings calendar.

Quotes: Finnhub's free quote API (real-time US prices) when FINNHUB_API_KEY is set, kept under its free limit of 60
calls a minute; otherwise Yahoo's public chart data (no key, may be delayed). The price chart of a company's page
(one day to all of its history) and the day's figures beside it (open, high, low, volume, the 52-week range) come from
the same Yahoo chart data. Earnings dates: Finnhub's free earnings calendar with the key, otherwise Nasdaq's public
earnings calendar. All of it is SECONDARY: a price or a date shown here is context for the page, never a source for a
figure in a note or a model (M39); a report date is confirmed on the company's own investor-relations page.

Everything is cached in memory (quotes for a minute, a chart for one to sixty minutes by its range, the calendar for
six hours) and the calendar also on disk, so a page refresh does not call out again. Named brain_* so a change here
does not make My reads read every saved file again.
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

# The company page's chart ranges: the range and bar size asked of Yahoo, and how long a copy is kept (seconds).
RANGES = {"1D": ("1d", "5m", 60), "1W": ("5d", "15m", 120), "1M": ("1mo", "60m", 600), "3M": ("3mo", "1d", 900),
          "YTD": ("ytd", "1d", 900), "1Y": ("1y", "1d", 900), "5Y": ("5y", "1wk", 3600), "MAX": ("max", "1mo", 3600)}
CHART_FAIL_TTL = 30                # a chart the source did not give is asked for again after half a minute

_quotes: dict = {}                 # ticker -> (fetched at, quote dict)
_charts: dict = {}                 # (ticker, range) -> (fetched at, chart dict)
_chart_calls: dict = {}            # (ticker, range) -> the call under way (two requests for one chart share it)
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


async def _yahoo_result(client: httpx.AsyncClient, t: str, rng: str, interval: str) -> Optional[dict]:
    """Yahoo's chart data for one ticker (its meta block and the bars), or None when neither host gives it."""
    r = None
    for host in ("query1", "query2"):                       # the second host when the first one is busy (429)
        r = await client.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{t}", params={"interval": interval, "range": rng},
                             headers=UA, timeout=TIMEOUT)
        if r.status_code == 200:
            break
    if r is None or r.status_code != 200:
        return None
    res = ((r.json().get("chart") or {}).get("result") or [None])[0]
    return res if isinstance(res, dict) else None


def _prev_close(res: dict) -> Optional[float]:
    """The close of the trading day before the last trade, read from the daily bars themselves: the last bar dated
    before the day of the last trade (the exchange's own calendar day). Yahoo's chartPreviousClose is the close before
    the chart's first bar, which on a five-day chart is about a week back, so it stands in only when the bars give none."""
    meta = res.get("meta") or {}
    at, off = _num(meta.get("regularMarketTime")), _num(meta.get("gmtoffset")) or 0
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    if at:
        today = _dt.datetime.fromtimestamp(at + off, _dt.timezone.utc).date()
        for t, c in reversed(list(zip(res.get("timestamp") or [], q.get("close") or []))):
            if _num(c) and _num(t) is not None and _dt.datetime.fromtimestamp(t + off, _dt.timezone.utc).date() < today:
                return _num(c)
    return _num(meta.get("previousClose")) or _num(meta.get("chartPreviousClose"))


async def _yahoo_quote(client: httpx.AsyncClient, t: str) -> Optional[dict]:
    res = await _yahoo_result(client, t, "5d", "1d") or {}
    meta = res.get("meta") or {}
    p, pc = meta.get("regularMarketPrice"), _prev_close(res)
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


def _num(x) -> Optional[float]:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and x == x and x not in (float("inf"), float("-inf")) else None


def chart_from_yahoo(res: dict, rng: str, ticker: str) -> Optional[dict]:
    """One range of a company's price chart from Yahoo's chart data: the closing price of each bar that has one
    ([time in ms, price], oldest first), the price the range is measured from (the close before it), its change, high
    and low, and the day's figures Yahoo gives with it. None when the data holds no price."""
    meta = res.get("meta") or {}
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0] or {}
    ts = res.get("timestamp") or []
    col = lambda k: q.get(k) or []
    closes, opens, vols = col("close"), col("open"), col("volume")
    pts, first_open, day_vols = [], None, []
    for i, t in enumerate(ts):
        c = _num(closes[i]) if i < len(closes) else None
        if c is None or c <= 0 or _num(t) is None:
            continue                                        # a bar with no trade (Yahoo sends null)
        pts.append([int(t) * 1000, round(c, 4)])
        o = _num(opens[i]) if i < len(opens) else None
        if first_open is None and o:
            first_open = round(o, 4)
        v = _num(vols[i]) if i < len(vols) else None
        if v is not None:
            day_vols.append(v)
    last_px, last_at = _num(meta.get("regularMarketPrice")), _num(meta.get("regularMarketTime"))
    if not pts and not last_px:
        return None
    bar = RANGES[rng][1]
    intraday = bar.endswith("m")
    if last_px and last_at:
        # the line ends on the last price: a new point after the last whole bar, or (daily, weekly and monthly bars)
        # the close of the bar still open
        if not pts or (intraday and last_at * 1000 > pts[-1][0]):
            pts.append([int(last_at) * 1000, round(last_px, 4)])
        elif not intraday:
            off = _num(meta.get("gmtoffset")) or 0
            a = _dt.datetime.fromtimestamp(pts[-1][0] / 1000 + off, _dt.timezone.utc)
            b = _dt.datetime.fromtimestamp(last_at + off, _dt.timezone.utc)
            days = (b.date() - a.date()).days
            open_bar = (a.year, a.month) == (b.year, b.month) if bar == "1mo" else 0 <= days < (7 if bar == "1wk" else 1)
            if open_bar:
                pts[-1][1] = round(last_px, 4)
            elif days > 0:
                pts.append([int(last_at) * 1000, round(last_px, 4)])
    first = pts[0][1]
    base = _num(meta.get("previousClose")) if rng == "1D" else None
    base = base or _num(meta.get("chartPreviousClose"))
    if rng == "MAX" or not base or not (0.2 < base / first < 5):
        base = first                                        # all of its history, or a base that is not this share's price
    last = pts[-1][1]
    out = {"ticker": ticker, "range": rng, "interval": RANGES[rng][1], "points": pts, "base": round(base, 4), "last": last,
           "change": round(last - base, 4), "change_pct": round((last - base) / base * 100, 3) if base else None,
           "high": max(p[1] for p in pts), "low": min(p[1] for p in pts),
           "currency": meta.get("currency"), "exchange": meta.get("fullExchangeName") or meta.get("exchangeName"),
           "name": meta.get("longName") or meta.get("shortName"), "timezone": meta.get("exchangeTimezoneName") or "America/New_York",
           "as_of": _dt.datetime.fromtimestamp(last_at, _dt.timezone.utc).isoformat() if last_at else None, "source": "Yahoo",
           "day": {"high": _num(meta.get("regularMarketDayHigh")), "low": _num(meta.get("regularMarketDayLow")),
                   "volume": _num(meta.get("regularMarketVolume")), "week52_high": _num(meta.get("fiftyTwoWeekHigh")),
                   "week52_low": _num(meta.get("fiftyTwoWeekLow"))}}
    if rng == "1D":
        out["day"]["open"] = first_open
        out["day"]["prev_close"] = round(base, 4)
        reg = ((meta.get("currentTradingPeriod") or {}).get("regular")) or {}
        a, b = _num(reg.get("start")), _num(reg.get("end"))
        # the whole trading day is the chart's width, so the line grows across it through the day (only when the
        # bars are that day's: before the open Yahoo still sends the day before)
        if a and b and b > a and a * 1000 - 3600000 <= pts[0][0] <= b * 1000:
            out["session"] = {"start": int(a) * 1000, "end": int(b) * 1000}
    elif not intraday and len(day_vols) > 5:
        out["avg_volume"] = round(sum(day_vols[:-1]) / len(day_vols[:-1]))      # whole days only: today's is still counting
    return out


async def chart(ticker: str, rng: str = "1D", client: Optional[httpx.AsyncClient] = None) -> dict:
    """A company's price chart over one range (RANGES), from memory when a copy is young enough. When the source does
    not give it: {"points": [], "error": why}. SECONDARY, like every price here."""
    t, rng = (ticker or "").strip().upper(), (rng or "1D").strip().upper()
    if rng not in RANGES:
        return {"ticker": t, "range": rng, "points": [], "error": f"Ranges: {', '.join(RANGES)}."}
    k = (t, rng)
    hit = _charts.get(k)
    if hit and time.time() - hit[0] < (RANGES[rng][2] if hit[1].get("points") else CHART_FAIL_TTL):
        return hit[1]
    going = _chart_calls.get(k)
    if going is None:
        async def fetch() -> dict:
            own = client is None
            c = client or httpx.AsyncClient(follow_redirects=True)
            out = None
            try:
                res = await _yahoo_result(c, t, RANGES[rng][0], RANGES[rng][1])
                out = chart_from_yahoo(res, rng, t) if res else None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                out = None
            finally:
                if own:
                    await c.aclose()
            out = out or {"ticker": t, "range": rng, "points": [], "source": "Yahoo",
                          "error": "The price source did not answer, or has no prices for this ticker."}
            out["note"] = "SECONDARY: prices from Yahoo's public chart data, which may be delayed. Context for the page, never a source for a figure."
            _charts[k] = (time.time(), out)
            return out
        going = _chart_calls[k] = asyncio.ensure_future(fetch())
        going.add_done_callback(lambda _f: _chart_calls.pop(k, None))
    return await asyncio.shield(going)


async def day_stats(ticker: str, client: Optional[httpx.AsyncClient] = None) -> dict:
    """The day's figures for a company's page: open, high, low, volume, the close before, the 52-week range (Yahoo's own
    fields) and the average daily volume of the last three months (worked out here from the daily bars)."""
    d, m = await asyncio.gather(chart(ticker, "1D", client), chart(ticker, "3M", client))
    day = dict((d.get("day") or m.get("day") or {}))
    out = {"ticker": (ticker or "").strip().upper(), "price": d.get("last") or m.get("last"), **{k: day.get(k) for k in
           ("open", "high", "low", "volume", "prev_close", "week52_high", "week52_low")}, "avg_volume": m.get("avg_volume"),
           "avg_volume_basis": "mean of the whole trading days in the last three months" if m.get("avg_volume") else None,
           "currency": d.get("currency") or m.get("currency"), "exchange": d.get("exchange") or m.get("exchange"),
           "as_of": d.get("as_of") or m.get("as_of"), "source": "Yahoo",
           "note": "SECONDARY: the day's figures from Yahoo's public chart data, which may be delayed. Context for the page, never a source for a figure."}
    if not d.get("points") and not m.get("points"):
        out["error"] = d.get("error") or m.get("error")
    return out


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
