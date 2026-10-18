"""What a document says beyond its statements, each item quoted with its page.

    build(doc, rep, title) -> dict      # stored with the read result as "insights"

* kind: annual report, quarterly report, earnings release, call transcript, insider
  trades or fund holdings (the last two are documents this server writes from EDGAR data)
* guidance: forward-looking sentences that give a number and a period ("expects 2026
  revenue of $X"), with the direction when the sentence states one (raised, lowered,
  reaffirmed); boilerplate safe-harbor text is left out
* red_flags: a fixed checklist (going concern, material weakness, restatement, auditor
  change, customer concentration, covenant trouble, impairments, investigations, late
  filing, delisting notices, officer departures, at-the-market selling, cyber incidents).
  "reported" means the document states the condition; "mentioned" means it is discussed
  as a possibility (a risk factor, a policy); "clear" means the document states the
  opposite ("in compliance with all covenants"); "none" means no matching text was found,
  which is not evidence that the condition is absent
* call: for a transcript, the speakers, where the questions start, each analyst question,
  guidance and claims by management
* map: where the main sections start (by page), for the filing map beside the document
* risk_items: every risk-factor heading, for the year-over-year comparison

Nothing here is estimated: every item is a sentence of the document with its offset, so
the page shows it and the spider can be sent to it.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Optional

KIND_LABEL = {"annual": "Annual report (10-K)", "quarterly": "Quarterly report (10-Q)", "release": "Earnings release",
              "transcript": "Earnings call transcript", "insider": "Insider trades (Form 4)", "holdings": "Fund holdings (13F)",
              "current": "Current report (8-K)", "other": "Document"}


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


# ---------------------------------------------------------------------- sentences
_TABLE_LINE = re.compile(r"\S {2,}(?:\(?\$?\s?[\d(]|\u2014)")


def _line_bounds(body: str, pos: int) -> tuple[int, int]:
    s = body.rfind("\n", 0, pos) + 1
    e = body.find("\n", pos)
    return s, (len(body) if e < 0 else e)


def _is_break_line(line: str) -> bool:
    """A line that ends a sentence run although it has no closing period: a table row,
    a heading, a bullet title, a headline set in Title Case."""
    t = line.strip()
    if not t:
        return True
    if _TABLE_LINE.search(t) and len(re.findall(r"\d", t)) >= 3:
        return True
    t = re.sub(r"^\(?(?:[a-z]|[ivx]{1,4}|\d{1,2})\)\s+", "", t)            # "(a) Dismissal of ..."
    if re.search(r"[.,;:!?]$", t) or not (t[:1].isupper() or t[:1].isdigit() or t[:1] == "$"):
        return False
    if len(t) < 70:
        return True
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", t) if w.lower() not in _SMALL_WORDS]
    return len(t) < 260 and len(words) >= 4 and sum(w[0].isupper() for w in words) >= 0.75 * len(words)


_SMALL_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "per", "the", "to", "with", "vs"}


def _bullet_item(body: str, ls: int, le: int) -> bool:
    """The line body[ls:le] is a bullet's text (the bullet alone on the line before) without a closing period."""
    if ls < 2 or re.search(r"[.!?:;]\s*$", body[ls:le]):
        return False
    ps = body.rfind("\n", 0, ls - 1) + 1
    return body[ps:ls - 1].strip() in tuple(_BULLETS) + ("-", "*", "\u00b7")


def _title_run_ends(body: str, j: int) -> bool:
    """A headline in Title Case runs straight into a sentence on the same line ("... Per Share To view this
    information ..."): the space at body[j] is where the headline ends."""
    before = body[max(0, j - 120):j].split("\n")[-1].split()[-7:]      # the headline's own line
    after = body[j + 1:j + 60].split()[:3]
    if len(before) < 7 or len(after) < 3:
        return False
    cap = lambda w: w[:1].isupper() or w[:1].isdigit() or w[:1] == "$" or w.lower() in _SMALL_WORDS
    words = [w for w in before if w[:1].isalpha() and w.lower() not in _SMALL_WORDS]
    return all(cap(w) for w in before) and len(words) >= 3 and all(w[:1].isupper() for w in words) and before[-1][:1].isupper() \
        and after[0][:1].isupper() and after[1][:1].islower() and after[2][:1].islower() and after[0] != "I"


_BULLETS = "\u2022\u25aa\u25cf\u25e6\u2023\u2043\uf0b7"


def sentence_at(body: str, pos: int, limit: int = 900) -> tuple[int, int]:
    """The sentence around body[pos]: back to the previous sentence end or heading line,
    forward to the next sentence end. Returns (start, end) offsets."""
    lo = max(0, pos - limit)
    start = lo
    i = pos
    while i > lo:
        ch = body[i - 1]
        if ch in _BULLETS:
            start = i
            break
        closed = ch in "\"'\u201d\u2019)" and i >= 2 and body[i - 2] in ".!?"          # a sentence that ends inside a quote
        if (ch in ".!?" or closed) and i < len(body) and body[i].isspace() \
                and not re.search(r"(?:\b(?:Inc|Corp|Co|Ltd|No|U\.S|vs|approx|Mr|Ms|Dr|Jr|St)|\b[A-Z])\.$", body[max(0, i - 6):i - (1 if closed else 0)]):
            start = i
            break
        if ch == "\n":
            ls, le = _line_bounds(body, i - 1)
            cont = body[i:i + 1].islower()          # the line after starts in lower case: a wrapped sentence, not a new one
            if body[ls:le].strip() == "" or (not cont and (_is_break_line(body[ls:le]) or _bullet_item(body, ls, le))):
                start = i
                break
        if ch == " " and i < pos - 20 and _title_run_ends(body, i - 1):
            start = i
            break
        i -= 1
    hi = min(len(body), pos + limit)
    end = hi
    j = pos
    while j < hi:
        ch = body[j]
        if ch in _BULLETS and j > pos:
            end = j
            break
        if ch in ".!?" and (j + 1 >= len(body) or body[j + 1].isspace() or body[j + 1] in "\"'”’)") \
                and not re.search(r"(?:\b(?:Inc|Corp|Co|Ltd|No|U\.S|vs|approx|Mr|Ms|Dr|Jr|St)|\b[A-Z])$", body[max(0, j - 5):j]):
            end = j + 1
            break
        if ch == "\n":
            ls, le = _line_bounds(body, j + 1)
            nxt = body[ls:le]
            cs = _line_bounds(body, j)[0]
            cont = nxt.lstrip()[:1].islower()
            if nxt.strip() == "" or (not cont and (_is_break_line(nxt) or _is_break_line(body[cs:j]) or _bullet_item(body, cs, j))):
                end = j
                break
        if ch == " " and j > pos + 20 and _title_run_ends(body, j):
            end = j
            break
        j += 1
    if start == lo and lo > 0:                     # no sentence start within reach: begin at a word
        nxt = re.search(r"\s", body[start:pos])
        start = start + nxt.end() if nxt else start
    while start < end and body[start].isspace():
        start += 1
    return start, end


def _loc(doc, off: int) -> dict:
    pg = doc.page_at(off)
    sec = doc.section_at(off)
    return {"page": pg, "printed_page": doc.printed_page(pg), "offset": off, "item": sec.item if sec else None,
            "section": f"Item {sec.item}. {sec.title}" if sec else None}


def _quote(doc, off: int, end: int) -> str:
    return _ws(doc.body[off:end])[:600]


# ---------------------------------------------------------------------- kind
def kind_of(doc, title: str = "") -> str:
    head = doc.body[:20000]
    t = (title or "").lower()
    if re.search(r"<meta name=\"bmg-doc\" content=\"insider-trades\"", head) or re.search(r"\bBMG insider trades\b", head[:400]):
        return "insider"
    if re.search(r"\bBMG fund holdings\b", head[:400]):
        return "holdings"
    turns = len(re.findall(r"(?m)^(?:Operator|OPERATOR)\b", doc.body)) + len(re.findall(r"(?i)question[- ]and[- ]answer|first question (?:comes|is) from", doc.body))
    if turns >= 2 and re.search(r"(?i)\b(?:earnings|conference) call\b|\bconference\b|\bcall participants\b|\bcorporate (?:speakers|participants)\b", head):
        return "transcript"
    if "earnings release" in t or re.search(r"\b8-k\b", t):
        return "release"
    if re.search(r"\b10-k\b|annual report", t):
        return "annual"
    if re.search(r"\b10-q\b|quarterly report", t):
        return "quarterly"
    cover = head[:6000]
    own = re.search(r"(?m)^[ \t\u00a0]*FORM[ \t\u00a0]+(10-K|10-Q|8-K|20-F|40-F)\b", cover)     # the cover's own form line, not a mention
    form = own.group(1) if own else ""
    if form in ("10-K", "20-F", "40-F") or (not form and re.search(r"(?i)annual\s+report\s+(?:pursuant\s+to|under)\s+section\s+13", cover)):
        return "annual"
    if form == "10-Q" or (not form and re.search(r"(?i)quarterly\s+report\s+(?:pursuant\s+to|under)\s+section\s+13", cover)):
        return "quarterly"
    first = head[:3000]
    if re.search(r"(?i)\b(?:reports?|announces?|posts?|delivers?)\b[^\n]{0,120}\b(?:quarter|year|fiscal)[^\n]{0,60}\bresults\b", first) \
            or re.search(r"(?i)\bpress release\b|\bexhibit 99\.1\b", first[:1500]):
        return "release"
    if form == "8-K" or (re.search(r"(?i)\bform\s+8-K\b", cover[:1500]) and re.search(r"(?i)current\s+report", cover)):
        return "current"
    return "other"


