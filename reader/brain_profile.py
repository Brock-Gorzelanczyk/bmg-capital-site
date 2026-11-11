"""About a company, for its page in the brain: who runs it, how many people it employs, where it is based, when it was
founded and what it does, each with where it came from.

Three sources, in this order:
- the company's latest annual filing (the Crawler's saved read of it, else the 10-K itself from SEC EDGAR): the chief
  executive from the signature page, the head count, the year it says it was founded and the opening of Item 1, each
  with its page and the words it was read from. PRIMARY.
- SEC EDGAR's own record of the company (the submissions feed): business address, exchange, state of incorporation,
  fiscal year end, industry code. PRIMARY.
- Wikidata and Wikipedia: the year founded, a short description and the website, and the chief executive or the head
  count only when the filing gave none. SECONDARY, shown as such.
Nothing is guessed: a field no source gives says NOT FOUND. Named brain_* so a change here does not make My reads read
every saved file again.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import re
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

WD_UA = {"User-Agent": "company-page-reader/1.0 (local desktop research tool)", "Accept": "application/json"}
WD_CONTACT = ".bmg-wikimedia-contact"      # in the home folder, when the owner chooses to name a contact (see _ua)
WD_API = "https://www.wikidata.org/w/api.php"
WP_SUMMARY = "https://en.wikipedia.org/api/rest_v1/page/summary/"
WD_TTL = 30 * 86400               # a company's Wikidata record is read again after a month
WD_MISS_TTL = 7 * 86400           # Wikidata has no record of it: asked again after a week
WD_FAIL_TTL = 15 * 60             # Wikidata did not answer (busy, offline): asked again after a quarter of an hour
TIMEOUT = 8.0
EXCHANGES = {"NASDAQ": "Q82059", "NYSE": "Q13677"}     # Wikidata's items for the two exchanges, to find a company by ticker

_wd_fail: dict = {}               # ticker -> when Wikidata last failed to answer


# ---------------------------------------------------------------------------------------------------------------------
# the annual filing
_AP = "'’"
_SENT_END = re.compile(r"(?:(?<=[.!?][\"”)])|(?<=[.!?]))\s+(?=[A-Z“\"(])")      # after the full stop, or after the quote mark or bracket that closes on it
_STARTS = re.compile(r"(?:We|Our|The|This|These|It|In|As|At|For|On|Through|Under|During|Since) ")
_ABBR = re.compile(r"(?:\b(?:Inc|Corp|Co|Ltd|Mr|Mrs|Ms|Dr|Jr|Sr|St|No|U\.S|U\.K|N\.A|L\.P|S\.A|vs|approx|e\.g|i\.e)|\b[A-Z]|"
                   r"\b(?:Items?|Notes?|Part|Section)\s+[\dIVX]+[A-Za-z]?)\.[\"”)]?$")      # "(U.S.)" and "Item 1A." end no sentence


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def _sentences(text: str) -> list[str]:
    """Sentences of a paragraph; a full stop after an initial or an abbreviation ("Inc.", "U.S.") does not end one."""
    out, cur = [], ""
    parts = _SENT_END.split(_ws(text))
    for k, part in enumerate(parts):
        cur = f"{cur} {part}".strip() if cur else part
        nxt = parts[k + 1] if k + 1 < len(parts) else ""
        if _ABBR.search(cur) and not _STARTS.match(nxt):
            continue                                 # "Prologis, Inc. is", "John W. Ketchum": not an end ("L.P. We invest" is one)
        out.append(cur)
        cur = ""
    if cur:
        out.append(cur)
    return out


def _sentence_around(text: str, a: int, b: int, limit: int = 420) -> str:
    """The sentence that holds text[a:b], for showing where a figure was read."""
    lo = max(0, a - limit)
    left = text[lo:a]
    cut = max(left.rfind(". "), left.rfind(".\n"), left.rfind("\n\n"), left.rfind(";\n"))
    start = lo + cut + 1 if cut >= 0 else (lo if lo else 0)
    right = text[b:b + limit]
    m = re.search(r"[.!?](?=\s|$)|\n\n", right)
    end = b + (m.end() if m else len(right))
    return _ws(text[start:end])[:limit]


_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(\s*(?:million|thousand))?(?![\d.,]*\s*(?:%|percent))"
_ABOUT = r"(?:a total of\s+)?(?:approximately|about|over|more than|nearly|roughly|around|some|almost)?\s*"
_HEAD_NOUN = r"employees|team members|associates|colleagues|teammates|crew members|personnel|professionals|people|persons|workers|individuals|partners|staff"
_SOFT_NOUN = {"people", "persons", "workers", "individuals", "partners", "staff", "professionals"}   # need a word about employing nearby
_FILL = r"(?:(?:full|part)[- ]?time|equivalent|and|regular|total|active|permanent|global|worldwide|salaried|hourly|seasonal|temporary|non-union|talented|dedicated|u\.s\.|fte|\(fte\)),?"
# the ways a filing states a head count: (pattern, the group that holds the number, the kind)
_COUNTS = [
    (re.compile(r"(?i)(?<![\d,.$%])" + _NUM + r"\+?\s+and\s+(?:approximately\s+|about\s+)?(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?:\s*(?:million|thousand))?\s+(?:" + _FILL + r"\s+){0,5}(" + _HEAD_NOUN + r")\b,?\s+respectively"), 1, "first"),
    (re.compile(r"(?i)(?<![\d,.$%])" + _NUM + r"\+?\s+((?:" + _FILL + r"\s+){0,6})(" + _HEAD_NOUN + r")\b"), 1, "noun"),
    (re.compile(r"(?i)\b(?:employed|employs|employ)\s+" + _ABOUT + _NUM), 1, "verb"),
    (re.compile(r"(?i)\b(?:workforce|employee population|employee base|head ?count|team|global community)\b[^.;\n]{0,90}?\b(?:consisted of|consists of|comprised of|comprises|comprised|totaled|numbered|was|included|includes|of)\s+" + _ABOUT + _NUM), 1, "verb"),
    (re.compile(r"(?i)\b[Tt]he (?:total )?number of (?:regular |full[- ]time |total |active )?employees\s+(?:was|were|totaled|is)\s+" + _ABOUT + _NUM), 1, "stated"),
    (re.compile(r"(?i)\bemployees\b[^.;\n]{0,80}?\b(?:totaled|numbered)\s+" + _ABOUT + _NUM), 1, "verb"),
]
_EMPLOY = re.compile(r"(?i)\bemploy|\bworkforce\b|\bhead ?count\b|\bour people\b|\bour team\b|\bteam members\b|\bassociates\b|\bcolleagues\b")
_WE = re.compile(r"(?i)\b(?:we|our|us|the company|the corporation|the firm|the registrant)\b")
_DATED = re.compile(r"(?i)\bas of\b|\bat (?:december|january|february|march|april|may|june|july|august|september|october|november)\b|\bat (?:the end of|year[- ]end|fiscal)\b"
                    r"|\bas at\b|\bat the close of\b|\bat years? ended\b|\byear[- ]end\b|\bend of (?:fiscal )?\d{4}\b")
_WIDE = re.compile(r"(?i)\bworldwide\b|\bglobally\b|\baround the world\b|\bin \d+ countries\b|\bin total\b|\btotal\b")
_PART = re.compile(r"(?i)^\W*(?:were|are|was|is)\s+(?:represented|covered|engaged|located|based|unionized|members of)|^\W*(?:represented by|covered by|under collective|outside|participat|completed|took part|responded"
                   r"|union|unionized|bargaining|leveraged|used|enrolled)|^\W*,?\s*or \d+(?:\.\d+)?\s*%")
_POSSESSIVE = re.compile(r"([A-Z][\w&.\-]*(?:\s+[A-Z&][\w&.\-]*){0,3})[’']s\s+(?:total |global |worldwide |employee |approximately )*(?:workforce|employees|head ?count|employee base|employee population|team)\b[^.;]{0,60}$")
_CUT = re.compile(r"(?i)(?:of whom|of which|of these|including|impact\w+|affect\w+|represent\w+|cover\w+|serv\w+|pay\w*|paid|benefit\w*|customers?|clients?|employers?|"
                  r"participants?|members|\d+\s*%\s*of|compared (?:to|with))\W+(?:\w+\W+){0,2}$")
_CHANGE = re.compile(r"(?i)\b(?:reduc\w+|eliminat\w+|terminat\w+|lay ?offs?|laid off|restructur\w+|severance|hired|hiring|added|cut|cuts|cutting)\b")   # a change in the work force, not its size
_UNIT = re.compile(r"(?i)\b(?:segment|division|business unit|franchisees?|contractors|service providers|joint venture)\b[^.;]{0,70}$")
_HC_HEAD = re.compile(r"(?im)^\W*(?:human capital(?: management| resources)?(?: and management)?|employees|our people|our employees|workforce|team members|our team|associates|people and culture|our workforce)\W*$")
_PAIR = re.compile(r"(?i)" + _NUM + r"\s*(?:\(\d+(?:\.\d+)?%\)\s*)?(?:permanent\s+|regular\s+)?(full[- ]?time|hourly)\b[^.;]{0,90}?\band\b[^.;]{0,30}?(?<![\d,.])"
                   + _NUM + r"\s*(?:\(\d+(?:\.\d+)?%\)\s*)?(?:permanent\s+|regular\s+)?(part[- ]?time|salaried)\b")
_SUBJECT = re.compile(r"([A-Z][\w&.’'\-]*(?:\s+(?:[A-Z&][\w&.’'\-]*|and|of|the)){0,5})\s+(?:(?:system|group)\s+)?(?:and (?:its )?(?:consolidated |operating )?(?:subsidiaries|affiliates)\s+)?"
                      r"(?:had|has|have|employed|employs)\b[^.;]{0,150}$")


def _count(m, g: int = 1) -> float:
    v = float(m.group(g).replace(",", ""))
    unit = (m.group(g + 1) or "").lower()
    return v * (1e6 if "million" in unit else 1e3 if "thousand" in unit else 1)


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


_CORP_WORDS = {"the", "inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd", "limited", "holdings", "holding", "group", "nv", "sa",
               "lp", "llc", "new", "de", "md", "and", "&", "of"}


def _name_toks(s: str) -> list:
    s = re.sub(r"[/\\][A-Za-z]{2,4}[/\\]?\s*$", " ", s or "")          # SEC's own tags on a name: "COSTCO WHOLESALE CORP /NEW", "\DE\"
    return [w for w in re.sub(r"[^a-z0-9& ]", "", s.lower().replace("-", " ")).split() if w not in _CORP_WORDS]


def _is_company(subject: str, names: list) -> bool:
    """The words name the company itself ("JPMorganChase", "Citi", "The Southern Company system", "Costco Wholesale
    Corporation"), not one of its units ("FPL", "BNSF Railway", "Berkshire's insurance businesses", "fairlife, LLC").
    Words before the name on the same line (a heading run into it) are passed over."""
    words = re.sub(r"^(?:As of|At|In|During|With)\b.*?,\s*", "", subject or "").split()
    for k in range(len(words)):
        subj = _name_toks(" ".join(words[k:]))
        tail = []
        while subj and subj[-1] in ("system", "companies", "consolidated", "subsidiaries"):
            tail.append(subj.pop())
        if not subj:
            continue
        sq = "".join(subj)
        for n in names or []:
            nm = _name_toks(n)
            if not nm:
                continue
            nq = "".join(nm)
            if subj == nm or sq == nq:
                return True                          # the name, however it is spaced ("JPMorganChase")
            if len(n or "") <= 5 and sq == _squash(n):
                return True                          # its ticker ("NEE had approximately ...")
            if len(sq) >= 4 and (nm[:len(subj)] == subj or (len(subj) == 1 and nq.startswith(sq))):
                return True                          # the name's first words ("Berkshire", "Costco"), or its short form ("Citi")
    return False


def _named(before: str, names: list) -> bool:
    m = _SUBJECT.search(before)
    return bool(m) and _is_company(m.group(1), names)


def employees(text: str, sections: Optional[list] = None, names: Optional[list] = None) -> Optional[dict]:
    """How many people the company employs, as its annual filing says it in a sentence: {value, quote, offset, parts},
    or None when no sentence states the company's own head count plainly. A part of the work force is passed over: one
    unit's or one subsidiary's people ("FPL had approximately 9,400 employees"), the union members, the people let
    go, a customer's employees, the people who took a survey. Full-time and part-time counts stated side by side are
    added (parts says so). names: what the company is called (name and ticker), for a sentence that names it."""
    if not text:
        return None
    secs = sorted(((s.get("offset") or 0, str(s.get("item") or "")) for s in sections or []), key=lambda x: x[0])
    biz = next(((o, next((o2 for o2, _i in secs if o2 > o), len(text))) for o, i in secs if i == "1"), None)
    heads = [m.start() for m in _HC_HEAD.finditer(text)]
    cands, firsts = [], []
    for rx, g, kind in _COUNTS:
        for m in rx.finditer(text):
            n = _count(m, g)
            if not (20 <= n <= 5_000_000) or (re.fullmatch(r"(?:19|20)\d\d", m.group(g)) and not m.group(g + 1)):
                continue
            a, b = m.start(g), m.end()
            if kind == "first":
                firsts.append((m.start(), m.end()))  # "65,900 and 69,700 employees, respectively": the first is this year's
            elif any(x <= a < y for x, y in firsts):
                continue
            ls = max(text.rfind(". ", 0, a), text.rfind("\n", 0, a), a - 320) + 1
            end = re.search(r"\.(?=\s|$)|\n", text[b:b + 320])
            le = b + (end.start() if end else 320)
            before, after = text[ls:a], text[b:le]
            noun = (m.group(m.lastindex) or "").lower() if kind in ("noun", "first") else ""
            # whose head count is it? the company's own: "we", "our", "the Company", the company by name, or said
            # right after the count ("With about 169,000 employees worldwide, the Company ...", "are employed by Newmont")
            lead = text[ls:m.start(g)]
            poss = _POSSESSIVE.search(lead)
            own = kind == "stated" or bool(_WE.search(before)) or _named(lead, names or []) or (bool(poss) and _is_company(poss.group(1), names or []))
            if not own:
                tail = re.match(r"[^.;\n]{0,90}?(?:,\s+|\bemployed by\s+)((?:[Tt]he |[Oo]ur )?[A-Z][\w&.’'\-]*(?:\s+[A-Z&][\w&.’'\-]*){0,3}|we\b|us\b)", after)
                own = bool(tail) and (tail.group(1).lower() in ("we", "us", "the company", "our company") or _is_company(tail.group(1), names or []))
            if not own or _UNIT.search(before) or _PART.search(after) or _CUT.search(before[-60:]) or _CHANGE.search(before[-110:]):
                continue
            if noun in _SOFT_NOUN and not _EMPLOY.search(before + after[:80]):
                continue
            sc = 4.0
            if _DATED.search(before) or _DATED.search(after[:90]):
                sc += 2
            if (biz and biz[0] <= a < biz[1]) or any(0 <= a - h <= 2500 for h in heads):
                sc += 2
            if _WIDE.search(before[-80:] + text[a:b] + after[:60]):
                sc += 1
            cands.append((sc, n, a, b, ls, le))
    if not cands:
        return None
    top = max(c[0] for c in cands)
    _sc, n, a, b, ls, le = max((c for c in cands if c[0] >= top - 1), key=lambda c: (c[1], -c[2]))    # the largest of the plain statements is the total
    out = {"value": int(round(n)), "quote": _ws(text[ls:le + 1])[:420], "offset": a}
    pair = _PAIR.search(text[ls:le])
    if pair:
        x, y = _count(pair, 1), _count(pair, 4)
        if int(round(n)) in (int(round(x)), int(round(y))):
            out["value"] = int(round(x + y))         # "167,000 full-time associates and 109,000 part-time associates": both, added
            out["parts"] = [{"value": int(round(x)), "what": pair.group(3).lower()}, {"value": int(round(y)), "what": pair.group(6).lower()}]
    return out


_TITLE = {"president", "chief", "executive", "officer", "chairman", "chairwoman", "chairperson", "chair", "director", "directors", "and", "of", "the",
          "board", "founder", "co-founder", "cofounder", "vice", "senior", "principal", "interim", "acting", "global", "group", "managing", "trustee",
          "member", "lead", "ceo", "co-chief", "co-ceo", "co-president", "co-chairman", "co-principal", "&", "an", "in", "his", "her",
          "capacity", "as", "our", "its", "company", "registrant", "trust", "manager", "general", "partner"}
_LABEL = {"title", "name", "by", "date", "signature", "signatures", "capacity", "dated"}      # a form's own labels ("Title: Director, Chairman and ...")
_MONTHS = {"january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"}
_NOT_NAME = {"inc", "corporation", "corp", "company", "co", "llc", "ltd", "plc", "registrant", "pursuant", "report", "form", "annual", "behalf",
             "attorney", "power", "act", "exchange", "securities", "section", "undersigned", "authorized", "duly", "thereunto", "indicated", "persons",
             "following", "below", "above", "exhibit", "certification", "attorney-in-fact", "each", "person", "whose", "appears",
             "for", "see", "item", "part", "note", "notes", "total", "with", "from", "this", "that", "these", "those", "under", "upon", "such", "all",
             "any", "per", "we", "it", "on", "at", "to", "or", "if", "is", "are", "was", "were", "has", "have", "not", "no", "yes", "none", "table",
             "page", "financial", "accounting", "controller", "treasurer", "secretary", "counsel", "legal", "operating", "commercial"} | _LABEL
_NAME_WORD = re.compile(r"[^\W\d_](?:[^\W\d_]|[" + _AP + r"\-])*\.?,?")
_INITIAL = re.compile(r"(?:[^\W\d_]\.)+,?")
_SUFFIX = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_PEO = re.compile(r"(?i)\(?\s*(co-)?principal\s+executive\s+officer")
_CEO = re.compile(r"(?i)\b(co-)?chief\s+executive\s+officer\b")
_SIGNED = re.compile(r"(?i)pursuant to the requirements of (?:section 13 or 15\(d\),? (?:of )?)?the securities (?:exchange )?act of 1934")
_SIG = re.compile(r"(?i)/s/")
_DASHES = str.maketrans({chr(0x2014): " ", chr(0x2013): " ", chr(0x2012): " ", chr(0x2015): " "})   # "Director" + a long dash + "President": two words


def _core(tok: str) -> str:
    return tok.strip("()[];:*").rstrip("*")           # "(Robert", "Stankey*" (signed under a power of attorney)


def _bare(tok: str) -> str:
    return _core(tok).strip(",").rstrip(".").lower()


def _is_name_tok(tok: str, nxt: str = "") -> bool:
    """A word of a person's name: an initial ("A."), or a capitalized word that is not part of a title, a form's
    label, a company's name or an everyday word. "May" is a surname unless a day or a year follows it."""
    core = _core(tok)
    if _INITIAL.fullmatch(core) and core[:1].isupper():
        return True
    if not _NAME_WORD.fullmatch(core) or not core[:1].isupper():
        return False
    b = _bare(tok)
    if b in _MONTHS:
        return not re.match(r"\d", nxt or "")
    return b not in _TITLE and b not in _NOT_NAME


def _skippable(tok: str, nxt: str = "") -> bool:
    """A word between a name and the end of its title, walking back: the title's own words, a label, a dash, a date."""
    core = _core(tok)
    if not re.search(r"[^\W\d_]", tok):
        return True                                  # a dash, a number, a bracket
    if _INITIAL.fullmatch(core):
        return False
    b = _bare(tok)
    return b in _TITLE or b in _LABEL or (b in _MONTHS and bool(re.match(r"\d", nxt or "")))


def _name_before(region: str, p: int) -> Optional[str]:
    """The person named just before the title that ends at region[p]: walking back over the title's own words
    ("President, Chief Executive Officer and Director") to the name they follow."""
    chunk = region[max(0, p - 500):p].translate(_DASHES)
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"\S+", chunk)]
    nxt = lambda k: toks[k + 1][0] if k + 1 < len(toks) else ""
    i = len(toks) - 1
    while i >= 0 and _skippable(toks[i][0], nxt(i)):
        i -= 1
    run = []
    while i >= 0 and len(run) < 8 and _is_name_tok(toks[i][0], nxt(i)):
        run.insert(0, _core(toks[i][0]))
        sep = chunk[toks[i - 1][2]:toks[i][1]] if i > 0 else "\n"
        if "\n" in sep or "\t" in sep or len(sep) > 1:
            break                                    # the name starts its line or its cell of the table
        i -= 1
    return _person(run)


