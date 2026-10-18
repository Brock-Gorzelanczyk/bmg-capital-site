"""Guidance history: the guidance lines of a company's recent earnings releases, lined up by what they guide and
the period they guide, oldest release first, each line quoted as printed with its page and filing.

Grouping is by fixed rules: the measure (EPS, revenue, margin, operating or pre-tax income, buybacks, capital
spending, cash flow, tax rate, dividend, expenses, or other) and the period named in the sentence (a fiscal or calendar
year, or a quarter, in the release's own words: "June quarter" stays "June quarter"). Lines that name neither
stay under "Other". Nothing is turned into a number.
"""
from __future__ import annotations

import re

TOPICS = [
    ("eps", "Earnings per share", re.compile(r"\b(?:EPS|earnings[- ]per[- ](?:diluted )?share|per diluted share|diluted earnings)(?:\d{1,2})?\b", re.I)),
    ("revenue", "Revenue", re.compile(r"\b(?:revenues?|net sales|sales)\b", re.I)),
    ("margin", "Margin", re.compile(r"\bmargins?\b", re.I)),
    ("operating_income", "Operating income", re.compile(r"\b(?:operating income|operating profit)\b", re.I)),
    ("pretax", "Pre-tax income", re.compile(r"\bpre-?tax (?:income|profit|earnings)\b", re.I)),          # Delta guides its pre-tax profit
    ("buybacks", "Share repurchases", re.compile(r"\b(?:share repurchases?|repurchas\w+ (?:of )?(?:shares|stock)|buybacks?)\b", re.I)),
    ("capex", "Capital spending and investment", re.compile(r"\b(?:capital expenditures?|capex|capital spending|investment volume|portfolio investments?)\b", re.I)),
    ("cash_flow", "Cash flow", re.compile(r"\b(?:free cash flow|operating cash flow|cash flow from operations)\b", re.I)),
    ("tax", "Tax rate", re.compile(r"\btax rate\b", re.I)),
    ("dividend", "Dividend", re.compile(r"\bdividends?\b", re.I)),
    ("expense", "Expenses", re.compile(r"\b(?:expenses?|costs?)\b", re.I)),
]
_PERIOD = re.compile(r"(?i)\b(?:full[- ]year|fiscal(?: year)?|FY)\s*['’]?(?:20)?(\d\d)\b|\b(first|second|third|fourth)[ \-\u2010\u2011]quarter(?: of)?(?: (?:fiscal (?:year )?|FY ?)?(20\d\d))?|\b(20\d\d)\b|\b(march|june|september|december)[ \-]quarter(?: (?:of )?(?:fiscal )?(20\d\d))?")


def topic_of(text: str) -> tuple[str, str]:
    for key, label, rx in TOPICS:
        if rx.search(text):
            return key, label
    return "other", "Other guidance"


_QN = re.compile(r"\bQ([1-4])\b(?:[ \t]*(?:FY|fiscal)?[ \t]*['’]?(20\d\d|\d\d)\b)?|\b([1-4])Q(?:[ \t]*['’]?(20\d\d|\d\d))?\b")   # Q2 2026, 2Q 2026, 3Q26
_BASE = re.compile(r"(?i)\b(?:from|versus|vs\.?|compared (?:with|to)|over|than)\b(?:[^.;]|\.(?=\d)){0,40}$")     # "... from $14.23 in fiscal 2025": the base year


_DATED = re.compile(r"\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?[ \t]+\d{1,2},[ \t]*$")


def period_of(text: str, skip_base: bool = False) -> str:
    """The period a guidance line names; with skip_base, a year it grows from ("from $14.23 in fiscal 2025") is not it."""
    for m in sorted([*_PERIOD.finditer(text), *_QN.finditer(text)], key=lambda m: m.start()):
        if skip_base and _BASE.search(text[:m.start()]):
            continue
        if m.re is _PERIOD and m.group(4) and _DATED.search(text[:m.start()]):
            continue
        if m.re is _QN:
            y = m.group(2) or m.group(4) or ""
            return f"{('First', 'Second', 'Third', 'Fourth')[int(m.group(1) or m.group(3)) - 1]} quarter{(' ' + ('20' + y if len(y) == 2 else y)) if y else ''}"
        if m.group(1):
            return f"FY{'20' + m.group(1) if len(m.group(1)) == 2 else m.group(1)}"
        if m.group(2):
            return f"{m.group(2).capitalize()} quarter{(' ' + m.group(3)) if m.group(3) else ''}"
        if m.group(5):                  # "the June quarter": its own name, not a guess at its fiscal number
            return f"{m.group(5).capitalize()} quarter{(' ' + m.group(6)) if m.group(6) else ''}"
        return m.group(4)
    return ""


def align(releases: list) -> list:
    """releases: [{"accession", "filed", "items": [guidance dicts]}] oldest first. Rows keyed by (topic, period)."""
    rows: dict = {}
    for rel in releases:
        for g in rel.get("items") or []:
            key, label = topic_of(g["text"])
            per = period_of(g["text"], skip_base=bool(g.get("period"))) or period_of(g.get("period") or "")   # else the period its list's heading names
            rk = f"{key}|{per}"
            row = rows.setdefault(rk, {"topic": key, "label": label + (f", {per}" if per else ""), "period": per, "cells": {}})
            row["cells"].setdefault(rel["accession"], []).append({"text": g["text"], "direction": g.get("direction") or "stated",
                                                                  "numbers": g.get("numbers") or [], "page": g.get("pdf_page") or g.get("page"),
                                                                  "offset": g.get("offset"), "table": g.get("table")})
    order = {k: i for i, (k, _, _) in enumerate(TOPICS)}
    return sorted(rows.values(), key=lambda r: (order.get(r["topic"], 99), r["period"] or "~", r["label"]))