# ---------------------------------------------------------------------- guidance
_GUIDE = re.compile(r"\b(?:guidance|outlook|expects?|expected to|expecting|anticipates?|anticipated to|forecasts?|we project|projecting|projected to|"
                    r"targets?|targeting|now sees|estimates?|on track to|reaffirm\w*|reiterat\w*|we see|we are guiding|guiding)\b", re.I)
_PERIOD = re.compile(r"\b(?:full[- ]year|fiscal(?: year)? ?(?:20)?\d\d|(?:19|20)\d\d|next (?:quarter|year)|(?:first|second|third|fourth|1st|2nd|3rd|4th) quarter|"
                     r"\bQ[1-4]\b|for the (?:full )?(?:year|quarter)|this year|coming year|second half|first half|the year|the quarter)\b", re.I)
_GNUM = re.compile(r"\$\s?\d|\d+(?:\.\d+)?\s?%|\b\d[\d,.]*\s?(?:million|billion|thousand|basis points|bps|percent)\b|\b(?:mid|high|low)[- ](?:single|double)[- ]digits?\b", re.I)
_BOILER = re.compile(r"forward[- ]looking statements?|safe harbor|private securities litigation|undue reliance|actual results (?:could|may|might|will) differ|"
                     r"risks? and uncertaint|no obligation to (?:update|revise)|undertake(?:s)? no obligation|does not undertake|cautionary|"
                     r"words such as|identified by words|could cause actual", re.I)
_DIRECTION = [
    ("raised", re.compile(r"\b(?:rais(?:e|es|ed|ing)|increas(?:e|es|ed|ing)|lift(?:s|ed)?|boost(?:s|ed)?|upgrad\w+)\b[^.]{0,60}\b(?:guidance|outlook|forecast|expectations?|range|target)\b|"
                          r"\b(?:guidance|outlook|forecast)\b[^.]{0,30}\b(?:raised|increased)\b", re.I)),
    ("lowered", re.compile(r"\b(?:lower(?:s|ed|ing)?|reduc(?:e|es|ed|ing)|cut(?:s|ting)?|trim(?:s|med)?|downgrad\w+)\b[^.]{0,60}\b(?:guidance|outlook|forecast|expectations?|range|target)\b|"
                           r"\b(?:guidance|outlook|forecast)\b[^.]{0,30}\b(?:lowered|reduced|cut)\b", re.I)),
    ("narrowed", re.compile(r"\bnarrow(?:s|ed|ing)?\b[^.]{0,60}\b(?:guidance|outlook|range)\b", re.I)),
    ("updated", re.compile(r"\bupdat(?:e|es|ed|ing)\b[^.]{0,60}\b(?:guidance|outlook|forecast|expectations?)\b", re.I)),
    ("reaffirmed", re.compile(r"\b(?:reaffirm\w*|reiterat\w*|maintain(?:s|ed|ing)?|confirm(?:s|ed|ing)?)\b[^.]{0,60}\b(?:guidance|outlook|forecast|expectations?|range|target)\b", re.I)),
]
_ACCOUNTING = re.compile(r"(?i)\b(?:expected (?:life|lives|term|volatility|dividend yield|return on (?:plan )?assets|credit loss(?:es)?)|expected to (?:be )?(?:paid|recogni[sz]ed?|amortized|reclassified|vest|realized|settled)|"
                         r"expect(?:s|ed)? to recogni[sz]e|to be (?:realized|recogni[sz]ed)|deferred revenue|remaining performance obligations?|vested and expected|expected to vest|unrecognized|"
                         r"amortization|accretion|weighted[- ]average|discount rate|actuarial|pension|postretirement|fair value|lease (?:payments|term|liabilit)|"
                         r"stock-based compensation|share-based compensation|abatements?|tax credits?|carryforwards?|valuation allowance|purchase obligations?|"
                         r"contractual obligations?|unconditional purchase|goodwill|intangible assets?)\b")
_OUTLOOK_HEAD = re.compile(r"(?im)^[ \t]*(?:(?:20\d\d|fiscal(?: year)? 20\d\d|full[- ]year|business|financial|company|updated|revised)[ \t]+)*(?:outlook|guidance)\b[^\n]{0,60}$")


def _guidance_tables(doc) -> list[dict]:
    """Rows of a guidance table under an outlook heading ("Fiscal 2026 Guidance", then "Organic revenue
    Approximately +3%  +2% to +4%"), each quoted as printed, with the previous column when there is one."""
    body, out = doc.body, []
    for m in re.finditer(r"(?im)^[^\n]{0,70}\b(?:guidance|outlook)\b[^\n]{0,90}$", body):
        head = m.group(0).strip()
        if head.endswith(".") or len(head) > 150 or _BOILER.search(head):
            continue
        pos, cols, rows = m.end() + 1, None, []
        hc = [c for c in re.split(r" {2,}", head) if c]
        if len(hc) >= 3 and not re.search(r"\d", " ".join(hc[1:])):
            cols = hc[1:]                        # the column titles printed on the heading's line
        for _ in range(30):
            if pos >= len(body):
                break
            e = body.find("\n", pos)
            e = len(body) if e < 0 else e
            t = body[pos:e].strip()
            cells = [c for c in re.split(r" {2,}", t) if c]
            if not t:
                if rows:
                    break
            elif len(cells) >= 2 and not re.search(r"\d", t) and len(t) < 80:
                cols = cells                     # "Current  Previous"
            elif len(cells) >= 2 and re.search(r"[\d%$]", " ".join(cells[1:])) and len(cells[0]) < 90 and not re.search(r"[.!?]$", cells[0]):
                rows.append((pos, e, cells))
            elif rows or len(t) > 200:
                break
            pos = e + 1
        if len(rows) < 2:
            continue
        for off, e, cells in rows[:16]:
            label, vals = cells[0], cells[1:]
            if cols and len(cols) == len(vals):
                desc = "; ".join(f"{c.lower()}: {v}" for c, v in zip(cols, vals))
                cur_prev = dict((c.lower(), v) for c, v in zip(cols, vals))
                cur, prev = cur_prev.get("current"), cur_prev.get("previous") or cur_prev.get("prior")
                direction = "unchanged" if cur and prev and _ws(cur) == _ws(prev) else "updated" if cur and prev else "stated"
            else:
                desc, direction = " ".join(vals), "stated"
            if not re.search(r"\d", desc):
                continue
            out.append({"text": f"{label}: {desc}", "direction": direction, "numbers": vals[:4], "table": _ws(head)[:120], **_loc(doc, off), "end": e})
    return out


def guidance(doc, kind: str = "other", limit: int = 14) -> list[dict]:
    body = doc.body
    heads = [m.start() for m in _OUTLOOK_HEAD.finditer(body)]
    seen, out = set(), []
    tables = _guidance_tables(doc) if kind in ("release", "transcript", "current", "other") else []
    taken = {(x["offset"]) for x in tables}
    for m in _GUIDE.finditer(body):
        s, e = sentence_at(body, m.start())
        if (s, e) in seen:
            continue
        seen.add((s, e))
        txt = _ws(body[s:e])
        if not (40 <= len(txt) <= 650) or _BOILER.search(txt) or not _GNUM.search(txt) or not _PERIOD.search(txt):
            continue
        if _ACCOUNTING.search(txt):
            continue                    # accounting estimates and schedules, not a business outlook
        sec = doc.section_at(s)
        score = 1.0
        if any(0 <= s - h <= 4000 for h in heads):
            score += 3
        if re.search(r"(?i)\b(?:guidance|outlook)\b", txt):
            score += 2
        if re.search(r"(?i)\bfull[- ]year|fiscal(?: year)? ?(?:20)?\d\d|next quarter|fourth quarter\b", txt):
            score += 1
        if sec and sec.item in ("7", "2"):
            score += 1
        if sec and sec.item == "1A":
            score -= 3                  # a risk factor's "we expect" is not guidance
        if kind in ("release", "transcript"):
            score += 1
        direction = next((d for d, rx in _DIRECTION if rx.search(txt)), "stated")
        nums = [x.group(0) for x in re.finditer(r"\$\s?[\d.,]+\s?(?:million|billion|thousand|[MBK]\b)?|\d+(?:\.\d+)?\s?%|\b(?:mid|high|low)[- ](?:single|double)[- ]digits?", txt, re.I)][:6]
        out.append({"text": txt, "direction": direction, "numbers": nums, "score": score, **_loc(doc, s), "end": e})
    out = [x for x in out if x["offset"] not in taken]
    out.sort(key=lambda x: -x["score"])
    uniq = []
    for x in out:                       # the same sentence printed twice (summary and MD&A) is listed once
        w = _words(x["text"])
        if not any(len(w & _words(y["text"])) >= 0.8 * max(1, len(w | _words(y["text"]))) for y in uniq):
            uniq.append(x)
    out = uniq
    keep = sorted(out[:max(4, limit - len(tables))] + tables[:limit], key=lambda x: x["offset"])
    for x in keep:
        x.pop("score", None)
    return keep


