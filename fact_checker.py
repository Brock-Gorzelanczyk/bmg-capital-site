"""Fact-Checker: text extraction, claim extraction, matching, staleness, diff.

Pure Python; no LLM. Dependencies at module scope kept to free libraries only.
"""
from __future__ import annotations

import io
import re
import unicodedata
from typing import Any, Optional
from datetime import date


# ----------------------------------------------------------------------
# Text extraction (file bytes to list of {page_or_slide, text})
# ----------------------------------------------------------------------

def extract_pdf(data: bytes) -> list[dict]:
    import pypdf
    reader = pypdf.PdfReader(io.BytesIO(data))
    pages = []
    for i, pg in enumerate(reader.pages, 1):
        try:
            text = pg.extract_text() or ""
        except Exception:
            text = ""
        pages.append({"locator": f"p.{i}", "text": text})
    return pages


def extract_docx(data: bytes) -> list[dict]:
    import docx
    doc = docx.Document(io.BytesIO(data))
    # DOCX has no inherent page markers; chunk every ~40 paragraphs so
    # the location is still useful.
    paras = [p.text for p in doc.paragraphs if p.text.strip()]
    for tbl in doc.tables:
        for row in tbl.rows:
            for c in row.cells:
                t = c.text.strip()
                if t:
                    paras.append(t)
    chunks = []
    size = 40
    for i in range(0, len(paras), size):
        chunk = "\n".join(paras[i:i+size])
        chunks.append({"locator": f"para {i+1}-{min(i+size, len(paras))}",
                       "text": chunk})
    return chunks if chunks else [{"locator": "doc", "text": ""}]


def extract_pptx(data: bytes) -> list[dict]:
    from pptx import Presentation
    p = Presentation(io.BytesIO(data))
    slides = []
    for i, s in enumerate(p.slides, 1):
        parts = []
        for sh in s.shapes:
            if sh.has_text_frame:
                parts.append(sh.text_frame.text)
            if sh.has_table:
                for row in sh.table.rows:
                    for c in row.cells:
                        parts.append(c.text)
        slides.append({"locator": f"slide {i}", "text": "\n".join(parts)})
    return slides


def extract_html(data: bytes) -> list[dict]:
    from bs4 import BeautifulSoup
    s = BeautifulSoup(data, "html.parser")
    for tag in s(["script", "style"]):
        tag.decompose()
    text = s.get_text(separator=" ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n+", "\n", text)
    return [{"locator": "doc", "text": text}]


def extract_text(data: bytes) -> list[dict]:
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        text = data.decode("latin-1", errors="replace")
    return [{"locator": "doc", "text": text}]


EXTRACTORS = {
    "pdf": extract_pdf,
    "docx": extract_docx,
    "pptx": extract_pptx,
    "html": extract_html,
    "text": extract_text,
}


def extract(file_bytes: bytes, kind: str) -> list[dict]:
    fn = EXTRACTORS.get(kind)
    if not fn:
        raise ValueError(f"unsupported kind: {kind}")
    return fn(file_bytes)


# ----------------------------------------------------------------------
# Claim extraction
# ----------------------------------------------------------------------

# Period words: FY2024, FY 2024, 2024, Q1 2026, Q1-2026, Q1/2026,
# H1 2026, "as of June 30, 2026", "December 31, 2025", 2027E (estimate),
# 2026-10-07
PERIOD_RE = re.compile(
    r"(?:"
    r"FY\s?\d{4}|"
    r"Q[1-4]\s?(?:FY)?\s?\d{4}|"
    r"H[12]\s?\d{4}|"
    r"as of \w+\s+\d{1,2},?\s+\d{4}|"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},?\s+\d{4}|"
    r"\d{4}-\d{2}-\d{2}|"
    r"\d{4}[EA]|"
    r"\b\d{4}\b"
    r")"
)

