"""The brain's pages: markdown files Brock reads in Obsidian, written from memory.json after every read.

Every page starts with frontmatter that says it is machine-written and not a source (Dataview can query it), and a
line naming the filings behind it. The brain rewrites only files that carry its own marker (generated_by:
crawler-brain): a file without it, such as one Brock wrote by hand, is never overwritten. On a company page,
everything under "## Brock's notes" is kept exactly as it was.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

MARK = "generated_by: crawler-brain"
NOTES_HEAD = "## Brock's notes"
BANNER = ("> Machine-written by the Crawler from the filings named on this page. Not a source: cite the filing and page, "
          "never this file (inbox/README.md, M39). Detection only; no judgment (M24).")


_NEEDS_QUOTE = re.compile(r"[:#\[\]{}]|^[-?]")


def clink(t: str, table: bool = False) -> str:
    """A link to a company page by its path, so Obsidian never opens another file with the same name; in a table the
    alias bar is escaped."""
    return f"[[brain/companies/{t}/{t}{chr(92) if table else ''}|{t}]]"


def sector_slug(name: str) -> str:
    """The file name (without .md) of an industry page in brain/sectors/ and of its tag."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


def industries(profile: Optional[str]) -> list:
    """The industries in a reader profile: "Railcar leasing + Railcar manufacturing" is two. "General" is the reader's
    catch-all, not an industry, so it gives none (it never links companies into one cluster)."""
    out = []
    for p in re.split(r"\+|,|/", profile or ""):
        p = p.strip()
        if p and p.lower() != "general" and p not in out:
            out.append(p)
    return out


def _alias(s: str) -> str:
    return re.sub(r"[\[\]|#^]", "", str(s or "")).strip()


def ilink(name: str, table: bool = False) -> str:
    """A link to an industry page by its path."""
    return f"[[brain/sectors/{sector_slug(name)}{chr(92) if table else ''}|{_alias(name)}]]"


def kpi_slug(base: str) -> str:
    return sector_slug(base.split(":", 1)[1] if base.startswith("kpi:") else base)


def klink(base: str, name: str, table: bool = False) -> str:
    """A link to a KPI note (brain/kpis/) by its path."""
    return f"[[brain/kpis/{kpi_slug(base)}{chr(92) if table else ''}|{_alias(name)}]]"


class NotOurs(Exception):
    pass


def _owned(path: Path) -> bool:
    if not path.exists():
        return True
    head = path.read_text(encoding="utf-8", errors="replace")[:600]
    return MARK in head


def put(path: Path, text: str) -> str:
    """Write a page only if it is new or the brain wrote it; returns the path written, relative to the vault's brain/.
    Filing text quoted on a page keeps its words but not its em dashes (the vault writes none)."""
    from .brain import _write
    if not _owned(path):
        raise NotOurs(f"{path} was not written by the brain; left as it is")
    _write(path, text.replace("\u2014", " - "))
    return str(path)


def _fm(d: dict) -> str:
    lines = ["---", MARK, "status: machine-written (Crawler brain)", "citable: false"]
    for k, v in d.items():
        if v is None:
            continue
        if isinstance(v, (list, dict)):
            lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
        else:
            s = str(v)
            quote = bool(_NEEDS_QUOTE.search(s))
            lines.append(f"{k}: " + (json.dumps(s, ensure_ascii=False) if quote else s))
    lines.append("---")
    return "\n".join(lines) + "\n"


def _cite(src: dict) -> str:
    """A point's source as one short citation: form, filing period, page, with a link when the filing has a URL."""
    if not src:
        return ""
    p = src.get("pdf_page")
    pp = src.get("printed_page")
    page = f"p.{p}" + (f" (printed {pp})" if pp and str(pp) != str(p) else "") if p else "page not recorded"
    per = src.get("filing_period") or src.get("filing_end") or src.get("period_end")
    what = " ".join(x for x in [src.get("form") or "", f"for {per}" if per else ""] if x)
    name = what or src.get("file") or "filing"
    if src.get("url"):
        return f"[{name}]({src['url']}) {page}"
    return f"{name} ({src.get('file') or 'file'}) {page}"


def _pg(src: Optional[dict]) -> str:
    """A short page cite for a value in a list: form, its period end and page ("10-Q 2026-06-30 p.30")."""
    s = src or {}
    return " ".join(x for x in [s.get("form") or "", s.get("filing_end") or "", f"p.{s['pdf_page']}" if s.get("pdf_page") else "page not recorded"] if x)


def _status(x: dict) -> str:
    """The legs column (M47's whole vocabulary: a count of touched legs, no status word): for a criterion that names
    a number of periods, how many periods in a row are on the trigger side; else whether this reading is."""
    if x.get("needs_consecutive"):
        s = f"{x.get('consecutive') or 0} in a row on the trigger side; the criterion names {x['needs_consecutive']}"
        if x.get("gap_before"):
            s += " (the period before is not in the brain)"
    else:
        s = "on the trigger side" if x.get("on_trigger_side") else "not on the trigger side"
    tags = [w for w, on in (("signpost", x.get("signpost")), ("watch only", x.get("watch_only"))) if on]
    if tags:
        s += "; " + ", ".join(tags)
    return s


def _md(s) -> str:
    return str(s if s is not None else "").replace("\u2014", " - ").replace("|", "\\|").replace("\n", " ")


def _clip(text: str, n: int = 420) -> str:
    """A quoted line cut at a sentence end (never inside the figures it guides), or at n characters."""
    t = " ".join((text or "").split())
    if len(t) <= n:
        return t
    cut = max(t.rfind(". ", 0, n), t.rfind("; ", 0, n))
    return t[:cut + 1] if cut > n // 2 else t[:n].rsplit(" ", 1)[0] + " ..."


def _chg_cell(c: dict, k: str) -> str:
    d = c.get(k)
    if not d:
        return ""
    at = f"({d['end']}, {_pg(d.get('src'))})"          # every earlier figure with its own filing and page
    if "pp" in d:
        return f"{d['pp']:+.1f} pp vs {d['display']} {at}"
    if "pct" in d:
        return f"{d['pct']:+.1f}%{' in size' if d.get('size') else ''} vs {d['display']} {at}"
    if d.get("no_pct"):
        return f"{d['no_pct']}: vs {d['display']} {at}"
    return f"vs {d['display']} {at}"


