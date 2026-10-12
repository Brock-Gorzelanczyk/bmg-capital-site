"""Daily digest builder: /data/digests/YYYY-MM-DD.md from rows added today.
Format per intake-bot-stack.md (c) 'Digest format'.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import store

log = logging.getLogger("intake.digest")


def build_digest(for_date: str | None = None) -> Path:
    """Build the digest for `for_date` (YYYY-MM-DD) or today UTC if None."""
    date = for_date or time.strftime("%Y-%m-%d", time.gmtime())
    run_id = store.start_run("digest")
    try:
        with store.connect() as conn:
            rows = conn.execute(
                """SELECT * FROM documents
                   WHERE date(fetched_at) = ?
                   ORDER BY ticker, type, published_at DESC""", (date,)
            ).fetchall()
        buckets = {"filings": [], "pr": [], "rail_macro": [], "news": [], "run": []}
        for r in rows:
            d = dict(r)
            try: d["meta"] = json.loads(d.get("meta_json") or "{}")
            except Exception: d["meta"] = {}
            if d["type"] in ("8-K", "10-Q", "10-K", "FORM4", "4", "EX-99"):
                buckets["filings"].append(d)
            elif d["type"] in ("PRESS_RELEASE", "DECK"):
                buckets["pr"].append(d)
            elif d["type"] in ("SERIES", "RAIL_TRAFFIC", "RAILCAR_ORDERS"):
                buckets["rail_macro"].append(d)
            elif d["type"] == "NEWS":
                buckets["news"].append(d)
        md = [f"# Desk digest, {date}\n"]
        md.append("## Filings")
        if not buckets["filings"]:
            md.append("- (none today)")
        for d in buckets["filings"]:
            title = (d["title"] or "")[:120]
            md.append(f"- {d['ticker']} {d['type']} ({d.get('published_at','')}): {title} [source]({d['source_url']})")
        md.append("\n## Press releases and decks")
        if not buckets["pr"]:
            md.append("- (none today)")
        for d in buckets["pr"]:
            md.append(f"- {d['ticker']} {d['type']} ({d.get('published_at','')}): {d['title']} [source]({d['source_url']})")
        md.append("\n## Rail and macro")
        if not buckets["rail_macro"]:
            md.append("- (none today)")
        for d in buckets["rail_macro"]:
            extra = ""
            if d["type"] == "SERIES":
                extra = f" last {d.get('meta',{}).get('last_date')}={d.get('meta',{}).get('last_value')}"
            if d["type"] == "RAIL_TRAFFIC":
                m = d.get("meta", {})
                if m.get("carloads") or m.get("intermodal"):
                    extra = f" carloads={m.get('carloads')} intermodal={m.get('intermodal')}"
            md.append(f"- {d['type']} {d['title']} ({d['source']}){extra} [source]({d['source_url']})")
        md.append("\n## Headlines (deduped)")
        if not buckets["news"]:
            md.append("- (none today)")
        # 10 max per ticker
        seen = {}
        for d in buckets["news"]:
            t = d["ticker"]
            seen.setdefault(t, 0)
            if seen[t] >= 10:
                continue
            seen[t] += 1
            md.append(f"- {t}: {d['title']} ({d.get('meta',{}).get('publisher','')} {d.get('published_at','')}) [{d['source_url']}]({d['source_url']})")
        md.append("\n## Run status")
        health = store.health_summary()
        for job, info in health["jobs"].items():
            s = info.get("started_at")
            ok = info.get("ok")
            md.append(f"- {job}: last_run={s} ok={ok}")
        out = store.DATA_DIR / "digests" / f"{date}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(md))
        store.export_index_json()
        store.end_run(run_id, ok=True, counts={"rows": len(rows), "path": str(out)})
        return out
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e))
        raise