def _name_after(region: str, p: int) -> Optional[str]:
    """A name that follows its title or label: on one of the next three lines, the first "/s/ Name", else a line
    that is a name and nothing else ("Principal Executive Officer:" then "John T. Stankey*")."""
    lines = region[p:p + 320].translate(_DASHES).split("\n")[1:4]
    for ln in lines:
        m = _SIG.search(ln)
        cand = ln[m.end():] if m else ln
        toks, run = cand.split(), []
        for k, tok in enumerate(toks):
            if not _is_name_tok(tok, toks[k + 1] if k + 1 < len(toks) else "") or len(run) >= 8:
                break
            run.append(_core(tok))
        got = _person(run)
        if got and (m or len(run) == len(toks)):
            return got
        if ln.strip() and not m and not all(_skippable(t) for t in toks):
            break                                    # another signer's line, or text: the name is not below
    return None


def _person(run: list) -> Optional[str]:
    while run and run[0].lower().rstrip(".") in {"mr", "ms", "mrs", "dr", "sir", "dame"}:
        run = run[1:]
    half = len(run) // 2
    if half >= 2 and len(run) % 2 == 0 and [t.lower() for t in run[:half]] == [t.lower() for t in run[half:]]:
        run = run[half:] if " ".join(run[half:]) != " ".join(run[half:]).upper() else run[:half]   # "JOHN DOE John Doe": once, as typed
    core = [t for t in run if t.lower().rstrip(".,") not in _SUFFIX]
    if not (2 <= len(core) <= 5) or not any(len(re.sub(r"[\W\d_]", "", t)) >= 3 for t in core):
        return None                                  # "J. Duato" is a name; "J. J." is not
    name = " ".join(run).rstrip(",")
    return name if len(name) <= 48 else None


