"""FastAPI router mounted at /desk. Endpoints per WORK-ORDER-INTAKE-1.md:
  GET /desk/feed  (ticker, limit, type, since)
  GET /desk/digest/latest
  GET /desk/health
  GET /desk/doc/{id}

Rate-limited per IP similar to /api/check. In-memory 60s cache on /desk/feed.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import store, events as _events_mod

log = logging.getLogger("intake.api")

router = APIRouter(prefix="/desk", tags=["desk"])

# Simple per-IP rate limiter: 60 requests per minute per IP.
_IP_WINDOW: dict[str, list[float]] = {}
_RATE_LIMIT_PER_MIN = 60


def _check_rate(request: Request) -> None:
    ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    window = _IP_WINDOW.setdefault(ip, [])
    cutoff = now - 60.0
    _IP_WINDOW[ip] = [t for t in window if t > cutoff]
    _IP_WINDOW[ip].append(now)
    if len(_IP_WINDOW[ip]) > _RATE_LIMIT_PER_MIN:
        raise HTTPException(status_code=429, detail="rate limit exceeded; retry in 60s")


_FEED_CACHE: dict[str, tuple[float, dict]] = {}


@router.get("/feed")
async def feed(
    request: Request,
    ticker: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    type: Optional[str] = Query(None, description="Comma-separated doc types"),
    since: Optional[str] = Query(None, description="ISO date; filter published_at >= since"),
) -> dict:
    _check_rate(request)
    types = [t.strip() for t in type.split(",")] if type else None
    cache_key = f"{ticker}|{limit}|{','.join(types or [])}|{since}"
    now = time.monotonic()
    cached = _FEED_CACHE.get(cache_key)
    if cached and now - cached[0] < 60:
        return cached[1]
    items = store.feed_rows(ticker=ticker, limit=limit, types=types, since=since)
    out = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ticker": ticker,
        "count": len(items),
        "items": items,
    }
    _FEED_CACHE[cache_key] = (now, out)
    return out


@router.get("/digest/latest")
async def latest_digest(request: Request) -> dict:
    _check_rate(request)
    digests_dir = store.DATA_DIR / "digests"
    if not digests_dir.exists():
        raise HTTPException(status_code=404, detail="no digests yet")
    files = sorted(digests_dir.glob("*.md"))
    if not files:
        raise HTTPException(status_code=404, detail="no digests yet")
    latest = files[-1]
    return {"date": latest.stem, "markdown": latest.read_text()}


@router.get("/health")
async def health(request: Request) -> dict:
    _check_rate(request)
    return store.health_summary()


@router.post("/_run/{job}")
async def run_job(job: str, request: Request) -> dict:
    """Manually trigger a scheduler job. Idempotent (jobs dedup on source_url).

    Allowed names: edgar, ir, news, fred, aar, stb, rsi, digest, extract.
    "live" is intercepted here and dispatched to run_live() because FastAPI's
    route-registration order would otherwise shadow the /_run/live specific
    route with this /_run/{job} parameterised one.
    """
    if job == "live":
        try:
            body = await request.json()
        except Exception:
            body = {}
        return await run_live(request, body)
    _check_rate(request)
    import asyncio
    from .scheduler import _load_tickers
    from . import digest as digest_mod, extract as extract_mod
    from .sources import edgar as edgar_src, ir_feeds, macro, news as news_src
    counts = {"job": job, "ran": [], "errors": []}
    try:
        if job == "edgar":
            for t in _load_tickers():
                try:
                    r = await edgar_src.poll_edgar(t["ticker"], t["cik"])
                    counts["ran"].append({t["ticker"]: r})
                except Exception as e:
                    counts["errors"].append(f"{t['ticker']}: {e}")
        elif job == "ir":
            for t in _load_tickers():
                try:
                    r = await ir_feeds.poll_feed(t["ticker"], t["ir_rss"])
                    counts["ran"].append({t["ticker"]: r})
                except Exception as e:
                    counts["errors"].append(f"{t['ticker']}: {e}")
        elif job == "news":
            for t in _load_tickers():
                try:
                    r = await news_src.poll_news(t["ticker"], t["news_query"])
                    counts["ran"].append({t["ticker"]: r})
                except Exception as e:
                    counts["errors"].append(f"{t['ticker']}: {e}")
        elif job == "fred":
            counts["ran"].append(await macro.poll_fred())
        elif job == "aar":
            counts["ran"].append(await macro.poll_aar())
        elif job == "stb":
            counts["ran"].append(await macro.poll_stb())
        elif job == "rsi":
            counts["ran"].append(await macro.poll_rsi())
        elif job == "extract":
            counts["ran"].append(extract_mod.extract_all_pending())
        elif job == "digest":
            counts["ran"].append(str(digest_mod.build_digest()))
        else:
            raise HTTPException(status_code=400, detail=f"unknown job: {job}")
    except HTTPException:
        raise
    except Exception as e:
        counts["errors"].append(str(e))
    return counts


@router.get("/run/latest")
async def run_latest(request: Request, window: int = Query(7200, ge=60, le=86400),
                      limit: int = Query(200, ge=1, le=500)) -> dict:
    """Last completed run as an ordered event list. See WORK-ORDER-DESK-2.md."""
    _check_rate(request)
    return store.latest_run_events(window_seconds=window, limit=limit)


@router.get("/events")
async def events_stream(request: Request):
    """Server-Sent Events stream of every job's live events. Heartbeat every 15 s.

    Events: job_start, fetch_start, fetch_done, fetch_done_insert,
    extract_fact, job_done, job_error, heartbeat. See WORK-ORDER-DESK-3.md.
    """
    import asyncio as _asyncio
    import queue as _queue
    import time as _time
    import json as _json
    q = _events_mod.subscribe()

    async def gen():
        try:
            yield _events_mod.sse_format({"type": "hello", "at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())})
            last_hb = _time.time()
            while True:
                if await request.is_disconnected():
                    break
                try:
                    ev = await _asyncio.wait_for(_asyncio.to_thread(q.get, True, 1.0), timeout=2.0)
                    yield _events_mod.sse_format(ev)
                    last_hb = _time.time()
                except (_queue.Empty, _asyncio.TimeoutError):
                    pass
                if _time.time() - last_hb >= 15:
                    yield _events_mod.sse_format({"type": "heartbeat",
                                                   "at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())})
                    last_hb = _time.time()
        finally:
            _events_mod.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/_run/live")
async def run_live(request: Request, body: dict) -> dict:
    """Start a live run (new or rewalk30) for one ticker; one at a time.

    Body: {"ticker": "GATX"|"GBX"|"TRN", "mode": "new"|"rewalk30"}.
    Returns 409 if a live run is already active. Events stream over /desk/events.
    """
    import asyncio as _asyncio
    import datetime as _dt
    from .scheduler import _load_tickers
    from .sources import edgar as edgar_src, ir_feeds
    from . import extract as extract_mod

    _check_rate(request)
    ticker = (body or {}).get("ticker")
    mode = (body or {}).get("mode", "new")
    if ticker not in ("GATX", "GBX", "TRN"):
        raise HTTPException(400, "ticker must be GATX, GBX, or TRN")
    if mode not in ("new", "rewalk30"):
        raise HTTPException(400, "mode must be new or rewalk30")

    label = f"live:{mode}:{ticker}"
    if not _events_mod.try_start(label):
        active = _events_mod.is_active()
        raise HTTPException(409, f"another live run is in progress: {active.get('label')} started {active.get('started_at')}")

    tcfg = next((t for t in _load_tickers() if t["ticker"] == ticker), None)
    if not tcfg:
        _events_mod.release()
        raise HTTPException(500, f"tickers.yaml missing {ticker}")

    async def _run():
        try:
            _events_mod.emit("job_start", label=label, ticker=ticker, mode=mode)
            if mode == "rewalk30":
                cutoff = (_dt.datetime.utcnow() - _dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
                deleted = store.delete_bot_docs_since(ticker, ["edgar", "ir"], cutoff)
                _events_mod.emit("job_log", label=label, msg=f"rewalk30 deleted {deleted} prior rows for {ticker}")
                since = cutoff[:10]
            else:
                since = None
            try:
                r1 = await edgar_src.poll_edgar(ticker, tcfg["cik"], since=since, max_filings=30)
                _events_mod.emit("job_log", label=label, msg=f"edgar: {r1.get('new',0)} new / {r1.get('scanned',0)} scanned")
            except Exception as e:
                _events_mod.emit("job_error", label=label, phase="edgar", message=str(e))
            try:
                r2 = await ir_feeds.poll_feed(ticker, tcfg["ir_rss"])
                _events_mod.emit("job_log", label=label, msg=f"ir_feeds: {r2.get('new',0)} new")
            except Exception as e:
                _events_mod.emit("job_error", label=label, phase="ir", message=str(e))
            try:
                xr = extract_mod.extract_all_pending()
                _events_mod.emit("job_log", label=label, msg=f"extract: {xr.get('facts',0)} facts, {xr.get('html',0)+xr.get('pdf',0)} new extractions")
            except Exception as e:
                _events_mod.emit("job_error", label=label, phase="extract", message=str(e))
            _events_mod.emit("job_done", label=label, ticker=ticker, mode=mode, ok=True)
        except Exception as e:
            _events_mod.emit("job_error", label=label, phase="live", message=str(e))
            _events_mod.emit("job_done", label=label, ticker=ticker, mode=mode, ok=False)
        finally:
            _events_mod.release()

    _asyncio.create_task(_run())
    return {"ok": True, "label": label}


@router.get("/_run/live/status")
async def run_live_status(request: Request) -> dict:
    _check_rate(request)
    return _events_mod.is_active()


@router.get("/doc/{doc_id}/text")
async def doc_text(doc_id: str, request: Request) -> dict:
    """Stored extracted markdown for a document. NEWS rows return title only."""
    _check_rate(request)
    row = store.get_doc(doc_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    if row["type"] == "NEWS":
        return {"id": row["id"], "type": "NEWS", "title": row["title"],
                "text": None,
                "attribution": "Headline only; article body never stored."}
    text = ""
    if row.get("text_path"):
        p = store.DATA_DIR / row["text_path"]
        if p.exists():
            text = p.read_text(errors="ignore")
    facts = store.facts_for_doc(doc_id)
    return {"id": row["id"], "type": row["type"], "title": row["title"],
            "source_url": row["source_url"], "fetched_at": row["fetched_at"],
            "text": text, "facts": facts, "bytes": len(text.encode("utf-8"))}


@router.get("/doc/{doc_id}")
async def doc(doc_id: str, request: Request) -> dict:
    _check_rate(request)
    row = store.get_doc(doc_id)
    if not row:
        raise HTTPException(status_code=404, detail="not found")
    text = None
    if row.get("text_path"):
        p = store.DATA_DIR / row["text_path"]
        if p.exists():
            text = p.read_text(errors="ignore")
    # Serving policy: filings + press release extracts + headlines (with link) + AAR numbers with attribution
    # Do not serve FRED values in bulk or third-party article bodies
    if row["type"] == "SERIES":
        # Only expose last value, not the series
        meta = row.get("meta") or {}
        return {
            "id": row["id"], "type": row["type"], "title": row["title"],
            "source": row["source"], "source_url": row["source_url"],
            "last_date": meta.get("last_date"), "last_value": meta.get("last_value"),
            "attribution": "Source: FRED (St. Louis Fed). Full series available via FRED API.",
        }
    if row["type"] == "NEWS":
        # Headlines only, with link to the publisher
        return {
            "id": row["id"], "type": "NEWS",
            "title": row["title"], "publisher": (row.get("meta") or {}).get("publisher"),
            "link": row["source_url"], "published_at": row["published_at"],
        }
    out = {
        "id": row["id"], "ticker": row["ticker"], "type": row["type"],
        "title": row["title"], "source": row["source"], "source_url": row["source_url"],
        "published_at": row["published_at"], "fetched_at": row["fetched_at"],
        "bytes": row["bytes"], "meta": row.get("meta") or {},
    }
    if text is not None:
        out["text"] = text
    return out