# Numbers with units: $12.3M, $1,881.1M, $1.9B, 29.3%, $9.12, 2.0x,
# $10.47 per share, 35.9M diluted shares
MONEY_RE = re.compile(
    r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s?([MB]?)(?!\w)"
)
# Standalone number followed by unit words like "shares", "cars", "customers"
# but keep simple: just use pct, dollar, ratio, decimal numbers > 1 digit
PCT_RE = re.compile(r"(?:(?<=\s)|^)(?:\+|-)?(\d{1,3}(?:\.\d+)?)\s?%")
RATIO_RE = re.compile(r"\b(\d+(?:\.\d+)?)x\b")
# Plain decimals like "9.12", "35.9" when preceded by $ (handled by MONEY) or
# when followed by "per share"
PERSHARE_RE = re.compile(r"\$?(\d+\.\d{1,4})\s*(?:per share|/sh|a share)",
                         re.IGNORECASE)

ESTIMATE_HINTS = re.compile(r"\b(?:consensus|forecast|BMG model|my model|"
                            r"target(?: price| of)|price target|FactSet|"
                            r"GATX-FSM|v\d\d\b)",
                            re.IGNORECASE)
ESTIMATE_SUFFIX = re.compile(r"\b20\d{2}E\b")  # 2027E


def _window(text: str, start: int, end: int, pad: int = 100) -> str:
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e].strip()


def _tight_window(text: str, start: int, end: int, pad: int = 40) -> str:
    """Smaller window used only for line-item mapping — avoids catching
    unrelated phrases that happen to share a paragraph."""
    s = max(0, start - pad)
    e = min(len(text), end + pad)
    return text[s:e]


def _sentence_window(text: str, start: int, end: int) -> str:
    """Return the sentence enclosing the match. Sentences bounded by
    '. ' or newline. This stops a nearby but unrelated EPS phrase in
    the next sentence from matching the current number."""
    left_cut = max(text.rfind(". ", 0, start),
                    text.rfind(".\n", 0, start),
                    text.rfind("\n", 0, start),
                    text.rfind("| ", 0, start))
    if left_cut < 0:
        left_cut = max(0, start - 120)
    else:
        left_cut += 2
    right_cut_candidates = [p for p in (
        text.find(". ", end), text.find(".\n", end),
        text.find("\n", end)) if p >= 0]
    right_cut = min(right_cut_candidates) if right_cut_candidates \
        else min(len(text), end + 120)
    return text[left_cut:right_cut]


# Phrases that indicate a number is a derived price or multiple, not
# the per-share line item it sits near.
PRICE_INDICATORS = re.compile(
    r"\b(?:implies|implied|at the|multiple|P/E|P/B|times|×|x on|"
    r"target|price target|at \d|at \$|yields|gives \$|is \$|get[s]? \$|"
    r"below about \$|above about \$|BUY line|SELL line)\b",
    re.IGNORECASE)


def _nearest_period(text: str, m_start: int, m_end: int) -> list[str]:
    """Pick the period token whose character distance to the match is
    smallest. Returns [token] or []. If a period directly abuts the
    number (table layout), it wins."""
    best = None
    best_dist = 10**9
    for m in PERIOD_RE.finditer(text):
        s, e = m.span()
        if s >= m_start and s <= m_end:
            dist = 0
        elif e <= m_start:
            dist = m_start - e
        else:
            dist = s - m_end
        if dist < best_dist:
            tok = re.sub(r"\s+", " ", m.group(0).strip())
            if tok.isdigit() and not (2000 <= int(tok) <= 2100):
                continue
            best = tok
            best_dist = dist
    return [best] if best else []


