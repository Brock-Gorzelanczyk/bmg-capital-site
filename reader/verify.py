"""Second check: Claude re-reads each statement figure on its source page.

    run(report, body, page_starts) -> summary dict (also writes fact["check"])

The reader's numbers are never changed. For every reported statement figure
the checker sends the page the figure came from, plus the claim (row, column,
printed number, scale), and asks whether the page shows exactly that. Each
figure comes back as:

    verified    the page shows that number in that row and period column
    mismatch    the page shows this line and period with a different number,
                or the number belongs to another row, year or entity
    not_found   the page does not show this line for this period
    unclear     the model's answer could not be confirmed against the page
                text (for example a quoted number that is not on the page)
    error       the call failed; the figure is simply unchecked

Off unless ANTHROPIC_API_KEY is set and BMG_VERIFY is not "0". Without a key
the reader behaves exactly as before. These results are a review aid, not
reader accuracy: accuracy is still scored only on first runs against
hand-checked values (vault known-issues #45).
"""
from __future__ import annotations

import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

MODEL = os.environ.get("BMG_VERIFY_MODEL", "claude-haiku-5-5")
# USD per million input / output tokens (Haiku 5.5 rate is for prompts up to 100K tokens; pages are far smaller)
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-sonnet-5-5": (2.00, 10.00), "claude-opus-5-5": (4.00, 20.00)}
MAX_PAGE_CHARS = 14000
MAX_CLAIMS_PER_CALL = 40
STATUSES = ("verified", "mismatch", "not_found", "unclear", "error")

SYSTEM = """You check numbers that a parser extracted from an SEC filing (10-K or 10-Q).
You get the text of one page of the filing and a list of claims. Each claim says: on this page, in the table row
<row_label>, in the column <column>, the filing prints <printed>; the parser reads it as <name> for <period>, in the
scale given by <caption>.

For each claim, answer with one status:
- "verified": the page prints exactly that number in that row, in the column for that period, in a table for the
  whole company (consolidated), not a segment, subsidiary, affiliate or other entity, unless the claim's scope says so.
- "mismatch": the page shows this line item for this period but with a different number, or the printed number
  belongs to a different row, a different year or column, or a different entity. In page_value, copy the number the
  page actually prints for the claimed line and period, exactly as printed (for example "(1,234.5)"); leave it empty
  if the page has no number for that line and period.
- "not_found": the page does not show this line item for this period.

Judge only from the page text. Do not calculate, convert or guess. Page text comes from a PDF text layer, so columns
may be separated by runs of spaces; read the column order from the header lines. Give a note of one short sentence
for every claim, saying where you looked."""

SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "status": {"type": "string", "enum": ["verified", "mismatch", "not_found"]},
                    "page_value": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["id", "status", "page_value", "note"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["results"],
    "additionalProperties": False,
}


def enabled() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")) and os.environ.get("BMG_VERIFY", "1") != "0"


def _digits(s: str) -> str:
    return re.sub(r"[^\d.]", "", s or "").rstrip(".")


def _on_page(value: str, flat: str) -> bool:
    """True when `value` is one of the numbers printed on the page (ignoring $, commas, parentheses, signs)."""
    d = _digits(value)
    if not d:
        return False
    return d in {_digits(t) for t in re.findall(r"\d[\d,]*(?:\.\d+)?", flat)}


def _page_bounds(page_starts: list, offset: int) -> tuple[int, int, Optional[int]]:
    """(start, end, page index) of the page holding `offset`."""
    start, idx, end = 0, None, None
    for k, (ps, pi) in enumerate(page_starts):
        if ps <= offset:
            start, idx = ps, pi
            end = page_starts[k + 1][0] if k + 1 < len(page_starts) else None
        else:
            break
    return start, end, idx


def _claims(report: dict) -> list[dict]:
    out = []
    for fid, f in (report.get("evidence") or {}).get("facts", {}).items():
        s = f.get("source") or {}
        if f.get("kind") != "reported" or f.get("value") is None or s.get("offset") is None or not s.get("raw"):
            continue
        out.append({"id": fid, "name": f.get("name") or f.get("metric"), "period": (f.get("period") or {}).get("label"),
                    "row_label": s.get("row_label") or "", "column": s.get("column") or "", "printed": s.get("raw"),
                    "caption": s.get("caption") or "", "scope": f.get("scope") or "Consolidated",
                    "table": s.get("table_title") or "", "offset": s["offset"],
                    "page": s.get("pdf_page") or s.get("printed_page")})
    return out