def _cased(name: str, region: str) -> str:
    """A name signed in capitals ("/s/ JEN-HSUN HUANG") as the filing types it elsewhere ("Jen-Hsun Huang")."""
    if name != name.upper():
        return name
    m = re.search(r"(?i)" + r"\s+".join(re.escape(t) for t in name.split()), region.replace(name, " " * len(name)))
    if m and m.group(0) != m.group(0).upper():
        return _ws(m.group(0))
    out = []
    for t in name.split():
        b = t.rstrip(".,")
        out.append(t if b.lower() in {"ii", "iii", "iv"} or len(b) == 1 else t.title())
    return " ".join(out)


def _last(name: str) -> str:
    toks = [t for t in re.sub(r"[^\w" + _AP + r"\- ]", " ", name or "").split() if t.lower().rstrip(".") not in _SUFFIX]
    return toks[-1].lower() if toks else ""


def _signed_here(region: str, name: str, at: int) -> bool:
    """The name is a signer's: a signature mark stands shortly before its title, or the name is printed twice on the
    page (signed and typed), or the Principal Executive Officer label stands beside it. A name in a glossary or an
    exhibit list that follows the signatures has none of these."""
    if _SIG.search(region[max(0, at - 400):at + 120]):
        return True
    if len(re.findall(r"(?i)" + r"\s+".join(re.escape(t) for t in name.split()), region)) >= 2:
        return True
    return bool(_PEO.search(region[max(0, at - 250):at + 250]))


def chief_executive(text: str) -> Optional[dict]:
    """The chief executive as the annual filing's signature page names them: the signer whose title is Chief Executive
    Officer (the name the title follows), else the one marked Principal Executive Officer: {value, quote, offset}.
    Two names when two people sign as chief executive. None when the signature page does not give one plainly."""
    if not text:
        return None
    half = int(len(text) * 0.4)
    m0 = _SIGNED.search(text, half) or _SIGNED.search(text)
    if m0:
        start = m0.start()
    else:
        heads = [m.start() for m in re.finditer(r"(?m)^\s*SIGNATURES?\s*$", text)]
        start = heads[-1] if heads else max(0, len(text) - 15000)
    region = text[start:start + 9000]
    m1 = _SIGNED.search(region, 60)
    # the company signs first (often by its chief executive), then its officers and directors sign for themselves
    blocks = [(0, m1.start()), (m1.start(), len(region))] if m1 else [(0, len(region))]
    by_title, by_label = [], []
    for a, b in blocks:
        for m in _CEO.finditer(region, a, b):
            lead = region[max(0, m.start() - 40):m.start()].lower()
            if re.search(r"(?:former|deputy|vice|assistant|certification of|to the|of the|by the|by our)\s*$", lead):
                continue
            name = _name_before(region, m.start()) or _name_after(region, m.end())
            if not name or not _signed_here(region, name, m.start()):
                continue
            name = _cased(name, region)
            co = bool(m.group(1)) or bool(_PEO.match(region[m.end():m.end() + 60].lstrip(" \n").replace("and Director", "").lstrip(" \n")))
            if _last(name) not in [_last(f[0]) for f in by_title]:
                if by_title and not (co and by_title[0][2]) and not m.group(1):
                    break                            # a second signer with the title is a co-chief only when both are marked so
                by_title.append((name, m.start(), co))
            if len(by_title) >= 2:
                break
        if by_title:
            break
    for m in _PEO.finditer(region):
        name = _name_before(region, m.start()) or _name_after(region, m.end())
        if name and _signed_here(region, name, m.start()):
            by_label.append((_cased(name, region), m.start(), bool(m.group(1))))
            break
    found = by_title or by_label
    if not found:
        return None
    names = [f[0] for f in found[:2]]
    at = start + found[0][1]
    # the words it was read from: the title with the name beside it, wherever the name stands (before or after)
    spots = [m.span() for m in re.finditer(r"(?i)" + r"\s+".join(re.escape(t) for t in names[0].split()), region)]
    a, b = min(spots, key=lambda sp: abs(sp[0] - found[0][1])) if spots else (found[0][1], found[0][1])
    lo, hi = min(found[0][1] - 110, a - 12), max(found[0][1] + 70, b + 4)
    quote = _ws(region[max(0, lo):hi])
    quote = quote[quote.find(" ") + 1:] if lo > 0 and " " in quote[:20] else quote      # not from the middle of a word
    last = _last(names[0])
    if not last or len(re.findall(r"(?i)\b" + re.escape(last) + r"\b", text)) < 2:
        return None                                  # a name the filing prints only once is not relied on
    return {"value": " and ".join(names), "quote": quote, "offset": at,
            "agrees": (_last(by_label[0][0]) in [_last(n) for n in names]) if (by_title and by_label) else None}


