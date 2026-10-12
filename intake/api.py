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
