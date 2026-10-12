"""IR RSS feeds (GATX / GBX / TRN). feedparser + follow same-domain PDF links."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from urllib.parse import urlparse

from .. import store, http as hp

log = logging.getLogger("intake.ir")

try:
    import feedparser
    FP_OK = True
except ImportError:
    FP_OK = False


def _slugify(s: str) -> str:
    import re
    s = re.sub(r"[^A-Za-z0-9]+", "-", s or "")
    return s.strip("-").lower()[:80]


async def poll_feed(ticker: str, feed_url: str) -> dict:
    counts = {"ticker": ticker, "new": 0, "errors": []}
    run_id = store.start_run(f"ir_feeds:{ticker}")
    try:
        if not FP_OK:
            raise RuntimeError("feedparser not installed")
        res = await hp.fetch(feed_url)
        parsed = feedparser.parse(res.content)
        for e in parsed.entries:
            guid = getattr(e, "id", None) or getattr(e, "guid", None) or getattr(e, "link", None)
            if not guid:
                continue
            title = getattr(e, "title", "") or ""
            link = getattr(e, "link", "")
            pub = getattr(e, "published", None) or getattr(e, "updated", None) or ""
            if store.already_have(source_url=link):
                continue
            # Fetch the HTML
            try:
                html_res = await hp.fetch(link)
            except Exception as fe:
                counts["errors"].append(f"{link}: {fe}")
                continue
            sha = store.sha256_bytes(html_res.content)
            raw_dir = store.DATA_DIR / "raw" / ticker / "ir"
            raw_dir.mkdir(parents=True, exist_ok=True)
            raw_path = raw_dir / f"{pub[:10]}-{_slugify(title)}.html"
            raw_path.write_bytes(html_res.content)
            did = store.insert_document(
                ticker=ticker, doc_type="PRESS_RELEASE", title=title,
                source="ir", source_url=link, published_at=pub,
                sha256=sha, raw_path=str(raw_path.relative_to(store.DATA_DIR)),
                text_path=None, bytes_=len(html_res.content),
                meta={"feed_url": feed_url},
            )
            if did:
                counts["new"] += 1
            # Follow same-domain PDF links in the HTML
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_res.content, "html.parser")
                host = urlparse(link).hostname
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if not href.lower().endswith(".pdf"):
                        continue
                    if href.startswith("//"):
                        href = "https:" + href
                    elif href.startswith("/"):
                        href = f"https://{host}" + href
                    elif not href.startswith("http"):
                        continue
                    if urlparse(href).hostname != host:
                        continue
                    if store.already_have(source_url=href):
                        continue
                    try:
                        pdf_res = await hp.fetch(href)
                        psha = store.sha256_bytes(pdf_res.content)
                        pdf_dir = store.DATA_DIR / "raw" / ticker / "ir"
                        pdf_dir.mkdir(parents=True, exist_ok=True)
                        name = urlparse(href).path.rsplit("/", 1)[-1] or "deck.pdf"
                        pdf_path = pdf_dir / f"{pub[:10]}-{name}"
                        pdf_path.write_bytes(pdf_res.content)
                        did = store.insert_document(
                            ticker=ticker, doc_type="DECK", title=f"{title} (deck)",
                            source="ir", source_url=href, published_at=pub,
                            sha256=psha, raw_path=str(pdf_path.relative_to(store.DATA_DIR)),
                            text_path=None, bytes_=len(pdf_res.content),
                            meta={"from_release": link},
                        )
                        if did:
                            counts["new"] += 1
                    except Exception as pe:
                        counts["errors"].append(f"PDF {href}: {pe}")
            except Exception as se:
                log.warning(f"soup parse failed on {link}: {se}")
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
        counts["errors"].append(str(e))
    return counts