def extract_claims(pages: list[dict]) -> list[dict]:
    """Return a list of claim dicts. One row per found number."""
    out = []
    for pg in pages:
        text = pg["text"] or ""
        if not text:
            continue
        for m in MONEY_RE.finditer(text):
            raw = m.group(0)
            amount = _parse_money(m.group(1), m.group(2))
            start, end = m.span()
            win = _window(text, start, end)
            tight = _tight_window(text, start, end, pad=40)
            sent = _sentence_window(text, start, end)
            out.append({
                "locator": pg["locator"],
                "raw": raw,
                "value": amount,
                "unit": _unit_of(m.group(2)),
                "class": "money",
                "context": win,
                "tight_context": tight,
                "sentence": sent,
                "periods": _nearest_period(text, start, end) or _periods_in(win),
                "estimate": _is_estimate(sent),
                "has_scale": bool(m.group(2)),
            })
        for m in PCT_RE.finditer(text):
            raw = m.group(0).strip()
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            start, end = m.span()
            win = _window(text, start, end)
            out.append({
                "locator": pg["locator"],
                "raw": raw,
                "value": v,
                "unit": "pct",
                "class": "pct",
                "context": win,
                "periods": _periods_in(win),
                "estimate": _is_estimate(win),
            })
        for m in RATIO_RE.finditer(text):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            start, end = m.span()
            win = _window(text, start, end)
            out.append({
                "locator": pg["locator"],
                "raw": m.group(0),
                "value": v,
                "unit": "x",
                "class": "ratio",
                "context": win,
                "periods": _periods_in(win),
                "estimate": _is_estimate(win),
            })
        for m in PERSHARE_RE.finditer(text):
            try:
                v = float(m.group(1))
            except ValueError:
                continue
            start, end = m.span()
            win = _window(text, start, end)
            out.append({
                "locator": pg["locator"],
                "raw": m.group(0),
                "value": v,
                "unit": "per_share",
                "class": "per_share",
                "context": win,
                "periods": _periods_in(win),
                "estimate": _is_estimate(win),
            })
    return out


def _parse_money(num: str, scale: str) -> float:
    try:
        n = float(num.replace(",", ""))
    except ValueError:
        return 0.0
    if scale == "B":
        return n * 1_000.0  # express in millions
    if scale == "M":
        return n
    return n / 1_000_000.0  # dollars to millions


def _unit_of(scale: str) -> str:
    if scale == "B":
        return "USD_m"
    if scale == "M":
        return "USD_m"
    return "USD"


def _periods_in(text: str) -> list[str]:
    out = set()
    for m in PERIOD_RE.finditer(text):
        tok = m.group(0).strip()
        # normalize whitespace within token
        tok = re.sub(r"\s+", " ", tok)
        if tok.isdigit():
            # Reject bare numbers that are not a 20xx year
            if not (2000 <= int(tok) <= 2100):
                continue
        out.add(tok)
    return sorted(out)


def _is_estimate(text: str) -> bool:
    return bool(ESTIMATE_HINTS.search(text) or ESTIMATE_SUFFIX.search(text))


# ----------------------------------------------------------------------
# Reported-line-item mapping
# ----------------------------------------------------------------------

# Phrases in the user's doc that we map to a reported line item.
LINE_ITEMS = [
    (re.compile(r"net income (?:attributable to|to) (?:GATX|the company|the "
                r"parent|stockholders)|net income\b", re.IGNORECASE),
     "net_income"),
    (re.compile(r"\brevenue(?:s)?\b|total revenue|net sales\b", re.IGNORECASE),
     "revenue"),
    (re.compile(r"\bdiluted eps\b|earnings per share\b|GAAP EPS\b|EPS\b",
                re.IGNORECASE),
     "eps_diluted"),
    (re.compile(r"\bdiluted shares\b|weighted-average diluted\b|"
                r"diluted shares outstanding\b", re.IGNORECASE),
     "shares_diluted"),
    (re.compile(r"stockholders'? equity\b|shareholders'? equity\b|"
                r"\btotal equity\b", re.IGNORECASE),
     "equity"),
    (re.compile(r"cash and cash equivalents\b|cash balance\b", re.IGNORECASE),
     "cash"),
    (re.compile(r"long-term debt\b|total debt\b|\bdebt\b", re.IGNORECASE),
     "debt"),
    (re.compile(r"cash from operations\b|operating cash flow\b|CFO\b",
                re.IGNORECASE),
     "cfo"),
    (re.compile(r"interest expense\b", re.IGNORECASE),
     "interest_expense"),
    (re.compile(r"dividend(?:s)?(?: per share)?\b", re.IGNORECASE),
     "dps"),
]