# ---------------------------------------------------------------------- red flags
_COND = re.compile(r"\b(?:could|may|might|would|if (?:we|the company|our|an?|any)|in the event|there can be no assurance|cannot assure|potential(?:ly)?|possible|whether)\b", re.I)


def _flag_scan(doc, find: str, cls, exclude: Optional[str] = None, near: Optional[str] = None, max_hits: int = 60):
    body = doc.body
    rx = re.compile(find, re.I)
    ex = re.compile(exclude, re.I) if exclude else None
    nr = re.compile(near, re.I) if near else None
    seen, hits = set(), []
    for m in rx.finditer(body):
        s, e = sentence_at(body, m.start())
        if (s, e) in seen:
            continue
        seen.add((s, e))
        txt = _ws(body[s:e])
        if len(txt) < 25 or (ex and ex.search(txt)) or (nr and not nr.search(txt)):
            continue
        st = cls(txt, doc.section_at(s))
        if st:
            hits.append((st, s, e, txt))
        if len(hits) >= max_hits:
            break
    return hits


def _in_risk_factors(sec) -> bool:
    return bool(sec and sec.item == "1A")


def _gc(txt, sec):
    if re.search(r"(?i)\b(?:no|not|did not|does not|do not|absence of)\b[^.]{0,60}substantial doubt|substantial doubt[^.]{0,40}\b(?:does not|did not) exist", txt):
        return "clear"
    if re.search(r"(?i)(?:evaluat|assess)\w*[^.]{0,40}whether (?:there|conditions|events)|management(?:'s)? (?:is required|evaluates|assesses)", txt) and not re.search(r"(?i)\b(?:raise|raised|raises) substantial doubt\b", txt):
        return "mentioned"
    if _in_risk_factors(sec) or (_COND.search(txt) and not re.search(r"(?i)\b(?:raise|raised|raises|there is|there was|exists|existed|indicate|indicates)\b[^.]{0,40}substantial doubt", txt)):
        return "mentioned"
    return "reported"


def _mw(txt, sec):
    if re.search(r"(?i)certifying officer|significant deficiencies and material weaknesses in the design", txt):
        return None                    # the officers' certification (Exhibit 31) wording, in every 10-K
    if re.search(r"(?i)\bno material weakness|not identified any material weakness|did not identify any material weakness|risk that a material weakness exists|reasonable possibility that a material misstatement", txt):
        return "clear" if re.search(r"(?i)\bno material weakness|not identified any|did not identify any", txt) else None
    if re.search(r"(?i)(?:identified|have|has|had|disclosed|remediat\w*|existence of)\b[^.]{0,80}material weakness|material weakness(?:es)?[^.]{0,120}\b(?:was|were|has been|have been) identified|"
                 r"(?:internal control over financial reporting|disclosure controls and procedures)[^.]{0,60}\b(?:was|were|is|are) not effective|did not maintain effective", txt) \
            and not (_in_risk_factors(sec) and _COND.search(txt) and not re.search(r"(?i)\bwe (?:have )?identified\b", txt)):
        return "reported"
    if re.search(r"(?i)\beffective\b", txt) and not re.search(r"(?i)not effective|ineffective", txt) and re.search(r"(?i)concluded", txt):
        return None
    return "mentioned"


def _restate(txt, sec):
    if re.search(r"(?i)indicate by check mark|recoup|claw ?back|recovery of erroneously awarded|incentive-based compensation", txt):
        return None                    # the cover's check box and the clawback policy describe what would happen, nothing that did
    if re.search(r"(?i)\b(?:have|has|had|were|was) (?:been )?restated\b|\bwe restated\b|\bthe restatement\b|\bas restated\b|restatement of (?:our|the|its) (?:previously issued|prior|consolidated|unaudited)|"
                 r"should no longer be relied upon|\bnon-reliance\b", txt) and not _COND.search(txt):
        return "reported"
    return "mentioned"


def _revision(txt, sec):
    if re.search(r"(?i)indicate by check mark", txt):
        return None
    return "reported" if re.search(r"(?i)\b(?:revised|corrected|correction of)\b", txt) and not _COND.search(txt) else None


def _auditor(txt, sec):
    if re.search(r"(?i)\bratif\w*|audit committee (?:has )?(?:appointed|selected|engaged)[^.]{0,80}for (?:the )?fiscal|served as (?:our|the company's) (?:auditor|independent)", txt):
        return None
    if re.search(r"(?i)\b(?:there were|we had|have been) no (?:changes in or )?disagreements\b|\bno disagreements\b", txt):
        return "clear"
    if re.search(r"(?i)\b(?:dismiss(?:ed|al)|resign(?:ed|ation)|declined to stand for re-?election|terminat(?:ed|ion) of)\b", txt):
        return "reported" if not _COND.search(txt) else "mentioned"
    if re.search(r"(?i)\b(?:engaged|appointed|approved the engagement of)\b[^.]{0,120}\bas (?:the company's|our|its) (?:new )?independent registered public accounting firm", txt) and re.search(r"(?i)\bnew\b|replac|succeed|effective", txt):
        return "reported"
    return None


def _cust(txt, sec):
    if re.search(r"(?i)\bno (?:single |one |individual )?customer\b[^.]{0,80}(?:accounted for|represented|comprised)|\bnone of (?:our|the) customers\b", txt):
        return "clear"
    pcts = [float(m.group(1)) for m in re.finditer(r"(?i)\b(?:customers?|clients?)\b[^.%]{0,100}?(?:accounted for|represented|comprised|made up)[^.%]{0,40}?(\d{1,3}(?:\.\d+)?)\s?%", txt)]
    pcts += [float(m.group(1)) for m in re.finditer(r"(?i)(\d{1,3}(?:\.\d+)?)\s?% of (?:our |its |the company.s )?(?:total |net |consolidated )?(?:revenues?|net sales|sales|trade receivables|receivables)[^.]{0,80}\b(?:customers?|clients?)\b", txt)]
    if pcts and max(pcts) >= 10:
        return "reported"
    if re.search(r"(?i)limited number of (?:customers|clients)|significant portion of (?:our )?(?:revenue|net sales|sales)[^.]{0,40}(?:from )?(?:a )?(?:small|limited) number|depend\w* on (?:a )?(?:few|small number of|limited number of|our largest) customers", txt):
        return "mentioned"
    return None


def _supplier(txt, sec):
    pcts = [float(x) for x in re.findall(r"(\d{1,3}(?:\.\d+)?)\s?%", txt)]
    if pcts and max(pcts) >= 10 and re.search(r"(?i)purchases|supplied|suppliers?", txt) and not _COND.search(txt):
        return "reported"
    return "mentioned"


def _covenant(txt, sec):
    if re.search(r"(?i)\b(?:holders?|trustee|indenture|hereunder|thereunder|hereof|thereof|shall)\b", txt):
        return None                    # the terms of a debt instrument (an exhibit), not a statement about the company
    if re.search(r"(?i)\b(?:were|was|are|is|remained|remain) in compliance with (?:all|each|the)?\b[^.]{0,80}covenants?", txt) and not re.search(r"(?i)\bnot in compliance", txt):
        return "clear"
    if not re.search(r"(?i)\b(?:we|the company|our|its)\b", txt):
        return None
    if re.search(r"(?i)\b(?:were|was|are|is) not in compliance\b[^.]{0,80}covenant|(?:obtained|received|entered into|executed)\b[^.]{0,60}\b(?:waiver|forbearance)|"
                 r"\bbreach(?:ed)?\b[^.]{0,40}covenant|event of default (?:has|had) occurred|\b(?:were|was|are|is) in default\b", txt) and not (_in_risk_factors(sec) or _COND.search(txt)):
        return "reported"
    return None


def _impair(txt, sec):
    if re.search(r"(?i)\bno (?:goodwill |asset |intangible )?impairment\b|did not (?:record|recognize|identify) (?:any )?(?:goodwill )?impairment|not impaired", txt):
        return "clear"
    if re.search(r"(?i)(?:recorded|recognized|incurred|recorded an?|recognized an?)\b[^.]{0,80}impairment (?:charges?|loss(?:es)?|expense)|impairment (?:charges?|loss(?:es)?) of \$\s?[\d.,]+", txt) \
            and re.search(r"\$\s?[\d.,]+", txt) and not _COND.search(txt):
        return "reported"
    return None


def _invest(txt, sec):
    if re.search(r"(?i)\b(?:received|receive|issued|served)\b[^.]{0,80}\b(?:subpoena|wells notice|civil investigative demand|grand jury)|"
                 r"\b(?:is|are) (?:conducting|cooperating|responding)\b[^.]{0,80}\b(?:investigation|inquiry|subpoena)|commenced an? (?:formal )?investigation|opened an? (?:formal )?investigation", txt) \
            and not (_in_risk_factors(sec) and _COND.search(txt)):
        return "reported"
    if _COND.search(txt) or _in_risk_factors(sec):
        return "mentioned"
    return None


