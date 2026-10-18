"""Run the reader stages on one document.

    run(data, kind, title, ticker, progress, cancelled) -> result dict

`progress(stage, detail)` is called as each stage starts and ends; the job
system turns these into events. `cancelled()` is checked between stages.
"""
from __future__ import annotations

import re
import time
from typing import Callable, Optional

from . import document, facts as F, profiles, report, schema, signals, tables


class Cancelled(Exception):
    pass


STAGES = ["Upload", "Parse", "Identify", "Extract", "Validate", "Analyze", "Build report", "Ready"]


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
        cancelled: Callable[[], bool] = lambda: False, company: Optional[str] = None) -> dict:
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

    # Identify: who this is, what kind of business, and what to look for,
    # before any figure is extracted.
    t = stage("Identify")
    co = report._company(doc)
    prof, scores = profiles.profile_for(doc)
    br = signals.brief(co, prof, scores)
    done("Identify", t, company=co.get("name"), ticker=co.get("ticker") or ticker, form=co.get("form"), profile=prof, brief=br)

    t = stage("Extract")
    rows = tables.parse(doc)
    fb = F.FactBook(doc, rows).extract()
    claims = _claims(doc)
    done("Extract", t, table_rows=len(rows), facts=len(fb.facts))

    t = stage("Validate")
    checks = len(fb.checks)
    done("Validate", t, checks=checks, failed=sum(1 for c in fb.checks if not c["passed"]), conflicts=len(fb.issues))

    t = stage("Analyze")
    if " + " in prof:
        # list the larger business first, by the revenue lines that name them
        a_, b_ = prof.split(" + ")
        word = lambda p: "leas" if "leasing" in p.lower() else "manufactur" if "manufactur" in p.lower() else p.split()[0].lower()
        rev = lambda w: sum(r["values"][0] for r in rows if r["statement"] == "income" and r["unit"] == "USD"
                            and r["key"].startswith(w) and len(r["key"].split()) <= 5)
        if rev(word(b_)) > rev(word(a_)):
            prof = f"{b_} + {a_}"
            br["profile"] = prof
            br["why"] = br["why"].replace(f"{a_.lower()} + {b_.lower()}", f"{b_.lower()} + {a_.lower()}") + f" {b_} is listed first because it is the larger revenue line."
    report.calculations(fb, prof == "Banking")
    kp = profiles.kpis(doc, rows)
    from . import risks as _risks
    watch, _ = _risks.watch_items(doc, rows)
    sigs = signals.analyze(fb, prof, kp["items"], watch)
    signals.focus_status(br, sigs, kp["items"], fb)
    sev = {k: sum(1 for x in sigs if x["severity"] == k) for k in signals.SEVERITIES}
    done("Analyze", t, signals=len(sigs), **sev, highlights=[{"headline": x["headline"], "severity": x["severity"]} for x in sigs[:8]])

    t = stage("Build report")
    rep = report.build(doc, rows, fb, claims, analysis={"brief": br, "signals": sigs})
    if ticker and not rep["company"].get("ticker"):
        rep["company"]["ticker_hint"] = ticker
    if company and not rep["company"].get("name"):
        rep["company"]["name_hint"] = company          # SEC EDGAR's name for the ticker; the document itself did not name the company
    rep = schema.validate(rep)
    # optional second check (Claude re-reads each statement figure on its page); off without ANTHROPIC_API_KEY
    from . import verify as _verify
    rep["second_check"] = _verify.run(rep, doc.body, doc.page_starts)
    sc = rep["second_check"]
    if sc.get("status") == "done":
        rep["methodology"].append(f"Second check: a language model ({sc.get('model')}) re-read {sc.get('checked')} figures on their cited pages and "
                                  "labelled each one; it cannot change a value. Its results are a review aid, not part of the extraction.")
    from . import live as _live
    rep["live"] = _live.plan(rep)            # built after validation: nothing withdrawn can appear
    # guidance, red flags, a call's speakers and questions, the section map: quoted sentences, kept beside the report
    from . import insights as _insights
    t_ins = time.monotonic()
    try:
        ins = _insights.build(doc, rep, title)
    except Exception as e:                   # never lose a finished report to the extra analysis
        ins = {"version": 1, "kind": "other", "label": "Document", "errors": [f"insights: {e.__class__.__name__}"]}
    timings["Insights"] = round(time.monotonic() - t_ins, 2)
    rep["timings"] = timings
    sc = rep["second_check"]
    done("Build report", t, findings=len(rep["findings"]), validation_passed=rep["validation"]["passed"],
         second_check=sc["status"], **({"checked": sc["checked"], **sc["counts"]} if sc["status"] == "done" else {}))

    return {"report": rep, "text": doc.body, "page_starts": doc.page_starts, "sha256": doc.sha256,
            "title": title, "kind": kind, "pages": len(doc.pages), "insights": ins}
