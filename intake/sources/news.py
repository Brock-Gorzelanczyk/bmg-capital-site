"""Google News RSS per ticker: headlines + links only; de-duplicated.
Never fetch article bodies. Store title + publisher + date + link.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from urllib.parse import quote

from .. import store, http as hp

log = logging.getLogger("intake.news")


def _title_hash(title: str, publisher: str = "") -> str:
    norm = (title + "|" + publisher).lower().strip()
    return hashlib.sha256(norm.encode()).hexdigest()


async def poll_news(ticker: str, query: str) -> dict:
    counts = {"ticker": ticker, "new": 0, "errors": []}
    run_id = store.start_run(f"news:{ticker}")
    try:
        try:
            import feedparser
        except ImportError:
            raise RuntimeError("feedparser not installed")
        rss_url = f"https://news.google.com/rss/search?q={quote(query)}&hl=en-US&gl=US&ceid=US:en"
        try:
            res = await hp.fetch(rss_url)
        except Exception as e:
            counts["errors"].append(f"news fetch failed, stopping: {e}")
            store.end_run(run_id, ok=False, error=str(e), counts=counts)
            return counts
        parsed = feedparser.parse(res.content)
        news_dir = store.DATA_DIR / "raw" / ticker / "news"
        news_dir.mkdir(parents=True, exist_ok=True)
        today = time.strftime("%Y-%m-%d", time.gmtime())
        jsonl_path = news_dir / f"{today}.jsonl"
        with jsonl_path.open("a") as f:
            for e in parsed.entries:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                source_name = ""
                if hasattr(e, "source") and e.source:
                    source_name = e.source.get("title", "") if isinstance(e.source, dict) else getattr(e.source, "title", "")
                pub = getattr(e, "published", None) or ""
                thash = _title_hash(title, source_name)
                # Dedup: SHA-256 of title plus publisher
                if store.already_have(sha256=thash):
                    continue
                # Google News links are redirect URLs; store title+link+publisher
                record = {"title": title, "link": link, "publisher": source_name, "published_at": pub, "ticker": ticker}
                f.write(json.dumps(record) + "\n")
                did = store.insert_document(
                    ticker=ticker, doc_type="NEWS", title=title,
                    source="gnews", source_url=link, published_at=pub,
                    sha256=thash, raw_path=str(jsonl_path.relative_to(store.DATA_DIR)),
                    text_path=None, bytes_=len(json.dumps(record)),
                    meta={"publisher": source_name},
                )
                if did:
                    counts["new"] += 1
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
        counts["errors"].append(str(e))
    return counts