def map_to_line_item(context: str) -> Optional[str]:
    for pat, name in LINE_ITEMS:
        if pat.search(context):
            return name
    return None


# ----------------------------------------------------------------------
# Period normalization and parsing
# ----------------------------------------------------------------------

MONTH_NUM = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
             "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
             "november": 11, "december": 12}


def parse_period(tok: str) -> Optional[tuple[str, int, Optional[int]]]:
    """Return (kind, year, q_or_month) where kind is 'FY' | 'Q' | 'H' | 'date'.
    For 'date', q_or_month is month int.
    """
    tok = tok.strip()
    if re.match(r"^FY\s?\d{4}$", tok):
        return ("FY", int(tok[-4:]), None)
    m = re.match(r"^Q([1-4])\s?(?:FY)?\s?(\d{4})$", tok)
    if m:
        return ("Q", int(m.group(2)), int(m.group(1)))
    m = re.match(r"^H([12])\s?(\d{4})$", tok)
    if m:
        return ("H", int(m.group(2)), int(m.group(1)))
    # ISO date
    if re.match(r"^\d{4}-\d{2}-\d{2}$", tok):
        return ("date", int(tok[:4]), int(tok[5:7]))
    m = re.match(r"^(\d{4})E$", tok)
    if m:
        return ("FY-E", int(m.group(1)), None)
    m = re.match(r"^(\d{4})A$", tok)
    if m:
        return ("FY-A", int(m.group(1)), None)
    m = re.match(r"^(?:as of )?(\w+)\s+(\d{1,2}),?\s+(\d{4})$", tok,
                 re.IGNORECASE)
    if m:
        mo = MONTH_NUM.get(m.group(1).lower())
        if mo:
            return ("date", int(m.group(3)), mo)
    if re.match(r"^\d{4}$", tok):
        return ("FY", int(tok), None)
    return None


def period_to_xbrl_key(kind: str, year: int, q_or_month: Optional[int]
                      ) -> Optional[tuple[str, str]]:
    """Map a parsed period to the key we use against XBRL series.
    Returns ('fy', year) or ('q', 'YYYY-MM-DD-ish') or None."""
    if kind in ("FY", "FY-A"):
        return ("fy", str(year))
    if kind == "Q":
        # Approximate end-month by quarter
        end_month = {1: 3, 2: 6, 3: 9, 4: 12}[q_or_month]
        return ("q", f"{year:04d}-{end_month:02d}")
    if kind == "date":
        return ("q", f"{year:04d}-{q_or_month:02d}")
    if kind == "H":
        # H1 ends June, H2 ends December
        end_month = 6 if q_or_month == 1 else 12
        return ("q", f"{year:04d}-{end_month:02d}")
    return None


# ----------------------------------------------------------------------
# Rounding comparison
# ----------------------------------------------------------------------

def near(a: float, b: float, tol_rel: float = 0.05) -> bool:
    """Return True if a is within tol_rel (relative) of b, with a small
    absolute floor for small numbers."""
    if b == 0:
        return abs(a - b) < 0.01
    if a == 0:
        return abs(a - b) < 0.01
    return abs(a - b) / abs(b) <= tol_rel


# ----------------------------------------------------------------------
# Matching: compare a claim to the XBRL fact for the same (line, period)
# ----------------------------------------------------------------------

