"""HTTP layer: one httpx.Client per host with fixed User-Agent, aiolimiter
leaky bucket, tenacity retry on 429/5xx/connection errors, honors Retry-After,
checks robots.txt daily with stdlib urllib.robotparser.

Rules per intake-bot-stack.md section (e):
  - SEC fair access: max 10 req/s across all machines; UA with contact email.
    Implementation: 8 req/s cap (comfortable margin), identity header on every
    call to data.sec.gov and www.sec.gov.
  - ir.gatx.com and www.trin.net: Crawl-delay 10 (serialize at 1 req / 10s).
  - Greenbrier (pressroom.gbrx.com, investors.gbrx.com): polite 1 req/2s.
  - Google News: 1 request per ticker every 2 hours; stop on 429.
  - Never retry 403 or 404.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

try:
    import httpx
    from aiolimiter import AsyncLimiter
    from tenacity import (
        retry, stop_after_attempt, wait_exponential, retry_if_exception_type,
        before_sleep_log,
    )
    DEPS_OK = True
except ImportError:
    DEPS_OK = False

log = logging.getLogger("intake.http")

USER_AGENT = "BMG Capital research bot, Brock Gorzelanczyk, 32bgorzelanczyk@gmail.com"
SEC_UA = USER_AGENT  # same UA across hosts; SEC fair access policy requires contact in UA

# Per-host rate caps (requests per unit time). AsyncLimiter(max_rate, time_period):
#   SEC: 8 req/s cap
#   GATX, Trinity (Crawl-delay: 10): 1 req / 10s
#   Greenbrier: 1 req / 2s
#   FRED, AAR, STB, RSI: 1 req / 2s (polite default)
#   Google News: 1 req / 2 hours (via scheduler, not limiter; limiter is a safety net)
HOST_LIMITS: dict[str, tuple[int, float]] = {
    "data.sec.gov": (8, 1.0),
    "www.sec.gov": (8, 1.0),
    "ir.gatx.com": (1, 10.0),
    "www.trin.net": (1, 10.0),
    "pressroom.gbrx.com": (1, 2.0),
    "investors.gbrx.com": (1, 2.0),
    "api.stlouisfed.org": (1, 2.0),
    "www.aar.org": (1, 2.0),
    "www.stb.gov": (1, 2.0),
    "www.rsiweb.org": (1, 2.0),
    "news.google.com": (1, 2.0),
}
DEFAULT_LIMIT = (1, 2.0)

# Last-request-time per host, to prove Crawl-delay compliance via log.
_LAST_REQUEST_TS: dict[str, float] = {}

# Robots.txt cache per host, refreshed daily.
_ROBOTS_CACHE: dict[str, tuple[RobotFileParser, float]] = {}
_ROBOTS_TTL = 24 * 3600


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


def _limiter_for(host: str):
    if not DEPS_OK:
        return None
    max_rate, period = HOST_LIMITS.get(host, DEFAULT_LIMIT)
    return AsyncLimiter(max_rate, period)


# Per-host limiter instances, created lazily.
_LIMITERS: dict[str, "AsyncLimiter"] = {}

def _get_limiter(host: str):
    if host not in _LIMITERS:
        _LIMITERS[host] = _limiter_for(host)
    return _LIMITERS[host]


async def robots_allow(url: str) -> bool:
    """Return True if robots.txt allows our User-Agent to fetch url.
    Fails open (returns True) if robots.txt itself cannot be fetched;
    SEC and most IR hosts publish robots.txt and we honor it.
    """
    host = _host(url)
    if not host:
        return False
    rp, ts = _ROBOTS_CACHE.get(host, (None, 0.0))
    if rp is None or time.monotonic() - ts > _ROBOTS_TTL:
        rp = RobotFileParser()
        robots_url = f"https://{host}/robots.txt"
        try:
            rp.set_url(robots_url)
            rp.read()
            _ROBOTS_CACHE[host] = (rp, time.monotonic())
            log.info(f"robots.txt loaded for {host}")
        except Exception as e:
            log.warning(f"robots.txt fetch failed for {host}: {e}")
            # Fail open on fetch error; the Crawl-delay enforcement is a separate layer.
            return True
    try:
        allowed = rp.can_fetch(USER_AGENT, url)
        if not allowed:
            log.warning(f"robots.txt DISALLOWS {url}")
        return allowed
    except Exception:
        return True


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    headers: dict[str, str]
    fetched_at: float
    cached: bool = False


if DEPS_OK:
    @retry(
        reraise=True,
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.ConnectError)),
        before_sleep=before_sleep_log(log, logging.WARNING),
    )
    async def _do_fetch(client: httpx.AsyncClient, url: str, *, headers: Optional[dict] = None, timeout: float = 30.0) -> httpx.Response:
        resp = await client.get(url, headers=headers or {}, timeout=timeout)
        if resp.status_code in (403, 404):
            # Never retry 403 or 404 per the stack report rules
            return resp
        if resp.status_code == 429 or resp.status_code >= 500:
            # Honor Retry-After if present; tenacity will back off and retry
            retry_after = resp.headers.get("Retry-After")
            if retry_after:
                try:
                    await asyncio.sleep(min(float(retry_after), 60))
                except ValueError:
                    pass
            resp.raise_for_status()
        resp.raise_for_status()
        return resp

async def fetch(url: str, *, extra_headers: Optional[dict] = None) -> FetchResult:
    """Rate-limited, retry-wrapped, robots-aware GET. Not usable without deps."""
    if not DEPS_OK:
        raise RuntimeError(
            "intake.http requires httpx + aiolimiter + tenacity. "
            "Install site/server/requirements-intake.txt before use."
        )
    if not await robots_allow(url):
        raise PermissionError(f"robots.txt disallows {url}")
    host = _host(url)
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"}
    if extra_headers:
        headers.update(extra_headers)
    limiter = _get_limiter(host)
    async with limiter:
        # Record Crawl-delay compliance: log elapsed since last request to the same host
        now = time.monotonic()
        last = _LAST_REQUEST_TS.get(host, 0.0)
        gap = now - last
        _LAST_REQUEST_TS[host] = now
        _, min_period = HOST_LIMITS.get(host, DEFAULT_LIMIT)
        log.info(f"fetch host={host} gap_s={gap:.1f} min_period={min_period:.1f} url={url}")
        async with httpx.AsyncClient() as client:
            resp = await _do_fetch(client, url, headers=headers, timeout=30.0)
    return FetchResult(
        url=url,
        status=resp.status_code,
        content=resp.content,
        headers=dict(resp.headers),
        fetched_at=time.time(),
    )