def _late(txt, sec):
    if re.search(r"(?i)\bform 12b-25\b|\bNT 10-[KQ]\b|\b(?:did not|was unable to|unable to) (?:timely )?file\b[^.]{0,60}(?:annual|quarterly) report|without unreasonable effort or expense", txt) and not _COND.search(txt):
        return "reported"
    return None


def _delist(txt, sec):
    if re.search(r"(?i)\b(?:have|has) been (?:suspended and )?delisted|\b(?:were|was) delisted|delisting (?:became|becomes|was|is) effective|form 25 notification", txt) and not _COND.search(txt):
        return "reported"
    if re.search(r"(?i)\b(?:received|receive)\b[^.]{0,40}\b(?:notice|letter|notification)\b[^.]{0,120}\b(?:nasdaq|nyse|new york stock exchange|listing)\b", txt) and re.search(r"(?i)not in compliance|non-?compliance|deficien|delist|minimum bid", txt):
        return "reported"
    if re.search(r"(?i)\bdelist", txt) and re.search(r"(?i)(?:our|the company.s|its) (?:common stock|class [ab] common|shares|securities|ordinary shares|ads|units|warrants)|from (?:the )?(?:nasdaq|nyse|new york stock exchange)", txt):
        return "mentioned"
    return None


def _officer(txt, sec):
    if re.search(r"(?i)\b(?:resigned|resignation|stepped down|will step down|departed|departure|retire(?:d|ment)|terminated|notified\b[^.]{0,40}\bdecision to (?:resign|retire|step down))\b", txt) \
            and not _COND.search(txt) and not _in_risk_factors(sec) and not re.search(r"(?i)\bretirement (?:plan|benefit|savings|account)", txt):
        return "reported"
    return None


def _atm(txt, sec):
    if re.search(r"(?i)\b(?:sold|issued|issuance|entered into|sale of|sales of|proceeds)\b", txt) and not _COND.search(txt):
        return "reported"
    return "mentioned"


def _cyber(txt, sec):
    if re.search(r"(?i)\b(?:have not|has not|did not|not aware of|no) (?:experienced|identified|detected|been)?[^.]{0,40}(?:material )?cybersecurity (?:incident|threat)s?", txt):
        return "clear"
    if re.search(r"(?i)\b(?:experienced|detected|identified|discovered|became aware of)\b[^.]{0,30}\b(?:a|an)\b[^.]{0,40}(?:cybersecurity|ransomware|security|data) (?:incident|attack|breach)", txt) and not _COND.search(txt):
        return "reported"
    return None


FLAGS = [
    # key, title, why, find, classify, exclude, near
    ("going_concern", "Going concern doubt", "The company or its auditor doubts it can keep operating for the next year without new money or a change in plans.",
     r"substantial doubt", _gc, None, r"going concern"),
    ("material_weakness", "Material weakness in internal control", "A control gap that could let a material error in the statements go uncaught.",
     r"material weakness", _mw, None, None),
    ("restatement", "Restatement of past results", "Earlier statements were wrong enough to be reissued; past numbers changed.",
     r"\brestat(?:e|ed|ement|ements|ing)\b|previously issued financial statements|non-reliance|should no longer be relied upon",
     _restate, r"amended and restated|restated (?:certificate|articles|bylaws|by-laws|charter|agreement|plan|credit|indenture)|second amended|restated and amended", None),
    ("revision", "Prior-period correction (revision)", "Smaller errors in earlier periods corrected without a full restatement.",
     r"(?:immaterial )?errors? (?:in|related to) (?:the |our )?(?:previously issued|prior period|prior year)|revis(?:ed|ion of) (?:the |our |certain )?(?:previously issued|prior[- ]period|prior[- ]year)",
     _revision, None, None),
    ("auditor_change", "Change of auditor", "Auditor departures can follow disagreements over accounting.",
     r"(?:independent registered public accounting firm|independent auditors?|independent accountants?|certifying accountant)", _auditor, None,
     r"dismiss|resign|declined to stand|engag|appoint|disagreement|terminat"),
    ("customer_concentration", "Customer concentration", "A few customers supply much of the revenue; losing one would hurt.",
     r"\bcustomers?\b[^.\n]{0,160}(?:accounted for|represented|comprised|made up)|(?:accounted for|represented|comprised)[^.\n]{0,80}\bcustomers?\b|limited number of (?:customers|clients)|largest customers?",
     _cust, r"\bvendors?\b", None),
    ("supplier_concentration", "Supplier concentration", "Reliance on one or a few suppliers for key inputs.",
     r"single[- ]source|sole[- ]source|single supplier|limited number of suppliers|one supplier|sole supplier", _supplier, None, None),
    ("covenant", "Debt covenant trouble", "Breaking loan terms can make debt due early or bring costly waivers.",
     r"\bcovenants?\b|\bevent of default\b|\bforbearance\b|\bwaiver\b", _covenant, None, r"covenant|default|forbearance|waiver"),
    ("impairment", "Impairment charges", "Assets were written down: the company expects less from them than it paid.",
     r"\bimpairment\b", _impair, None, None),
    ("investigation", "Government investigation or subpoena", "Regulators or prosecutors are examining the company.",
     r"\bsubpoena|wells notice|civil investigative demand|grand jury|\b(?:SEC|Securities and Exchange Commission|Department of Justice|DOJ|FTC|attorneys? general)\b[^.\n]{0,80}\b(?:investigation|inquiry|probe)",
     _invest, None, None),
    ("late_filing", "Late filing", "A report was not filed on time, often a sign of accounting problems.",
     r"form 12b-25|\bNT 10-[KQ]\b|unable to (?:timely )?file|did not (?:timely )?file|without unreasonable effort or expense", _late, None, None),
    ("delisting", "Exchange listing notice", "The stock exchange says the company falls short of a listing rule.",
     r"\bdelist|\blisting (?:rule|requirement|standard)s?\b|minimum bid price", _delist, None, None),
    ("officer_departure", "Senior officer departure", "A chief executive or chief financial officer leaving, stated in the document.",
     r"(?:chief executive officer|chief financial officer|chief accounting officer|\bCEO\b|\bCFO\b|principal (?:executive|financial|accounting) officer)", _officer, None,
     r"resign|step(?:ped)? down|depart|retire|terminat"),
    ("atm_offering", "At-the-market share selling", "New shares sold into the market dilute existing holders.",
     r"at[- ]the[- ]market (?:offering|program|equity|sales)|\bATM (?:program|offering|facility|agreement)", _atm, None, None),
    ("cyber_incident", "Cybersecurity incident", "A breach or attack the company says it experienced.",
     r"cybersecurity (?:incident|attack|breach)|ransomware|data breach|security incident", _cyber, None, None),
]


def red_flags(doc) -> list[dict]:
    out = []
    for key, title, why, find, cls, exclude, near in FLAGS:
        hits = _flag_scan(doc, find, cls, exclude, near)
        rank = {"reported": 0, "mentioned": 1, "clear": 2}
        best = sorted(hits, key=lambda h: (rank[h[0]], len(h[3]) < 60, h[1]))       # a whole sentence before a heading
        status = best[0][0] if best else "none"
        quotes = [{"text": h[3][:600], "status": h[0], **_loc(doc, h[1]), "end": h[2]} for h in best if h[0] == status][:3]
        out.append({"key": key, "title": title, "why": why, "status": status, "count": sum(1 for h in hits if h[0] == status), "quotes": quotes})
    return out


# ---------------------------------------------------------------------- transcripts
_ROLE = re.compile(r"(?i)\b(?:chief|ceo|cfo|coo|cto|president|chair\w*|officer|director|vice president|vp|svp|evp|treasurer|controller|head of|investor relations|"
                   r"general counsel|counsel|founder|secretary|analyst|research|managing director|partner|portfolio manager|associate|strategist|economist)\b")
_NAME = r"[A-Z][A-Za-z.'’\-]+(?:\s+(?:[A-Z][A-Za-z.'’\-]+|de|van|von|da|di|del|la|le)|,?\s+(?:III|II|IV|Jr\.?|Sr\.?)(?![A-Za-z])){0,4}"
_NOT_NAME = {"section", "session", "participants", "participant", "speakers", "speaker", "remarks", "call", "conference", "company", "corporate",
             "presentation", "discussion", "question", "questions", "answer", "answers", "instructions", "thank", "thanks", "good", "morning",
             "afternoon", "evening", "welcome", "earnings", "quarter", "results", "transcript", "page", "pages", "total", "exhibit", "management",
             "prepared", "other", "executives", "analysts", "operator", "unidentified", "inc", "inc.", "corp", "corporation", "llc", "group", "fiscal"}
_SUFFIX = {"iii", "ii", "iv", "jr", "jr.", "sr", "sr."}


def _last(name: str) -> str:
    ws = [w.strip(",.").lower() for w in name.split()]
    ws = [w for w in ws if w and w not in _SUFFIX]
    return ws[-1] if ws else ""


def _name_ok(name: str) -> bool:
    ws = name.replace(",", " ").split()
    return 2 <= len(ws) <= 5 and not any(w.strip(".").lower() in _NOT_NAME for w in ws) and not _ROLE.fullmatch(name) \
        and not name.isupper() or (2 <= len(ws) <= 4 and name.isupper() and not any(w.lower() in _NOT_NAME for w in ws))