def match_claim_to_filings(claim: dict, xbrl: dict
                           ) -> dict:
    """Return {status, filing_value, basis, snippet, filing}.
    status in {MATCH, MISMATCH, NOT_IN_FILINGS}."""
    if claim.get("estimate"):
        return {"status": "NOT_IN_FILINGS",
                "reason": "estimate / forecast / consensus / model number",
                "filing": None}
    # Reject units we cannot reconcile against XBRL dollar/share facts.
    if claim.get("unit") in ("pct", "x"):
        return {"status": "NOT_IN_FILINGS",
                "reason": "percentage or ratio; not a reported line item",
                "filing": None}
    # Line-item match uses the SENTENCE enclosing the number so a stray
    # phrase in a different sentence does not borrow a mapping.
    scope = claim.get("sentence") or claim.get("tight_context") \
        or claim["context"]
    line = map_to_line_item(scope)
    if not line:
        return {"status": "NOT_IN_FILINGS",
                "reason": "no mapped reported line item in the sentence",
                "filing": None}
    # If the user wrote $171 without an M/B scale and we mapped to a
    # dollar-millions metric (revenue, net income, debt, etc), that is
    # almost certainly a price or a rounded figure the user intended as
    # absolute, not millions. Skip the comparison rather than risk a
    # false MISMATCH.
    dollar_millions_metrics = {"revenue", "net_income", "equity", "cash",
                                "debt", "cfo", "capex", "interest_expense"}
    if (line in dollar_millions_metrics
            and claim.get("unit") == "USD"
            and not claim.get("has_scale")):
        return {"status": "NOT_IN_FILINGS",
                "reason": "number has no M/B scale; cannot compare a "
                          "bare dollar figure to a millions line item",
                "filing": None, "line_item": line}
    # If the mapped line is EPS/DPS but the sentence has a price-marker
    # (implies, multiple, P/E, target, "at $X"), this is a derived price
    # not an actual EPS, so do not try to match.
    if line in ("eps_diluted", "dps") and PRICE_INDICATORS.search(scope):
        return {"status": "NOT_IN_FILINGS",
                "reason": "number sits in a price / multiple / target "
                          "sentence, not a reported EPS or DPS claim",
                "filing": None, "line_item": line}
    series = xbrl.get(line) or {}
    fy = series.get("fy") or []
    q = series.get("q") or []
    # Try to pin a period
    period = None
    for tok in claim.get("periods", []):
        pp = parse_period(tok)
        if pp:
            period = pp
            break
    if period is None:
        return {"status": "NOT_IN_FILINGS",
                "reason": "no period token near the number; can only "
                          "verify period-tagged line items",
                "filing": None,
                "line_item": line}
    key = period_to_xbrl_key(*period)
    if key is None:
        return {"status": "NOT_IN_FILINGS",
                "reason": f"period {period} not XBRL-mappable",
                "filing": None,
                "line_item": line}

    kind, k = key
    filing_val = None
    filing_end = None
    if kind == "fy":
        for r in fy:
            if str(r.get("fy")) == k:
                filing_val = r.get("val")
                filing_end = r.get("end")
                break
    else:
        for r in q:
            end = r.get("end", "")
            if end.startswith(k):
                filing_val = r.get("val")
                filing_end = end
                break
    if filing_val is None:
        return {"status": "NOT_IN_FILINGS",
                "reason": f"line item {line} has no XBRL value for "
                          f"period {period[0]} {period[1]}",
                "filing": None,
                "line_item": line}

    # Convert both to the same scale
    claim_v = claim["value"]
    unit = claim.get("unit")
    if line in ("eps_diluted", "dps"):
        # Per-share figures: use the raw USD amount, not the M/B scaled.
        # _parse_money divides by 1M for bare dollars, so recover.
        if unit == "USD" and not claim.get("has_scale"):
            # Raw USD $9.12 was stored as 9.12 / 1_000_000 = 9.12e-6;
            # recover the real $9.12 figure.
            try:
                import re as _re
                m = _re.match(r"\$?\s?(\d[\d,]*(?:\.\d+)?)",
                              (claim.get("raw") or "").strip())
                if m:
                    claim_v = float(m.group(1).replace(",", ""))
            except Exception:
                pass
        filing_scaled = filing_val
    elif unit == "USD_m" and line in ("revenue", "net_income", "equity",
                                       "cash", "debt", "cfo", "capex",
                                       "interest_expense"):
        filing_scaled = filing_val / 1_000_000.0
    elif unit == "USD_m" and line == "shares_diluted":
        filing_scaled = filing_val / 1_000_000.0
    else:
        filing_scaled = filing_val

    tol = 0.05
    if line == "debt":
        tol = 0.08  # debt totals have more rounding variance
    if near(claim_v, filing_scaled, tol_rel=tol):
        status = "MATCH"
    else:
        status = "MISMATCH"
    return {"status": status,
            "line_item": line,
            "filing_value": filing_scaled,
            "filing_period": f"{kind} {k}",
            "filing_end": filing_end,
            "claim_value": claim_v,
            "basis": "XBRL companyfacts"}


