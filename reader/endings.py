"""Form-specific clip endings, selected from the finished report and cited document text.

This adds no statement facts and never changes a figure. A normal 10-K or Form 4 keeps its
existing ending. Other forms share this payload between the clip, completed report and PDF.
"""
from __future__ import annotations

import re

from . import insights


_LABELS = {"quarterly": ("Quarterly results and liquidity", "Quarterly report"),
           "release": ("Reported results and stated outlook", "Earnings release"),
           "current": ("What this filing reports", "Current report"),
           "holdings": ("Largest reported positions", "Reported 13F holdings"),
           "transcript": ("What management said and analysts asked", "Earnings call"),
           "annual": ("Findings in this annual filing", "Annual filing"),
           "other": ("Findings in this document", "Document summary")}
_UNAVAILABLE = "No supported findings were located for this section in the reviewed document."


def _source(doc, item):
    src = dict(item.get("source") or {})
    off = src.get("offset", item.get("offset"))
    if off is not None and isinstance(off, int) and off >= 0:
        src = {**doc.location(off), **src, "offset": off}
    if not src.get("pdf_page") and item.get("page"):
        src["pdf_page"] = item["page"]
        src["printed_page"] = item.get("printed_page")
    if item.get("text"):
        src.setdefault("excerpt", item["text"])
    return src


def _quoted(doc, item, label, basis="Quoted from the document"):
    return {"label": label, "text": item.get("text") or "", "value": "", "period": item.get("period") or "",
            "basis": basis, "source": _source(doc, item), "refs": [], "speaker": item.get("speaker") or item.get("analyst") or ""}


def _fact(doc, fact, all_facts):
    src = _source(doc, fact)
    refs = list(fact.get("inputs") or [])
    sources = [all_facts[i].get("source") for i in refs if i in all_facts and all_facts[i].get("source")]
    if not src.get("pdf_page") and sources:
        src = dict(sources[0])
    if not src.get("pdf_page") and not src.get("anchor"):
        return None
    p = fact.get("period") or {}
    basis = "Calculated from cited inputs" if fact.get("kind") == "calculated" else "Reported statement figure"
    if p.get("type") in ("6M", "9M"):
        basis += "; year-to-date, not the quarter alone"
    return {"label": fact.get("name") or fact.get("metric"), "text": "", "value": fact.get("display") or "",
            "period": p.get("label") or "Period not identified", "period_type": p.get("type"),
            "currency": fact.get("currency") or ("USD" if (fact.get("unit") or "").startswith("USD") else ""),
            "unit": fact.get("unit") or "", "scale": fact.get("scale") or (fact.get("source") or {}).get("scale") or "",
            "basis": basis, "source": src, "sources": sources, "refs": [fact["id"]] if fact.get("id") else refs}


def _financial(doc, rep, metrics, quarter=False, historical=False):
    evidence = rep.get("evidence") or {}
    all_facts = {**(evidence.get("facts") or {}), **(evidence.get("calcs") or {})}
    out = []
    for metric in metrics:
        fs = [f for f in all_facts.values() if f.get("metric") == metric and f.get("value") is not None
              and f.get("display") and f.get("status") in ("Source matched", "Cross-checked", "Calculation checked")]
        if quarter and metric in ("revenue", "net_income", "operating_income", "eps_diluted"):
            fs = [f for f in fs if (f.get("period") or {}).get("type") == "Q"]
        if historical:
            fs = [f for f in fs if not insights._GUIDE.search(" ".join(str((f.get("source") or {}).get(k) or "") for k in ("table_title", "header", "row_label", "caption")))
                  and not re.search(r"(?i)non-GAAP|adjusted", " ".join(str((f.get("source") or {}).get(k) or "") for k in ("table_title", "row_label", "caption")))]
        fs.sort(key=lambda f: ((f.get("period") or {}).get("end") or (f.get("period") or {}).get("year") or ""), reverse=True)
        if fs:
            item = _fact(doc, fs[0], all_facts)
            if item:
                out.append(item)
    return out