# ---------------------------------------------------------------------- one read
def read_note(memory: dict, out: dict, meta: dict) -> str:
    from .brain import display_name
    t = memory["ticker"]
    fm = _fm({"ticker": t, "company": memory.get("name"), "form": meta.get("form"), "period": meta.get("period"), "period_end": meta.get("period_end"),
              **({"period_basis": meta["period_basis"]} if meta.get("period_basis") else {}), "file": meta.get("file"), "url": meta.get("url"), "sha8": meta.get("sha8"),
              "read_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), "type": "crawler-read",
              "kpis_read": len(out.get("kpis_read") or []),
              "thresholds_measured": [m["id"] for m in out.get("thresholds", {}).get("measured", [])],
              "restatements": len(out.get("merged", {}).get("restated", [])), "missing_kpis": len(out.get("missing", []))})
    src = f"[{meta.get('file')}]({meta['url']})" if meta.get("url") else f"`{meta.get('file')}`"
    L = [fm, f"# {t} {meta.get('form') or ''}: {meta.get('period') or meta.get('period_end') or ''}", "", BANNER, "",
         f"Read {src} ({meta.get('pages') or '?'} pages). Back to {clink(t)}.", ""]
    exp = out.get("expected") or {}
    L.append("## What the brain expected before reading")
    if not exp.get("known"):
        L += ["", "First read of this company: nothing to compare against yet.", ""]
    else:
        L += ["", f"{exp.get('reads', 0)} earlier read(s); last: {exp.get('last_read') or 'none'}.", ""]
        if exp.get("kpis"):
            L += ["| KPI | Last value | Period | Printed in |", "|---|---|---|---|"]
            for k in exp["kpis"]:
                L.append(f"| {_md(k['name'])} | {_md(k['display'])} | {k['end']} | {_md(_cite(k.get('src')))} |")
            L.append("")
        if exp.get("guidance_for_period"):
            L.append("Guidance naming this year (checked against the year's 10-K, not against this read):")
            for g in exp["guidance_for_period"]:
                L.append(f"- \"{_md(g['text'][:220])}\" ({_cite(g.get('src'))})")
            L.append("")
        if exp.get("open_questions"):
            L.append("Open questions carried in:")
            L += [f"- {_md(q)}" for q in exp["open_questions"]]
            L.append("")
        if exp.get("forecasts"):
            L.append("Brock's open pre-registered forecasts (calibration/forecasts.yaml; listed, not scored: scoring is the postmortem's, M40, M41):")
            for f in exp["forecasts"]:
                rng = f" (80% range {f.get('ci_80_lower')} to {f.get('ci_80_upper')})" if f.get("ci_80_lower") is not None else ""
                L.append(f"- {_md(f.get('period'))} {_md(f.get('metric'))}: {f.get('forecast')}{'%' if f.get('units') == 'percent' else ''}{rng}, {_md(f.get('preview_path') or '')}")
            L.append("")
    # what came in and what moved
    chg = out.get("changes", [])
    L += ["## What changed", ""]
    if not chg:
        L += ["No series this read printed for its own period had an earlier point to compare with.", ""]
    else:
        L += ["| Figure | This period | Period before | A year before | Known history | Source |", "|---|---|---|---|---|---|"]
        for c in sorted(chg, key=lambda c: (c["kind"] != "kpi", c["name"])):
            r = c.get("range")
            hist = ""
            if r:
                hi, lo = ("largest in size", "smallest in size") if r.get("by_size") else ("highest", "lowest")
                hist = f"{r['n']} points since {r['first']}" + (f" ({hi})" if r["highest"] else f" ({lo})" if r["lowest"] else "")
            nm = c["name"] + (f" ({c['scope']})" if c.get("scope") else "")
            L.append(f"| {_md(nm)} | {_md(c['display'])} | {_md(_chg_cell(c, 'before'))} | {_md(_chg_cell(c, 'year_ago'))} | {_md(hist)} | {_md(_cite(c.get('src')))} |")
        L.append("")
    m = out.get("merged", {})
    L += [f"New series this read: {len(m.get('new_series', []))}. New points: {m.get('new_points', 0)}. Figures confirmed against earlier reads: {m.get('confirmed', 0)}.", ""]
    firsts = [k for k in m.get("new_series", []) if k.startswith("kpi:")]
    if firsts:
        # a KPI read for the first time has nothing to compare with, so it is not in "What changed"; it is said here
        # (Microsoft's first RPO reading, $398 billion in the September 2025 10-Q, was filed and never shown)
        L += ["First readings of a KPI (nothing earlier to compare with):"]
        for k in firsts[:20]:
            sr = memory["series"].get(k) or {}
            pt = (sr.get("points") or {}).get(meta.get("period_end") or "")
            if pt:
                L.append(f"- {_md(display_name(k, sr.get('name') or k))}: {_md(pt.get('display'))} for {meta.get('period_end')} ({_md(_cite(pt.get('src')))})")
        L.append("")
    if m.get("restated"):
        L += ["## Same period, two different figures", "",
              "Two filings give this period different figures. Either the company recast it, or one of the two reads took another "
              "line. Both are kept; check both pages before using either.", ""]
        for r in m["restated"]:
            L.append(f"- **{_md(r['name'])}, {r['end']}**: first {r['first_display']} ({_cite(r.get('first_src'))}), now {r['now_display']} ({_cite(r.get('now_src'))})")
        L.append("")
    if m.get("printed_differently"):
        L += ["## Printed differently in a table and in the text", "",
              "The same name and date with another figure, one read from a table row and one from a sentence. They may measure "
              "different things (a segment's fleet against the whole fleet); both are kept and neither is called a restatement.", ""]
        for r in m["printed_differently"]:
            L.append(f"- **{_md(r['name'])}, {r['end']}**: {r['first_display']} ({_cite(r.get('first_src'))}); {r['now_display']} ({_cite(r.get('now_src'))})")
        L.append("")
    if out.get("missing"):
        L += ["## Printed before, not found in this read (M47)", ""]
        for x in out["missing"]:
            w = x.get("where") or {}
            same = (w.get("form") or "") == (meta.get("form") or "")          # a page learned from another form says nothing about this one
            where = (f"expected around {('p.' + str(w['pdf_page'])) if w.get('pdf_page') else 'an unknown page'}{(', ' + w['table']) if w.get('table') else ''}"
                     if same else f"last seen in a {w.get('form') or 'filing'}, not in a {meta.get('form') or 'filing'} like this one")
            L.append(f"- **{_md(x['name'])}**: last {x.get('last_display')} for {x.get('last_end')} ({_cite(x.get('last_src'))}); "
                     f"{where}. Not found by this read: either the filing dropped it or the Crawler missed it.")
        L.append("")
    sc = out.get("scores") or {}
    if sc.get("inputs"):
        cell = lambda k: (sc.get(k) or {}).get("display") or (sc.get(k) or {}).get("value")
        L += ["## Quality scores from this read", "",
              f"Altman Z {cell('altman') if cell('altman') is not None else 'not computed'}; Piotroski F {cell('piotroski') if cell('piotroski') is not None else 'not computed'}; "
              f"Beneish M {cell('beneish') if cell('beneish') is not None else 'not computed'}. Computed by the reader from these figures of this filing:", "",
              "| Input | This year | Page | Year before | Page |", "|---|---|---|---|---|"]
        for x in sc["inputs"]:
            L.append(f"| {_md(x.get('name'))} | {_md(x.get('now'))} | {('p.' + str(x['now_page'])) if x.get('now_page') else ''} | {_md(x.get('before') or '')} | {('p.' + str(x['before_page'])) if x.get('before_page') else ''} |")
        L.append("")
    th = out.get("thresholds", {})
    if th.get("rows"):
        L += ["## Pre-registered thresholds (monitoring/THRESHOLDS.md)", ""]
        if th.get("measured"):
            L += ["| Threshold | Reading | Trigger | Distance | Periods (legs) | Source |", "|---|---|---|---|---|---|"]
            for x in th["measured"]:
                L.append(f"| {x['id']} | {_md(x['reading_display'])} | {x['operator']} {x['threshold']} {x['unit']} | {_md(x['distance_display'])} | {_status(x)} | {_md(_cite(x.get('src')))} |")
            L.append("")
        if th.get("unseen"):
            L.append("Not observable from this read:")
            L += [f"- {x['id']} ({_md(x.get('item'))}): {x['why']}" for x in th["unseen"]]
            L.append("")
        L += ["Readings, distances and period counts only. Whether a criterion has fired is Brock's call (M24); a reading newer than THRESHOLDS.md goes to `inbox/crawler/` for disposition (M39).", ""]
    if out.get("guidance_checked"):
        L += ["## Guidance against the actual", ""]
        for g in out["guidance_checked"]:
            o = g["outcome"]
            act = f"actual {o['actual_display']} ({_cite(o.get('actual_src'))}), {o['verdict']}." if o.get("actual") is not None else f"{o['verdict']}."
            L.append(f"- \"{_md(g['text'][:220])}\" ({_cite(g.get('src'))}): {act} {o['note']}")
        L.append("")
    if out.get("guidance_added"):
        L += [f"Guidance lines recorded from this read: {out['guidance_added']} (see [[brain/companies/{t}/{t}#Guidance ledger|{t}, Guidance ledger]]).", ""]
    lang = out.get("language") or {}
    if lang.get("added") or lang.get("dropped"):
        L += [f"## Risk factor headings against the annual report for {lang.get('against')}", ""]
        L += [f"- Added: {_md(h)}" for h in lang.get("added", [])[:30]]
        L += [f"- Dropped: {_md(h)}" for h in lang.get("dropped", [])[:30]]
        L.append("")
    q = out.get("questions") or {}
    if q.get("added") or q.get("closed"):
        L += ["## Questions", ""]
        L += [f"- New: {_md(x)}" for x in q.get("added", [])]
        L += [f"- No longer raised: {_md(x)}" for x in q.get("closed", [])]
        L.append("")
    rd = out.get("reading") or {}
    L += ["## Machine reading", ""]
    if rd.get("note"):
        L += [f"_{_md(rd['note'])}_", ""]
    if not rd.get("sentences") and not rd.get("questions"):
        L += ["This read filed no change, restatement, threshold reading, missing line or checked guidance, so the reading has nothing to say. "
              f"The figures it read went into the series on [[brain/companies/{t}/{t}|the {t} page]].", ""]
    for s in rd.get("sentences", []):
        L.append(f"- {s['text']} [{', '.join(s['refs'])}]")
    if rd.get("questions"):
        L += ["", "Questions for the next read:"] + [f"- {s['text']} [{', '.join(s['refs'])}]" for s in rd["questions"]]
    if rd.get("facts"):
        L += ["", "<details><summary>Numbered facts behind the reading</summary>", ""]
        L += [f"- {f['id']}: {_md(f['text'])}" for f in rd["facts"]]
        L += ["", "</details>"]
    L.append("")
    return "\n".join(L)