# ----------------------------------------------------------------------
# Staleness
# ----------------------------------------------------------------------

def staleness(doc_periods: list[str], filings: list[dict]
              ) -> list[dict]:
    """filings = [{form, filed (YYYY-MM-DD), accession, url, items?}]."""
    # Latest doc period as (year, month)
    latest = None
    for tok in doc_periods:
        pp = parse_period(tok)
        if not pp: continue
        kind, year, m_or_q = pp
        if kind == "Q":
            month = {1:3,2:6,3:9,4:12}[m_or_q]
        elif kind == "H":
            month = 6 if m_or_q == 1 else 12
        elif kind == "date":
            month = m_or_q
        elif kind in ("FY","FY-A"):
            month = 12
        else:
            continue
        tup = (year, month)
        if latest is None or tup > latest:
            latest = tup
    stale_list = []
    if latest is None:
        return stale_list
    cutoff = date(latest[0], latest[1], 28)
    for f in filings:
        try:
            fd = date.fromisoformat(f["filed"])
        except Exception:
            continue
        if fd > cutoff:
            stale_list.append(f)
    return stale_list


# ----------------------------------------------------------------------
# Section diff (two latest same-type filings)
# ----------------------------------------------------------------------

SECTION_PATTERNS = {
    "risk_factors": re.compile(r"risk factors", re.IGNORECASE),
    "mda_liquidity": re.compile(r"liquidity|capital resources", re.IGNORECASE),
    "debt_note": re.compile(r"long-?term debt|credit (?:agreement|facility)",
                            re.IGNORECASE),
    "legal_proceedings": re.compile(r"legal proceedings|litigation",
                                     re.IGNORECASE),
}


def _normalize_sentence(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = re.sub(r"\s+", " ", s).strip()
    # Normalize dollar amounts to a token so trivial reformatting
    # does not look like a change.
    s = re.sub(r"\$\s?\d[\d,\.]*", "$<num>", s)
    s = re.sub(r"\d+(?:\.\d+)?%", "<pct>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}", "<date>", s)
    return s.lower()


def _split_sentences(text: str) -> list[str]:
    text = re.sub(r"\s+", " ", text)
    parts = re.split(r"(?<=[\.\!\?])\s+(?=[A-Z\$\(\[])", text)
    return [p.strip() for p in parts if len(p.strip()) > 10]


def section_diff(new_text: str, old_text: str) -> dict:
    """Return {added, removed} dict keyed by section."""
    out = {}
    for sec_name, pat in SECTION_PATTERNS.items():
        n_sents = [s for s in _split_sentences(new_text)
                   if pat.search(s)]
        o_sents = [s for s in _split_sentences(old_text)
                   if pat.search(s)]
        n_norm = {_normalize_sentence(s): s for s in n_sents}
        o_norm = {_normalize_sentence(s): s for s in o_sents}
        added = [n_norm[k] for k in n_norm.keys() - o_norm.keys()][:5]
        removed = [o_norm[k] for k in o_norm.keys() - n_norm.keys()][:5]
        out[sec_name] = {"added": added, "removed": removed}
    return out
