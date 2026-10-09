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
# Accept ASCII minus, Unicode minus (U+2212) and HTML &minus; before the digits.
PCT_RE = re.compile(r"(?:(?<=\s)|^)([+\-−]?)(\d{1,3}(?:\.\d+)?)\s?%")
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
    '. ' / newline / '; ' / '| '. The semicolon split is important for
    doc text like 'Break-even LPI in 2027 is -29% ; the 2017 trough
    was -24%' where the two clauses should classify independently."""
    left_cut = max(text.rfind(". ", 0, start),
                    text.rfind(".\n", 0, start),
                    text.rfind("\n", 0, start),
                    text.rfind("| ", 0, start),
                    text.rfind("; ", 0, start),
                    text.rfind(";\n", 0, start))
    if left_cut < 0:
        left_cut = max(0, start - 120)
    else:
        left_cut += 2
    right_cut_candidates = [p for p in (
        text.find(". ", end), text.find(".\n", end),
        text.find("\n", end), text.find("; ", end),
        text.find(";\n", end)) if p >= 0]
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
            sign_tok = m.group(1) or ""
            try:
                v = float(m.group(2))
            except ValueError:
                continue
            if sign_tok in ("-", "−"):
                v = -v
            start, end = m.span()
            win = _window(text, start, end)
            sent = _sentence_window(text, start, end)
            out.append({
                "locator": pg["locator"],
                "raw": raw,
                "value": v,
                "unit": "pct",
                "class": "pct",
                "context": win,
                "sentence": sent,
                "periods": _nearest_period(text, start, end) or _periods_in(win),
                "estimate": _is_estimate(sent),
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


# ======================================================================
# TOOL-3 additions: number-first scan, derived metrics, model guard,
# text-fact scanner.
# ======================================================================

# ----- 3a. MODEL_OR_ADJUSTED guard -----

MODEL_OR_ADJUSTED_RE = re.compile(
    r"\b(?:adjusted|normalized|normal[\s-]?gains|ex[\s-]?gains|"
    r"ex\s+gains|excluding|estimate|consensus|target(?:ed)?|"
    r"BMG|my model|the model|our model|GATX-FSM|FSM-v\d+|v\d\d|"
    r"pro[\s-]?forma|non-?GAAP|\bcore\b|break[\s-]?even|"
    r"fades|fading|scenario|renewal bridge|repricing bridge|"
    r"\bbridge\b)|20\d{2}E\b",
    re.IGNORECASE,
)


def is_model_or_adjusted(sentence: str) -> bool:
    if not sentence:
        return False
    return bool(MODEL_OR_ADJUSTED_RE.search(sentence))


# ----- 3b. Number-first scan over the full companyfacts JSON -----

def _implied_period(claim: dict) -> Optional[tuple[str, int, Optional[int]]]:
    """Return the strongest period signal the claim's periods carry.
    Prefer tightest (date / quarter / H) over FY, then latest year.
    Returns (kind, year, month_or_q) or None."""
    best: Optional[tuple[str, int, Optional[int]]] = None
    best_rank = -1
    rank = {"date": 4, "Q": 3, "H": 2, "FY-E": 1, "FY-A": 1, "FY": 1}
    for tok in claim.get("periods") or []:
        pp = parse_period(tok)
        if not pp:
            continue
        kind, year, mq = pp
        r = rank.get(kind, 0)
        if r > best_rank or (r == best_rank and best and year > best[1]):
            best = pp
            best_rank = r
    return best


def _period_end_date(pp: tuple[str, int, Optional[int]]) -> Optional[str]:
    """Convert parsed period to a YYYY-MM-DD end date for matching."""
    kind, year, mq = pp
    if kind in ("FY", "FY-A"):
        return f"{year:04d}-12-31"
    if kind == "Q":
        em = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}.get(mq)
        return f"{year:04d}-{em}" if em else None
    if kind == "H":
        em = "06-30" if mq == 1 else "12-31"
        return f"{year:04d}-{em}"
    if kind == "date" and mq:
        # Last day of month, approximate
        last = {1: 31, 2: 28, 3: 31, 4: 30, 5: 31, 6: 30,
                7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}.get(mq, 28)
        return f"{year:04d}-{mq:02d}-{last}"
    return None


def _row_matches_period(row: dict,
                         pp: Optional[tuple[str, int, Optional[int]]],
                         latest_ok: bool = True) -> bool:
    """Does this XBRL fact row's end-date match the implied period?
    If pp is None and latest_ok, any row qualifies (will be sorted later)."""
    if pp is None:
        return latest_ok
    kind, year, mq = pp
    end = row.get("end") or ""
    if len(end) < 10:
        return False
    try:
        ey, em = int(end[:4]), int(end[5:7])
    except (ValueError, TypeError):
        return False
    if kind in ("FY", "FY-A", "FY-E"):
        # Flow fact: fp=FY, form=10-K, end in year.
        if row.get("fp") == "FY" and ey == year:
            return True
        # Balance-sheet fact at year-end:
        if ey == year and em == 12:
            return True
        return False
    if kind == "Q":
        want_em = {1: 3, 2: 6, 3: 9, 4: 12}.get(mq)
        return ey == year and em == want_em
    if kind == "H":
        want_em = 6 if mq == 1 else 12
        return ey == year and em == want_em
    if kind == "date" and mq:
        return ey == year and em == mq
    return False


# Known categories per XBRL unit: used to reject cross-category matches
# (don't match "$3M dividend" to an option exercise price just because
# the magnitudes line up).
XBRL_UNIT_CATEGORY = {
    "USD": "dollars",
    "USD/shares": "per_share",
    "shares": "shares",
    "pure": "ratio",   # usually decimals like 0.24 (tax rate)
}


def _claim_unit_category(claim: dict) -> str:
    """What kind of number did the user write?
    dollars_abs: $171 (bare dollar, no scale) in a sentence that is not EPS/dividend
    per_share: EPS-shaped $9.12 or "per share"
    dollars_scaled: $333M or $1.74B
    pct: percentage
    ratio: 2.0x
    """
    unit = claim.get("unit")
    sent = (claim.get("sentence") or "").lower()
    if unit == "USD_m":
        return "dollars_scaled"
    if unit == "USD":
        if claim.get("has_scale"):
            return "dollars_scaled"
        # Bare dollar: EPS/DPS context -> per_share; else dollars_abs
        if any(kw in sent for kw in ("eps", "earnings per share", "per share",
                                       "dividend per share", "dps")):
            return "per_share"
        return "dollars_abs"
    if unit == "pct":
        return "pct"
    if unit == "x":
        return "ratio"
    if unit == "per_share":
        return "per_share"
    return "other"


def scan_all_tags(claim: dict, companyfacts: dict,
                   tol_rel: float = 0.005
                   ) -> Optional[dict]:
    """Walk every tag + unit + fact in companyfacts and return the first
    row whose scaled value is within tol_rel of claim.value in a period
    consistent with claim.periods. Returns {tag, unit, period, end,
    value, fp, form, filed, scale} or None.

    Guard: unit-category compatibility. A dollars_scaled claim (USD_m)
    only compares against USD-unit tags whose magnitude is >=$1M to
    avoid matching per-share exercise prices.
    """
    if not companyfacts:
        return None
    gaap = (companyfacts.get("facts", {}) or {}).get("us-gaap", {})
    if not gaap:
        return None
    pp = _implied_period(claim)

    cat = _claim_unit_category(claim)
    raw = claim.get("raw", "")

    try:
        v_m = float(claim.get("value") or 0.0)
    except (TypeError, ValueError):
        return None
    if v_m == 0.0:
        return None

    # Build candidate (expected_unit_category, scaled_value) list.
    candidates: list[tuple[str, float]] = []
    if cat == "dollars_scaled":
        candidates.append(("dollars", v_m * 1_000_000.0))
    elif cat == "dollars_abs":
        m = re.match(r"\$?\s?(\d[\d,]*(?:\.\d+)?)", (raw or "").strip())
        try:
            recovered = float(m.group(1).replace(",", "")) if m else v_m
        except Exception:
            recovered = v_m
        # Could legitimately be a scaled dollar reported without M/B
        # (e.g. "$9.12 EPS"), or a real dollar amount if sentence says so.
        candidates.append(("per_share", recovered))
        # Also try as a shares count expressed in millions-of-shares
        candidates.append(("shares_m", recovered * 1_000_000.0))
    elif cat == "per_share":
        m = re.match(r"\$?\s?(\d[\d,]*(?:\.\d+)?)", (raw or "").strip())
        try:
            recovered = float(m.group(1).replace(",", "")) if m else v_m
        except Exception:
            recovered = v_m
        candidates.append(("per_share", recovered))
    elif cat == "pct":
        candidates.append(("ratio", v_m / 100.0))
        candidates.append(("pct_whole", v_m))
    elif cat == "ratio":
        candidates.append(("ratio", v_m))

    if not candidates:
        return None

    best: Optional[dict] = None
    best_dist = 10 ** 9

    # Minimum-magnitude guard: a dollars_scaled claim should not match
    # a tag whose value is tiny (per-share price) or vice versa.
    def _unit_compatible(unit_name: str, expected_cat: str,
                           filing_val: float) -> bool:
        if expected_cat == "dollars":
            return unit_name == "USD" and abs(filing_val) >= 1_000_000.0
        if expected_cat == "per_share":
            return unit_name in ("USD/shares", "USD") and abs(filing_val) < 1_000_000.0
        if expected_cat == "shares_m":
            return unit_name == "shares"
        if expected_cat == "ratio":
            return unit_name in ("pure", None)
        if expected_cat == "pct_whole":
            return False
        return True

    for tag, fact in gaap.items():
        units = fact.get("units") or {}
        for u_name, rows in units.items():
            if not rows:
                continue
            matching = [r for r in rows if _row_matches_period(r, pp)]
            if not matching:
                if pp is None:
                    matching = sorted(rows,
                                       key=lambda r: (r.get("end") or ""))[-3:]
                else:
                    continue
            for r in matching:
                val = r.get("val")
                if val is None:
                    continue
                try:
                    b = float(val)
                except (TypeError, ValueError):
                    continue
                for expected_cat, cv in candidates:
                    if not _unit_compatible(u_name, expected_cat, b):
                        continue
                    try:
                        a = float(cv)
                    except (TypeError, ValueError):
                        continue
                    if a == 0 and b == 0:
                        continue
                    denom = abs(b) if b != 0 else abs(a)
                    if denom == 0:
                        continue
                    dist = abs(a - b) / denom
                    if dist <= tol_rel and dist < best_dist:
                        best_dist = dist
                        best = {
                            "tag": tag,
                            "unit": u_name,
                            "period_end": r.get("end"),
                            "value": b,
                            "fp": r.get("fp"),
                            "form": r.get("form"),
                            "filed": r.get("filed"),
                            "scale_tried": expected_cat,
                            "distance": round(dist, 6),
                        }
    return best


# ----- 3c. Derived metrics -----

def derived_metrics(companyfacts: dict) -> dict:
    """Return a dict of derived series:
      derived[name][year] = {"val": float, "formula": str, "inputs": {...}}
    name is one of: eps_from_ni_shares, net_income_margin, debt_to_equity,
      net_debt, revenue_yoy, net_income_yoy
    """
    out: dict[str, dict[int, dict]] = {}
    if not companyfacts:
        return out
    gaap = (companyfacts.get("facts", {}) or {}).get("us-gaap", {})
    if not gaap:
        return out

    def _best_fy(tag_candidates: list[str]) -> dict[int, dict]:
        for tag in tag_candidates:
            fact = gaap.get(tag)
            if not fact:
                continue
            fy = _fy_series_strict(fact)
            if fy:
                return {r["fy"]: r for r in fy}
        return {}

    ni = _best_fy(["NetIncomeLoss"])
    shares = _best_fy(["WeightedAverageNumberOfDilutedSharesOutstanding"])
    rev = _best_fy(["Revenues",
                     "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "SalesRevenueNet"])
    eq = _best_fy(["StockholdersEquity"])
    cash = _best_fy(["CashAndCashEquivalentsAtCarryingValue"])
    ltd = _best_fy(["LongTermDebt", "LongTermDebtNoncurrent"])
    ltd_cur = _best_fy(["LongTermDebtCurrent"])

    out["eps_from_ni_shares"] = {}
    for yr, r in ni.items():
        s = shares.get(yr)
        if s and s.get("val"):
            v = r["val"] / s["val"]
            out["eps_from_ni_shares"][yr] = {
                "val": v,
                "formula": "NetIncomeLoss / WeightedAverageNumberOfDilutedSharesOutstanding",
                "inputs": {"net_income": r["val"],
                           "diluted_shares": s["val"]},
                "end": r.get("end"),
            }

    out["net_income_margin"] = {}
    for yr, r in ni.items():
        rv = rev.get(yr)
        if rv and rv.get("val"):
            v = r["val"] / rv["val"]
            out["net_income_margin"][yr] = {
                "val": v,
                "formula": "NetIncomeLoss / Revenues",
                "inputs": {"net_income": r["val"], "revenue": rv["val"]},
                "end": r.get("end"),
            }

    out["debt_to_equity"] = {}
    for yr, e in eq.items():
        d_nc = ltd.get(yr, {}).get("val") or 0
        d_cu = ltd_cur.get(yr, {}).get("val") or 0
        tot = (d_nc or 0) + (d_cu or 0)
        if e.get("val"):
            out["debt_to_equity"][yr] = {
                "val": tot / e["val"],
                "formula": "(LongTermDebt + LongTermDebtCurrent) / StockholdersEquity",
                "inputs": {"debt_nc": d_nc, "debt_cur": d_cu,
                           "equity": e["val"]},
                "end": e.get("end"),
            }

    out["net_debt"] = {}
    for yr in sorted(set(list(ltd.keys()) + list(cash.keys()))):
        d_nc = ltd.get(yr, {}).get("val") or 0
        d_cu = ltd_cur.get(yr, {}).get("val") or 0
        c = cash.get(yr, {}).get("val") or 0
        tot = (d_nc or 0) + (d_cu or 0)
        out["net_debt"][yr] = {
            "val": tot - c,
            "formula": "LongTermDebt + LongTermDebtCurrent - CashAndCashEquivalents",
            "inputs": {"debt_nc": d_nc, "debt_cur": d_cu, "cash": c},
            "end": (ltd.get(yr) or cash.get(yr) or {}).get("end"),
        }

    def _yoy(series: dict[int, dict], label: str) -> dict[int, dict]:
        out_s: dict[int, dict] = {}
        years = sorted(series.keys())
        for i, yr in enumerate(years):
            prev = years[i - 1] if i > 0 else None
            if prev is None:
                continue
            pv = series[prev].get("val")
            cv = series[yr].get("val")
            if pv and cv and pv != 0:
                out_s[yr] = {
                    "val": (cv - pv) / abs(pv),
                    "formula": f"({label}[{yr}] - {label}[{prev}]) / {label}[{prev}]",
                    "inputs": {"prev": pv, "current": cv},
                    "end": series[yr].get("end"),
                }
        return out_s

    out["revenue_yoy"] = _yoy(rev, "Revenues")
    out["net_income_yoy"] = _yoy(ni, "NetIncomeLoss")

    return out


def _fy_series_strict(fact: dict) -> list[dict]:
    """FY entries only, keyed by end-year, latest filed wins."""
    units = fact.get("units") or {}
    rows: list[dict] = []
    for u, rs in units.items():
        for r in rs:
            if r.get("fp") == "FY" and r.get("form") == "10-K":
                rows.append(r)
    by_year: dict[int, dict] = {}
    for r in rows:
        end = r.get("end") or ""
        if len(end) < 4:
            continue
        try:
            yr = int(end[:4])
        except ValueError:
            continue
        prev = by_year.get(yr)
        if prev is None or (r.get("filed") or "") > (prev.get("filed") or ""):
            by_year[yr] = r
    out = []
    for yr in sorted(by_year.keys()):
        r = by_year[yr]
        out.append({"fy": yr, "end": r.get("end"),
                    "val": r.get("val"), "filed": r.get("filed")})
    return out


def match_derived(claim: dict, derived: dict,
                   tol_rel: float = 0.02
                   ) -> Optional[dict]:
    """Match a claim against the derived series. Returns {metric, year,
    val, formula, inputs} or None."""
    try:
        v_m = float(claim.get("value") or 0.0)
    except (TypeError, ValueError):
        return None
    if v_m == 0.0:
        return None
    pp = _implied_period(claim)
    unit = claim.get("unit")
    sentence = (claim.get("sentence") or "").lower()

    # Build candidate (metric-name, scaler to compare against derived val)
    cands: list[tuple[str, float]] = []
    if unit == "pct":
        cands.append(("net_income_margin", v_m / 100.0))
        cands.append(("revenue_yoy", v_m / 100.0))
        cands.append(("net_income_yoy", v_m / 100.0))
    elif unit == "USD" and not claim.get("has_scale"):
        m = re.match(r"\$?\s?(\d[\d,]*(?:\.\d+)?)", (claim.get("raw") or "").strip())
        try:
            dollars = float(m.group(1).replace(",", "")) if m else v_m
        except Exception:
            dollars = v_m
        if "eps" in sentence or "per share" in sentence or "diluted" in sentence:
            cands.append(("eps_from_ni_shares", dollars))
    elif unit == "USD_m":
        # net debt comparisons in dollar millions
        cands.append(("net_debt", v_m * 1_000_000.0))
    elif unit == "x":
        cands.append(("debt_to_equity", v_m))

    best: Optional[dict] = None
    best_dist = 10 ** 9
    for metric_name, cv in cands:
        series = derived.get(metric_name) or {}
        for yr, row in series.items():
            if pp:
                _, want_yr, _ = pp
                if yr != want_yr:
                    continue
            dv = row.get("val")
            if dv is None:
                continue
            denom = abs(dv) if dv != 0 else abs(cv)
            if denom == 0:
                continue
            dist = abs(cv - dv) / denom
            if dist <= tol_rel and dist < best_dist:
                best_dist = dist
                best = {
                    "metric": metric_name,
                    "year": yr,
                    "val": dv,
                    "formula": row.get("formula"),
                    "inputs": row.get("inputs"),
                    "end": row.get("end"),
                    "distance": round(dist, 6),
                }
    return best


# ----- 3c-bis. Text claims (rating tokens, covenants, fleet counts) -----

# Credit-rating token mentioned in the doc.
DOC_RATING_RE = re.compile(
    r"\b(Baa[123]|Aa[a1-3]|BBB[+-]?|BB[+-]?|AA[+-]?|AAA|CCC[+-]?)\b"
)

# Covenant with ratio: "FCCR min 1.2x; actual 2.0x" / "fixed charge coverage ratio of 2.0x".
DOC_COVENANT_RE = re.compile(
    r"(FCCR|fixed charge coverage ratio|asset coverage ratio|"
    r"recourse leverage|lien\s+(?:cap|limit)|secured debt cap)"
    r"[\s\S]{0,80}?(\d+(?:\.\d+)?)\s?(x|times|%|B|billion|M|million)?",
    re.IGNORECASE,
)

# Fleet count: "about 278,000 railcars" / "~156,000 cars" / "107,625 cars"
DOC_FLEET_RE = re.compile(
    r"(?:about|approximately|~)?\s?(\d{1,3}(?:,\d{3})+)\s"
    r"(railcars|cars|locomotives|engines|wholly owned cars|owned cars|"
    r"consolidated cars|railcars worldwide|cars worldwide)",
    re.IGNORECASE,
)


def extract_text_claims(pages: list[dict]) -> list[dict]:
    """Pull non-numeric or hard-to-parse claim tokens from the doc:
    credit ratings, covenant+ratio pairs, fleet counts."""
    out = []
    for pg in pages:
        text = pg["text"] or ""
        if not text:
            continue
        for m in DOC_RATING_RE.finditer(text):
            s, e = m.span()
            sent = _sentence_window(text, s, e)
            out.append({
                "locator": pg["locator"],
                "class": "text_rating",
                "raw": m.group(1),
                "sentence": sent,
                "estimate": is_model_or_adjusted(sent),
            })
        for m in DOC_COVENANT_RE.finditer(text):
            s, e = m.span()
            sent = _sentence_window(text, s, e)
            out.append({
                "locator": pg["locator"],
                "class": "text_covenant",
                "raw": m.group(0).strip(),
                "covenant_name": m.group(1),
                "covenant_value": m.group(2),
                "covenant_unit": m.group(3) or "",
                "sentence": sent,
                "estimate": is_model_or_adjusted(sent),
            })
        for m in DOC_FLEET_RE.finditer(text):
            s, e = m.span()
            sent = _sentence_window(text, s, e)
            # exclude purely narrative numbers inside historical context
            n = int(m.group(1).replace(",", ""))
            if n < 1_000 or n > 1_500_000:
                continue
            out.append({
                "locator": pg["locator"],
                "class": "text_fleet",
                "raw": m.group(0).strip(),
                "fleet_count": n,
                "fleet_noun": m.group(2),
                "sentence": sent,
                "estimate": is_model_or_adjusted(sent),
            })
    return out


def match_text_claim(claim: dict, filing_text_10k: str,
                     filing_text_10q: str) -> Optional[dict]:
    """Match a text claim (rating/covenant/fleet) against filing text."""
    cls = claim.get("class")
    if cls == "text_rating":
        want = claim["raw"]
        for text, label in ((filing_text_10q, "latest 10-Q"),
                             (filing_text_10k, "latest 10-K")):
            if not text:
                continue
            ratings = {m.group(1).lower() for m in DOC_RATING_RE.finditer(text)}
            if want.lower() in ratings:
                sent = _find_sentence_with(text, want) or ""
                return {
                    "status": "MATCH",
                    "basis": "credit_rating_text",
                    "claim_text": want,
                    "filing_sentence": sent[:400],
                    "filing_label": label,
                }
            # Same prefix family (Baa) but different rating -> MISMATCH
            if want.lower().startswith("baa"):
                others = [r for r in ratings if r.startswith("baa")]
                if others:
                    other = sorted(others)[0]
                    sent = _find_sentence_with(text, other) or ""
                    return {
                        "status": "MISMATCH",
                        "basis": "credit_rating_text",
                        "claim_text": want,
                        "filing_text_value": other,
                        "filing_sentence": sent[:400],
                        "filing_label": label,
                    }
            if want.upper() in ("BBB+", "BBB-", "BBB"):
                others = [r for r in ratings if r.lower().startswith("bbb")]
                if others:
                    other = sorted(others)[0]
                    if other.lower() != want.lower():
                        sent = _find_sentence_with(text, other) or ""
                        return {
                            "status": "MISMATCH",
                            "basis": "credit_rating_text",
                            "claim_text": want,
                            "filing_text_value": other,
                            "filing_sentence": sent[:400],
                            "filing_label": label,
                        }
        return {
            "status": "NOT_IN_FILINGS",
            "basis": "credit_rating_text",
            "reason": f"rating {want} not found in latest 10-K or 10-Q text",
        }
    if cls == "text_fleet":
        n = claim["fleet_count"]
        for text, label in ((filing_text_10q, "latest 10-Q"),
                             (filing_text_10k, "latest 10-K")):
            if not text:
                continue
            # Allow +/- 1% tolerance for rounded fleet counts
            for m in re.finditer(r"(\d{1,3}(?:,\d{3})+)\s"
                                 r"(?:railcars|cars|locomotives|engines)",
                                 text, re.IGNORECASE):
                try:
                    fn = int(m.group(1).replace(",", ""))
                except ValueError:
                    continue
                if fn == 0:
                    continue
                if abs(fn - n) / fn <= 0.02:
                    s = _find_sentence_near(text, m.start())
                    return {
                        "status": "MATCH",
                        "basis": "fleet_count_text",
                        "claim_text": f"{n:,}",
                        "filing_text_value": f"{fn:,}",
                        "filing_sentence": s[:400],
                        "filing_label": label,
                    }
        return {
            "status": "NOT_IN_FILINGS",
            "basis": "fleet_count_text",
            "reason": f"fleet count {n:,} not found within 2% in latest filings",
        }
    if cls == "text_covenant":
        want_name = (claim.get("covenant_name") or "").lower()
        want_val = claim.get("covenant_value")
        for text, label in ((filing_text_10q, "latest 10-Q"),
                             (filing_text_10k, "latest 10-K")):
            if not text:
                continue
            for m in DOC_COVENANT_RE.finditer(text):
                if (m.group(1) or "").lower().startswith(want_name[:5]):
                    fv = m.group(2)
                    s = _find_sentence_near(text, m.start())
                    try:
                        if abs(float(fv) - float(want_val)) / max(float(fv), 0.01) <= 0.05:
                            return {
                                "status": "MATCH",
                                "basis": "covenant_text",
                                "claim_text": f"{claim['covenant_name']} {want_val}",
                                "filing_text_value": f"{m.group(1)} {fv}",
                                "filing_sentence": s[:400],
                                "filing_label": label,
                            }
                    except (TypeError, ValueError):
                        pass
                    return {
                        "status": "MISMATCH",
                        "basis": "covenant_text",
                        "claim_text": f"{claim['covenant_name']} {want_val}",
                        "filing_text_value": f"{m.group(1)} {fv}",
                        "filing_sentence": s[:400],
                        "filing_label": label,
                    }
        return {
            "status": "NOT_IN_FILINGS",
            "basis": "covenant_text",
            "reason": f"covenant {claim.get('covenant_name')} not found in latest filings",
        }
    return None


# ----- 3d. Text-fact scanner -----

RATING_RE = re.compile(
    r"\b(Baa[123]|Ba[a1-3]|BBB[+-]?|AAA|Aa[123]|CCC[+-]?)\b",
    re.IGNORECASE,
)

# Only treat the sentence as a credit-rating sentence if an agency keyword
# or outlook word is nearby (prevents "P/B" or "AA batteries" matches).
RATING_CONTEXT_RE = re.compile(
    r"\b(Moody'?s|S&P|Standard\s+&\s+Poor|Fitch|credit rating|rating[s]?|"
    r"outlook|stable|positive|negative)\b",
    re.IGNORECASE,
)

COVENANT_RATIO_RE = re.compile(
    r"(fixed charge coverage ratio|FCCR|asset coverage|recourse leverage|"
    r"lien\s+(?:cap|limit)|secured debt cap|debt[\s-]+to[\s-]+equity)"
    r"[\s\S]{0,120}?(\$?\d+(?:\.\d+)?(?:[xX%]|\s?billion|\s?B|\s?million|\s?M)?)",
    re.IGNORECASE,
)

LPI_HISTORY_RE = re.compile(
    r"(?:LPI|lease price index|renewal (?:rate|price|lease rate)|"
    r"renewal rate change)[\s\S]{0,160}?"
    r"((?:negative\s+)?(?:[-+−]?\d{1,3}(?:\.\d+)?)\s?%|"
    r"\(\s?\d{1,3}(?:\.\d+)?\s?%\s?\))",
    re.IGNORECASE,
)


def scan_text_facts(claim: dict,
                     filing_text: str,
                     filing_label: str,
                     ticker: str) -> Optional[dict]:
    """Try to anchor the claim to a sentence in the filing text.
    Handles credit ratings, covenant ratios, LPI history, fleet counts,
    bridge figures.

    Returns {sentence, filing_label, text_match, status, basis}.
    status in MATCH / MISMATCH; basis = text category.
    """
    if not filing_text:
        return None
    sent = (claim.get("sentence") or "").strip()
    raw = (claim.get("raw") or "").strip()
    if not sent or not raw:
        return None

    # Ratings: user wrote "Baa1" or "Baa2 (positive)"; find the same label
    # in filing text. Only fire if the claim sentence has a rating-context
    # token (Moody's / S&P / Fitch / "credit rating" / outlook word).
    rating_in_claim = RATING_RE.search(sent)
    if rating_in_claim and RATING_CONTEXT_RE.search(sent):
        want = rating_in_claim.group(1)
        ratings_in_filing = {m.group(1).lower() for m in RATING_RE.finditer(filing_text)}
        if want.lower() in ratings_in_filing:
            found_sent = _find_sentence_with(filing_text, want)
            return {
                "status": "MATCH",
                "basis": "credit_rating",
                "claim_text": want,
                "filing_sentence": (found_sent or "")[:400],
                "filing_label": filing_label,
            }
        # If user says Baa2 but filing has Baa1 (for Moody's), MISMATCH
        if "baa" in want.lower() and any("baa" in r for r in ratings_in_filing):
            found = next((r for r in ratings_in_filing if "baa" in r), None)
            found_sent = _find_sentence_with(filing_text, found) if found else ""
            return {
                "status": "MISMATCH",
                "basis": "credit_rating",
                "claim_text": want,
                "filing_text": found or "",
                "filing_sentence": (found_sent or "")[:400],
                "filing_label": filing_label,
            }

    # LPI trough references: user says "-24% LPI trough" or "-28.2% trough"
    if re.search(r"\btrough\b|LPI|lease price index", sent, re.IGNORECASE):
        lpi_match = re.search(r"[-−]\s?(\d{1,3}(?:\.\d+)?)\s?%", raw)
        if lpi_match:
            try:
                claim_v = -float(lpi_match.group(1))
            except ValueError:
                claim_v = None
            if claim_v is not None:
                # Find all sentences in filing_text that mention LPI / lease
                # price index / renewal rate; within those sentences pull
                # every percent value.
                # Build (percent_value, nearby_year, sentence) triples by
                # extracting pairs of percent and year token that occur
                # within ~40 chars of each other in LPI sentences. This
                # lets us tie "negative 28.2% in 2017" to year=2017 and
                # distinguish from "negative 23.5% in 2020" in the same
                # paragraph.
                found_vals: list[tuple[float, Optional[str], str]] = []
                raw_sents = re.split(r"\.\s+|\.\n|\n|;\s", filing_text)
                lpi_sentences = [s for s in raw_sents
                                 if re.search(
                                     r"LPI|lease price index|renewal rate",
                                     s, re.IGNORECASE)]
                PCT_INLINE = re.compile(
                    r"(negative\s+|\()?\s?([-+−]?\d{1,3}(?:\.\d+)?)\s?%")
                YEAR_RE = re.compile(r"\b(20\d{2}|19\d{2})\b")
                for ls in lpi_sentences:
                    # Infer the sentence's "subject year" from phrases like
                    # "During YYYY" or "In YYYY, the renewal rate change".
                    subj_year = None
                    subj_m = re.search(
                        r"(?:During|In)\s+(\d{4})[,\s].*?"
                        r"(?:renewal rate|LPI|lease price)",
                        ls, re.IGNORECASE)
                    if subj_m:
                        subj_year = subj_m.group(1)
                    for pm in PCT_INLINE.finditer(ls):
                        prefix = (pm.group(1) or "").lower()
                        try:
                            v = float(pm.group(2).replace("−", "-"))
                        except ValueError:
                            continue
                        if "negative" in prefix or "(" in prefix:
                            v = -abs(v)
                        tail = ls[pm.end():pm.end()+40]
                        # Priority: "in YYYY" immediately after the pct wins
                        # (that explicitly ties the value to a year); else
                        # fall back to the sentence's subject year.
                        year_tok = subj_year
                        in_year_m = re.match(r"\s*(?:in|of)\s+(\d{4})",
                                              tail, re.IGNORECASE)
                        if in_year_m:
                            year_tok = in_year_m.group(1)
                        found_vals.append((v, year_tok, ls.strip()[:400]))
                # Prefer filing values tagged with the same year as the
                # user's sentence. The user's sentence may contain multiple
                # years (e.g. "Break-even LPI in 2027 is -29%; the 2017
                # trough was -24%"); pick the year closest to the raw
                # percent token.
                year_in_claim = None
                raw_pos = sent.find(raw)
                best_dist = 10**9
                for ym in re.finditer(r"\b(20\d{2}|19\d{2})\b", sent):
                    y = ym.group(1)
                    if raw_pos < 0:
                        year_in_claim = y
                        break
                    dist = min(abs(ym.start() - raw_pos),
                                abs(ym.end() - raw_pos))
                    if dist < best_dist:
                        best_dist = dist
                        year_in_claim = y
                troughish = [p for p in found_vals if p[0] < -15]
                prioritized: list[tuple[float, Optional[str], str]] = []
                if year_in_claim:
                    for v, yr, s_ in (troughish or found_vals):
                        if yr == year_in_claim:
                            prioritized.append((v, yr, s_))
                if not prioritized:
                    prioritized = troughish or found_vals
                for v, yr, s_ in prioritized:
                    if abs(v - claim_v) <= 0.5:
                        return {
                            "status": "MATCH",
                            "basis": "lpi_value",
                            "claim_text": f"{claim_v}%",
                            "filing_text": f"{v}% (year {yr})" if yr else f"{v}%",
                            "filing_sentence": s_[:400],
                            "filing_label": filing_label,
                        }
                # MISMATCH: filing has a DIFFERENT trough value for the SAME year
                for v, yr, s_ in prioritized:
                    if abs(v - claim_v) > 0.5 and v < -15:
                        return {
                            "status": "MISMATCH",
                            "basis": "lpi_value",
                            "claim_text": f"{claim_v}% ({year_in_claim or 'trough'})",
                            "filing_text": f"{v}% (year {yr})" if yr else f"{v}%",
                            "filing_sentence": s_[:400],
                            "filing_label": filing_label,
                        }

    return None


def _find_sentence_with(text: str, needle: str) -> Optional[str]:
    for m in re.finditer(re.escape(needle), text, re.IGNORECASE):
        start = m.start()
        return _find_sentence_near(text, start)
    return None


def _find_sentence_near(text: str, pos: int) -> str:
    left_cut = max(text.rfind(". ", 0, pos),
                    text.rfind(".\n", 0, pos),
                    text.rfind("\n", 0, pos))
    if left_cut < 0:
        left_cut = max(0, pos - 150)
    else:
        left_cut += 1
    right_candidates = [p for p in (text.find(". ", pos),
                                      text.find(".\n", pos),
                                      text.find("\n", pos)) if p >= 0]
    right = min(right_candidates) if right_candidates \
        else min(len(text), pos + 150)
    out = text[left_cut:right].strip()
    out = re.sub(r"\s+", " ", out)
    return out


# ----- 3e. Entry point used by server.py -----

def classify_claim_v3(claim: dict,
                       companyfacts: dict,
                       derived: dict,
                       filing_text_10k: str,
                       filing_text_10q: str,
                       ticker: str) -> dict:
    """Return a status row with the richest hit we can find.
    status is MATCH / MISMATCH / MODEL_OR_ADJUSTED / NOT_IN_FILINGS."""
    # 1. Model/adjusted guard — never MISMATCH.
    sent = claim.get("sentence") or claim.get("context") or ""
    if is_model_or_adjusted(sent):
        return {
            "status": "MODEL_OR_ADJUSTED",
            "basis": "model_or_adjusted guard",
            "reason": "sentence contains adjusted/normalized/model/consensus"
                       "/target/estimate marker; not a filing line item",
        }

    # 2. Number-first full-tag scan.
    hit = scan_all_tags(claim, companyfacts, tol_rel=0.005)
    if hit:
        return {
            "status": "MATCH",
            "basis": "xbrl_full_scan",
            "xbrl_tag": hit["tag"],
            "xbrl_unit": hit["unit"],
            "filing_period_end": hit["period_end"],
            "filing_fp": hit["fp"],
            "filing_form": hit["form"],
            "filing_filed": hit["filed"],
            "filing_value": hit["value"],
            "distance_rel": hit["distance"],
            "scale_tried": hit["scale_tried"],
        }

    # 3. Derived metrics.
    d_hit = match_derived(claim, derived, tol_rel=0.02)
    if d_hit:
        return {
            "status": "MATCH",
            "basis": "derived_metric",
            "metric": d_hit["metric"],
            "year": d_hit["year"],
            "formula": d_hit["formula"],
            "inputs": d_hit["inputs"],
            "filing_value": d_hit["val"],
            "distance_rel": d_hit["distance"],
            "filing_period_end": d_hit.get("end"),
        }

    # 4. Text-fact scanner (credit ratings, LPI).
    for text, label in ((filing_text_10q, "latest 10-Q"),
                         (filing_text_10k, "latest 10-K")):
        t_hit = scan_text_facts(claim, text, label, ticker)
        if t_hit:
            return t_hit

    # 5. Legacy mapped-line-item path (so existing MATCH behavior is kept
    # when the number-first scan fell through due to period gaps).
    legacy = match_claim_to_filings(claim, _xbrl_series_from_facts(companyfacts))
    if legacy.get("status") in ("MATCH", "MISMATCH"):
        legacy["basis"] = legacy.get("basis") or "xbrl_mapped_line_item"
        return legacy

    return {
        "status": "NOT_IN_FILINGS",
        "basis": "unverified",
        "reason": "no matching XBRL tag within 0.5%, no derived metric "
                   "within 2%, and no text-fact match",
    }


def _xbrl_series_from_facts(companyfacts: Optional[dict]) -> dict:
    """Compact {metric: {fy, q, tag}} shape used by the legacy matcher."""
    out: dict[str, dict] = {}
    if not companyfacts:
        return out
    gaap = (companyfacts.get("facts", {}) or {}).get("us-gaap", {})
    XBRL_TAGS = {
        "revenue": ["Revenues",
                     "RevenueFromContractWithCustomerExcludingAssessedTax",
                     "SalesRevenueNet"],
        "net_income": ["NetIncomeLoss"],
        "eps_diluted": ["EarningsPerShareDiluted"],
        "shares_diluted": ["WeightedAverageNumberOfDilutedSharesOutstanding"],
        "equity": ["StockholdersEquity"],
        "cash": ["CashAndCashEquivalentsAtCarryingValue"],
        "debt": ["LongTermDebt"],
        "cfo": ["NetCashProvidedByUsedInOperatingActivities"],
        "capex": ["PaymentsToAcquirePropertyPlantAndEquipment"],
        "dps": ["CommonStockDividendsPerShareDeclared"],
        "interest_expense": ["InterestExpense"],
    }
    for metric, tag_list in XBRL_TAGS.items():
        for tag in tag_list:
            fact = gaap.get(tag)
            if not fact:
                continue
            series = _fy_series_strict(fact)
            # Convert to the shape match_claim_to_filings expects
            fy = [{"fy": r["fy"], "end": r["end"], "val": r["val"]}
                   for r in series]
            out[metric] = {"fy": fy, "q": [], "tag": tag}
            break
    return out
