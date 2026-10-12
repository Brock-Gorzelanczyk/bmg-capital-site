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


def extract_all_pending() -> dict:
    """Walk /data/raw and extract any file whose text/ counterpart is missing.

    Also UPDATES the document row's text_path so /desk/doc/{id} can serve the
    extracted markdown. Prior version wrote the .md file but left text_path
    NULL in SQLite, so /desk/doc returned no text (observed on live 2026-10-11).
    """
    counts = {"html": 0, "pdf": 0, "skipped": 0, "errors": [], "linked": 0}
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
        try:
            with store.connect() as conn:
                r = conn.execute(
                    "UPDATE documents SET text_path = ? WHERE raw_path = ? AND (text_path IS NULL OR text_path = '')",
                    (text_rel, raw_rel),
                )
                if r.rowcount > 0:
                    counts["linked"] += 1
        except Exception as e:
            counts["errors"].append(f"link {p}: {e}")
    return counts