_YEAR = r"((?:1[6-9]|20)\d\d)"
_SUBJ = r"((?:[Ww]e|[Tt]he Company|[Oo]ur [Cc]ompany|[Tt]he Corporation|[Tt]he Firm)|[A-Z][\w&.’'\-]*(?:[ \t]+(?:[A-Z&][\w&.’'\-]*|of|the|and)){0,5})"
_APPOS = r"(?:\s*\([^)]{0,90}\))?(?:,\s+(?:a|an)\s+[^,.;]{2,70}?)?"          # "GATX Corporation ("GATX"), a New York corporation founded in 1898"
# (the filing's word, pattern, the group that holds the subject or 0 when the words themselves name the company)
_FOUNDED = [(w, re.compile(p), g) for w, p, g in (
    ("founded", _SUBJ + r"\s+(?:was|were|has been)\s+(?:originally\s+|first\s+)?(?:founded|established)\s+(?:in\s+[A-Z][\w ,]{2,40}\s+)?in\s+(?:[A-Z][a-z]+\s+)?" + _YEAR, 1),
    ("founded", _SUBJ + _APPOS + r",?\s+(?:a|an)\s+[^,.;]{2,70}?\s+(?:originally\s+)?founded\s+in\s+" + _YEAR, 1),
    ("founded", r"\b[Ss]ince\s+(?:our|its|the Company's|the Company’s)\s+(?:founding|inception)\s+in\s+" + _YEAR, 0),
    ("founded", r"\b[Ff]ounded\s+in\s+" + _YEAR + r",?\s+(?:we\b|the Company\b|our\b)", 0),
    ("began operations", _SUBJ + r"(?:\s+and its subsidiaries)?(?:\s*\([^)]{0,90}\))?\s+(?:began|commenced|started)\s+(?:its |our )?(?:business )?operations\s+in\s+" + _YEAR, 1),
    ("incorporated", _SUBJ + _APPOS + r"\s+(?:was|were)\s+(?:originally\s+)?(?:incorporated|organized)\s+(?:under the laws of |in |as a )(?:the )?(?:[Ss]tate of )?[A-Z][A-Za-z]+(?: [A-Z][a-z]+)?(?: corporation)?\s+(?:on |in )(?:[A-Z][a-z]+\s+(?:\d{1,2},\s+)?)?" + _YEAR, 1),
    ("incorporated", _SUBJ + _APPOS + r",?\s+(?:a|an)\s+[^,.;]{2,70}?\s+incorporated\s+under\s+[A-Z][a-z]+(?: [A-Z][a-z]+)?\s+law\s+in\s+" + _YEAR, 1),
    ("incorporated", _SUBJ + _APPOS + r"\s+(?:was|were)\s+(?:originally\s+)?incorporated\s+in\s+(?:[A-Z][a-z]+\s+)?" + _YEAR, 1))]


def founded(text: str, sections: Optional[list] = None, names: Optional[list] = None, today: Optional[_dt.date] = None) -> Optional[dict]:
    """The year the filing says the company was founded (or began operations, or, failing those, was incorporated),
    read from its description of the business: {value, word, quote, offset}. The sentence must be about the company
    itself ("we", "the Company", or its own name): a brand's or a subsidiary's year, or one in a director's
    biography, is not the company's."""
    if not text:
        return None
    secs = sorted(((s.get("offset") or 0, str(s.get("item") or "")) for s in sections or []), key=lambda x: x[0])
    a = next((o for o, i in secs if i == "1"), 0)
    b = min(next((o for o, _i in secs if o > a), a + 60000), a + 60000)
    part = text[a:b]
    cut = re.search(r"(?i)information about (?:our )?executive officers|executive officers of the (?:registrant|company)", part)
    if cut:
        part = part[:cut.start()]                    # the officers' biographies ("co-founded Acme in 1990") are not the company
    now = (today or _dt.date.today()).year
    # "founded" before "began operations" before "incorporated" (a company is often incorporated again, later and elsewhere)
    for want in ("founded", "began operations", "incorporated"):
        hits = []
        for word, rx, g in _FOUNDED:
            if word != want:
                continue
            for m in rx.finditer(part):
                y = int(m.group(m.lastindex))
                subj = re.split(r"[.!?]\s+", m.group(g))[-1] if g else ""        # "North America. We": the sentence's own subject
                if g and not (re.fullmatch(r"(?i)we|the company|our company|the corporation|the firm", subj) or _is_company(subj, names or [])):
                    continue
                if 1600 <= y <= now:
                    hits.append((m.start(), y, m))
        if hits:
            pos, y, m = min(hits, key=lambda h: h[0])
            return {"value": y, "word": want, "quote": _sentence_around(text, a + m.start(), a + m.end()), "offset": a + pos}
    return None


# ---- what the company says it does: the opening of Item 1 (Business)
# a sentence that is a standing notice, not a description (tested after the name definitions in brackets are taken out)
_BOILER = re.compile(r"(?i)forward[- ]looking|as used (?:herein|in this)|unless the context|unless otherwise (?:specified|indicated|noted|stated)|"
                     r"\brefer(?:s|red|ences?)? to\b|\bthe terms? [“\"]|\bmeans? [^.]{0,80}\b(?:subsidiaries|entities)\b|annual report on form 10-k|"
                     r"this (?:annual |combined )?report|this form 10-k|fiscal year (?:is|ends|ended)|52- or 53-week|\bsee (?:item|part|note|pages?)\b|"
                     r"incorporated (?:herein )?by reference|table of contents|trademarks? (?:of|or)\b|registered trademarks?|\bare (?:the )?(?:trademarks|service marks)\b|"
                     r"available,? free of charge|website|internet address|www\.|sec\.gov|safe harbor|no material changes|"
                     r"amounts (?:are|have been) (?:presented|stated|reported|expressed|shown) in|\bin (?:millions|thousands|billions),? (?:except|unless)|"
                     r"\bfile[sd]?\b[^.]{0,120}\bwith the (?:securities and exchange commission|sec)\b|\bforms? (?:8-k|10-q|s-3)\b|code of ethics|"
                     r"\b(?:ticker|trading|stock) symbol\b|\bunder the symbol\b|\bplease (?:see|refer)\b|\bdiscussion below\b|"
                     r"^(?:our|its|the company[’']s) (?:principal |corporate |executive )*(?:offices|headquarters) (?:are|is)\b")
# a paragraph that is a notice from end to end (its later sentences carry no marker of their own)
_NOTICE_PARA = re.compile(r"(?i)forward[- ]looking|private securities litigation reform|safe harbor|undue reliance|differ materially|"
                          r"no obligation to (?:update|revise|publicly)|voluntary disclosures")
# a heading whose paragraphs are notices, up to the next heading
_NOTICE_HEAD = re.compile(r"(?i)cautionary|forward[- ]looking|special note|explanatory note|available information|additional information|where you can find|"
                          r"website|trademarks?|glossary|defined terms|presentation of (?:financial )?information|basis of presentation|use of (?:certain )?terms|"
                          r"risk factors? summary|summary of (?:principal )?risk")
# a description does not open on a sentence that points back at something
_BACKREF = re.compile(r"(?:As such|Furthermore|In addition|Additionally|Accordingly|Moreover|However|Therefore|Thus|Also|Further|For example|For instance|Such|These|"
                      r"Those|This|Refer|See|Please|Certain|References|Throughout|Statements|Note)\b")
# a bracket that defines a name: ("Apple" or the "Company"), (the Company), (collectively, John Deere), (herein referred to as ...)
_DEFN = re.compile(r"\s*\((?:[^()]{0,240}[“”\"][^()]{0,240}|[^()]{0,200}\b(?i:the company|the corporation|the registrant|the firm|referred to|collectively|"
                   r"herein(?:after)?)\b[^()]{0,200}|(?:(?:we|us|our|ours)\b[\s,]*(?:and |or )?){2,6})\)")