# ---------------------------------------------------------------------- the company book
def company_page(memory: dict, last: dict, existing: str = "", ctx: Optional[dict] = None) -> str:
    from .brain import latest, track_record, _fmt, display_name
    t = memory["ticker"]
    inds = industries(memory.get("profile"))
    kp = {k: s for k, s in memory.get("series", {}).items() if s["kind"] == "kpi"}
    reads = memory.get("reads", [])
    lr = reads[-1] if reads else {}
    th = (last.get("thresholds") or {}).get("measured", [])
    fm = _fm({"ticker": t, "company": memory.get("name"), "cik": memory.get("cik"), "profile": memory.get("profile"), "type": "crawler-company",
              "reads": len(reads), "last_read": f"{lr.get('form')} {lr.get('period') or lr.get('period_end') or ''}".strip() if lr else None,
              "last_period_end": lr.get("period_end"), "kpis": sorted(display_name(k, s["name"]) for k, s in kp.items()),
              "thresholds_watched": sorted({x["id"] for x in th}),
              "industries": inds or None, "tags": [f"industry/{sector_slug(x)}" for x in inds] or None})
    L = [fm, f"# {memory.get('name') or t} ({t})", "", BANNER, ""]
    profs = [r.get("profile") for r in reads if r.get("profile")]
    others = sorted({x for x in profs if x != memory.get("profile")})
    pnote = f" ({profs.count(memory.get('profile'))} of {len(profs)} reads; also read as {', '.join(others)})" if others else ""
    L += [f"Profile: {memory.get('profile') or 'not identified'}{pnote}. Reads in the brain: {len(reads)}. Last: {lr.get('form') or ''} {lr.get('period') or lr.get('period_end') or ''}.", ""]
    if inds:
        L += ["Industry: " + ", ".join(ilink(x) for x in inds), ""]
    if th:
        L += ["## Watch list: pre-registered thresholds at the last read", "", "| Threshold | Reading | Trigger | Distance | Periods (legs) |", "|---|---|---|---|---|"]
        for x in th:
            L.append(f"| {x['id']} | {_md(x['reading_display'])} ({x['period_end']}; {_pg(x.get('src'))}) | {x['operator']} {x['threshold']} {x['unit']} | {_md(x['distance_display'])} | {_status(x)} |")
        L += ["", "Readings only (M24). The rows and their values live in `monitoring/THRESHOLDS.md`.", ""]
    if kp:
        L += ["## KPIs", "", "| KPI | Scope | Latest | Period | Points | Source |", "|---|---|---|---|---|---|"]
        for k, s in sorted(kp.items(), key=lambda x: x[1]["name"]):
            lt = latest(s)
            if not lt:
                continue
            L.append(f"| {_md(display_name(k, s['name']))} | {_md(s.get('scope'))} | {_md(lt[1].get('display') or _fmt(lt[1]['value'], s.get('unit')))} | {lt[0]} | {len(s['points'])} | {_md(_cite(lt[1].get('src')))} |")
        L.append("")
        L += ["### KPI history", ""]
        for k, s in sorted(kp.items(), key=lambda x: x[1]["name"]):
            pts = sorted(s["points"].items())
            L.append(f"**{_md(display_name(k, s['name']))}**{' (' + _md(s['scope']) + ')' if s.get('scope') else ''}: " +
                     "; ".join(f"{e} {p.get('display') or _fmt(p['value'], s.get('unit'))} ({_pg(p.get('src'))})" + (" (two figures)" if p.get("restated") else "") for e, p in pts[-12:]))
            L.append("")
    key_facts = [("fact:revenue:Q", "Revenue (quarter)"), ("fact:revenue:FY", "Revenue (year)"), ("fact:net_income:Q", "Net income (quarter)"),
                 ("fact:net_income:FY", "Net income (year)"), ("fact:eps_diluted:Q", "Diluted EPS (quarter)"), ("fact:eps_diluted:FY", "Diluted EPS (year)"),
                 ("fact:cfo:FY", "Operating cash flow (year)"), ("fact:total_debt:instant", "Total debt"), ("fact:cash:instant", "Cash"),
                 ("fact:equity_parent:instant", "Equity attributable to the company"), ("fact:equity_total:instant", "Total equity"),
                 ("fact:equity:instant", "Equity")]
    rows = []
    for key, label in key_facts:
        s = memory["series"].get(key)
        if not s or (key == "fact:equity:instant" and ("fact:equity_parent:instant" in memory["series"] or "fact:equity_total:instant" in memory["series"])):
            continue                     # "Equity" only for a company with no minority interest line
        pts = sorted(s["points"].items())
        rows.append(f"| {label} | " + " | ".join(f"{e}: {p.get('display') or _fmt(p['value'], s.get('unit'))} ({_pg(p.get('src'))})" for e, p in pts[-5:]) + " |")
    if rows:
        L += ["## Reported figures (last five periods the brain has)", "", "| Figure | Periods |", "|---|---|"] + rows + [""]
    g = memory.get("guidance", [])
    if g:
        tr = track_record(memory)
        L += ["## Guidance ledger", "", f"Lines recorded: {len(g)}. Checked against an actual: {tr['checked']}. Closed without a comparison (adjusted basis, a part of the company, or no named measure): {tr.get('not_compared', 0)}. Open: {tr['open']}.", ""]
        for x in g[-15:]:
            o = x.get("outcome")
            L.append(f"- {x['given_in'].get('form')} {x['given_in'].get('period') or x['given_in'].get('period_end') or ''} ({x.get('topic_label')}, covers {x.get('covers') or 'unstated'}): "
                     f"\"{_md(_clip(x['text']))}\" ({_cite(x.get('src'))})" + (f" **Actual {o['actual_display']} ({_pg(o.get('actual_src'))}): {o['verdict']}.**" if o and o.get('actual') is not None else f" *{o['note']}*" if o else ""))
        L.append("")
    rest = [(k, e, p) for k, s in memory.get("series", {}).items() for e, p in s["points"].items() if p.get("restated")]
    if rest:
        L += ["## Same period, two different figures (recast, or one read took another line: check both pages)", ""]
        for k, e, p in rest[-20:]:
            s = memory["series"][k]
            L.append(f"- {_md(display_name(k, s['name']))} {e}: shown {p.get('display')} ({_cite(p.get('src'))}); other print " +
                     "; ".join(f"{r.get('display')} ({_cite(r.get('src'))})" for r in p["restated"]))
        L.append("")
    qs = [q for q in memory.get("questions", []) if q.get("status") == "open"]
    if qs:
        L += ["## Open questions", ""] + [f"- {_md(q['q'])} (raised in the {q.get('form')}{(' for ' + str(q['raised_in'])) if q.get('raised_in') else ''})" for q in qs[:20]] + [""]
    sc = memory.get("scores", [])
    if sc:
        L += ["## Quality scores by year", "", "Each score with its inputs and their pages is in the read it came from.", "",
              "| Year end | Altman Z | Piotroski F | Beneish M | Read |", "|---|---|---|---|---|"]
        for r in sc:
            cell = lambda k: ((r.get(k) or {}).get("display") or ((r.get(k) or {}).get("value") if r.get(k) else "")) if not (r.get(k) or {}).get("max") else \
                f"{(r.get(k) or {}).get('value')} of {(r.get(k) or {}).get('max')} tests (partial: {(r.get(k) or {}).get('caveat') or 'not every test applies'})"
            rd = next((x for x in reads if x.get("sha8") == r.get("read")), None)
            src = f"[[brain/companies/{t}/reads/{note_name(t, rd)}\\|{rd.get('form')} {rd.get('period_end')}]]" if rd else ""
            L.append(f"| {r.get('period_end')} | {_md(cell('altman'))} | {_md(cell('piotroski'))} | {_md(cell('beneish'))} | {src} |")
        L.append("")
    w = memory.get("where", {})
    if w:
        L += ["## Where this company prints its KPIs (learned from reads)", ""]
        for k, x in sorted(w.items()):
            nm = display_name(k, (memory["series"].get(k) or {}).get("name") or k)
            bits = [f"{x.get('form')} p.{x.get('pdf_page')}", x.get("section") or "", ("table " + x["table"]) if x.get("table") else "",
                    ("row " + x["row"]) if x.get("row") else ""]
            L.append(f"- {_md(nm)}: {_md(', '.join(b for b in bits if b))} ({x.get('how')})")
        L.append("")
    L += competitors_block(memory, ctx)
    L += ["## Reads", ""]
    for r in reversed(reads[-30:]):
        L.append(f"- [[brain/companies/{t}/reads/{note_name(t, r)}|{r.get('form')} {r.get('period') or r.get('period_end') or ''}]] ({r.get('file')})")
    L.append("")
    keep = ""
    if existing and NOTES_HEAD in existing:
        keep = existing[existing.index(NOTES_HEAD):].rstrip() + "\n"
    L.append(keep or f"{NOTES_HEAD}\n\nAnything below this heading is yours: the Crawler never changes it. Thesis notes, questions for the next read, corrections.\n")
    return "\n".join(L)