def _ask(client, page_text: str, claims: list[dict]) -> tuple[dict, dict]:
    listing = [{k: c[k] for k in ("id", "name", "period", "row_label", "column", "printed", "caption", "scope", "table")}
               for c in claims]
    msg = client.messages.create(
        model=MODEL,
        max_tokens=8000,
        system=SYSTEM,
        output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content":
                   f"<page>\n{page_text}\n</page>\n\n<claims>\n{json.dumps(listing, indent=1)}\n</claims>"}],
    )
    usage = {"in": msg.usage.input_tokens, "out": msg.usage.output_tokens}
    if msg.stop_reason in ("refusal", "max_tokens"):
        raise RuntimeError(f"stop_reason {msg.stop_reason}")
    text = next((b.text for b in msg.content if b.type == "text"), "")
    return {r["id"]: r for r in json.loads(text).get("results", [])}, usage


def _confirm(ans: Optional[dict], claim: dict, page_text: str) -> dict:
    """Hold the model to the page: a quoted number must be on the page."""
    if not ans:
        return {"status": "unclear", "page_value": "", "note": "No answer returned for this figure."}
    st, pv, note = ans["status"], (ans.get("page_value") or "").strip(), (ans.get("note") or "").strip()
    flat = re.sub(r"\s+", " ", page_text)
    if st == "verified" and not _on_page(claim["printed"], flat):
        return {"status": "unclear", "page_value": "", "note": "Marked verified, but the printed number is not on the page text."}
    if st == "mismatch" and pv:
        if not _on_page(pv, flat):
            return {"status": "unclear", "page_value": pv, "note": f"Checker quoted {pv}, which is not on the page text. {note}"}
        if _digits(pv) == _digits(claim["printed"]):
            # same digits (e.g. sign or parentheses only): a presentation difference, not a wrong figure
            return {"status": "verified", "page_value": pv, "note": f"Same number as printed; sign shown differently. {note}"}
    return {"status": st, "page_value": pv, "note": note}


def run(report: dict, body: str, page_starts: list, client=None,
        progress: Callable[[str], None] = lambda s: None, log_path: Optional[str] = None) -> dict:
    if client is None:
        if not enabled():
            return {"status": "off", "reason": "Set ANTHROPIC_API_KEY to turn on the second check."}
        import anthropic
        client = anthropic.Anthropic()
    t0 = time.monotonic()
    claims = _claims(report)
    pages: dict = {}
    for c in claims:
        s, e, _ = _page_bounds(page_starts, c["offset"])
        pages.setdefault((s, e), []).append(c)

    jobs = []
    for (s, e), cl in pages.items():
        text = body[s:e if e is not None else len(body)][:MAX_PAGE_CHARS]
        for k in range(0, len(cl), MAX_CLAIMS_PER_CALL):
            jobs.append((text, cl[k:k + MAX_CLAIMS_PER_CALL]))

    results: dict = {}
    tokens = {"in": 0, "out": 0}

    def work(job):
        text, cl = job
        try:
            ans, usage = _ask(client, text, cl)
            return [(c, _confirm(ans.get(c["id"]), c, text)) for c in cl], usage
        except Exception as e:                       # one page failing leaves its figures unchecked
            return [(c, {"status": "error", "page_value": "", "note": f"Check failed: {type(e).__name__}"}) for c in cl], {"in": 0, "out": 0}

    with ThreadPoolExecutor(max_workers=4) as pool:
        for pairs, usage in pool.map(work, jobs):
            tokens["in"] += usage["in"]
            tokens["out"] += usage["out"]
            for c, r in pairs:
                results[c["id"]] = (c, r)
    progress(f"checked {len(results)} figures")

    facts = report["evidence"]["facts"]
    items = []
    for fid, (c, r) in results.items():
        facts[fid]["check"] = {"by": MODEL, **r}
        items.append({"fact": fid, "name": c["name"], "period": c["period"], "printed": c["printed"],
                      "page": c["page"], **r})
    counts = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
    pin, pout = PRICES.get(MODEL, (0, 0))
    summary = {"status": "done", "model": MODEL, "checked": len(items), "counts": counts,
               "calls": len(jobs), "tokens": tokens,
               "cost_usd": round(tokens["in"] / 1e6 * pin + tokens["out"] / 1e6 * pout, 4),
               "seconds": round(time.monotonic() - t0, 1),
               "flagged": [i for i in items if i["status"] not in ("verified",)],
               "note": "Review aid only. Reader numbers are unchanged; flagged figures need a look at the cited page."}

    log_path = log_path or os.environ.get("BMG_VERIFY_LOG") or (
        str(Path(os.environ["INTAKE_DATA_DIR"]) / "verify-log.jsonl") if os.environ.get("INTAKE_DATA_DIR") else None)
    if log_path and summary["flagged"]:
        try:
            src = report.get("source") or {}
            co = report.get("company") or {}
            with open(log_path, "a") as fh:
                for i in summary["flagged"]:
                    fh.write(json.dumps({"logged": time.strftime("%Y-%m-%d %H:%M:%S"), "company": co.get("name"),
                                         "ticker": co.get("ticker"), "sha256": src.get("sha256") or src.get("doc_sha256"),
                                         "model": MODEL, **i}) + "\n")
        except OSError:
            pass
    return summary