_FOOTMARK = re.compile(r"\b([A-Z][a-z]{3,})\d(?=[,\s])")          # "Chevron Corporation1, a Delaware corporation": the footnote's number, set as text
# a heading the opening may run on through while it is still short: the account of the business carries on under it
_RUN_ON = re.compile(r"(?i)\b(?:overview|general|business|mission|purpose|vision|who we are|what we do|our company|the company|history|background|at a glance|profile)\b")
_BULLETS = "".join(chr(c) for c in (0x2022, 0x25CF, 0x25AA, 0x25A0, 0x25E6, 0x2023))
_HEADING = re.compile(r"^(?:part\s+[ivx]+\W*)?(?:item\s+1\.?\W*)?(?:business|general|overview|our company|company background|the company|company overview|"
                      r"introduction|background|general development of business|description of (?:the )?business|our business|who we are|corporate overview|"
                      r"business summary|business overview)?\W*$", re.I)
# the heading of Item 1 itself, with what a filing hangs on it: "Items 1. and 2. Business and Properties.", "ITEM 1. BUSINESS (dollars in millions, ...)"
_ITEM1 = re.compile(r"(?im)^[ \t]*(?:part\s+i\b\W{0,6})?items?\s+1\.?(?:\s*(?:and|&)\s*2\.?)?\W{0,4}business\b(?:[ \t]+and[ \t]+properties)?\.?(?:[ \t]*\([^)\n]{0,140}\))?")
# where a filing with no Item 1 heading of its own (an annual report with a cross-reference index) opens its account of the business
_OPENERS = re.compile(r"(?im)^[ \t]*(?:about[ \t]+[^\n]{2,40}?|overview|business|general|our business|who we are|our company|company overview|business overview|"
                      r"corporate overview|introduction|description of (?:the )?business)[ \t]*$|^[ \t]*ABOUT [A-Z][A-Z0-9 &" + _AP + r".\-]{1,40}\.(?=[ \t]+[A-Z])")
_ITEM_REF = re.compile(r"(?i)\bitems?\s+\d{1,2}[a-c]?\b")
# a sentence that says what a company is or does (asked of an opening found without an Item 1 heading)
_SAYS_WHAT = re.compile(r"(?i)\b(?:is|are|was|were|operat\w+|provid\w+|design\w*|develop\w*|manufactur\w+|produc\w+|offer\w*|sell\w*|mak(?:e|es|ing)|build\w*|serv\w+|deliver\w*|"
                        r"own\w*|franchis\w+|engag\w+|speciali[sz]\w+|focus\w*|invest\w*|leas\w+|distribut\w+|market\w*|suppl\w+|dates back|pioneer\w+|founded)\b")


def _index_like(after: str) -> bool:
    """The lines after a heading are a table of contents or a cross-reference index, not the section: other Items
    follow at once, or the lines end in page numbers."""
    head = after[:700]
    if len(re.findall(r"(?im)^\W{0,6}(?:part\s+[ivx]+\W{1,6})?items?\s+\d{1,2}[a-c]?\b", head)) >= 2:
        return True
    lines = [ln.strip() for ln in head.split("\n") if ln.strip()][:8]
    return sum(1 for ln in lines if len(ln) < 120 and (re.search(r"(?:\s|\.{2,})\d{1,3}(?:-\d{1,3})?$", ln) or re.match(r"\d{1,3}\s+[A-Z]", ln))) >= 3


def _plain(sentence: str) -> str:
    """A sentence without the brackets that define names: 'BlackRock, Inc. (together ... "BlackRock" or the "Company") is a
    leading ...' reads 'BlackRock, Inc. is a leading ...'."""
    out = _DEFN.sub("", sentence.replace("(s)", "\x01")).replace("\x01", "(s)")      # "platform(s)" inside a bracket is not a bracket's end
    out = re.sub(r"(?<=[a-z])(?:TM|SM)\b", "", _FOOTMARK.sub(r"\1", out))              # "ProxximaTM": the trademark sign, set as text
    out = re.sub(r"^(?:General|Overview|Business|Introduction|Background|Summary|The Company|Our Company)\s*[:.]\s+(?=[A-Z0-9])", "", out.strip())   # a heading run into its sentence
    return re.sub(r"(?<!\.)\.\.$", ".", _ws(re.sub(r"\s+([,.;])", r"\1", out)))        # "Nestle S.A. (...)." without its bracket ends on one full stop


def _opening(text: str, start: int, limit: int) -> Optional[dict]:
    """The first sentences of the account of the business that starts at text[start]: {text, offset, trimmed}, or
    None when what follows is notices, lists or nothing a reader would call a description."""
    window = text[start:start + 16000]
    m = _ITEM1.match(window)
    if m:
        window = window[m.end():]
        start += m.end()
    # block by block: a PDF's wrapped lines are joined into their paragraph; a heading stands alone; a sentence a page
    # break cut in two ("... sale and distribution of", blank line, "biopharmaceutical products worldwide.") is one
    blocks, cur, gap = [], "", False
    rows = window.split("\n")
    for k, raw in enumerate(rows):
        line = raw.strip()
        nxt = next((r.strip() for r in rows[k + 1:k + 3] if r.strip()), "")
        # a designed report's short lines: "This Annual Report and related discussions" / "contains statements ...". The
        # next line carries the sentence on when it opens on a plain lower-case word ("iPhone" under "Products" does not)
        runs_on = bool(re.match(r"[a-z]+(?:[\s,.;:)]|$)", nxt))
        if not line:
            if cur and re.search(r"[.!?:;][\"”)]?$", cur):
                blocks.append(("para", cur))
                cur = ""
            gap = bool(cur)
            continue
        if gap and not line[:1].islower():
            blocks.append(("para", cur))             # the paragraph did end there, on no full stop
            cur = ""
        gap = False
        pieces = re.split(r"\s{2,}", line)           # "PART I  Item 1.  Business  Company Background" on one PDF line
        for j, piece in enumerate(pieces):
            head = bool(_HEADING.match(piece)) or (len(piece) < 60 and not re.search(r"[.!?,;:][\"”)]?$", piece)
                                                   and not re.search(r"\b(?:and|of|the|to|in|a|or|for|with|its|is|are)$", piece)
                                                   and not (runs_on and j == len(pieces) - 1))
            if not cur and head:
                blocks.append(("head", piece))
                continue
            cur = f"{cur} {piece}".strip()
        if re.search(r"[.!?][\"”)]?$", cur):
            blocks.append(("para", cur))
            cur = ""
    if cur:
        blocks.append(("para", cur))
    out, first, trimmed, full, notice, fresh = [], None, False, False, False, True
    size = lambda: sum(len(x) for x in out)
    for kind, body in blocks:
        if kind == "head":
            if re.match(r"(?i)\W*(?:part\s+[ivx]+\W+)?items?\s+(?:1[a-c]|[2-9]|1\d)\b", body):
                break                                # the next Item: its text is no part of the business's account
            notice = bool(_NOTICE_HEAD.search(body))
            if out and (size() >= 220 or not (notice or _HEADING.match(body) or _RUN_ON.search(body))):
                break                                # the next heading ("Products") ends the opening; a short one runs on
            fresh = True                             # through "Available Information" (skipped) and "Overview"
            continue
        if notice:
            continue
        for sent in _sentences(body):
            clean = _plain(sent)
            if _NOTICE_PARA.search(clean):
                break                                # from a notice's first sentence to the paragraph's end is notice
            if _BOILER.search(clean) or (fresh and (len(clean) < 40 or _BACKREF.match(clean))):
                continue
            if any(ch in clean for ch in _BULLETS) or clean.endswith(":"):
                if out:
                    full = True                      # a list starts here: the opening is what came before it
                    break
                continue
            if out and size() >= 220 and size() + len(clean) > limit:
                full = True                          # long enough; a short opening still takes its next sentence, however long
                break
            if not out:
                first = sent
            trimmed = trimmed or clean != _ws(sent)
            out.append(clean)
            fresh = False
        if full or size() >= 220 or len(out) >= 4:
            break
    if not out or size() < 60:
        return None
    at = re.search(r"\s+".join(re.escape(w) for w in first.split()[:6]), text[start:start + 16000])
    body = " ".join(out)
    if len(body) > limit + 240:
        body = body[:limit + 240].rsplit(" ", 1)[0].rstrip(",;:") + " ..."
    return {"text": body, "offset": start + at.start() if at else start, "trimmed": trimmed}


_SELF = re.compile(r"(?:We|Our|The Company|The Group|The Corporation|The Firm|The Bank)\b")


def _opens_on_company(sentence: str, names: Optional[list]) -> bool:
    """The sentence is about the company itself: "We ...", "Our mission ...", "The Company ...", or it opens on the
    company's name ("Citigroup's history dates back ..."). With no names given, any sentence passes."""
    if not names or _SELF.match(sentence):
        return True
    m = re.match(r"((?:[A-Z][\w&.’'\-]*)(?:\s+(?:[A-Z&][\w&.’'\-]*|of|the|and)){0,5})", sentence)
    return bool(m) and _is_company(re.sub(r"[’']s\b", "", m.group(1)), names)