# a part of a participant line that names a firm, though it holds a role word ("Thompson Research Group")
_FIRM_END = re.compile(r"(?i)\b(?:group|llc|l\.l\.c\.?|inc\.?|incorporated|corp\.?|corporation|company|co\.?|ltd\.?|limited|plc|lp|l\.p\.?|securities|"
                       r"capital|partners|associates|advisors|advisers|bank|bancorp|markets|investments?)\s*$")


# where the participant list ends: the call itself begins
_LIST_END = re.compile(r"(?i)^(presentation|management discussion|operator|question|q&a|prepared remarks|good (?:morning|afternoon|day))\b")


def _participants(body: str) -> dict:
    """Names listed at the top of a transcript, with role and firm, split into the company's
    speakers and outside participants (analysts)."""
    head = body[:9000]
    people = {}
    blocks = re.split(r"(?im)^[ \t]*((?:corporate|company|executive)[ \t]+(?:speakers|participants)|(?:conference[ \t]+call[ \t]+|other[ \t]+)?participants|analysts?)[ \t]*:?[ \t]*$", head)
    for i in range(1, len(blocks) - 1, 2):
        label, txt = blocks[i].lower(), blocks[i + 1]
        side = "company" if re.search(r"corporate|company|executive", label) else "analyst"
        lines = [l.strip(" \t·•*-") for l in txt.split("\n")]
        k = 0
        while k < len(lines):
            l = lines[k]
            if not l:
                k += 1
                continue
            if _LIST_END.match(l):
                break
            m = re.match(rf"^({_NAME})\s*(?:[;\u2013\u2014]+|\s-\s|,\s)\s*(.+)$", l)
            if m and _name_ok(m.group(1).strip(", ")):
                name, rest = m.group(1).strip(", "), m.group(2).strip()
                # a role that wraps onto the next line ("Senior Vice President, General" / "Counsel")
                if k + 1 < len(lines) and lines[k + 1] and not re.search(r"[;\u2013\u2014]|\s-\s", lines[k + 1]) and len(lines[k + 1].split()) <= 4 \
                        and _ROLE.search(lines[k + 1]) and (rest.rstrip().endswith((",", "and", "of", "&")) or len(lines[k + 1].split()) == 1):
                    rest += " " + lines[k + 1]
                    k += 1
            elif re.fullmatch(_NAME, l) and _name_ok(l) and k + 1 < len(lines) and lines[k + 1] \
                    and not (re.fullmatch(_NAME, lines[k + 1]) and _name_ok(lines[k + 1])):
                # "Joseph Hayek" / "Worthington Enterprises, Inc.; President and Chief Executive Officer", and the same
                # wrapped over short lines: "Jawad Chaudhry" / "BCB" / "Bancorp, Inc.; Chief Financial Officer", "KBW;" / "Analyst"
                rest, used = lines[k + 1], 1
                while used < 3 and k + used + 1 < len(lines) and lines[k + used + 1] and (rest.endswith(";") or (";" not in rest and len(rest.split()) <= 3)) \
                        and not (re.fullmatch(_NAME, lines[k + used + 1]) and _name_ok(lines[k + used + 1])) and not _LIST_END.match(lines[k + used + 1]):
                    nxt = lines[k + used + 1]
                    # a wrapped name continues with a space; a line of its own (a table cell: "Example Corp" / "Chief
                    # Executive Officer") is another field
                    rest += (" " if rest.endswith((";", ",", "&", " and", " of", "-")) or ";" in nxt else "; ") + nxt
                    used += 1
                if not (_ROLE.search(rest) or ";" in rest or side == "analyst"):
                    k += 1
                    continue
                name = l
                k += used
            else:
                k += 1
                continue
            parts = []
            # "Name; Firm; Role" splits on the semicolons only, so "Sturm, Ruger & Company" stays one firm
            delim = r"\s*;\s*" if ";" in rest else r"\s*[\u2013\u2014]\s*|\s+-\s+|,\s+"
            for p in (x.strip() for x in re.split(delim, rest)):
                if p and parts and re.fullmatch(r"(?i)(?:inc|incorporated|llc|l\.l\.c|ltd|limited|plc|co|corp|corporation|lp|l\.p|n\.a|s\.a|ag|se|nv)\.?", p):
                    parts[-1] += ", " + p          # "Greif, Inc." stays one name
                elif p:
                    parts.append(p)
            roles = [p for p in parts if _ROLE.search(p) and not _FIRM_END.search(p)]
            firm = next((p for p in parts if p not in roles), "")
            if not roles and side == "analyst":
                roles = ["Analyst"]
            people[name] = {"name": name, "side": side, "role": ", ".join(roles)[:90], "firm": firm[:80]}
            k += 1
    return people


def _speaker_lines(body: str, known: dict):
    """(offset, label, text_start) for each speaker turn: "Name: text", "Name^ text", "Name -- text", or a
    name alone on its line (optionally followed by a role line). A label counts when it is a listed participant,
    shares a last name with one (a nickname: "Bill Angrick" for "William P. Angrick, III"), is the operator, or
    labels at least two turns."""
    rx_inline = re.compile(rf"(?m)^[ \t]*(Operator|OPERATOR|{_NAME})[ \t]*(?:[:^]|--|\u2014|\u2013)[ \t]*(?=\S)")
    rx_alone = re.compile(rf"(?m)^[ \t]*(Operator|OPERATOR|{_NAME})[ \t]*$")
    cands = []
    for rx in (rx_inline, rx_alone):
        for m in rx.finditer(body):
            name = m.group(1).strip().rstrip(",")
            if name.lower() == "operator":
                name = "Operator"
            elif not _name_ok(name):
                continue
            ts = m.end()
            if rx is rx_alone:
                if ts < len(body) and body[ts] == "\n":
                    ts += 1
                nxt_e = body.find("\n", ts)
                nxt = body[ts:nxt_e if nxt_e >= 0 else len(body)].strip()
                if not nxt or (re.fullmatch(_NAME, nxt) and _name_ok(nxt)):
                    if not (nxt and _ROLE.search(nxt)):
                        continue
                if nxt and len(nxt) < 110 and _ROLE.search(nxt) and not re.search(r"[.?!]$", nxt):
                    ts = nxt_e + 1 if nxt_e >= 0 else len(body)        # "Bill D'Onofrio" / "Vice President of Investor Relations"
            cands.append((m.start(), name, ts))
    cands.sort()
    head_end = max([m.end() for m in re.finditer(r"(?im)^[ \t]*(?:presentation|management discussion section|prepared remarks)[ \t]*:?[ \t]*$", body[:12000])] + [0])
    lasts = {_last(n): n for n in known}
    count = Counter(n for _, n, _ in cands)
    out, seen = [], set()
    for off, name, ts in cands:
        if off in seen or (off < head_end and name != "Operator"):
            continue                 # the participant list itself is not a turn
        canon = name if name in known else lasts.get(_last(name))
        if name != "Operator" and not canon and count[name] < 2:
            continue
        seen.add(off)
        out.append((off, canon or name, ts))
    return out


_QA = re.compile(r"(?im)^[ \t]*(?:question[-\s]*and[-\s]*answer(?:\s+session|\s+section)?|questions?\s+and\s+answers?(?:\s+session|\s+section)?|q\s?&\s?a(?:\s+session)?)[ \t]*:?[ \t]*$")
_QA_OPEN = re.compile(r"(?i)first question|question[-\s]*and[-\s]*answer|\(operator instructions\)|open (?:up )?the (?:line|lines|call|floor) (?:up )?(?:for|to) questions|"
                      r"begin (?:the )?question|take (?:our|your) (?:first )?questions?")


_LISTED = re.compile(r"(?m)^[ \t]*(?:[QH][1-4]\s+(?:FY\s?)?\d{4}\s+)?(?P<name>[A-Z][^\n()]{1,80}?)\s*\((?:(?P<ex>NYSE American|NYSE MKT|NYSE|NASDAQ|Nasdaq|AMEX|TSX|OTCQX|OTC)\s*:\s*)?"
                     r"(?P<ticker>[A-Z]{1,5}(?:\.[A-Z])?)\)")
_CO_END = re.compile(r"(?i)(?:\binc\.?|\bincorporated|\bcorp\.?|\bcorporation|\bcompany|\bco\.|\bbancorp|\bholdings|\bgroup|\bltd\.?|\bplc|\bn\.v\.|\bs\.a\.|"
                     r"\bl\.p\.|\bllc|\benterprises|\btrust|\bbank)\s*$")


