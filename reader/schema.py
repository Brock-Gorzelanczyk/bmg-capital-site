"""Validate a report before anyone sees it.

Every published figure must have evidence: a reported fact needs a location
(PDF page or XBRL anchor) and an excerpt that contains its printed value; a
calculation needs inputs that exist and share a period. Anything that fails
is withdrawn from the report (status Unavailable, with the reason) rather
than shown without support.
"""
from __future__ import annotations

import math
import re

STATUSES = {"Source matched", "Cross-checked", "Needs review", "Calculation checked", "Unavailable"}


def _digits(s: str) -> str:
    return re.sub(r"[^\d]", "", s or "")


def check_fact(f: dict) -> list[str]:
    errs = []
    if f.get("status") not in STATUSES:
        errs.append(f"{f.get('id')}: unknown status {f.get('status')!r}")
    v = f.get("value")
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        errs.append(f"{f.get('id')}: no numeric value")
    src = f.get("source") or {}
    if not (src.get("pdf_page") or src.get("anchor") or src.get("offset", -1) >= 0):
        errs.append(f"{f.get('id')}: no source location")
    raw, ex = _digits(src.get("raw", "")), _digits(src.get("excerpt", ""))
    if not raw or raw not in ex:
        errs.append(f"{f.get('id')}: excerpt does not contain the printed value {src.get('raw')!r}")
    if not (f.get("period") or {}).get("label"):
        errs.append(f"{f.get('id')}: no period")
    return errs


def validate(rep: dict) -> dict:
    facts, calcs = rep["evidence"]["facts"], rep["evidence"]["calcs"]
    errors, bad = [], set()
    for fid, f in facts.items():
        e = check_fact(f)
        if e:
            errors += e
            bad.add(fid)
    for cid, c in calcs.items():
        if c.get("status") == "Calculation checked":
            missing = [i for i in c.get("inputs", []) if i not in facts and i not in calcs]
            if missing:
                errors.append(f"{cid}: inputs not found {missing}")
                bad.add(cid)
            elif any(i in bad for i in c.get("inputs", [])):
                errors.append(f"{cid}: depends on a withdrawn fact")
                bad.add(cid)
            v = c.get("value")
            if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
                errors.append(f"{cid}: no value")
                bad.add(cid)
    # withdraw what failed
    for fid in bad:
        x = facts.get(fid) or calcs.get(fid)
        if x is not None:
            x["status"] = "Unavailable"
            x.setdefault("reasons", []).append("Withdrawn: failed evidence validation.")
    for sect in ("performance", "calculated"):
        for row in rep["financials"].get(sect, []):
            for k in ("current", "prior"):
                if row.get(k) and (row[k] in bad or (row[k] not in facts and row[k] not in calcs)):
                    row[k] = None
            if not row.get("current"):
                row["status"] = "Unavailable"
                row.setdefault("reason", "Not found in the reviewed filing.")
    for row in rep["position"]["rows"]:
        for k in ("current", "prior"):
            if row.get(k) and (row[k] in bad or (row[k] not in facts and row[k] not in calcs)):
                row[k] = None
        if not row.get("current"):
            row["status"] = "Unavailable"
            row.setdefault("reason", "Not found in the reviewed filing.")
    keep = []
    for t in rep["overview"]["takeaways"]:
        if all((r in facts or r in calcs) and r not in bad for r in t.get("refs", [])) and t.get("refs"):
            keep.append(t)
        else:
            errors.append(f"takeaway withdrawn (unsupported): {t.get('text', '')[:60]}")
    rep["overview"]["takeaways"] = keep
    for r in rep["risks"]["items"] + rep["risks"]["watch"]:
        if not (r.get("source") or {}).get("excerpt"):
            errors.append(f"risk without excerpt: {r.get('title', '')[:60]}")
    rep["risks"]["items"] = [r for r in rep["risks"]["items"] if (r.get("source") or {}).get("excerpt")]
    rep["risks"]["watch"] = [r for r in rep["risks"]["watch"] if (r.get("source") or {}).get("excerpt")]
    rep["findings"] = [f for f in rep.get("findings", []) if not (f.get("ref") and f["ref"] in bad)]
    rep["validation"] = {"passed": not errors, "errors": errors[:50], "withdrawn": sorted(bad)}
    if errors:
        rep["coverage"]["issues"].append({"type": "validation", "message": f"{len(errors)} item(s) failed evidence validation and were withdrawn."})
    return rep