def description(text: str, sections: Optional[list] = None, limit: int = 560, names: Optional[list] = None) -> Optional[dict]:
    """What the company says it does: the opening sentences of Item 1 (Business) of its annual filing: {text, offset,
    trimmed}. Headings, the standing notices (forward-looking statements, defined terms, where to find reports) and
    the brackets that define names are left out (trimmed says a bracket was). A table of contents or cross-reference
    index that names Item 1 is passed over; a filing with no Item 1 heading of its own is read from the first heading
    that opens its account of the business ("About ...", "Overview"), and only when that opening says what the company
    itself is or does (names: the company's names, to know its own name as a subject). None beats a paragraph that is
    not a description."""
    if not text:
        return None
    starts = [m.start() for m in _ITEM1.finditer(text)]
    sec = next((s.get("offset") for s in sections or [] if str(s.get("item") or "") == "1"), None)
    if sec is not None and not any(abs(sec - x) < 120 for x in starts):
        starts.append(sec)                           # the reader's own Item 1, where its heading is set another way
    for st in starts:
        nl = text.find("\n", st)
        if _index_like(text[(nl if 0 <= nl < st + 200 else st):][:900]):
            continue
        got = _opening(text, st, limit)
        if got:
            return got
    if starts and not all(_index_like(text[(text.find("\n", st) if 0 <= text.find("\n", st) < st + 200 else st):][:900]) for st in starts):
        return None                                  # Item 1 is there and opens on something else: no guess from elsewhere
    for m in _OPENERS.finditer(text[: min(len(text), max(60000, int(len(text) * 0.3)))]):
        if not m.group(0).strip()[:1].isupper() or _index_like(text[m.end():m.end() + 900]):
            continue                                 # a wrapped line of prose ("about meeting or being on track to meet"), or a contents page
        got = _opening(text, m.end(), limit)
        one = _sentences(got["text"])[0] if got else ""
        if got and _SAYS_WHAT.search(one) and _opens_on_company(one, names):
            return got                               # with no Item 1 to anchor it, the opening must say what the company itself is or does
    return None


def page_of(page_starts: Optional[list], offset: Optional[int]) -> Optional[int]:
    pg = None
    for ps, idx in page_starts or []:
        if offset is not None and ps <= offset:
            pg = idx
        else:
            break
    return pg


def from_filing(text: str, page_starts: Optional[list] = None, sections: Optional[list] = None, printed: Optional[Callable] = None,
                names: Optional[list] = None) -> dict:
    """Everything the annual filing gives for the About box, each with its page: {employees, ceo, founded, description}."""
    out = {}
    for key, got in (("employees", employees(text, sections, names)), ("ceo", chief_executive(text)), ("founded", founded(text, sections, names)),
                     ("description", description(text, sections, names=names))):
        if got:
            pg = page_of(page_starts, got.get("offset"))
            got["page"] = (printed(pg) if printed else None) or pg
            got["pdf_page"] = pg
            out[key] = got
    return out


# ---------------------------------------------------------------------------------------------------------------------
# SEC EDGAR's record of the company
_STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
           "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
           "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
           "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey",
           "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
           "PA": "Pennsylvania", "PR": "Puerto Rico", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
           "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}
_SMALL = {"of", "the", "and", "de", "la", "del", "da", "van", "von", "du", "le", "on", "upon", "in"}


def _city(s: str) -> str:
    words = [w if (w.lower() in _SMALL and i) else (w.upper() if w.lower() in {"st", "st."} and False else w[:1].upper() + w[1:].lower())
             for i, w in enumerate(_ws(s).split())]
    return " ".join("-".join(p[:1].upper() + p[1:] for p in w.split("-")) for w in words).replace("Mc", "Mc")


def sec_record(sub: dict) -> dict:
    """What SEC EDGAR's own record of the company says: where it is based, where it is listed and incorporated, its
    fiscal year end and industry code."""
    sub = sub or {}
    biz = ((sub.get("addresses") or {}).get("business")) or {}
    city, st = _ws(biz.get("city") or ""), _ws(biz.get("stateOrCountry") or "")
    where = _STATES.get(st.upper()) or _ws(biz.get("stateOrCountryDescription") or "") or st
    if where and where.upper() == st.upper() and len(st) == 2 and st.upper() not in _STATES:
        where = _ws(biz.get("stateOrCountryDescription") or st)
    hq = ", ".join(x for x in (_city(city) if city else "", where if where and where.upper() != st.upper() or len(where) > 2 else where) if x) or None
    fye = str(sub.get("fiscalYearEnd") or "")
    months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
    fy = f"{months[int(fye[:2]) - 1]} {int(fye[2:])}" if re.fullmatch(r"(0[1-9]|1[0-2])([0-2]\d|3[01])", fye) else None
    inc = _ws(sub.get("stateOfIncorporationDescription") or "") or _ws(sub.get("stateOfIncorporation") or "")
    inc = _STATES.get(inc.upper(), inc) if len(inc) == 2 else inc
    site = _ws(sub.get("website") or "") or None
    if site and not re.match(r"https?://", site):
        site = "https://" + site
    street = ", ".join(_ws(biz.get(k) or "") for k in ("street1", "street2") if _ws(biz.get(k) or ""))
    return {"name": sub.get("name"), "hq": hq, "street": _city(street) if street else None, "zip": _ws(biz.get("zipCode") or "") or None,
            "phone": _ws(sub.get("phone") or "") or None, "exchange": ", ".join(dict.fromkeys(x for x in sub.get("exchanges") or [] if x)) or None,
            "incorporated_in": inc or None, "fiscal_year_end": fy, "sic": sub.get("sic"), "sic_description": sub.get("sicDescription"),
            "website": site, "former_names": [f.get("name") for f in (sub.get("formerNames") or []) if f.get("name")][:4],
            "cik": str(sub.get("cik") or "").lstrip("0") or None}


# ---------------------------------------------------------------------------------------------------------------------
# Wikidata and Wikipedia
class WikidataBusy(Exception):
    """Wikidata did not answer (busy, refused, offline): not the same as having no record."""


def _ua() -> dict:
    """The headers sent to Wikidata and Wikipedia: a plain client name, with no address and no project name. Wikimedia
    asks tools to say who runs them; the owner can do so by putting one line (an address or a page) in
    ~/.bmg-wikimedia-contact, which is then named in the User-Agent. Nothing is named unless that file exists."""
    try:
        contact = re.sub(r"[^\x20-\x7e]", "", (Path.home() / WD_CONTACT).read_text().strip().splitlines()[0])[:120].strip()
    except (OSError, IndexError):
        contact = ""
    return {**WD_UA, "User-Agent": f"company-page-reader/1.0 (local desktop research tool; {contact})"} if contact else WD_UA


async def _wd(client: httpx.AsyncClient, params: dict) -> dict:
    last = None
    for wait in (0, 2.5):
        if wait:
            await asyncio.sleep(wait)
        try:
            r = await client.get(WD_API, params={**params, "format": "json"}, headers=_ua(), timeout=TIMEOUT)
        except httpx.HTTPError as e:
            last = e.__class__.__name__
            continue
        if r.status_code == 200:
            try:
                d = r.json()
            except ValueError:
                last = "not JSON"
                continue
            if isinstance(d, dict) and not d.get("error"):
                return d
            last = str((d.get("error") or {}).get("code") if isinstance(d, dict) else "error")
        else:
            last = f"HTTP {r.status_code}"
    raise WikidataBusy(last or "no answer")


def _norm_name(s: str) -> set:
    drop = {"inc", "incorporated", "corp", "corporation", "co", "company", "plc", "ltd", "limited", "holdings", "holding", "group", "the", "and", "sa", "nv", "ag", "lp", "llc", "class", "a", "b", "c"}
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", (s or "").lower().replace("&", " and ")).split() if w not in drop}


def _claims(ent: dict, pid: str) -> list:
    return [c for c in ((ent.get("claims") or {}).get(pid) or []) if (c.get("mainsnak") or {}).get("snaktype") == "value" and c.get("rank") != "deprecated"]


def _val(c: dict):
    return ((c.get("mainsnak") or {}).get("datavalue") or {}).get("value")


def _qual(c: dict, pid: str):
    q = ((c.get("qualifiers") or {}).get(pid) or [{}])[0]
    return (q.get("datavalue") or {}).get("value")


def _year(v) -> Optional[int]:
    m = re.match(r"[+-]?(\d{4})-", (v or {}).get("time") or "") if isinstance(v, dict) else None
    return int(m.group(1)) if m else None


