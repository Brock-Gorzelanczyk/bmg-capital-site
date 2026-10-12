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
from fastapi.responses import JSONResponse

from . import store

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
    Rate-limited per IP (same as every /desk endpoint); no auth required because
    every underlying source is public and every writer is dedup-safe.
    """
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
