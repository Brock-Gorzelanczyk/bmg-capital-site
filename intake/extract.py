"""Text extraction: trafilatura for HTML (markdown, tables on); pdfplumber for PDF."""
from __future__ import annotations

import logging
from pathlib import Path

from . import store

log = logging.getLogger("intake.extract")

try:
    import trafilatura
    TRA_OK = True
except ImportError:
    TRA_OK = False

try:
    import pdfplumber
    PDF_OK = True
except ImportError:
    PDF_OK = False


def extract_html_to_md(html_path: Path) -> Path:
    """Run trafilatura on an HTML file; write markdown next to it under /data/text/."""
    if not TRA_OK:
        raise RuntimeError("trafilatura not installed")
    data = html_path.read_bytes()
    md = trafilatura.extract(
        data, output_format="markdown", include_tables=True, include_links=True, include_images=False
    ) or ""
    rel = html_path.relative_to(store.DATA_DIR / "raw")
    out = store.DATA_DIR / "text" / rel.with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(md)
    return out


def extract_pdf_to_md(pdf_path: Path) -> Path:
    """pdfplumber: page text + tables as markdown."""
    if not PDF_OK:
        raise RuntimeError("pdfplumber not installed")
    rel = pdf_path.relative_to(store.DATA_DIR / "raw")
    out = store.DATA_DIR / "text" / rel.with_suffix(".md")
    out.parent.mkdir(parents=True, exist_ok=True)
    chunks = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages, start=1):
            chunks.append(f"\n\n## Page {i}\n")
            text = page.extract_text() or ""
            chunks.append(text)
            for ti, tbl in enumerate(page.extract_tables() or [], start=1):
                chunks.append(f"\n\n### Page {i} table {ti}\n")
                if not tbl:
                    continue
                header = tbl[0]
                chunks.append("| " + " | ".join(str(c or "") for c in header) + " |")
                chunks.append("| " + " | ".join(["---"] * len(header)) + " |")
                for row in tbl[1:]:
                    chunks.append("| " + " | ".join(str(c or "") for c in row) + " |")
    out.write_text("\n".join(chunks))
    return out


import re as _re


# Fact-extractor regexes. Each entry: (label, pattern, group_index, unit).
# Patterns run against the extracted markdown text; `group_index` is which
# capture group holds the printable value. Char offset is `match.start(group)`.
# Facts are stored per-document in SQLite so /desk/live can draw a box
# around the matching substring in the client-side text pane.
_FACT_PATTERNS: list[tuple] = [
    ("Revenue",        _re.compile(r'Revenues?\s*[|$\s]*\$\s*([\d,]+\.?\d*)'),            1, "$M"),
    ("EPS",            _re.compile(r'(?:diluted earnings per (?:common )?share|EPS|earnings per common share)[^$]{0,80}?\$([\d.]+)', _re.I), 1, "$/sh"),
    ("Backlog units",  _re.compile(r'backlog[^.]{0,80}?([\d,]{4,7})\s*(?:unit|railcar|cars)', _re.I), 1, "units"),
    ("Backlog $B",     _re.compile(r'backlog[^.]{0,80}?\$?([\d.]+)\s*billion', _re.I), 1, "$B"),
    ("Fleet",          _re.compile(r'([\d,]{3,6})\s*railcars'), 1, "cars"),
    ("Utilization",    _re.compile(r'utilization[^.]{0,80}?([\d.]+)\s*%', _re.I), 1, "%"),
    ("Dividend",       _re.compile(r'(?:dividend|per\s+share\s+dividend)[^$]{0,60}?\$([\d.]+)', _re.I), 1, "$/sh"),
    ("Net income",     _re.compile(r'Net (?:earnings|income)\s*[|$\s]*\$\s*([\d,]+\.?\d*)'), 1, "$M"),
]


def _extract_facts_from_text(doc_id: str, text: str) -> int:
    """Scan text with _FACT_PATTERNS, insert any matches into facts table.
    Dedup within one call by (label, char_offset)."""
    inserted = 0
    seen: set[tuple] = set()
    for label, pat, grp, unit in _FACT_PATTERNS:
        for m in pat.finditer(text):
            try:
                val = m.group(grp)
            except IndexError:
                continue
            offset = m.start(grp)
            key = (label, offset)
            if key in seen:
                continue
            seen.add(key)
            try:
                store.insert_fact(document_id=doc_id, label=label, value=val,
                                   unit=unit, char_offset=offset)
                inserted += 1
                try:
                    from . import events as _events
                    _events.emit("extract_fact", doc_id=doc_id, label=label,
                                 value=val, unit=unit, char_offset=offset)
                except Exception:
                    pass
            except Exception as e:
                log.warning(f"insert_fact failed {doc_id} {label}: {e}")
            if inserted >= 10:
                return inserted
    return inserted


