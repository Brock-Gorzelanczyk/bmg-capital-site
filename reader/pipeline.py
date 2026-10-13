"""Run the reader stages on one document.

    run(data, kind, title, ticker, progress, cancelled) -> result dict

`progress(stage, detail)` is called as each stage starts and ends; the job
system turns these into events. `cancelled()` is checked between stages.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from . import document, facts as F, report, schema, tables


class Cancelled(Exception):
    pass


STAGES = ["Upload", "Parse", "Extract", "Validate", "Build report", "Ready"]


def _claims(doc) -> list[dict]:
    """Numbers mentioned in the text (fact-checker claims), for the
    'Other figures' group. Shown as unvalidated mentions only."""
    # MD&A only (Item 7 in a 10-K, Item 2 in a 10-Q), capped: narrative
    # figures are most useful there, and scanning a whole 10-K is slow.
    sec = next((s for s in doc.sections if s.item == "7"), None) or next((s for s in doc.sections if s.item == "2" and "discussion" in s.title.lower()), None)
    if not sec:
        return []
    nxt = [s.offset for s in doc.sections if s.offset > sec.offset]
    start, end = sec.offset, min(nxt[0] if nxt else len(doc.body), sec.offset + 60000)
    try:
        import fact_checker as fc
        cl = fc.extract_claims([{"locator": "doc", "text": doc.body[start:end]}])
    except Exception:
        return []
    out = []
    for c in cl[:400]:
        raw = (c.get("raw") or "").strip()
        num = re.search(r"\d[\d,]*\.?\d*", raw)
        if not raw or not num:
            continue
        pos = doc.body.find(raw, start)
        out.append({"display": raw, "value": num.group(0).rstrip("."), "char_offset": pos, "page": doc.page_at(pos) if pos >= 0 else None,
                    "snippet": re.sub(r"\s+", " ", c.get("sentence") or c.get("context") or "")[:220]})
    return out


def run(data: bytes, kind: str, title: str, ticker: Optional[str] = None,
        progress: Callable[[str, dict], None] = lambda s, d: None,
        cancelled: Callable[[], bool] = lambda: False) -> dict:
    timings = {}

    def stage(name: str):
        if cancelled():
            raise Cancelled()
        progress(name, {"state": "running"})
        return time.monotonic()

    def done(name: str, t0: float, **detail):
        timings[name] = round(time.monotonic() - t0, 2)
        progress(name, {"state": "done", "seconds": timings[name], **detail})

    t = stage("Parse")
    doc = document.load(data, kind, title)
    done("Parse", t, pages=len(doc.pages), sections=len(doc.sections), warnings=len(doc.warnings))

    t = stage("Extract")
    rows = tables.parse(doc)
    fb = F.FactBook(doc, rows).extract()
    claims = _claims(doc)
    done("Extract", t, table_rows=len(rows), facts=len(fb.facts))

    t = stage("Validate")
    checks = len(fb.checks)
    done("Validate", t, checks=checks, failed=sum(1 for c in fb.checks if not c["passed"]), conflicts=len(fb.issues))

    t = stage("Build report")
    rep = report.build(doc, rows, fb, claims)
    if ticker and not rep["company"].get("ticker"):
        rep["company"]["ticker_hint"] = ticker
    rep = schema.validate(rep)
    rep["timings"] = timings
    done("Build report", t, findings=len(rep["findings"]), validation_passed=rep["validation"]["passed"])

    return {"report": rep, "text": doc.body, "page_starts": doc.page_starts, "sha256": doc.sha256,
            "title": title, "kind": kind, "pages": len(doc.pages)}