def _date(v) -> Optional[str]:
    m = re.match(r"[+-]?(\d{4})-(\d\d)-(\d\d)", (v or {}).get("time") or "") if isinstance(v, dict) else None
    if not m:
        return None
    prec = (v or {}).get("precision") or 11
    return m.group(1) if prec <= 9 else f"{m.group(1)}-{m.group(2)}" if prec == 10 else f"{m.group(1)}-{m.group(2)}-{m.group(3)}"


def pick_entity(ents: dict, cik: Optional[str], ticker: str, name: str) -> Optional[dict]:
    """Of the Wikidata items found for a company, the one that is it: the same SEC number or ticker, the nearest name,
    still in being (an item with a dissolution date is the company it used to be)."""
    best, top = None, -99.0
    want = _norm_name(name)
    for ent in (ents or {}).values():
        if not isinstance(ent, dict) or "missing" in ent:
            continue
        sc = 0.0
        if cik and any(str(_val(c) or "").lstrip("0") == str(cik).lstrip("0") for c in _claims(ent, "P5531")):
            sc += 3
        for c in _claims(ent, "P414"):
            tk = _qual(c, "P249")
            if isinstance(tk, str) and tk.upper().replace(".", "-") == ticker.upper().replace(".", "-"):
                sc += 2 if not _qual(c, "P582") else 0.5
                break
        label = ((ent.get("labels") or {}).get("en") or {}).get("value") or ""
        got = _norm_name(label)
        if want and got:
            sc += 3 * len(want & got) / max(len(want | got), 1)
        if _claims(ent, "P576"):
            sc -= 3
        if _claims(ent, "P169"):
            sc += 0.5
        if sc > top:
            best, top = ent, sc
    return best if top >= 2 else None


def facts_from_entity(ent: dict, labels: dict) -> dict:
    """The About facts of a Wikidata item: founded, chief executive now (a statement with no end date; the preferred
    one, else the latest to start), head count with its date, headquarters, website, founders, the Wikipedia page."""
    lab = lambda q: ((labels.get(q) or {}).get("labels") or {}).get("en", {}).get("value") if q else None
    out = {"id": ent.get("id"), "label": ((ent.get("labels") or {}).get("en") or {}).get("value"),
           "wikipedia": ((ent.get("sitelinks") or {}).get("enwiki") or {}).get("title")}
    inc = sorted((y for y in (_year(_val(c)) for c in _claims(ent, "P571")) if y))
    if inc:
        out["founded"] = inc[0]
    now = [c for c in _claims(ent, "P169") if not _qual(c, "P582") and isinstance(_val(c), dict)]
    if now:
        pref = [c for c in now if c.get("rank") == "preferred"] or now
        dated = [c for c in pref if _qual(c, "P580")]
        # one statement with no end date is the chief executive; several without a preferred one or dates are not told apart
        if len(pref) == 1 or (dated and len(dated) == len(pref)) or len({(_val(c) or {}).get("id") for c in pref}) == 1:
            c = max(pref, key=lambda c: (_qual(c, "P580") or {}).get("time", "").lstrip("+")) if len(pref) > 1 else pref[0]
            nm = lab((_val(c) or {}).get("id"))
            if nm:
                out["ceo"] = {"value": nm, "since": _date(_qual(c, "P580"))}
    emp = []
    for c in _claims(ent, "P1128"):
        v = _val(c) or {}
        try:
            n = float(str(v.get("amount")).lstrip("+"))
        except (TypeError, ValueError):
            continue
        emp.append((_date(_qual(c, "P585")) or "", 1 if c.get("rank") == "preferred" else 0, int(n)))
    if emp:
        at, _r, n = max(emp)
        out["employees"] = {"value": n, "as_of": at or None}
    hq = next((lab((_val(c) or {}).get("id")) for c in sorted(_claims(ent, "P159"), key=lambda c: c.get("rank") != "preferred") if isinstance(_val(c), dict)), None)
    if hq:
        out["hq"] = hq
    site = next((_val(c) for c in sorted(_claims(ent, "P856"), key=lambda c: c.get("rank") != "preferred") if isinstance(_val(c), str)), None)
    if site:
        out["website"] = site
    fnd = [lab((_val(c) or {}).get("id")) for c in _claims(ent, "P112") if isinstance(_val(c), dict)]
    if [f for f in fnd if f]:
        out["founders"] = [f for f in fnd if f][:5]
    return out


def _wd_ids(ent: dict) -> list:
    ids = []
    for pid in ("P169", "P159", "P112"):
        for c in _claims(ent, pid):
            v = _val(c)
            if isinstance(v, dict) and v.get("id") and v["id"] not in ids:
                ids.append(v["id"])
    return ids[:45]


async def wikidata(ticker: str, cik: Optional[str], name: str, exchanges: Optional[list] = None, cache_dir: Optional[Path] = None,
                   client: Optional[httpx.AsyncClient] = None, fresh: bool = False) -> dict:
    """What Wikidata and Wikipedia hold about the company: {"found": True, founded, ceo, employees, hq, website,
    founders, wikipedia: {title, url, extract}, read_at}, {"found": False} when they have no record of it, or
    {"found": None, "error": ...} when they did not answer (asked again later, never kept as a miss)."""
    t = (ticker or "").upper()
    disk = Path(cache_dir) / f"wikidata-{re.sub(r'[^A-Z0-9.-]', '', t)}.json" if cache_dir and t else None
    if disk and not fresh and disk.exists():
        try:
            kept = json.loads(disk.read_text())
            if time.time() - kept.get("_at", 0) < (WD_TTL if kept.get("found") else WD_MISS_TTL):
                return kept
        except (ValueError, OSError):
            pass
    if not fresh and time.time() - _wd_fail.get(t, 0) < WD_FAIL_TTL:
        return {"found": None, "error": "Wikidata did not answer a few minutes ago; it is asked again later."}
    own = client is None
    client = client or httpx.AsyncClient(follow_redirects=True)
    try:
        ids = []
        c = str(cik or "").lstrip("0")
        asks = ([f"haswbstatement:P5531={int(c):010d}", f"haswbstatement:P5531={c}"] if c.isdigit() else [])
        order = [EXCHANGES[k] for k in EXCHANGES if any(k in (x or "").upper() for x in exchanges or [])] or list(EXCHANGES.values())
        asks += [f"haswbstatement:P414={q}[P249={t.replace('-', '.')}]" for q in order + [q for q in EXCHANGES.values() if q not in order]]
        for i, ask in enumerate(asks):
            d = await _wd(client, {"action": "query", "list": "search", "srsearch": ask, "srlimit": "5", "srnamespace": "0"})
            ids += [h["title"] for h in (d.get("query") or {}).get("search") or [] if re.fullmatch(r"Q\d+", h.get("title") or "") and h["title"] not in ids]
            if ids and i >= 0:
                break                                # found by its SEC number (or, failing that, by its ticker): no need to ask on
        if not ids:
            out = {"found": False}
        else:
            d = await _wd(client, {"action": "wbgetentities", "ids": "|".join(ids[:6]), "props": "claims|labels|sitelinks", "languages": "en", "sitefilter": "enwiki"})
            ent = pick_entity(d.get("entities") or {}, c or None, t, name)
            if not ent:
                out = {"found": False}
            else:
                labels = {}
                want = _wd_ids(ent)
                if want:
                    labels = (await _wd(client, {"action": "wbgetentities", "ids": "|".join(want), "props": "labels", "languages": "en"})).get("entities") or {}
                out = {"found": True, **facts_from_entity(ent, labels)}
                title = out.pop("wikipedia", None)
                if title:
                    out["wikipedia"] = {"title": title, "url": "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")}
                    try:
                        r = await client.get(WP_SUMMARY + httpx.URL("/" + title.replace(" ", "_")).raw_path.decode()[1:], headers=_ua(), timeout=TIMEOUT)
                        if r.status_code == 200:
                            j = r.json()
                            if j.get("type") != "disambiguation" and j.get("extract"):
                                out["wikipedia"]["extract"] = _ws(j["extract"])[:900]
                                out["wikipedia"]["url"] = (((j.get("content_urls") or {}).get("desktop") or {}).get("page")) or out["wikipedia"]["url"]
                    except (httpx.HTTPError, ValueError):
                        pass                         # the facts stand without the summary
        out["_at"] = time.time()
        out["read_at"] = _dt.date.today().isoformat()
        if disk:
            try:
                disk.parent.mkdir(parents=True, exist_ok=True)
                disk.write_text(json.dumps(out))
            except OSError:
                pass
        return out
    except WikidataBusy as e:
        _wd_fail[t] = time.time()
        return {"found": None, "error": f"Wikidata did not answer ({e})."}
    finally:
        if own:
            await client.aclose()