def _prose(doc, event=False, limit=4):
    """Actual event paragraphs or document findings, never a generated interpretation."""
    out, seen = [], set()
    starts = list(re.finditer(r"(?im)^\s*Item\s+(\d+\.\d+)\.?\s+([^\n]{4,160})", doc.body)) if event else []
    for n, m in enumerate(starts):
        end = starts[n + 1].start() if n + 1 < len(starts) else len(doc.body)
        body = doc.body[m.end():end]
        # Skip an empty index entry; the later real Item heading remains eligible.
        paras = list(re.finditer(r"(?m)^\s*([^\n]{45,1000})", body))
        chosen = next((p for p in paras if not re.match(r"(?i)Item\s+\d|SIGNATURES|EXHIBIT INDEX|Pursuant to", p[1].strip())), None)
        if chosen:
            off = m.end() + chosen.start(1)
            text = chosen[1].strip()
            if text not in seen:
                out.append(_quoted(doc, {"text": text, "offset": off}, f"Item {m[1]}. {m[2].strip()}"))
                seen.add(text)
        if len(out) >= limit:
            return out
    if out:
        return out
    for m in re.finditer(r"(?m)^([^\n]{70,850})$", doc.body):
        text = m[1].strip()
        if not re.search(r"\d", text) or re.search(r"(?i)forward.looking|safe harbor|table of contents|commission file|washington,? d\.?c|not investment advice", text):
            continue
        # Avoid cover tables, numeric table fragments and boilerplate.
        if len(re.findall(r"[A-Za-z]{3,}", text)) < 10 or len(text.split("  ")) > 4:
            continue
        if text not in seen:
            out.append(_quoted(doc, {"text": text, "offset": m.start()}, "As stated in the document"))
            seen.add(text)
        if len(out) >= limit:
            break
    return out


