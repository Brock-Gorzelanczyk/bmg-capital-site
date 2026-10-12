"""BMG Capital research intake bot ("the desk") package.

Modules:
  http: httpx client with aiolimiter + tenacity + robots.txt check.
  store: SQLite at /data/intake.db; documents + runs tables; index.json export.
  sources.edgar: edgartools wrapper per CIK.
  sources.ir_feeds: feedparser on IR RSS.
  sources.macro: FRED API + AAR page + STB xlsx + RSI PDF.
  sources.news: Google News RSS per ticker.
  extract: trafilatura + pdfplumber -> markdown.
  digest: daily digest builder.
  scheduler: APScheduler jobs.
  api: FastAPI /desk router.
  selftest: python -m intake.selftest acceptance.

Entry point from the main FastAPI app:
  from intake.api import router as desk_router
  from intake.scheduler import start_scheduler, stop_scheduler
  app.include_router(desk_router)
"""
__version__ = "0.1.0"