def _extract_facts_from_meta(doc_id: str, doc_type: str, meta: dict, text_sample: str = "") -> int:
    """Meta-driven facts (AAR carloads/intermodal, FRED series value)."""
    inserted = 0
    if doc_type == "SERIES":
        sid = meta.get("series_id") or ""
        val = meta.get("last_value")
        last = meta.get("last_date")
        if sid and val is not None:
            try:
                store.insert_fact(document_id=doc_id, label=sid, value=str(val),
                                   unit=str(last) if last else None, char_offset=None)
                inserted += 1
            except Exception as e:
                log.warning(f"insert_fact SERIES {sid} failed: {e}")
    if doc_type == "RAIL_TRAFFIC":
        for key, label in (("carloads", "Carloads"), ("intermodal", "Intermodal")):
            v = meta.get(key)
            if v:
                offset = text_sample.find(str(v)) if text_sample else None
                if offset == -1:
                    offset = None
                try:
                    store.insert_fact(document_id=doc_id, label=label, value=str(v),
                                       unit="units/week", char_offset=offset)
                    inserted += 1
                except Exception as e:
                    log.warning(f"insert_fact {label} failed: {e}")
    return inserted


def extract_all_pending() -> dict:
    """Walk /data/raw and extract any file whose text/ counterpart is missing.

    Also UPDATES the document row's text_path so /desk/doc/{id} can serve the
    extracted markdown. Prior version wrote the .md file but left text_path
    NULL in SQLite, so /desk/doc returned no text (observed on live 2026-10-11).
    Also runs the fact extractor on the extracted text and populates the
    facts table so /desk/live can draw a box around each stored match.
    """
    counts = {"html": 0, "pdf": 0, "skipped": 0, "errors": [], "linked": 0, "facts": 0}
    raw_root = store.DATA_DIR / "raw"
    if not raw_root.exists():
        return counts
    for p in raw_root.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(raw_root)
        text_target = store.DATA_DIR / "text" / rel.with_suffix(".md")
        raw_rel = str(p.relative_to(store.DATA_DIR))
        text_rel = str(text_target.relative_to(store.DATA_DIR))
        if text_target.exists():
            counts["skipped"] += 1
        else:
            try:
                if p.suffix.lower() in (".htm", ".html"):
                    extract_html_to_md(p)
                    counts["html"] += 1
                elif p.suffix.lower() == ".pdf":
                    extract_pdf_to_md(p)
                    counts["pdf"] += 1
                else:
                    continue
            except Exception as e:
                counts["errors"].append(f"{p}: {e}")
                log.warning(f"extract failed {p}: {e}")
                continue
        # Link the document row to its extracted text (idempotent)
        doc_id = None
        try:
            with store.connect() as conn:
                r = conn.execute(
                    "UPDATE documents SET text_path = ? WHERE raw_path = ? AND (text_path IS NULL OR text_path = '')",
                    (text_rel, raw_rel),
                )
                if r.rowcount > 0:
                    counts["linked"] += 1
                row = conn.execute(
                    "SELECT id, type, meta_json FROM documents WHERE raw_path = ?",
                    (raw_rel,),
                ).fetchone()
                if row:
                    doc_id = row["id"]
                    doc_type = row["type"]
                    import json as _json
                    try:
                        meta = _json.loads(row["meta_json"] or "{}")
                    except Exception:
                        meta = {}
        except Exception as e:
            counts["errors"].append(f"link {p}: {e}")
            doc_id = None
            doc_type = None
            meta = {}
        # Fact extraction (idempotent via (label, char_offset) uniqueness within doc)
        if doc_id and text_target.exists():
            try:
                existing = len(store.facts_for_doc(doc_id))
                if existing == 0:
                    text = text_target.read_text(errors="ignore")
                    counts["facts"] += _extract_facts_from_text(doc_id, text)
                    counts["facts"] += _extract_facts_from_meta(doc_id, doc_type, meta, text)
            except Exception as e:
                counts["errors"].append(f"fact-extract {doc_id}: {e}")
    # Also mine facts from SERIES/RAIL_TRAFFIC rows that have meta but no text
    try:
        with store.connect() as conn:
            rows = conn.execute(
                "SELECT id, type, meta_json FROM documents "
                "WHERE type IN ('SERIES','RAIL_TRAFFIC') "
                "AND id NOT IN (SELECT document_id FROM facts)"
            ).fetchall()
        import json as _json
        for row in rows:
            try:
                meta = _json.loads(row["meta_json"] or "{}")
            except Exception:
                meta = {}
            counts["facts"] += _extract_facts_from_meta(row["id"], row["type"], meta, "")
    except Exception as e:
        counts["errors"].append(f"meta-mine: {e}")
    return counts