def build(doc, rep: dict, ins: dict, title: str = ""):
    kind, co = ins.get("kind") or "other", rep.get("company") or {}
    form = co.get("form") or ""
    if not form or form in ("20-F", "40-F"):
        m = re.search(r"(?im)^\s*FORM\s+(10-K|10-Q|8-K|20-F|40-F)(/A)?\b", doc.body[:6000])
        form = m[0].strip().split(None, 1)[1].upper() if m else ""
    if kind == "insider" or (kind == "annual" and form in ("", "10-K") and not co.get("amendment")):
        return None
    headings = _LABELS.get(kind, _LABELS["other"])
    out = {"version": 1, "kind": kind, "form": form, "name": co.get("name") or co.get("name_hint") or title,
           "ticker": co.get("ticker") or co.get("ticker_hint") or "", "identity": {"form": form, "period": co.get("period") or ""},
           "title": headings[0], "summary_title": headings[1], "subtitle": co.get("period") or "",
           "keys": [], "groups": [], "notes": []}
    groups = out["groups"]
    def group(name, items):
        groups.append({"title": name, "items": items})
    if kind == "quarterly":
        results = _financial(doc, rep, ("revenue", "operating_income", "net_income", "eps_diluted"), quarter=True)
        liquidity = _financial(doc, rep, ("cash", "cfo", "capex", "total_debt"))
        group("Quarter results", results)
        group("Liquidity and cash flows", liquidity)
        out["keys"] = results[:3] + liquidity[:1]
        out["notes"].append("Each period is shown as reported. Cash flow can cover year-to-date while the income statement covers the quarter.")
        if not results:
            out["notes"].append("Quarter-only statement figures were not located. No year-to-date figure is substituted for a quarter.")
    elif kind == "release":
        results = _financial(doc, rep, ("revenue", "net_income", "eps_diluted", "cfo"), historical=True)
        group("Reported statement results", results)
        adjusted = []
        for m in re.finditer(r"(?im)^([^\n]*(?:non-GAAP|adjusted)[^\n]*)$", doc.body):
            text = m[1].strip()
            if len(text) >= 20 and re.search(r"\d", text) and not insights._GUIDE.search(text) and not insights._BOILER.search(text):
                adjusted.append(_quoted(doc, {"text": text, "offset": m.start()}, "Adjusted result as stated", "Company's stated non-GAAP measure; not a GAAP figure"))
            if len(adjusted) >= 4:
                break
        if adjusted:
            group("Non-GAAP results, as stated", adjusted)
        guidance = [_quoted(doc, x, "Stated outlook", "Forward-looking company guidance, not historical results") for x in ins.get("guidance") or []][:4]
        group("Explicitly stated guidance", guidance)
        out["keys"] = (results + adjusted + guidance)[:4]
        if not guidance:
            out["notes"].append("No explicit numeric guidance was located in this document.")
        out["notes"].append("GAAP statement results, company-defined adjusted measures and forward-looking guidance are kept separate. No consensus comparison is inferred.")
    elif kind == "holdings":
        summary = ins.get("holdings_summary") or {}
        out.update(name=summary.get("manager") or out["name"], ticker="", form=summary.get("form") or "13F-HR")
        out["identity"] = {k: summary.get(k) or "" for k in ("form", "period", "filed", "accession")}
        out["subtitle"] = "Quarter ended " + summary["period"] if summary.get("period") else "Reporting quarter not identified"
        positions = []
        for row in ins.get("holdings") or []:
            label = row["issuer"] + (" (" + ", ".join(x for x in (row.get("class"), row.get("put_call")) if x) + ")" if row.get("class") or row.get("put_call") else "")
            amount = (row.get("shares") or "Not supplied") + (" principal amount" if row.get("kind") == "PRN" else " underlying shares" if row.get("put_call") else " shares")
            positions.append({"label": label, "value": row.get("value") or "Not supplied", "text": amount + ("; weight " + row["weight"] if row.get("weight") not in (None, "n/a") else ""),
                              "period": out["subtitle"], "currency": "USD", "unit": "USD", "basis": "Reported quarter-end position; weight calculated from reported values",
                              "source": {**_source(doc, row), "accession": summary.get("accession") or ""}, "refs": []})
        out["keys"] = positions[:4]
        out["details"] = [{"title": "All reported positions", "items": positions}]
        metrics = []
        for label, key, srckey, basis in (("Reported 13F holdings value", "total", "totals_source", "Calculated sum of the reported 13F positions, not assets under management"),
                                          ("Reported security positions", "positions", "totals_source", "Count of securities after preserving class, PUT/CALL and amount type"),
                                          ("Top ten concentration", "top10_weight", "concentration_source", "Calculated share of the reported 13F holdings value")):
            if summary.get(key) is not None:
                metrics.append({"label": label, "value": str(summary[key]), "text": "", "period": out["subtitle"], "basis": basis,
                                "source": {**(summary.get(srckey) or summary.get("source") or {}), "accession": summary.get("accession") or ""}, "refs": []})
        group("Quarter-end reported holdings", metrics)
        group("Largest reported positions", positions[:4])
        if summary.get("prior"):
            prior = summary["prior"]
            changes = [_quoted(doc, {"text": row["issuer"] + ": " + row["change"], **row}, "Quarter-end amount change", "Calculated comparison of reported quarter-end amounts, not trades")
                       for row in ins.get("holdings") or [] if row.get("change") not in (None, "n/a", "Unchanged")][:4]
            for item in changes:
                item["period"] = prior["period"] + " to " + summary.get("period", "")
                item["sources"] = [prior["source"], {**summary.get("source", {}), "accession": summary.get("accession", "")}]
            group("Compared with the cited prior quarter", changes)
        else:
            out["notes"].append(summary.get("comparison_note") or "The prior quarter was not fetched and cited, so no changes are inferred.")
        if summary.get("amendment_note"):
            out["notes"].append(summary["amendment_note"])
        if summary.get("value_note"):
            out["notes"].append(summary["value_note"])
        out["notes"].append("Reported 13F securities are a partial, delayed view. Cash, short positions and securities outside the 13F list are excluded; permitted small or confidential holdings may be omitted. This is not AUM.")
        out["notes"].append("Quarter-end changes do not establish trades, execution dates, prices or profits. PUT/CALL rows refer to underlying securities.")
    elif kind == "transcript":
        call = ins.get("call") or {}
        claims = [_quoted(doc, x, x.get("speaker") or "Management statement", "Attributed management statement; not an independently checked statement figure") for x in call.get("claims") or []][:4]
        guidance = [_quoted(doc, x, x.get("speaker") or "Management outlook", "Attributed forward-looking guidance; not historical results") for x in call.get("guidance") or []][:4]
        questions = [_quoted(doc, x, x.get("analyst") or "Analyst question", "Attributed analyst question") for x in call.get("questions") or []][:4]
        group("Management's stated results", claims)
        group("Management's stated guidance", guidance)
        group("Analyst questions", questions)
        out["keys"] = (claims[:2] + guidance[:1] + questions[:1])[:4]
        out["notes"].append("These are attributed transcript statements and questions, kept apart from checked statement figures. No answer, motive or forecast is inferred.")
    else:
        items = _prose(doc, event=kind == "current")
        out["keys"] = items
        group("Reported event and terms" if kind == "current" else "Sourced findings", items)
        if kind == "annual":
            out["notes"].append("The actual form and its reported currency, units and period govern this filing. Quoted figures are not converted or filled from another form.")
        if not items:
            out["notes"].append("No supported numeric finding was located in this document. Missing data is not treated as zero.")
    if co.get("amendment") or form.endswith("/A"):
        out["summary_title"] = "Amended " + out["summary_title"].lower()
        out["notes"].insert(0, "This is an amended filing. Only what this amendment contains is shown; omitted original statements are not filled in.")
    for entry in groups:
        if not entry["items"]:
            entry["note"] = _UNAVAILABLE
    return out