def _call_company(doc, known: dict, view: str) -> Optional[dict]:
    """The company holding the call: a name with its ticker near the top ("Red Violet, Inc. (NASDAQ: RDVT)"),
    else the firm its listed speakers share ("Greif, Inc."), else a company name just above the call title."""
    head = view[:4000]
    at = lambda name, off, how, ticker=None: {"name": name, "ticker": ticker, "page": doc.page_at(off), "offset": off, "how": how}
    for m in _LISTED.finditer(head):
        name = m["name"].strip(" ,")
        if m["ex"] or _CO_END.search(name):
            return at(name, m.start("name"), "title line", m["ticker"])
    key = lambda f: re.sub(r"[^a-z0-9]", "", f.lower())        # "BCB Bancorp, Inc." wrapped as "BCB" / "Bancorp, Inc." is the same firm
    named = [p["firm"] for p in known.values() if p["side"] == "company" and p["firm"]]
    firms = Counter(key(f) for f in named)
    if firms:
        top, n = firms.most_common(1)[0]
        if n >= 2 or len(firms) == 1:
            firm = next(f for f in named if key(f) == top)
            i = head.find(firm)
            return at(firm, max(0, i), "participants list")
    pos = 0
    lines = head.split("\n")[:25]
    for a, b in zip(lines, lines[1:]):
        if len(a.strip()) < 80 and _CO_END.search(a.strip()) and re.search(r"(?i)earnings|conference call|\bcall\b", b):
            return at(a.strip(" ,"), pos + len(a) - len(a.lstrip()), "title line")
        pos += len(a) + 1
    return None


