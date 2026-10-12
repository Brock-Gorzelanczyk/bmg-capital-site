"""APScheduler BackgroundScheduler wired into FastAPI lifespan.

Schedule (UTC) per WORK-ORDER-INTAKE-1.md:
  edgar_poll: every 15 min, 06:00-23:00 weekdays
  ir_feeds: every 30 min
  news: every 2 hours
  fred: daily 22:00
  aar_weekly: Wed hourly 16:00-22:00 until found
  stb_ep724: weekly Thursday
  rsi_arci: monthly
  digest: daily 23:30

max_instances=1, coalesce=True, misfire_grace_time=600.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

log = logging.getLogger("intake.scheduler")

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    APS_OK = True
except ImportError:
    APS_OK = False

_SCHED: "BackgroundScheduler" | None = None


def _load_tickers() -> list[dict]:
    try:
        import yaml
        p = Path(__file__).parent / "tickers.yaml"
        return yaml.safe_load(p.read_text())["tickers"]
    except Exception as e:
        log.warning(f"tickers.yaml load failed: {e}")
        return []


def _run_async(coro):
    """Run coroutine from APScheduler sync context."""
    try:
        return asyncio.run(coro)
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


def _job_edgar():
    from .sources.edgar import poll_edgar
    for t in _load_tickers():
        _run_async(poll_edgar(t["ticker"], t["cik"]))


def _job_ir():
    from .sources.ir_feeds import poll_feed
    for t in _load_tickers():
        _run_async(poll_feed(t["ticker"], t["ir_rss"]))


def _job_news():
    from .sources.news import poll_news
    for t in _load_tickers():
        _run_async(poll_news(t["ticker"], t["news_query"]))


def _job_fred():
    from .sources.macro import poll_fred
    _run_async(poll_fred())


def _job_aar():
    from .sources.macro import poll_aar
    _run_async(poll_aar())


def _job_stb():
    from .sources.macro import poll_stb
    _run_async(poll_stb())


def _job_rsi():
    from .sources.macro import poll_rsi
    _run_async(poll_rsi())


def _job_digest():
    from .digest import build_digest
    from .extract import extract_all_pending
    extract_all_pending()
    build_digest()


def start_scheduler() -> None:
    global _SCHED
    if not APS_OK:
        log.warning("APScheduler not installed; scheduler not started")
        return
    if _SCHED is not None:
        return
    s = BackgroundScheduler(timezone="UTC", job_defaults={"max_instances": 1, "coalesce": True, "misfire_grace_time": 600})
    s.add_job(_job_edgar, CronTrigger(minute="*/15", hour="6-23", day_of_week="mon-fri"), id="edgar_poll")
    s.add_job(_job_ir, CronTrigger(minute="*/30"), id="ir_feeds")
    s.add_job(_job_news, CronTrigger(hour="*/2"), id="news")
    s.add_job(_job_fred, CronTrigger(hour="22", minute="0"), id="fred")
    s.add_job(_job_aar, CronTrigger(day_of_week="wed", hour="16-22"), id="aar_weekly")
    s.add_job(_job_stb, CronTrigger(day_of_week="thu", hour="20"), id="stb_ep724")
    s.add_job(_job_rsi, CronTrigger(day="1", hour="21"), id="rsi_arci")
    s.add_job(_job_digest, CronTrigger(hour="23", minute="30"), id="digest")
    s.start()
    _SCHED = s
    log.info("intake scheduler started with 8 jobs")


def stop_scheduler() -> None:
    global _SCHED
    if _SCHED is not None:
        _SCHED.shutdown(wait=False)
        _SCHED = None