# ---------------------------------------------------------------------------------------------------------------------
# the About box: one answer per fact, with where it came from
VERSION = "4"                      # raised when the reading above changes, so kept answers are worked out again
NOTE = ("PRIMARY: read from the company's own annual filing (with its page) or SEC EDGAR's record of the company. SECONDARY: Wikidata or "
        "Wikipedia, shown for context and never a source for a note or a model. A fact no source gives says NOT FOUND.")


def _fact(key: str, label: str, value=None, **kw) -> dict:
    out = {"key": key, "label": label, "value": value, "basis": None, "source": None}
    out.update({k: v for k, v in kw.items() if v is not None})
    if value is None and not out.get("note"):
        out["note"] = "NOT FOUND"
    return out


def build(ticker: str, name: str, co: Optional[dict] = None, rec: Optional[dict] = None, filing: Optional[dict] = None, wd: Optional[dict] = None) -> dict:
    """The About box of a company's page. co: its entry on the brain's map (or {}); rec: sec_record(); filing: {"facts":
    from_filing(), "source": {form, period, period_end, filed, url, read}} or {"error": why}; wd: wikidata()."""
    co, rec, filing, wd = co or {}, rec or {}, filing or {}, wd or {}
    ff, fs = filing.get("facts") or {}, filing.get("source") or {}
    form = fs.get("form") or "10-K"
    filed = fs.get("filed")
    wiki_at = wd.get("read_at")
    wd_url = f"https://www.wikidata.org/wiki/{wd['id']}" if wd.get("id") else None

    item1 = str(form).upper().startswith("10-K")     # a 20-F or 40-F has no Item 1 (Business): its facts are cited by form and page only

    def from_filing_(got: dict, what: str) -> dict:
        return {"basis": "PRIMARY", "source": form if what == "Item 1" and not item1 else f"{form}, {what}", "page": got.get("page"), "pdf_page": got.get("pdf_page"), "quote": got.get("quote"),
                "url": fs.get("url"), "read": fs.get("read"), "as_of": filed or fs.get("period_end")}

    facts = []
    # chief executive: the signature page of the annual filing; Wikidata when the filing gives none, or names someone who started after it was filed
    f_ceo, w_ceo = ff.get("ceo"), wd.get("ceo")
    if f_ceo:
        newer = (w_ceo and _last(w_ceo["value"]) not in {_last(n) for n in f_ceo["value"].split(" and ")}
                 and w_ceo.get("since") and filed and w_ceo["since"] > filed[:len(w_ceo["since"])])
        if newer:
            facts.append(_fact("ceo", "CEO", w_ceo["value"], basis="SECONDARY", source="Wikidata", url=wd_url, as_of=wiki_at, since=w_ceo.get("since"),
                               note=f"Since {w_ceo['since']} by Wikidata. The {form} filed {filed} was signed by {f_ceo['value']} as chief executive."))
        else:
            other = (f"Wikidata names {w_ceo['value']}." if w_ceo and _last(w_ceo["value"]) not in {_last(n) for n in f_ceo["value"].split(" and ")} else None)
            facts.append(_fact("ceo", "CEO", f_ceo["value"], note=other, **from_filing_(f_ceo, "signature page")))
    elif w_ceo:
        facts.append(_fact("ceo", "CEO", w_ceo["value"], basis="SECONDARY", source="Wikidata", url=wd_url, as_of=wiki_at, since=w_ceo.get("since")))
    else:
        facts.append(_fact("ceo", "CEO", note="NOT FOUND: the annual filing's signature page did not name one plainly, and Wikidata has none."
                                              if ff or wd.get("found") else "NOT FOUND"))
    # founded
    f_fd, w_fd = ff.get("founded"), wd.get("founded")
    if f_fd and f_fd.get("word") != "incorporated":
        facts.append(_fact("founded", "Founded", str(f_fd["value"]), note=(f"Wikidata says {w_fd}." if w_fd and w_fd != f_fd["value"] else None),
                           **from_filing_(f_fd, "Item 1")))
    elif w_fd:
        facts.append(_fact("founded", "Founded", str(w_fd), basis="SECONDARY", source="Wikidata", url=wd_url, as_of=wiki_at,
                           founders=wd.get("founders"),
                           note=(f"The {form} says it was incorporated in {f_fd['value']}" + (f" (p.{f_fd['page']})." if f_fd.get("page") else ".") if f_fd else None)))
    elif f_fd:
        facts.append(_fact("founded", "Incorporated", str(f_fd["value"]), **from_filing_(f_fd, "Item 1")))
    else:
        facts.append(_fact("founded", "Founded"))
    # head count
    f_em, w_em = ff.get("employees"), wd.get("employees")
    if f_em:
        facts.append(_fact("employees", "Employees", f"{f_em['value']:,}", raw=f_em["value"], **from_filing_(f_em, "Item 1")))
    elif w_em:
        facts.append(_fact("employees", "Employees", f"{w_em['value']:,}", raw=w_em["value"], basis="SECONDARY", source="Wikidata", url=wd_url,
                           as_of=w_em.get("as_of") or wiki_at, note=None if w_em.get("as_of") else "Wikidata gives no date for this count."))
    else:
        facts.append(_fact("employees", "Employees", note="NOT FOUND: the annual filing does not state the company's own head count in a sentence."
                                                          if ff else "NOT FOUND"))
    # headquarters
    edgar = f"https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK={rec['cik']}&type=10-K" if rec.get("cik") else None
    if rec.get("hq"):
        facts.append(_fact("hq", "Headquarters", rec["hq"], basis="PRIMARY", source="SEC EDGAR company record (business address)", url=edgar))
    elif co.get("hq"):
        facts.append(_fact("hq", "Headquarters", co["hq"], basis="SECONDARY", source="the brain's map (Wikipedia's S&P 500 list)"))
    elif wd.get("hq"):
        facts.append(_fact("hq", "Headquarters", wd["hq"], basis="SECONDARY", source="Wikidata", url=wd_url, as_of=wiki_at))
    else:
        facts.append(_fact("hq", "Headquarters"))
    more = []
    if co.get("gics_sector"):
        more.append(_fact("sector", "Sector", co["gics_sector"], basis="MAP", source="GICS, from the brain's map"))
        more.append(_fact("industry", "Industry", co.get("gics_sub_industry"), basis="MAP", source="GICS sub-industry, from the brain's map"))
    elif rec.get("sic_description"):
        more.append(_fact("industry", "Industry", str(rec["sic_description"]).title().replace("&", "and"), basis="PRIMARY", source=f"SEC industry code {rec.get('sic')}", url=edgar))
    if rec.get("exchange"):
        more.append(_fact("exchange", "Listed on", rec["exchange"], basis="PRIMARY", source="SEC EDGAR company record", url=edgar))
    if rec.get("fiscal_year_end"):
        more.append(_fact("fiscal_year_end", "Fiscal year ends", rec["fiscal_year_end"], basis="PRIMARY", source="SEC EDGAR company record", url=edgar))
    if rec.get("incorporated_in"):
        more.append(_fact("incorporated", "Incorporated in", rec["incorporated_in"], basis="PRIMARY", source="SEC EDGAR company record", url=edgar))
    if co.get("added_to_index") and co.get("index"):
        more.append(_fact("index", f"In the {co['index']} since", str(co["added_to_index"])[:4], basis="SECONDARY", source="the brain's map (Wikipedia's S&P 500 list)"))
    site = rec.get("website") or wd.get("website")
    if site:
        more.append(_fact("website", "Website", re.sub(r"^https?://(?:www\.)?", "", site).rstrip("/"), href=site,
                          basis="PRIMARY" if rec.get("website") else "SECONDARY", source="SEC EDGAR company record" if rec.get("website") else "Wikidata",
                          url=edgar if rec.get("website") else wd_url))
    desc = None
    if ff.get("description"):
        d = ff["description"]
        desc = {"text": d["text"], "basis": "PRIMARY", "source": f"{form}, Item 1 (the company's own words)" if item1 else f"{form} (the company's own words)", "page": d.get("page"), "pdf_page": d.get("pdf_page"),
                "url": fs.get("url"), "read": fs.get("read"), "as_of": filed or fs.get("period_end"), "trimmed": bool(d.get("trimmed"))}
    wp = wd.get("wikipedia") or {}
    wiki = {"text": wp["extract"], "basis": "SECONDARY", "source": "Wikipedia", "url": wp.get("url"), "as_of": wiki_at} if wp.get("extract") else None
    return {"ticker": ticker, "name": name, "legal_name": rec.get("name") or co.get("sec_name"), "description": desc, "wikipedia": wiki,
            "facts": facts, "more": [m for m in more if m.get("value")],
            "filing": {**fs, "error": filing.get("error")} if (fs or filing.get("error")) else None,
            "wikidata": {"found": wd.get("found"), "error": wd.get("error"), "url": wd_url, "read_at": wiki_at},
            "note": NOTE}
