"""Management tone: positive and negative words in an earnings release or call, counted with a word list kept here
in the open (BMG's own list, written for this reader; it is not a licensed dictionary).

    score = (positive - negative) / (positive + negative) x 100, from -100 (all negative) to +100 (all positive)

A word preceded within three words by a negation ("not", "no", "never", "without", "n't") counts the other way.
Safe-harbor and forward-looking-statement boilerplate is left out, and so are tables (rows of numbers). For a call,
the prepared remarks and the questions and answers are scored apart. Each counted sentence is kept with its page so
the score can be checked line by line. The score is a reading aid, not a measure of results.
"""
from __future__ import annotations

import bisect
import re
from typing import Optional

POSITIVE = frozenset("""
strong stronger strongest strength strengthened growth grew grow growing record records improved improve improvement improvements
improving exceeded exceed exceeding momentum confident confidence robust accelerate accelerated accelerating acceleration outperform
outperformed gain gains expand expanded expanding expansion favorable healthy solid upside beat raised raise raising tailwind tailwinds
opportunity opportunities pleased excellent success successful successfully benefit benefited benefiting positive attractive resilient
resilience outstanding efficient efficiencies profitable profitability encouraged encouraging optimistic achieved achieve progress
""".split())
NEGATIVE = frozenset("""
decline declined declines declining decrease decreased decreases decreasing weak weaker weakness weakening soft softer softness headwind
headwinds pressure pressures pressured challenging challenge challenges challenged difficult difficulty loss losses impairment impairments
uncertainty uncertainties uncertain delay delays delayed slowdown slower slowing shortfall adverse adversely miss missed cut cuts reduced
reduction downturn volatility volatile disruption disruptions disrupted concern concerns negative negatively unfavorable deteriorate
deteriorated deterioration restructuring layoffs inflationary disappointing disappointed lawsuit litigation recall shortage shortages
""".split())
NEGATION = re.compile(r"^(?:not|no|never|without|neither|nor|cannot|hardly)$|n't$")
BOILER = re.compile(r"(?i)forward[- ]looking statements?|safe harbor|private securities litigation reform act|risk factors? (?:described|discussed|included)"
                    r"|undue reliance|actual results (?:could|may) differ|undertakes? no obligation|risks and uncertainties include|without limitation")
TABLE_ROW = re.compile(r"\(?\$?[\d,]{3,}(?:\.\d+)?\)?%?\s+\(?\$?[\d,]{3,}(?:\.\d+)?\)?%?")
WORD = re.compile(r"[A-Za-z][A-Za-z']*")
SENT = re.compile(r"[^.!?\n]+[.!?]?")


def _page(page_starts: list, off: int) -> Optional[int]:
    starts = [o for o, _ in page_starts]
    k = bisect.bisect_right(starts, off) - 1
    return page_starts[k][1] if k >= 0 else None


_FLS_HEAD = re.compile(r"(?im)^[ \t]*(?:cautionary (?:statement|note|language)[^\n]{0,60}|forward[- ]looking (?:statements?|information)[^\n]{0,40}|safe harbor[^\n]{0,40})$")
_NEXT_HEAD = re.compile(r"(?m)^[ \t]*[A-Z][A-Z0-9 ,.&'()\-]{14,}$")


def boiler_spans(text: str) -> list:
    """The forward-looking-statements section (its heading to the next all-capitals heading, at most 12,000
    characters): a list of risks that is not management's tone about the period."""
    out = []
    for m in _FLS_HEAD.finditer(text):
        nxt = _NEXT_HEAD.search(text, m.end() + 1)
        end = min(nxt.start() if nxt else len(text), m.end() + 12000)
        out.append((m.start(), end))
    return out


def score_text(text: str, page_starts: list, base: int = 0, keep: int = 6) -> dict:
    """Counts, score and the sentences that carry the most tone, for one stretch of text starting at `base`."""
    pos = neg = 0
    sents = []
    skip = boiler_spans(text)
    for m in SENT.finditer(text):
        if any(a <= m.start() < b for a, b in skip):
            continue
        s = m.group(0).strip()
        if len(s) < 30 or BOILER.search(s) or len(re.findall(r"\d", s)) > len(s) * 0.25 or TABLE_ROW.search(s):
            continue                         # boilerplate, or a table row of numbers
        words = [w.lower() for w in WORD.findall(s)]
        p = n = 0
        hits = []
        for i, w in enumerate(words):
            sign = 1 if w in POSITIVE else -1 if w in NEGATIVE else 0
            if not sign:
                continue
            if any(NEGATION.search(x) for x in words[max(0, i - 3):i]):
                sign = -sign
            if sign > 0:
                p += 1
            else:
                n += 1
            hits.append([w, sign])
        if p or n:
            pos += p
            neg += n
            off = base + m.start() + (len(m.group(0)) - len(m.group(0).lstrip()))
            sents.append({"text": s[:400], "pos": p, "neg": n, "words": hits, "offset": off, "page": _page(page_starts, off)})
    total = pos + neg
    out = {"positive": pos, "negative": neg, "score": round((pos - neg) / total * 100) if total else None, "sentences_counted": len(sents)}
    out["most_positive"] = sorted((x for x in sents if x["pos"] > x["neg"]), key=lambda x: -(x["pos"] - x["neg"]))[:keep]
    out["most_negative"] = sorted((x for x in sents if x["neg"] > x["pos"]), key=lambda x: -(x["neg"] - x["pos"]))[:keep]
    return out


def score(text: str, page_starts: list, kind: str = "release", qa_offset: Optional[int] = None) -> dict:
    """The whole document, and for a call its prepared remarks and Q&A apart."""
    out = {"method": "BMG word list (positive and negative words, negation within three words); score = (positive - negative) / (positive + negative) x 100.",
           "whole": score_text(text, page_starts)}
    if kind == "transcript" and qa_offset and 0 < qa_offset < len(text):
        out["prepared"] = score_text(text[:qa_offset], page_starts, 0)
        out["qa"] = score_text(text[qa_offset:], page_starts, qa_offset)
    return out


def compare(now: dict, before: dict) -> dict:
    a, b = (now.get("whole") or {}).get("score"), (before.get("whole") or {}).get("score")
    if a is None or b is None:
        return {"change": None}
    d = a - b
    return {"change": d, "reading": "more positive" if d > 5 else "more negative" if d < -5 else "about the same"}


def word_lists() -> dict:
    return {"positive": sorted(POSITIVE), "negative": sorted(NEGATIVE)}