def citation(item: dict) -> str:
    out = []
    for source in [item.get("source") or {}] + (item.get("sources") or []):
        parts = []
        if source.get("pdf_page"):
            parts.append("p." + str(source["pdf_page"]))
            if source.get("printed_page") and str(source["printed_page"]) != str(source["pdf_page"]):
                parts.append("printed " + str(source["printed_page"]))
        if source.get("accession"):
            parts.append("accession " + source["accession"])
        if source.get("anchor"):
            parts.append("XBRL " + source["anchor"])
        text = ", ".join(parts)
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def item_text(item: dict) -> str:
    return "; ".join(str(x) for x in (item.get("value"), item.get("text")) if x not in (None, ""))


def markdown(rep: dict, source_name: str, read_at: str) -> str:
    model = rep["document_ending"]
    ident = model.get("identity") or {}
    out = ["# BMG Capital " + model["summary_title"] + ": " + (model.get("name") or source_name), "",
           "- Filing: " + "; ".join(str(ident[k]) for k in ("form", "period", "filed", "accession") if ident.get(k)),
           f"- Source: {source_name} (PRIMARY), sha256 {rep['source']['sha256']}", "- Generated: " + read_at, ""]
    for group in model["groups"]:
        out += ["## " + group["title"], ""]
        if group.get("note"):
            out.append(group["note"])
        for item in group["items"]:
            context = "; ".join(str(x) for x in (item.get("period"), item.get("basis"), citation(item)) if x)
            out.append("- **" + item["label"] + "**: " + item_text(item) + (" [" + context + "]" if context else ""))
        out.append("")
    out += model["notes"]
    for detail in model.get("details") or []:
        out += ["", "## " + detail["title"], ""]
        for item in detail["items"]:
            out.append("- **" + item["label"] + "**: " + item_text(item) + " [" + citation(item) + "]")
    return "\n".join(out) + "\n"