def transcript(doc) -> dict:
    body = doc.body
    # Text and PDF pages fold runs of short lines into one ("CORPORATE SPEAKERS:  Jane Rivera  Chief Executive
    # Officer"). The participant list, the speaker labels and the headings are read with each fold back on a line
    # of its own; the view has the body's length, so every offset still points into the body.
    view = body.replace("  ", "\n ")
    known = _participants(view)
    turns_raw = _speaker_lines(view, known)
    turns = []
    for k, (off, name, ts) in enumerate(turns_raw):
        end = turns_raw[k + 1][0] if k + 1 < len(turns_raw) else len(body)
        txt = body[ts:end].strip()
        if not txt:
            continue
        p = known.get(name) or {"name": name, "side": "operator" if name == "Operator" else "unknown", "role": "", "firm": ""}
        turns.append({"name": name, "side": "operator" if name == "Operator" else p["side"], "role": p.get("role", ""), "firm": p.get("firm", ""),
                      "offset": off, "text_offset": ts, "end": end, "words": len(txt.split())})
    # who asks: listed analysts, or anyone introduced ("Our first question comes from the line of X of Y"),
    # wherever the introduction sits (some transcripts print it without an "Operator" label)
    intro, first_intro = {}, None
    for mm in re.finditer(rf"(?:question|line|comes)\s+(?:comes |is |will come )?(?:from|of)\s+(?:the line of\s+)?({_NAME})\s+(?:with|from|of|at)\s+([^\n]{{2,90}})", body):
        firm = re.split(r"\.\s+(?:Your|Please|Go|You)|,\s*(?:please|your)|\s+(?:please|your line)", mm.group(2))[0].strip().rstrip(".")
        intro[mm.group(1).strip().rstrip(",")] = firm[:80]
        first_intro = mm.start() if first_intro is None else first_intro
    # where the questions start: the earliest of a Q&A heading, the operator opening the line, the first introduction
    marks = []
    m = _QA.search(view, len(body) // 20)
    if m:
        marks.append(m.start())
    for t in turns:
        if t["side"] == "operator" and t["offset"] > len(body) * 0.1 and _QA_OPEN.search(body[t["text_offset"]:t["end"]]):
            marks.append(t["offset"])
            break
    if first_intro is not None and first_intro > len(body) * 0.1:
        marks.append(body.rfind("\n", 0, first_intro) + 1)
    qa = min(marks) if marks else None
    company_seen = set()
    for t in turns:
        if t["side"] == "unknown":
            if t["name"] in intro:
                t["side"], t["firm"], t["role"] = "analyst", intro[t["name"]], "Analyst"
            elif qa is not None and t["offset"] < qa:
                t["side"] = "company"
        if t["side"] == "company":
            company_seen.add(t["name"])
        if t["side"] == "analyst" and not t["firm"] and t["name"] in intro:
            t["firm"] = intro[t["name"]]
    for t in turns:                     # after the questions start, someone never introduced who asks a question is an analyst
        if t["side"] == "unknown":
            asks = "?" in body[t["text_offset"]:t["end"]]
            t["side"] = "company" if t["name"] in company_seen or not asks else "analyst"
    speakers = {}
    for t in turns:
        s = speakers.setdefault(t["name"], {"name": t["name"], "side": t["side"], "role": t["role"], "firm": t["firm"], "turns": 0, "words": 0, "offset": t["offset"]})
        s["turns"] += 1
        s["words"] += t["words"]
    # analyst questions: the sentences that end in a question mark, whole
    questions = []
    for t in turns:
        if t["side"] != "analyst" or (qa is not None and t["offset"] < qa):
            continue
        seg_s, seg_e = t["text_offset"], t["end"]
        qs = []
        for qm in re.finditer(r"\?", body[seg_s:seg_e]):
            s, e = sentence_at(body, seg_s + qm.start())
            s, e = max(s, seg_s), min(e, seg_e)
            if not qs or s >= qs[-1][1]:
                qs.append((s, e))
            if len(qs) >= 2:
                break
        if not qs:
            if t["words"] < 25:
                continue           # "Thanks, that's all from me"
            qs = [(seg_s, min(seg_e, seg_s + 400))]
        text = _ws(" ".join(body[s:e] for s, e in qs))
        if len(text) < 12:
            continue
        questions.append({"analyst": t["name"], "firm": t["firm"], "text": text[:600], **_loc(doc, qs[0][0]), "end": qs[-1][1], "turn_offset": t["offset"]})
    # management statements: guidance and claims, by company speakers only
    guid, claims = [], []
    claim_rx = re.compile(r"(?i)\b(?:record|all-time|highest|strongest|best|largest|grew|growth of|increased|up \d|doubled|tripled|accelerat\w+|exceeded|beat|outperform\w*|market share|margin expansion)\b")
    for t in turns:
        if t["side"] != "company":
            continue
        seg_s, seg_e = t["text_offset"], t["end"]
        pos = seg_s
        while pos < seg_e:
            s, e = sentence_at(body, pos)
            if e <= pos:
                e = pos + 1
            s, e = max(s, seg_s), min(e, seg_e)
            txt = _ws(body[s:e])
            pos = e + 1
            if len(txt) < 35 or _BOILER.search(txt):
                continue
            item = {"speaker": t["name"], "role": t["role"], "text": txt[:500], **_loc(doc, s), "end": e}
            if _GUIDE.search(txt) and _GNUM.search(txt) and _PERIOD.search(txt) and not _ACCOUNTING.search(txt) \
                    and not re.search(r"(?i)\b(?:totaled|was|were) \$", txt[:80]):
                item["direction"] = next((d for d, rx in _DIRECTION if rx.search(txt)), "stated")
                guid.append(item)
            elif claim_rx.search(txt) and re.search(r"\d", txt):
                claims.append(item)
    return {"speakers": sorted(speakers.values(), key=lambda s: s["offset"]), "qa_offset": qa, "qa_page": doc.page_at(qa) if qa is not None else None,
            "turns": len(turns), "questions": questions[:40], "guidance": guid[:25], "claims": claims[:25],
            "participants_listed": len(known), "company": _call_company(doc, known, view)}


# ---------------------------------------------------------------------- the filing map
def section_map(doc, kind: str, rep: Optional[dict] = None) -> list[dict]:
    out = []

    def add(key, label, off, page=None):
        if off is None or off < 0:
            return
        out.append({"key": key, "label": label, "offset": off, "page": page or doc.page_at(off),
                    "excerpt": _ws(doc.body[off:off + 80])})

    secs = {s.item: s for s in doc.sections}
    if kind == "transcript":
        tr = (rep or {}).get("_call") or {}
        add("remarks", "Prepared remarks", 0)
        if tr.get("qa_offset") is not None:
            add("qa", "Questions and answers", tr["qa_offset"])
        return out
    if kind in ("annual", "quarterly", "other", "release"):
        if "1" in secs and kind != "quarterly" and re.match(r"(?i)business", secs["1"].title):
            add("business", "Business", secs["1"].offset)
        if "1A" in secs:
            add("risks", "Risk factors", secs["1A"].offset)
        mdna = secs.get("7") if kind != "quarterly" else next((s for s in doc.sections if s.item == "2" and re.search(r"(?i)discussion", s.title)), None)
        if mdna is None:
            mdna = next((s for s in doc.sections if re.search(r"(?i)management.s discussion", s.title)), None)
        if mdna:
            add("mdna", "MD&A", mdna.offset)
        m = re.search(r"(?im)^[ \t]*(?:[A-Za-z0-9 ,&.]{0,40})?(?:outlook|guidance)\b[^\n]{0,50}$", doc.body) if kind == "release" else None
        if m:
            add("outlook", "Outlook", m.start())
    # the statements: the first page that holds a primary statement fact
    st_page = None
    if rep:
        F = (rep.get("evidence") or {}).get("facts") or {}
        prim = [f for f in F.values() if (f.get("source") or {}).get("pdf_page") and (f.get("source") or {}).get("statement") in ("income", "balance", "cashflow")]
        cons = [f for f in prim if re.match(r"(?i)(condensed )?consolidated", (f["source"].get("table_title") or ""))]
        pages = [f["source"]["pdf_page"] for f in (cons or prim)]
        if pages:
            st_page = min(pages)
            offs = [f["source"].get("offset") for f in (cons or prim) if f["source"]["pdf_page"] == st_page and f["source"].get("offset", -1) >= 0]
            st_off = min(offs) if offs else next((ps for ps, idx in doc.page_starts if idx == st_page), None)
            add("statements", "Statements", st_off, st_page)
    if st_page:
        start = next((ps for ps, idx in doc.page_starts if idx >= st_page), 0)
        m = re.compile(r"(?im)^[ \t]*notes to (?:the )?(?:unaudited )?(?:condensed )?consolidated financial statements\b").search(doc.body, start)
        if m:
            add("notes", "Notes", m.start())
    if "9A" in secs and kind != "release":
        add("controls", "Controls", secs["9A"].offset)
    out.sort(key=lambda x: x["offset"])
    return out


# ---------------------------------------------------------------------- risk factors for year over year
def risk_items(doc) -> list[dict]:
    from . import risks as R
    st, en = R._section_text(doc, "1A")
    if st < 0:
        return []
    found = R.headings(doc)
    return [{"title": f["title"], "category": f["category"], **_loc(doc, f["source"]["offset"])} for f in found]


_STOP = set("a an and are as at be been by can could do for from has have if in into is it its may might more not of on or our over such than that the their them these they this to under was we were which will with would you your".split())


def _words(s: str) -> set:
    return {w for w in re.findall(r"[a-z][a-z'\-]+", s.lower()) if w not in _STOP and len(w) > 2}


def _norm_text(s: str) -> str:
    s = (s or "").lower().replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"').replace("\u2013", "-").replace("\u2014", "-")
    return re.sub(r"\s+", " ", s).strip()


def _section_sentences(body: str, span: Optional[tuple]) -> list[str]:
    a, b = span if span else (0, len(body))
    return [_ws(m.group(0)) for m in re.finditer(r"[^.!?]{30,2500}[.!?]", body[a:b].replace("\n", " "))]


def risk_diff(cur: list[dict], prior: list[dict], cur_body: str = "", prior_body: str = "",
              cur_span: Optional[tuple] = None, prior_span: Optional[tuple] = None) -> dict:
    """New, removed and reworded risk-factor headings between two years' filings. Two headings are the
    same risk when at least half of their content words are shared. A heading is called new only when last
    year's risk-factor text does not contain it (nor a sentence sharing 60% of its words), and no longer listed
    only when this year's text does not: a heading the detector missed in one filing is not a change."""
    def sim(a, b):
        A, B = _words(a), _words(b)
        return len(A & B) / max(1, len(A | B))
    used = set()
    new, changed = [], []
    for c in cur:
        best, bj = 0.0, None
        for j, p in enumerate(prior):
            sc = sim(c["title"], p["title"])
            if sc > best:
                best, bj = sc, j
        if best >= 0.5:
            used.add(bj)
            if best < 0.85:
                changed.append({"now": c, "before": prior[bj], "similarity": round(best, 2)})
        else:
            new.append(c)
    removed = [p for j, p in enumerate(prior) if j not in used]
    checked = bool(cur_body and prior_body)
    if checked:
        pn, cn = _norm_text(prior_body[slice(*prior_span)] if prior_span else prior_body), _norm_text(cur_body[slice(*cur_span)] if cur_span else cur_body)
        psents, csents = _section_sentences(prior_body, prior_span), _section_sentences(cur_body, cur_span)

        def found_in(title, norm, sents):
            t = _norm_text(title)
            if t[:160] in norm:
                return "same", None
            W = _words(title)
            best, bs = 0.0, None
            for snt in sents:
                # how much of the heading the other year's sentence holds: a heading promoted from a paragraph
                # ("gross margins are subject to volatility ..." gaining "net sales and") is reworded, not new
                sc = len(W & _words(snt)) / max(1, len(W))
                if sc > best:
                    best, bs = sc, snt
            return ("reworded", (bs, round(best, 2))) if best >= 0.75 else (None, None)
        keep_new = []
        for c in new:
            how, got = found_in(c["title"], pn, psents)
            if how == "reworded":
                changed.append({"now": c, "before": {"title": got[0], "page": None}, "similarity": got[1], "basis": "text"})
            elif how is None:
                keep_new.append(c)
        keep_removed = []
        for p in removed:
            how, got = found_in(p["title"], cn, csents)
            if how == "reworded":
                changed.append({"now": {"title": got[0], "page": None}, "before": p, "similarity": got[1], "basis": "text"})
            elif how is None:
                keep_removed.append(p)
        new, removed = keep_new, keep_removed
    return {"new": new, "removed": removed, "reworded": changed, "counts": {"now": len(cur), "before": len(prior)}, "text_checked": checked}


# ---------------------------------------------------------------------- ask a question
_SYN = {
    "revenue": ["revenue", "revenues", "sales", "net sales", "total revenues", "top line", "turnover"],
    "net_income": ["net income", "profit", "earnings", "net earnings", "bottom line", "made"],
    "eps_diluted": ["eps", "earnings per share", "diluted", "per share"],
    "operating_income": ["operating income", "operating profit", "ebit", "income from operations"],
    "gross_profit": ["gross profit", "gross margin"],
    "cash": ["cash", "cash and cash equivalents"],
    "total_debt": ["debt", "borrowings", "notes payable", "owe", "loans"],
    "cfo": ["operating cash flow", "cash from operations", "cash flow"],
    "capex": ["capex", "capital expenditures", "property and equipment", "spend", "spent"],
    "fcf": ["free cash flow", "fcf"],
    "buybacks": ["buyback", "buybacks", "repurchase", "repurchases", "repurchased"],
    "dividends": ["dividend", "dividends"],
    "total_assets": ["assets", "total assets"],
    "equity_total": ["equity", "shareholders equity", "book value"],
}


def _stem(w: str) -> str:
    for suf in ("ies", "es", "s", "ing", "ed"):
        if len(w) > len(suf) + 3 and w.endswith(suf):
            return w[:-len(suf)] + ("y" if suf == "ies" else "")
    return w


_QWORDS = set("how many much what which who whom whose when where why does do did is are was were say says said tell about the company company's "
              "their there any total biggest largest main major key top most important primary".split())


def _tok(s: str, drop: frozenset = frozenset()) -> list[str]:
    words = (w.rstrip(".'-") for w in re.findall(r"[a-z0-9][a-z0-9'\-.]*", s.lower().replace("\u2019", "'")))
    return [_stem(w) for w in words if w not in _STOP and w not in drop and len(w) > 1]


_TEXT_SYN = {"buyback": ("repurchase", "repurchased"), "repurchase": ("buyback",), "staff": ("employee",), "headcount": ("employee",),
             "worker": ("employee",), "workforce": ("employee",), "people": ("employee",), "sale": ("revenue",), "revenue": ("sale",),
             "profit": ("income", "earning"), "earning": ("income",), "ceo": ("chief", "executive"), "cfo": ("chief", "financial"),
             "debt": ("borrowing", "note"),
             "lawsuit": ("litigation", "proceeding"), "suit": ("litigation",), "dividend": ("dividends",), "competitor": ("competition", "competitive"),
             "tariff": ("trade",), "plant": ("facility", "manufacturing"), "factory": ("facility", "manufacturing")}


class Index:
    """A sentence index over one document for questions (BM25). Sentences are found the way quotes are
    (sentence_at), so a wrapped PDF line does not cut one in two."""

    def __init__(self, body: str, page_starts: list, spans: Optional[list] = None):
        self.body, self.page_starts = body, page_starts
        self.spans = spans or []               # (start, end, key) of the filing's sections, from the map
        self.sents, self.table = [], []
        i, n = 0, len(body)
        while i < n:
            while i < n and body[i].isspace():
                i += 1
            if i >= n:
                break
            s, e = sentence_at(body, i)
            if e <= i:
                e = body.find("\n", i)
                e = n if e < 0 else e
            s = max(s, i)
            txt = _ws(body[s:e])
            if len(txt) >= 25:
                self.sents.append((s, txt))
                self.table.append(bool(_TABLE_LINE.search(body[s:e])) and len(re.findall(r"\d[\d,.]*", txt)) >= 3)
            i = e + 1
        self.toks = [Counter(_tok(t)) for _, t in self.sents]
        self.df = Counter()
        for c in self.toks:
            self.df.update(c.keys())
        self.avg = sum(sum(c.values()) for c in self.toks) / max(1, len(self.toks))

    def page_at(self, off: int):
        pg = None
        for ps, idx in self.page_starts:
            if ps <= off:
                pg = idx
            else:
                break
        return pg

    def search(self, q: str, k: int = 3, drop: frozenset = frozenset()) -> list[dict]:
        qt = [t for t in _tok(q, drop | _QWORDS)]
        weight = {t: 1.0 for t in qt}
        groups = [{t, *_TEXT_SYN.get(t, ())} for t in qt]   # each word asked, with its synonyms
        for t in list(qt):
            for x in _TEXT_SYN.get(t, ()):
                if x not in weight:
                    qt.append(x)
                    weight[x] = 0.4            # a synonym counts, less than the word asked
        numeric = bool(re.search(r"(?i)\bhow (?:many|much)\b|\bnumber of\b|\bwhat (?:was|is|were|are) (?:the )?(?:total|amount)|\bbiggest\b|\blargest\b", q))
        noun = re.search(r"(?i)\bhow many ([a-z]+)", q)
        risky = bool(re.search(r"(?i)\brisk", q))
        outlook = bool(re.search(r"(?i)\b(?:guidance|outlook|expect|forecast|next year)", q))
        count_rx = re.compile(r"(?i)(?<![\d,.])(?!(?:19|20)\d\d\b)\d[\d,.]*\+?\s+(?:[\w-]+\s+){0,3}" + re.escape(_stem(noun.group(1).lower())[:6])) if noun else None
        n = len(self.toks)
        scores = []
        for i, c in enumerate(self.toks):
            if not any(t in c for t in qt):
                continue
            L = sum(c.values())
            sc = 0.0
            for t in qt:
                f = c.get(t, 0)
                if not f:
                    continue
                idf = math.log(1 + (n - self.df[t] + 0.5) / (self.df[t] + 0.5))
                sc += weight[t] * idf * f * 2.2 / (f + 1.2 * (0.25 + 0.75 * L / max(1, self.avg)))
            if len(groups) > 1:                 # a sentence that answers every part of the question comes first
                sc *= (sum(1 for g in groups if any(x in c for x in g)) / len(groups)) ** 2
            if self.table[i] and not numeric:
                sc *= 0.5                       # a table row answers "how much", not "what does it say"
            if numeric:
                sc *= 1.6 if re.search(r"\d", self.sents[i][1]) else 0.6
            if self.spans and (risky or outlook):
                off = self.sents[i][0]
                key = next((k for a, b, k in self.spans if a <= off < b), "")
                if (risky and key == "risks") or (outlook and key in ("mdna", "outlook")):
                    sc *= 1.6                   # a risk question is answered first from Risk Factors
            if count_rx and count_rx.search(self.sents[i][1]):
                sc *= 2.5                       # "166,000 full-time equivalent employees" answers "how many employees"
            if len(self.sents[i][1]) < 50:
                sc *= 0.7                       # a heading or table label, not an answer
            if sc > 0:
                scores.append((sc, i))
        scores.sort(reverse=True)
        out, seen = [], set()
        for sc, i in scores:
            off, txt = self.sents[i]
            key = txt[:80].lower()
            if key in seen:
                continue                        # the same sentence printed twice
            seen.add(key)
            out.append({"kind": "text", "text": txt[:500], "offset": off, "page": self.page_at(off), "score": round(sc, 2)})
            if len(out) >= k:
                break
        return out


def spans_from_map(m: list, length: int) -> list:
    pts = sorted((x["offset"], x["key"]) for x in m or [] if x.get("offset") is not None)
    return [(o, pts[k + 1][0] if k + 1 < len(pts) else length, key) for k, (o, key) in enumerate(pts)]


def ask(question: str, rep: dict, index: Index) -> dict:
    q = (question or "").strip()[:300]
    if not q:
        return {"question": q, "answers": [], "note": "Type a question."}
    low = q.lower()
    F = (rep.get("evidence") or {}).get("facts") or {}
    C = (rep.get("evidence") or {}).get("calcs") or {}
    answers = []
    # a figure from the statements when the question names one
    want = [m for m, words in _SYN.items() if any(re.search(rf"\b{re.escape(w)}\b", low) for w in words)]
    if re.search(r"\bmargin\b", low):
        want = [w for w in want if w != "gross_profit"] + ["gross_margin", "operating_margin", "net_margin"] if "gross" not in low else ["gross_margin"]
    years = re.findall(r"\b(?:19|20)\d\d\b", q)
    cands = []
    for x in list(F.values()) + list(C.values()):
        if x.get("status") == "Unavailable" or x.get("value") is None:
            continue
        if x.get("metric") in want:
            per = x.get("period") or {}
            lab = " ".join(str(per.get(k) or "") for k in ("label", "short", "end"))
            yr_ok = not years or any(y in lab for y in years)
            if not yr_ok:
                continue
            score = 10 - want.index(x["metric"]) + (1 if x.get("kind") != "calculated" else 0)
            cands.append((score, per.get("end") or "", x))
    cands.sort(key=lambda t: (t[0], t[1]), reverse=True)
    seen = set()
    for score, _, x in cands:
        if x["metric"] in seen:
            continue
        seen.add(x["metric"])
        src = x.get("source") or {}
        if not src.get("pdf_page") and x.get("inputs"):
            ins = [F.get(i) for i in x["inputs"] if F.get(i)]
            src = (ins[0].get("source") if ins else {}) or {}
        answers.append({"kind": "fact", "label": x.get("name"), "value": x.get("display"), "period": (x.get("period") or {}).get("label"),
                        "page": src.get("pdf_page"), "raw": src.get("raw"), "excerpt": (src.get("excerpt") or "")[:220], "offset": src.get("offset"),
                        "formula": x.get("formula"), "status": x.get("status")})
        if len(answers) >= 2:
            break
    co = ((rep.get("company") or {}).get("name") or "").lower()
    drop = frozenset(w for w in re.findall(r"[a-z]+", co) if w not in ("inc", "corp", "co", "the"))
    answers += index.search(q, 3, drop)
    note = "" if answers else "Not found in this document. The answer comes only from the document that was read."
    return {"question": q, "answers": answers, "note": note}


# ---------------------------------------------------------------------- documents this server writes
def _rows(doc) -> list[tuple[int, list[str]]]:
    out, pos = [], 0
    for line in doc.body.split("\n"):
        cells = [c.strip() for c in re.split(r" {2,}", line.strip()) if c.strip()]
        out.append((pos, cells))
        pos += len(line) + 1
    return out


# the full-width line above each filing's transactions in a generated insider-trades document
_FILED_BY = re.compile(r"^Filed (\d{4}-\d\d-\d\d) by (.+?)(?: \(([^()]*)\))?, filing (\d{10}-\d\d-\d{6})$")


def insider(doc) -> dict:
    """Trades listed in a generated insider-trades document: a line per filing (date, insider, role, accession),
    then one row per transaction."""
    rows, trades = _rows(doc), []
    head = filing = None
    for off, cells in rows:
        if cells[:3] == ["Trade date", "Code", "Action"]:
            head = cells
            continue
        if not head:
            continue
        m = _FILED_BY.match(cells[0]) if len(cells) == 1 else None
        if m:
            filing = m
            continue
        if filing and len(cells) == len(head) and re.match(r"(\d{4}-\d\d-\d\d|n/a)$", cells[0]):
            r = dict(zip(head, cells))
            trades.append({"filed": filing[1], "date": r["Trade date"], "insider": filing[2], "title": filing[3] or "", "code": r["Code"],
                           "action": r["Action"], "shares": r["Shares"], "price": r["Price"], "value": r["Value"],
                           "owned_after": r["Owned after"], "accession": filing[4], **_loc(doc, off)})
    return {"trades": trades}


def holdings(doc) -> dict:
    rows, out = _rows(doc), []
    head = None
    for off, cells in rows:
        if cells[:2] == ["Rank", "Issuer"]:
            head = cells
            continue
        if head and len(cells) == len(head) and cells[0].isdigit():
            r = dict(zip(head, cells))
            m = re.match(r"(.*?) \(([^()]*)\)$", r["Issuer"])          # "ALPHABET INC (CAP STK CL C)": the class rides with the issuer
            out.append({"rank": int(r["Rank"]), "issuer": m[1] if m else r["Issuer"], "class": m[2] if m else "", "value": r.get("Value"),
                        "shares": r.get("Shares"), "weight": r.get("Weight"), "change": r.get("Change"), **_loc(doc, off)})
    return {"holdings": out}


# ---------------------------------------------------------------------- all of it
def build(doc, rep: dict, title: str = "") -> dict:
    kind = kind_of(doc, title)
    out = {"version": 1, "kind": kind, "label": KIND_LABEL[kind]}
    try:
        out["guidance"] = guidance(doc, kind)
    except Exception as e:                  # an analysis failure never fails the read
        out["guidance"], out.setdefault("errors", []).append(f"guidance: {e.__class__.__name__}")
    try:
        out["red_flags"] = red_flags(doc) if kind not in ("insider", "holdings") else []
    except Exception as e:
        out["red_flags"], out.setdefault("errors", []).append(f"red flags: {e.__class__.__name__}")
    if kind == "transcript":
        try:
            out["call"] = transcript(doc)
        except Exception as e:
            out.setdefault("errors", []).append(f"transcript: {e.__class__.__name__}")
    if kind in ("annual", "quarterly", "other"):
        try:
            out["risk_items"] = risk_items(doc)
        except Exception as e:
            out.setdefault("errors", []).append(f"risk factors: {e.__class__.__name__}")
    if kind == "insider":
        out.update(insider(doc))
    if kind == "holdings":
        out.update(holdings(doc))
    try:
        out["map"] = section_map(doc, kind, {**rep, "_call": out.get("call") or {}})
    except Exception as e:
        out["map"], out.setdefault("errors", []).append(f"map: {e.__class__.__name__}")
    return out