def note_name(t: str, r: dict) -> str:
    return f"{r.get('period_end') or 'undated'}-{re.sub(r'[^A-Za-z0-9]+', '', r.get('form') or 'doc')}-{r.get('sha8') or 'nosha'}"


# ---------------------------------------------------------------------- inbox observation (agent contract)
def inbox_note(memory: dict, out: dict, meta: dict) -> str:
    th = out.get("thresholds") or {}
    day = datetime.now().strftime("%Y-%m-%d")
    fm = ["---", MARK, f"date: {day}", "agent: crawler-brain", f"ticker: {memory['ticker']}",
          f"filing: {json.dumps((meta.get('form') or '') + ' ' + (meta.get('period') or meta.get('period_end') or ''))}", f"file: {json.dumps(meta.get('file') or '')}",
          f"url: {json.dumps(meta.get('url') or '')}", "tier: TIER 1 pointer to the primary filing; verify the page before citing (inbox/README.md)",
          "governing_rules: M24 (detection, not judgment), M39 (inbox only), M47 (say what was not seen)", "---", ""]
    L = fm + [f"# Crawler reading: {memory['ticker']} {meta.get('form') or ''} {meta.get('period') or meta.get('period_end') or ''}", "",
              "Machine-gathered. The figures are pointers to the filing pages named; open the page before citing it. Disposition in `inbox/LEDGER.md`.", "",
              "## Pre-registered items touched", ""]
    if th.get("measured"):
        L += ["| Threshold | Item | Trigger | Today's reading | Distance | Periods (legs) | Source |", "|---|---|---|---|---|---|---|"]
        for x in th["measured"]:
            L.append(f"| {x['id']} | {_md(x['item'])} | {x['operator']} {x['threshold']} {x['unit']} | {_md(x['reading_display'])} for {x['period_end']} | {_md(x['distance_display'])} | {_status(x)} | {_md(_cite(x.get('src')))} |")
        L.append("")
        newer = [x["id"] for x in new_observations(out)]
        L += [f"Newer than the reading in THRESHOLDS.md: {', '.join(newer) if newer else 'none'}. Last readings recorded there: "
              + "; ".join(f"{x['id']} {x.get('last_reading_in_thresholds')!s:.60} ({x.get('last_reading_date') or 'no date'})" for x in th["measured"]) + ".", ""]
    else:
        L += ["None measured by this read.", ""]
    L += ["## Not observable from this read (M47)", ""]
    L += [f"- {x['id']} ({_md(x.get('item'))}): {x['why']}" for x in th.get("unseen", [])] or ["- (every threshold for this ticker was measured)"]
    L += ["", "The rows give the item, the trigger, today's reading, the arithmetic distance and a count of periods (legs) and nothing more. This file does not say any criterion has fired, "
          "does not recommend an action and does not characterize the thesis (THRESHOLDS.md, agent contract).", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------- catalog, log, sectors
def index_page(vault: Path) -> str:
    from .brain import root, latest, _fmt, display_name
    rows = []
    for p in sorted((root(vault) / "companies").glob("*/memory.json")):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
        except ValueError:
            continue
        reads = m.get("reads", [])
        lr = reads[-1] if reads else {}
        kp = [(k, s) for k, s in m.get("series", {}).items() if s["kind"] == "kpi"]
        bits = []
        for k, s in sorted(kp, key=lambda x: x[1]["name"])[:3]:
            lt = latest(s)
            if lt:
                bits.append(f"{display_name(k, s['name'])} {lt[1].get('display') or _fmt(lt[1]['value'], s.get('unit'))} ({_pg(lt[1].get('src'))})")
        rows.append(f"| {clink(m['ticker'], True)} | {_md(m.get('name'))} | {_md(m.get('profile'))} | {len(reads)} | {_md((lr.get('form') or '') + ' ' + (lr.get('period') or lr.get('period_end') or ''))} | {_md('; '.join(bits))} |")
    fm = _fm({"type": "crawler-index"})
    return "\n".join([fm, "# Crawler brain: index", "", BANNER, "",
                      "What the Crawler has read and remembers. Rules: [[brain/README|README]]. History: [[brain/log|log]].", "",
                      "This is a listing, not a comparison. Each row's KPIs are that company's own, as its filings define them; a KPI with "
                      "the same name at another company is a different measure (M03; falsified.md FALS-02 and FALS-10 for GATX LPI against "
                      "Trinity FLRD), so values in this column are not read across rows.", "",
                      "| Company | Name | Profile | Reads | Last read | KPIs (latest) |", "|---|---|---|---|---|---|"] + rows + [""])


def graph_ctx(snap: list) -> dict:
    """What the pages that link companies need, read once per write: every company's name and profile, an index of
    their names for matching a named competitor, and the KPI keys two or more companies print."""
    from .brain_peers import ticker_index
    return {"companies": {m["ticker"]: {"name": m.get("name"), "profile": m.get("profile")} for m in snap},
            "index": ticker_index([(m["ticker"], m.get("name") or "", m.get("cik")) for m in snap]),
            "kpis": shared_kpis(snap)}


def shared_kpis(snap: list) -> dict:
    """{KPI key without its period: [tickers]} for every KPI printed by two or more companies in the brain."""
    from .brain import latest
    from .brain_peers import _base
    by: dict = {}
    for m in snap:
        for k, s in (m.get("series") or {}).items():
            if s.get("kind") == "kpi" and latest(s):
                by.setdefault(_base(k), set()).add(m["ticker"])
    return {k: sorted(v) for k, v in by.items() if len(v) >= 2}


_ANNUAL = ("10-K", "10-K/A", "20-F", "40-F")


def competitors_block(memory: dict, ctx: Optional[dict]) -> list:
    """The company's competitors as its latest annual report names them (each sentence quoted once, with its page),
    linked when the brain holds the company, and the companies of the same industry in the brain. Names only."""
    if not ctx:
        return []
    from .brain_peers import match_in_brain
    t = memory["ticker"]
    inds = set(industries(memory.get("profile")))
    peers = sorted(x for x, c in ctx["companies"].items() if x != t and inds & set(industries(c.get("profile"))))
    comp = memory.get("competitors") or {}
    if not comp and not peers:
        return []
    L = ["## Competitors and peers", ""]
    if comp:
        fr = comp.get("from") or {}
        L += [f"Named in its {fr.get('form') or 'annual report'} for {fr.get('period') or fr.get('period_end') or 'the year'}, in a sentence "
              "about competitors (each sentence quoted with its PDF page). Names as the Crawler took them from the sentence: "
              "check the quote. Names only; nothing here compares them.", ""]
        newer = max((r for r in memory.get("reads") or [] if (r.get("form") or "").upper() in _ANNUAL
                     and (r.get("period_end") or "") > (fr.get("period_end") or "")), key=lambda r: r.get("period_end") or "", default=None)
        if newer:
            L += [f"Its {newer.get('form')} for {newer.get('period') or newer.get('period_end')} was read before the brain kept competitors, so "
                  "this list is from the older report. Read the newer one again (the Crawler's Industry tab, or POST /api/brain-learn) "
                  "to replace it.", ""]
        groups = []
        for n in comp.get("named") or []:
            g = next((x for x in groups if x["quote"] == n.get("quote")), None)
            if g is None:
                g = {"quote": n.get("quote") or "", "page": n.get("pdf_page"), "names": []}
                groups.append(g)
            g["names"].append(n)
        for g in groups:
            bits = []
            for n in g["names"]:
                mt = match_in_brain(n.get("name") or "", ctx["index"])
                if mt and mt["ticker"] != t:
                    bits.append(f"{clink(mt['ticker'])} ({_md(n['name'])}{'; ' + _md(mt['match']) if mt['match'] != 'same name' else ''})")
                else:
                    bits.append(f"{_md(n.get('name'))} (no company in the brain matched by name)")
            L += [f"- p.{g['page'] or '?'}: " + "; ".join(bits), f"  > {_md(g['quote'])}"]
        if not groups:
            L.append("- No company is named in a sentence about competitors.")
        L.append("")
    elif any((r.get("form") or "").upper() in _ANNUAL for r in memory.get("reads") or []):
        L += ["Its annual report was read before the brain kept competitors; they are added when that report is read again "
              "(the Crawler's Industry tab, or POST /api/brain-learn; scripts/local/brain_backfill.py does it for the reads saved "
              "in My reads on the Mac).", ""]
    else:
        L += ["No annual report of this company has been read yet, so no competitors named by the company.", ""]
    if peers:
        L += [f"Same industry in the brain ({', '.join(sorted(inds))}; the Crawler's industry label, not a list of competitors): "
              + ", ".join(clink(p) for p in peers), ""]
    return L


def kpi_page(base: str, snap: list) -> str:
    """One KPI key across the companies that print it: each company's own printed name and where it prints it. Names
    and places only, never figures side by side: the same key at two companies is not the same definition (M03)."""
    from .brain import latest, display_name
    from .brain_peers import _base
    rows = []
    for m in sorted(snap, key=lambda m: m["ticker"]):
        for k, s in sorted((m.get("series") or {}).items()):
            if s.get("kind") == "kpi" and latest(s) and _base(k) == base:
                rows.append((m, k, s))
    tickers = sorted({m["ticker"] for m, _k, _s in rows})
    name = (rows[0][2].get("name") if rows else None) or kpi_slug(base).replace("-", " ")
    L = [_fm({"type": "crawler-kpi", "key": base, "companies": tickers}), f"# {name}: who prints it", "", BANNER, "",
         "The companies in the brain that print this KPI, each with the line its filing prints it on (or, without one, the "
         "Crawler's label) and where. A KPI with the same key at two companies is not the same definition: compare figures "
         "only where both definitions are quoted and match (M03; research/coverage/rail/falsified.md FALS-02, FALS-10). "
         "Figures are in each company page and in the Crawler's Industry tab.", ""]
    if not tickers:
        L += ["No company in the brain prints this KPI now. The note stays: the brain deletes nothing.", ""]
    elif len(tickers) < 2:
        L += ["One company in the brain prints this KPI now. The note stays: the brain deletes nothing.", ""]
    for m, k, s in rows:
        w = (m.get("where") or {}).get(k) or {}
        src = (latest(s)[1].get("src") or {})
        at = ", ".join(x for x in (w.get("table") and f"table {w['table']}", w.get("row") and f"row {w['row']}",
                                   (w.get("form") or src.get("form")) and f"{w.get('form') or src.get('form')} p.{w.get('pdf_page') or src.get('pdf_page') or '?'}") if x)
        inds = industries(m.get("profile"))
        own = f"as printed: {_md(w['row'])}" if w.get("row") else f"the Crawler's label: {_md(display_name(k, s.get('name') or k))}"
        L.append(f"- {clink(m['ticker'])}, {own}" + (f" ({_md(s['scope'])})" if s.get("scope") else "")
                 + (f": {_md(at)}" if at else "") + (f". Industry: {', '.join(inds)}" if inds else ""))
    L.append("")
    return "\n".join(L)


def general_page(snap: list) -> str:
    """The companies the reader files under "General", its catch-all. Listed without links, so the graph does not draw
    them as one industry."""
    comps = sorted(m["ticker"] for m in snap if (m.get("profile") or "").strip().lower() == "general")
    return "\n".join([_fm({"type": "crawler-sector", "profile": "General", "companies": comps}), "# General: the reader's catch-all", "", BANNER, "",
                      "These companies were read under the reader's General profile, which is not an industry. They are listed "
                      "without links so the graph does not group them; each has its own page under brain/companies/.", "",
                      ", ".join(comps) if comps else "None.", ""])


def pointer_page(profile: str, parts: list) -> str:
    """A page written for a two-industry profile before 2026-10-30 now points at one page per industry."""
    return "\n".join([_fm({"type": "crawler-sector", "profile": profile, "superseded_by": [f"brain/sectors/{sector_slug(p)}.md" for p in parts]}),
                      f"# {profile}", "", BANNER, "",
                      "Companies read under this profile are now listed on one page per industry: " + ", ".join(ilink(p) for p in parts) + ".", ""])


def industry_pages(vault: Path, profile: Optional[str], snap: list) -> list:
    """(path, writer) for the industry pages a company of this profile appears on: one per industry, the catch-all
    page for General, and the old page of a two-industry profile, when there is one, rewritten to point at both."""
    from .brain import root
    if not profile:
        return []
    parts = industries(profile)
    d = root(vault) / "sectors"
    out = [(d / f"{sector_slug(p)}.md", (lambda p=p: sector_page(vault, p, snap))) for p in parts]
    if not parts:
        out.append((d / "general.md", lambda: general_page(snap)))
    if len(parts) > 1 and (d / f"{sector_slug(profile)}.md").exists():
        out.append((d / f"{sector_slug(profile)}.md", lambda: pointer_page(profile, parts)))
    return out


def kpi_pages(vault: Path, memory: dict, snap: list, ctx: dict) -> list:
    """(path, writer) for the KPI notes of this company's KPIs: every key two or more companies print, and any note
    already written for a key that now has fewer (rewritten, never deleted)."""
    from .brain import root
    from .brain_peers import _base
    keys = {_base(k) for k, s in (memory.get("series") or {}).items() if s.get("kind") == "kpi"}
    d = root(vault) / "kpis"
    keys |= {b for b in ctx["kpis"] if not (d / f"{kpi_slug(b)}.md").exists()}      # the industry pages link these
    return [(d / f"{kpi_slug(b)}.md", (lambda b=b: kpi_page(b, snap))) for b in sorted(keys)
            if b in ctx["kpis"] or (d / f"{kpi_slug(b)}.md").exists()]


def sector_page(vault: Path, profile: str, snap: Optional[list] = None) -> str:
    """The companies of one profile, each with its own KPIs. Never a table across companies: measures with the same
    name are defined differently by each filer (M03; falsified.md FALS-02 and FALS-10 for GATX LPI against Trinity
    FLRD), so a side-by-side would read as a comparison no filing supports."""
    from .brain import latest, _fmt, display_name
    from .brain_peers import memories
    snap = memories(vault) if snap is None else snap
    comps = [m for m in snap if profile in industries(m.get("profile"))]
    kshared = shared_kpis(snap)
    L = [_fm({"type": "crawler-sector", "profile": profile, "companies": [m["ticker"] for m in comps],
              "tags": [f"industry/{sector_slug(profile)}"] if industries(profile) else None}),
         f"# {profile}: companies the Crawler has read", "", BANNER, "",
         "Each company's KPIs are listed under that company, as its own filings define them. They are not set side by side: a "
         "measure with the same name is defined differently by each filer, and no comparison is made here (M03; "
         "research/coverage/rail/falsified.md FALS-02, FALS-10).", ""]
    if not comps:
        return "\n".join(L + ["No companies recorded yet.", ""])
    from .brain_peers import playbook
    pb = playbook(vault, profile, snap=snap)
    if pb["kpis"]:
        L += ["## Playbook: what this industry's filings print", "",
              "Every KPI the Crawler has found in these companies' filings, and where each company prints it. A new "
              "company read under this profile is checked for these. Names only: a KPI is set beside another company's "
              "in the Crawler's Brain tab, ranked only when both definitions match (M03).", ""]
        for g in pb["kpis"]:
            who = []
            for tk in g["companies"]:
                ex = g["by"][tk][0]
                w = ex.get("where") or {}
                form, page = (w.get("form"), w.get("pdf_page")) if w.get("form") else (ex["src"].get("form"), ex["src"].get("pdf_page"))
                at = ", ".join(x for x in (w.get("table") and f"table {w['table']}", w.get("row") and f"row {w['row']}",
                                           form and f"{form} p.{page or '?'}") if x)
                who.append(f"{clink(tk)} as {_md(ex['printed'])}" + (f" ({_md(at)})" if at else ""))
            head = klink(g["key"], g["name"]) if g["key"] in kshared else _md(g["name"])
            L.append(f"- **{head}**: {'; '.join(who)}")
        L.append("")
    for m in comps:
        L += [f"## {m['ticker']}: {m.get('name') or ''}", "", f"Company page: {clink(m['ticker'])}", ""]
        kp = sorted(((k, s) for k, s in m.get("series", {}).items() if s["kind"] == "kpi"), key=lambda x: x[1]["name"])
        if not kp:
            L += ["No KPIs recorded yet.", ""]
            continue
        for k, s in kp:
            lt = latest(s)
            if lt:
                L.append(f"- {_md(display_name(k, s['name']))}: {lt[1].get('display') or _fmt(lt[1]['value'], s.get('unit'))} at {lt[0]} "
                         f"({_md((lt[1].get('src') or {}).get('form') or '')} p.{(lt[1].get('src') or {}).get('pdf_page') or '?'})")
        L.append("")
    return "\n".join(L)


def append_log(vault: Path, memory: dict, out: dict, meta: dict) -> str:
    from .brain import root
    p = root(vault) / "log.md"
    if not p.exists():
        head = _fm({"type": "crawler-log"}) + "# Crawler brain: log\n\nOne entry per read, newest last (`grep \"^## \\[\" log.md | tail`).\n\n"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(head, encoding="utf-8")
    elif not _owned(p):
        raise NotOurs(f"{p} was not written by the brain")
    th = out.get("thresholds", {}).get("measured", [])
    line = (f"## [{datetime.now().strftime('%Y-%m-%d %H:%M')}] read | {memory['ticker']} {meta.get('form') or ''} {meta.get('period') or meta.get('period_end') or ''} | "
            f"{meta.get('file')}\n- {out['merged']['new_points']} new points, {len(out['merged']['new_series'])} new series, "
            f"{len(out['merged']['restated'])} restated, {len(out.get('missing', []))} KPIs not found, {len(th)} thresholds measured; "
            f"status {out.get('status')}\n\n")
    with p.open("a", encoding="utf-8") as fh:
        fh.write(line)
    return str(p)


def new_observations(out: dict, memory: Optional[dict] = None) -> list:
    """Threshold readings for a period later than the reading THRESHOLDS.md already records, and not already sent to
    the inbox by another filing for the same period (an earnings release and then its 10-Q print the same reading).
    Only these go to the inbox: re-reading an older filing (or seeding the brain with history) adds no observation,
    so it never holds up the ship gate (Gate 14) with a reading Brock already has."""
    th = out.get("thresholds") or {}
    sent = (memory or {}).get("observed") or {}
    return [m for m in th.get("measured", []) if (not m.get("last_reading_date") or (m.get("period_end") or "") > str(m["last_reading_date"])[:10])
            and f"{m['id']}|{m.get('period_end')}" not in sent]


def write_all(vault: Path, memory: dict, out: dict, meta: dict, inbox: bool = True, only_read_note: bool = False) -> dict:
    """The company's pages under its lock (held by the caller); the pages every company shares (index, sector, log)
    under a lock of their own, so two reads finishing together never write them at once."""
    from .brain import root, company_dir, _lock, save
    t = memory["ticker"]
    cd = company_dir(vault, t)
    files, errors = [], []
    rec = next((r for r in memory.get("reads", []) if r.get("sha8") == meta.get("sha8")), {**meta})
    snap, ctx = None, None
    if not only_read_note:
        from .brain_peers import memories
        snap = memories(vault)              # every company once per write (memory.json is saved before the pages)
        ctx = graph_ctx(snap)
    own = [(cd / "reads" / f"{note_name(t, rec)}.md", lambda: read_note(memory, out, meta)),
           (cd / f"{t}.md", lambda: company_page(memory, out, (cd / f"{t}.md").read_text(encoding="utf-8") if (cd / f"{t}.md").exists() else "", ctx))]
    shared = [(root(vault) / "index.md", lambda: index_page(vault))]
    if out.get("status") == "seen before" and own[0][0].exists():
        own = own[1:]               # a filing read again keeps the note of its first read (what was expected then, Claude's reading)
    if only_read_note:
        own, shared = own[:1], []
    obs = new_observations(out, memory) if inbox and not only_read_note else []
    if obs:
        day = datetime.now().strftime("%Y-%m-%d")
        path = Path(vault) / "inbox" / "crawler" / f"{day}-{t}-{re.sub(r'[^A-Za-z0-9]+', '', meta.get('form') or 'doc')}-{meta.get('period_end') or 'undated'}-{meta.get('sha8') or ''}.md"
        own.append((path, lambda: inbox_note(memory, out, meta)))
        memory.setdefault("observed", {}).update({f"{m['id']}|{m.get('period_end')}": path.name for m in obs})
        save(vault, memory)              # the next filing for the same period does not send the same reading again
    for path, make in own:
        try:
            files.append(put(path, make()))
        except NotOurs as e:
            errors.append(str(e))
    if not only_read_note:
        with _lock("__shared__"):
            from .brain_peers import memories
            snap2 = memories(vault)          # read again here: the last writer of a shared page sees every company saved
            shared += industry_pages(vault, memory.get("profile"), snap2) + kpi_pages(vault, memory, snap2, graph_ctx(snap2))
            for path, make in shared:
                try:
                    files.append(put(path, make()))
                except NotOurs as e:
                    errors.append(str(e))
            try:
                files.append(append_log(vault, memory, out, meta))
            except NotOurs as e:
                errors.append(str(e))
    base = str(Path(vault))
    return {"written": [f[len(base) + 1:] if f.startswith(base) else f for f in files], "refused": errors}


def _front(path: Path) -> dict:
    """The frontmatter of a page the brain wrote, as {key: value} (JSON values read back)."""
    fm = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return fm
    if not text.startswith("---\n"):
        return fm
    for line in text[4:text.find("\n---", 4)].splitlines():
        k, _, v = line.partition(": ")
        try:
            fm[k] = json.loads(v)
        except ValueError:
            fm[k] = v
    return fm


def _rewrites(vault: Path, snap: list, seen: set) -> list:
    """(path, writer) for every other page the brain wrote under sectors/ and kpis/, from its own frontmatter: an industry
    no company holds now, a KPI fewer than two print. Pages that point at others (superseded_by) keep their own text."""
    from .brain import root
    out = []
    for path in sorted((root(vault) / "kpis").glob("*.md")):
        fm = _front(path) if path not in seen and _owned(path) else {}
        if fm.get("key"):
            out.append((path, (lambda b=fm["key"]: kpi_page(b, snap))))
    for path in sorted((root(vault) / "sectors").glob("*.md")):
        fm = _front(path) if path not in seen and _owned(path) else {}
        prof = fm.get("profile")
        if not prof or fm.get("superseded_by"):
            continue
        out.append((path, (lambda: general_page(snap)) if str(prof).strip().lower() == "general" else (lambda p=prof: sector_page(vault, p, snap))))
    return out


def rebuild_all(vault: Path) -> dict:
    """Every company page, industry page, KPI note and the index again from memory.json (each watch list measured again
    against the current monitoring/THRESHOLDS.md and brain/thresholds-map.json), without reading a filing:
    after a change to the pages, the whole graph changes at once instead of company by company. Read notes, log.md,
    inbox notes and memory.json are not touched. Runs in the local Crawler's server process (its locks are that
    process's), one company lock at a time and then the shared lock. Returns the files written and refused."""
    from .brain import not_set_up, root, company_dir, _lock, load, measure_thresholds
    from .brain_peers import memories
    why = not_set_up(vault)
    if why:
        return {"status": "off", "reason": why, "written": [], "refused": []}
    snap = memories(vault)
    ctx = graph_ctx(snap)
    files, errors = [], []
    for m0 in snap:
        t = m0["ticker"]
        with _lock(t):
            try:
                mem = load(vault, t)
            except PermissionError as e:
                errors.append(str(e))
                continue
            reads = mem.get("reads") or []
            th = measure_thresholds(vault, mem, reads[-1]) if reads else {"measured": []}
            p = company_dir(vault, t) / f"{t}.md"
            try:
                files.append(put(p, company_page(mem, {"thresholds": th}, p.read_text(encoding="utf-8") if p.exists() else "", ctx)))
            except NotOurs as e:
                errors.append(str(e))
    have = {m["ticker"] for m in snap}
    for p in sorted((root(vault) / "companies").glob("*/memory.json")):
        if p.parent.name not in have:
            errors.append(f"{p.relative_to(Path(vault))} is not a company book the brain can use (it does not parse, or "
                          "it was not written by the brain): left as it is, and its company is not on the rebuilt pages")
    with _lock("__shared__"):
        snap = memories(vault)
        ctx = graph_ctx(snap)
        shared, seen = [(root(vault) / "index.md", lambda: index_page(vault))], set()
        for prof in sorted({m.get("profile") for m in snap if m.get("profile")}):
            for path, make in industry_pages(vault, prof, snap):
                if path not in seen:
                    seen.add(path)
                    shared.append((path, make))
        for b in sorted(ctx["kpis"]):
            path = root(vault) / "kpis" / f"{kpi_slug(b)}.md"
            seen.add(path)
            shared.append((path, (lambda b=b: kpi_page(b, snap))))
        for path, make in _rewrites(vault, snap, seen):
            shared.append((path, make))
        for path, make in shared:
            try:
                files.append(put(path, make()))
            except NotOurs as e:
                errors.append(str(e))
    base = str(Path(vault))
    return {"status": "rebuilt", "companies": len(snap), "kpi_notes": len(ctx["kpis"]),
            "written": [f[len(base) + 1:] if f.startswith(base) else f for f in files], "refused": errors}
