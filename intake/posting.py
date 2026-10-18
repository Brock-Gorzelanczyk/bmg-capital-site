"""Posting desk for the local Crawler: five post drafts a day from three places, checked against social/README.md.

    ideas from the daily hook routine   social/ideas.md on the ideas branch (already written to hook-rules.md)
    ideas from Crawler reads            My reads: each number a statement figure of the read, with its page
    ideas from the news desk            the month's top stories (no numbers taken from a headline)

A draft has the vault's draft shape (social/drafts/_TEMPLATE.md): hook, on-screen text, three captions, hashtags, the
TikTok, Instagram and X blocks, the Sources table and the Checks table. The checks are run on every edit:

    every number in the post is in the Sources table (a year alone is not a figure)
    no buy or sell call; no hype words; lowercase; the hook at most 8 words
    a held name (state/current.md) is disclosed on screen and in the caption
    nothing on GBX until it is cleared; the X post fits in 280 characters; TikTok 3 to 5 hashtags, Instagram up to 8
    and the "link in bio" line

Nothing here posts, schedules or signs in anywhere. The owner reviews each draft and presses post.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

PATTERNS = ["contradiction", "contrast", "stakes", "curiosity gap", "challenge", "process / pov", "series"]
DEFAULT_TIMES = ["08:00", "11:30", "14:30", "18:00", "21:00"]

HYPE = re.compile(r"(?i)\b(?:insane|crazy|mind[- ]?blowing|shocking|unbelievable|you won'?t believe|game[- ]?changer|skyrocket\w*|explod(?:e|es|ed|ing)"
                  r"|massive|huge|epic|wild|bonkers|let'?s dive in|dive in)\b")
CALL_HARD = re.compile(r"(?i)\b(?:price target|will (?:go up|rise|soar|rally|pop|crash|tank|fall|drop)|buy the dip|buy now|sell now|to the moon|load up"
                       r"|not financial advice|nfa|guaranteed|can'?t lose|easy money|next (?:nvidia|tesla|bitcoin)|10x your)\b")
CALL_SOFT = re.compile(r"(?i)\b(?:buy|sell|short|undervalued|overvalued|cheap stock|bargain|steal)\b")
GBX = re.compile(r"(?i)\b(?:gbx|greenbrier)\b")
HELD_NAMES = {"COIN": ["coinbase", "coin"], "ORCL": ["oracle", "orcl"], "GATX": ["gatx"], "TRN": ["trinity", "trn"]}
_NUM = re.compile(r"(?<![\w.])(-?\$?\d[\d,]*(?:\.\d+)?)(\s?(?:%|percent|x\b|[kmbt]\b|thousand|million|billion|trillion|bn\b|mn\b))?", re.I)
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_MONTH_BEFORE = re.compile(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s$")
SCALE = {"k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6, "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12}


# ---------------------------------------------------------------- numbers
def numbers_in(text: str) -> list:
    """Each figure in a text as (as written, value): "$416.2b" -> 416.2e9, "69%" -> 69 (a percent), "166,000" -> 166000.
    A year alone ("2025") and a version or model number glued to a word ("gpt-6", "q3") are not figures."""
    out = []
    for m in _NUM.finditer(text or ""):
        raw, unit = m.group(1), (m.group(2) or "").strip().lower()
        start = m.start(1)
        if start > 0 and (text[start - 1].isalpha() or text[start - 1] in "-/"):
            continue                                     # q3, gpt-6: a number glued to a word
        if re.match(r"-[kq]\b", text[m.end(1):m.end(1) + 3], re.I):
            continue                                     # a form's name: 10-k, 10-q, 8-k
        if not unit and "$" not in raw and _MONTH_BEFORE.search(text[max(0, start - 12):start]):
            continue                                     # the day of a date: "jun 30, 2026"
        bare = raw.replace("$", "").replace(",", "")
        if _YEAR.match(bare) and not unit and "$" not in raw:
            continue
        try:
            v = float(bare)
        except ValueError:
            continue
        if unit in SCALE:
            v *= SCALE[unit]
        out.append((m.group(0).strip(), round(v, 6), "%" if unit in ("%", "percent") else unit))
    return out


def same_number(a: float, b: float) -> bool:
    if a == b:
        return True
    if not a or not b:
        return False
    return abs(a - b) / max(abs(a), abs(b)) < 0.006     # the way the source rounds: $416.2b against 416,161 million


def money(v: float) -> str:
    """A dollar amount the way the posts write it: $416.2b, $11b, $156m, $980k (lowercase, the source's value rounded once)."""
    a = abs(v)
    for div, suf in ((1e12, "t"), (1e9, "b"), (1e6, "m"), (1e3, "k")):
        if a >= div:
            x = a / div
            s = f"{x:.1f}".rstrip("0").rstrip(".") if x < 100 else f"{x:.0f}"
            return ("-" if v < 0 else "") + "$" + s + suf
    return ("-" if v < 0 else "") + f"${a:,.0f}"


def pct(x: float) -> str:
    return f"{abs(x) * 100:.0f}%" if abs(x) >= 0.095 else f"{abs(x) * 100:.1f}%"


# ---------------------------------------------------------------- holdings and names
def held_tickers(state_md: str, readme_md: str = "") -> list:
    """Names the owner holds: rows of state/current.md that say the position is held, names with an "analyst position",
    and the list in social/README.md rule 4. A union; the page shows it and the owner can change it."""
    out = []
    for line in (state_md or "").splitlines():
        if line.startswith("|") and re.search(r"(?i)position held", line):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) > 1 and re.fullmatch(r"[A-Z][A-Z.\-]{0,6}", cells[1]):
                out.append(cells[1])
        for m in re.finditer(r"\b(?:\d{3}-)?([A-Z][A-Z.\-]{1,6})\s*\(analyst position", line):
            out.append(m.group(1))
    m = re.search(r"(?i)disclose holdings.*?that is ([^.]+)\.", readme_md or "", re.S)
    if m:
        out += re.findall(r"\b([A-Z]{2,6})\b(?= \()", m.group(1))
    return list(dict.fromkeys(out))


def short_name(company: str) -> str:
    """"Apple Inc." -> "apple", "JPMorgan Chase & Co." -> "jpmorgan chase", "GATX Corporation" -> "gatx"."""
    n = re.sub(r"(?i)\b(?:inc|incorporated|corp|corporation|company|co|group|holdings?|plc|ltd|limited|n\.?v|s\.?a|the|class [a-c])\b\.?", " ", company or "")
    n = re.sub(r"[,.]+", " ", n)
    n = re.sub(r"\s*&\s*$", "", re.sub(r"\s+", " ", n).strip())
    words = n.split()
    keep = 3 if len(words) >= 3 and words[1] == "&" else 2          # "johnson & johnson", "procter & gamble"
    return " ".join(words[:keep]).lower() if words else (company or "").lower()


def poss(who: str) -> str:
    """"apple" -> "apple's", "trinity industries" -> "trinity industries'"."""
    return who + ("'" if who.endswith("s") else "'s")


def words8(hook: str) -> int:
    return len([w for w in re.findall(r"\S+", hook or "") if re.search(r"[A-Za-z0-9$]", w)])


def names_for(ticker: str, company: str = "") -> list:
    return [x for x in [ticker.lower(), short_name(company)] + HELD_NAMES.get(ticker.upper(), []) if x]


# ---------------------------------------------------------------- ideas from a Crawler read
def _fact(F: dict, C: dict, rid: Optional[str]) -> Optional[dict]:
    x = (F.get(rid) or C.get(rid)) if rid else None
    return x if x and x.get("value") is not None and x.get("status") != "Unavailable" else None


def _src(x: dict, doc: str) -> dict:
    s = x.get("source") or {}
    page = s.get("pdf_page")
    printed = s.get("printed_page")
    where = f"{doc}, p.{page}" + (f" (printed {printed})" if printed and str(printed) != str(page) else "") if page else doc
    per = x.get("period") or {}
    return {"where": where, "page": page, "raw": s.get("raw"), "quote": (s.get("excerpt") or "")[:240], "period": (per.get("short") or per.get("label") or "").lower()}


def ideas_from_read(entry: dict, result: dict) -> list:
    """Hook ideas from one read's report: each number is a statement figure with its page, or a calculation from two of
    them that says how it was made."""
    rep = result.get("report") or {}
    co = rep.get("company") or {}
    company = co.get("name") or co.get("name_hint") or entry.get("company") or ""
    ticker = (co.get("ticker") or co.get("ticker_hint") or entry.get("ticker") or "").upper()
    if not company or (result.get("insights") or {}).get("kind") in ("insider", "holdings"):
        return []
    who = short_name(company)
    form = co.get("form") or ("8-K" if (result.get("insights") or {}).get("kind") == "release" else "")
    doc = f"{company} {form} {co.get('period') or ''}".strip()
    F, C = (rep.get("evidence") or {}).get("facts") or {}, (rep.get("evidence") or {}).get("calcs") or {}
    rows = {r.get("metric"): r for r in ((rep.get("financials") or {}).get("performance") or []) + ((rep.get("financials") or {}).get("calculated") or [])}

    def pair(metric):
        r = rows.get(metric) or {}
        return _fact(F, C, r.get("current")), _fact(F, C, r.get("prior"))

    def num(x, shown, label):
        return {"shown": shown, "value": x["value"], "label": label, **_src(x, doc)}

    base = {"source": "read", "sha": entry.get("sha256"), "job_ticker": entry.get("job_ticker") or "", "ticker": ticker, "company": company,
            "form": form, "period": co.get("period") or "", "title": result.get("title") or ""}
    out = []
    rev, rev0 = pair("revenue")
    ni, ni0 = pair("net_income")
    period = ((rev or ni or {}).get("period") or {}).get("short") or ((rev or ni or {}).get("period") or {}).get("label") or ""
    # 1. contradiction: sales and profit moved in opposite directions
    if rev and rev0 and ni and ni0 and rev0["value"] > 0 and ni0["value"] > 0 and ni["value"] > 0:
        dr, dn = rev["value"] / rev0["value"] - 1, ni["value"] / ni0["value"] - 1
        if dr * dn < 0 and abs(dn) >= 0.05 and abs(dr) >= 0.005:
            more = dn > 0
            hook = f"{who} sold {'less' if dr < 0 else 'more'} and earned {pct(dn)} {'more' if more else 'less'}"
            out.append({**base, "pattern": "contradiction", "hook": hook, "plain": f"{poss(who)} profit {'rose' if more else 'fell'}",
                        "payoff": f"the crawler zaps revenue {money(rev['value'])} vs {money(rev0['value'])} and net income {money(ni['value'])} vs {money(ni0['value'])}",
                        "cards": [f"revenue {money(rev['value'])} vs {money(rev0['value'])}", f"net income {money(ni['value'])} vs {money(ni0['value'])}"],
                        "overlay": f"{who} logo next to the spider",
                        "numbers": [num(rev, money(rev["value"]), "revenue"), num(rev0, money(rev0["value"]), "revenue, prior year"),
                                    num(ni, money(ni["value"]), "net income"), num(ni0, money(ni0["value"]), "net income, prior year"),
                                    {"shown": pct(dn), "value": round(abs(dn) * 100, 1), "label": "change in net income", "where": "derived",
                                     "derived": f"{ni['value'] / 1e6:,.0f} / {ni0['value'] / 1e6:,.0f} - 1 = {dn * 100:.1f}%"}]})
    # 2. stakes: profit per day
    if ni and ni["value"] > 0 and ((ni.get("period") or {}).get("type") == "FY"):
        per_day = ni["value"] / 365
        out.append({**base, "pattern": "stakes", "hook": f"{who} made {money(per_day)} a day in {str((ni.get('period') or {}).get('end') or '')[:4]}".strip(),
                    "plain": f"{who} earned {money(ni['value'])}", "payoff": f"net income {money(ni['value'])} divided by 365 days, on average",
                    "cards": [f"net income {money(ni['value'])}", f"/ 365 days = {money(per_day)} a day, on average"],
                    "overlay": f"a {who} product next to \"{money(per_day)} / day\"",
                    "numbers": [num(ni, money(ni["value"]), "net income"),
                                {"shown": money(per_day), "value": per_day, "label": "net income per day, on average", "where": "derived",
                                 "derived": f"{ni['value'] / 1e6:,.0f} million / 365 = {per_day / 1e6:,.1f} million a day"}]})
    # 3. contrast: cash made against cash spent
    cfo, _ = pair("cfo")
    capex, _ = pair("capex")
    if cfo and capex and cfo["value"] > 0 and abs(capex["value"]) >= 0.5 * cfo["value"]:
        out.append({**base, "pattern": "contrast", "hook": f"{who} made {money(cfo['value'])} in cash. it spent {money(abs(capex['value']))}",
                    "plain": f"{poss(who)} free cash flow", "payoff": "operating cash flow against capital spending, from the cash flow statement",
                    "cards": [f"cash from operations {money(cfo['value'])}", f"capital spending {money(abs(capex['value']))}"],
                    "overlay": f"{who} logo next to \"{money(cfo['value'] - abs(capex['value']))} left\"",
                    "numbers": [num(cfo, money(cfo["value"]), "operating cash flow"), num(capex, money(abs(capex["value"])), "capital expenditures")]})
    # 4. curiosity gap: how much of each sales dollar is kept
    if rev and ni and rev["value"] > 0 and ni["value"] > 0:
        cents = ni["value"] / rev["value"] * 100
        if cents >= 1:
            out.append({**base, "pattern": "curiosity gap", "hook": f"{who} keeps {cents:.0f} cents of every dollar",
                        "plain": f"{poss(who)} net margin", "payoff": f"net income {money(ni['value'])} over revenue {money(rev['value'])}",
                        "cards": [f"revenue {money(rev['value'])}", f"net income {money(ni['value'])}"],
                        "overlay": f"a dollar bill next to the {who} logo",
                        "numbers": [num(rev, money(rev["value"]), "revenue"), num(ni, money(ni["value"]), "net income"),
                                    {"shown": f"{cents:.0f} cents", "value": round(cents), "label": "net income per dollar of revenue", "where": "derived",
                                     "derived": f"{ni['value'] / 1e6:,.0f} / {rev['value'] / 1e6:,.0f} = {cents:.1f} cents"}]})
    # 5. a red flag the reader found (a quote, no number)
    for f in ((result.get("insights") or {}).get("red_flags") or []):
        if f.get("status") == "found" and f.get("quotes"):
            q = f["quotes"][0]
            out.append({**base, "pattern": "challenge", "hook": f"find the warning in {poss(who)} {form.lower() or 'filing'}",
                        "plain": f"{who} {f.get('title', '').lower()}", "payoff": f"the spider runs to p.{q.get('page')}: {f.get('title')}",
                        "cards": [f"{(f.get('title') or '').lower()}", f"page {q.get('page')}"],
                        "overlay": "a magnifying glass next to the spider",
                        "numbers": [], "quote": {"text": (q.get("text") or "")[:240], "page": q.get("page"), "where": f"{doc}, p.{q.get('page')}"}})
            break
    # 6. process / pov: the crawler reading this document
    pages = (rep.get("source") or {}).get("pages")
    if pages and pages >= 10:
        out.append({**base, "pattern": "process / pov", "hook": f"pov: a spider reads {poss(who)} {pages}-page {form.lower() or 'filing'}",
                    "plain": f"the crawler reads {poss(who)} {form or 'filing'}", "payoff": "the full read at speed, every figure landing in the report with its page",
                    "cards": ["every figure lands in the report", "with its page"],
                    "overlay": f"{who} logo next to the spider",
                    "numbers": [{"shown": f"{pages}-page", "value": pages, "label": "pages in the document", "where": f"{doc}, page count of the file read"}]})
    kept = []
    for x in out:
        if words8(x["hook"]) > 8 and ticker and " " in who:
            x["hook"] = x["hook"].replace(poss(who), poss(ticker.lower())).replace(who, ticker.lower())
        if words8(x["hook"]) > 8:
            continue
        x["id"] = "r-" + hashlib.sha1(f"{entry.get('sha256')}|{x['pattern']}|{x['hook']}".encode()).hexdigest()[:10]
        x["period_label"] = period
        kept.append(x)
    return kept


def unique(ideas: list) -> list:
    """One idea per hook (two reads of the same company can give the same line)."""
    seen, out = set(), []
    for x in ideas:
        k = re.sub(r"\W+", " ", (x.get("hook") or "").lower()).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out


# ---------------------------------------------------------------- ideas from the daily routine and the news
def ideas_from_routine(days: list) -> list:
    out = []
    for d in days[:2]:
        for i, it in enumerate(d.get("ideas") or []):
            if it.get("done"):
                continue
            fmt = (it.get("format") or "").lower()
            pat = next((p for p in PATTERNS if p.split(" ")[0] in fmt), "process / pov" if "pov" in fmt else "")
            nums = it.get("numbers", "")
            cards = []
            for part in re.split(r";\s+(?![^()]*\))", nums):
                phrase = re.sub(r"\s*\([^()]*(?:\([^()]*\)[^()]*)*\)", "", part).strip().lower()      # the figure without its source
                if phrase and re.search(r"\d", phrase) and not re.search(r"not found|derived", phrase) and len(phrase) <= 70:
                    cards.append(phrase)
            from .newsdesk import tickers_in
            tick = tickers_in(nums + " " + (it.get("why") or ""))
            out.append({"id": "h-" + hashlib.sha1(f"{d.get('day')}|{it.get('hook')}".encode()).hexdigest()[:10], "source": "routine", "day": d.get("day"),
                        "pattern": pat, "hook": it.get("hook", ""), "format": it.get("format", ""), "payoff": it.get("why", ""), "platform": it.get("platform", ""),
                        "numbers_text": nums, "numbers": [], "company": "", "ticker": tick[0] if tick else "", "cards": cards[:2], "raw": it.get("raw", "")})
    return out


_MODEL = re.compile(r"\b((?:Claude|GPT|Gemini|Llama|Grok|Mistral|DeepSeek|Qwen|Sora|Copilot|Codex)(?:[\s-](?:Opus|Sonnet|Haiku|Pro|Ultra|Flash|Nano|Mini|o\d|\d+(?:\.\d+)?|[A-Z][a-z]+)){0,3})")
_LAUNCH = re.compile(r"(?i)\b(?:unveils?|launch(?:es|ed)?|releases?|introduces?|rolls? out|debuts?|ships?|announces?|new)\b")


def _ai_angle(title: str) -> tuple[str, str]:
    """(hook, pattern) for a story about building with AI; the hook ties it to the crawler."""
    m = _MODEL.search(title or "")
    if m and _LAUNCH.search(title):
        model = " ".join(m.group(1).lower().split()[:3])
        return f"{model} is out. i tested it", "stakes"
    t = (title or "").lower()
    if re.search(r"\bagents?\b", t):
        return "everyone's building ai agents. mine reads 10-ks", "contrast"
    if re.search(r"\bcod(?:e|ing|er|ers)\b|\bsoftware\b|\bdevelopers?\b", t):
        return "ai writes code now. here's what i built", "process / pov"
    if re.search(r"\bclaude\b|\banthropic\b", t):
        return "i built wall street's web crawler with claude", "series"
    return "i built wall street's web crawler with ai", "series"


def ideas_from_news(stories: list, limit: int = 6) -> list:
    """Story angles that send the viewer to a filing or to the build. A headline's numbers are not used as facts: a
    post's numbers come from a filing page or a vault file (social/README.md rule 2); a model's version number in its
    name is cited to the story."""
    out, used = [], set()
    for s in stories:
        if len(out) >= limit:
            break
        sid = hashlib.sha1((s["url"] or s["title"]).encode()).hexdigest()[:10]
        news = {"title": s["title"], "publisher": s["publisher"], "url": s["url"], "latest": s.get("latest"), "count": s.get("count")}
        if s.get("tickers") and s["subject"] in ("finance", "crawler"):
            t = s["tickers"][0]
            from .newsdesk import COMPANIES
            name = next((k for k, v in COMPANIES.items() if v == t), t).lower()
            hook, pat = f"the spider read {poss(name)} filing. here's the line", "curiosity gap"
            if words8(hook) > 8:
                hook = f"the spider read {poss(t.lower())} filing. here's the line"
            item = {"id": "n-" + sid, "source": "news", "pattern": pat, "ticker": t, "company": "", "hook": hook, "plain": f"{name} in the news",
                    "payoff": f"load {t}'s latest filing in the Crawler and zap the line the story is about; every number from the filing",
                    "overlay": "the headline next to the spider", "numbers": [], "news": news, "needs_read": True,
                    "cards": [f"{name} is in the news", "the filing says"]}
        elif s["subject"] == "ai":
            hook, pat = _ai_angle(s["title"])
            item = {"id": "n-" + sid, "source": "news", "pattern": pat, "ticker": "", "company": "", "hook": hook, "plain": "how the crawler was built",
                    "payoff": f"this week in ai: {s['title']} ({s['publisher']}); then the crawler reading a 10-k, built with claude",
                    "overlay": "the headline next to the spider", "numbers": [], "news": news, "cards": ["built with claude", "reads any 10-k, cites every page"]}
        else:
            continue
        if item["hook"] in used:
            continue
        used.add(item["hook"])
        out.append(item)
    return out


# ---------------------------------------------------------------- the day's five
def plan(ideas: list, n: int = 5, taken: Optional[set] = None, kept: Optional[list] = None) -> list:
    """Five ideas, a mix: up to 2 from the daily routine, up to 2 from Crawler reads, 1 about building with AI or a story
    in the news, then the rest from whatever is left. Different patterns and companies where the ideas allow it.
    `kept` are the day's slots that stay (posted ones): they count toward the mix."""
    taken = set(taken or ())
    kept = kept or []
    rank = lambda x: PATTERNS.index(x["pattern"]) if x.get("pattern") in PATTERNS else 9
    by = {k: [x for x in ideas if x["source"] == k and x["id"] not in taken] for k in ("routine", "read", "news")}
    chosen = []
    pats = {x.get("pattern") for x in kept}
    cos = {(x.get("ticker") or x.get("company") or x.get("id") or "").lower() for x in kept}

    def take(x, strict):
        key = (x.get("ticker") or x.get("company") or x["id"]).lower()
        if x in chosen or (strict and ((x.get("pattern") and x["pattern"] in pats) or key in cos)):
            return False
        chosen.append(x)
        pats.add(x.get("pattern"))
        cos.add(key)
        return True
    for src, quota in (("routine", 2), ("read", 2), ("news", 1)):
        got = sum(1 for x in kept if x.get("source") == src)
        for strict in (True, False):
            for x in sorted(by[src], key=rank) if src == "read" else by[src]:
                if got >= quota or len(chosen) >= n:
                    break
                if take(x, strict):
                    got += 1
    for strict in (True, False):
        for src in ("routine", "read", "news"):
            for x in by[src]:
                if len(chosen) >= n:
                    break
                take(x, strict)
    return chosen[:n]


# ---------------------------------------------------------------- a draft
HASHTAGS = {"read": ["stocks", "investing", "finance", "earnings", "wallstreet", "10k", "webcrawler"],
            "routine": ["stocks", "investing", "finance", "earnings", "wallstreet", "webcrawler"],
            "news": ["ai", "buildinpublic", "claude", "finance", "stocks", "webcrawler"]}


def draft(idea: dict, held: list, when: str = "", platform: str = "") -> dict:
    hook = idea.get("hook") or ""
    co = idea.get("company") or ""
    who = short_name(co) if co else (idea.get("ticker") or "").lower()
    lines = [hook] + [c.lower() for c in idea.get("cards") or []]
    tags = HASHTAGS.get(idea.get("source") or "read", HASHTAGS["read"])[:]
    if idea.get("ticker"):
        tags.insert(0, idea["ticker"].lower().replace("-", ""))
    d = {"id": "d-" + hashlib.sha1(f"{idea.get('id')}|{time.time()}".encode()).hexdigest()[:10], "idea": idea.get("id"), "source": idea.get("source"),
         "status": "DRAFT", "when": when, "platform": platform or (idea.get("platform") or "tiktok, instagram").lower().replace("+", ",").replace("ig", "instagram"),
         "hook": hook, "onscreen": [x for x in lines if x][:3],
         "captions": {"plain": hook, "number": _number_first(idea) or hook, "question": _question(idea, who or "they")},
         "hashtags": list(dict.fromkeys(tags))[:8], "sources": _sources(idea), "news": idea.get("news"), "ticker": idea.get("ticker") or "",
         "company": co, "sha": idea.get("sha"), "pattern": idea.get("pattern") or "", "plain": idea.get("plain") or "", "payoff": idea.get("payoff") or "",
         "overlay": idea.get("overlay") or "", "quote": idea.get("quote"), "created": time.strftime("%Y-%m-%d %H:%M")}
    disclose(d, held)
    d["blocks"] = blocks(d)
    d["checks"] = check(d, held)
    return d


def _number_first(idea: dict) -> str:
    nums = [n for n in idea.get("numbers") or [] if n.get("shown") and n.get("where") != "derived" and n.get("label") != "pages in the document"]
    if nums:
        n = nums[0]
        return f"{n['shown']} {n['label']}{(', ' + n['period']) if n.get('period') else ''}. {idea.get('hook', '')}"
    cards = [c for c in idea.get("cards") or [] if numbers_in(c)]
    return f"{cards[0].lower()}. {idea.get('hook', '')}" if cards else ""


ROUTINE_Q = {"contradiction": "how does that add up?", "contrast": "which number tells the story?", "stakes": "what would you do with it?",
             "curiosity gap": "did you know this?", "challenge": "can you find it first?"}


def _question(idea: dict, who: str) -> str:
    p = idea.get("pattern")
    if idea.get("source") != "read":
        return ROUTINE_Q.get(p, "what should the spider read next?")
    if p == "contradiction":
        return f"how did {who} earn more on less?" if " less and earned" in (idea.get("hook") or "") else f"why did {who} earn less on more?"
    if p == "contrast":
        return f"where did {poss(who)} cash go?"
    if p == "stakes" and idea.get("source") == "read":
        return f"what would you do with a day of {poss(who)} profit?"
    if p == "challenge":
        return "can you find it before the spider does?"
    if p == "curiosity gap":
        return f"how much of your dollar does {who} keep?"
    return "what should the spider read next?"


def _sources(idea: dict) -> list:
    rows = []
    for n in idea.get("numbers") or []:
        rows.append({"shown": n["shown"], "where": n.get("where") or "", "derived": n.get("derived") or "", "quote": n.get("quote") or "",
                     "page": n.get("page"), "raw": n.get("raw"), "period": n.get("period") or ""})
    if idea.get("numbers_text"):
        for part in re.split(r";\s+(?![^()]*\))", idea["numbers_text"]):
            if part.strip():
                rows.append({"shown": "", "where": part.strip(), "derived": "", "quote": "", "page": None, "raw": None, "period": ""})
    if idea.get("quote"):
        rows.append({"shown": "(quote)", "where": idea["quote"]["where"], "derived": "", "quote": idea["quote"]["text"], "page": idea["quote"]["page"], "raw": None, "period": ""})
    if idea.get("news"):
        nw = idea["news"]
        rows.append({"shown": "(story)", "where": f"located news, not a filing: {nw['title']} ({nw['publisher']}, {(nw.get('latest') or '')[:10]}) {nw['url']}",
                     "derived": "", "quote": "", "page": None, "raw": None, "period": "", "tier3": True})
    return rows


def mentions(text: str, held: list) -> list:
    low = (text or "").lower()
    out = []
    for t in held:
        for nm in names_for(t) + [t.lower()]:
            if nm and re.search(rf"(?<![\w$]){re.escape(nm)}(?![\w])", low):
                out.append(t)
                break
    return out


def disclose(d: dict, held: list) -> None:
    """Add "i own <name>." on screen and in every caption when the post names a held company (README rule 4)."""
    text = " ".join([d["hook"], *d["onscreen"], *d["captions"].values(), d.get("ticker") or "", d.get("company") or ""])
    for t in mentions(text, held):
        line = f"i own {t.lower()}."
        if not any(line in x for x in d["onscreen"]):
            d["onscreen"].append(line)
        for k, v in d["captions"].items():
            if line not in v:
                d["captions"][k] = (v + " " + line).strip()


def blocks(d: dict) -> dict:
    tags = [f"#{t}" for t in d["hashtags"]]
    tiktok = f"{d['captions']['plain']}\n{' '.join(tags[:5])}"
    insta = f"{d['captions']['plain']}\n\nfull write-up on the bmg capital site, link in bio\n{' '.join(tags[:8])}"
    x = d["captions"]["number"] or d["captions"]["plain"]
    return {"tiktok": tiktok, "instagram": insta, "x": x}          # never cut: the check says when it is too long


def check(d: dict, held: list) -> list:
    """The Checks table, run on the draft as it stands. Each row: check, pass (True / False / None for a manual check),
    detail."""
    text_all = " ".join([d["hook"], *d["onscreen"], *d["captions"].values(), *d["blocks"].values()])
    rows = []
    srcs = d.get("sources") or []
    vals = []
    for s in srcs:
        for _, v, _u in numbers_in(s.get("shown") or ""):
            vals.append(v)
        for _, v, _u in numbers_in(s.get("where") or ""):
            vals.append(v)
        for _, v, _u in numbers_in(s.get("derived") or ""):
            vals.append(v)
    missing = []
    for shown, v, unit in numbers_in(" ".join([d["hook"], *d["onscreen"], *d["captions"].values()])):
        if not any(same_number(v, x) for x in vals):
            missing.append(shown)
    rows.append({"check": "Every number is in the Sources table", "pass": not missing,
                 "detail": "Not in Sources: " + ", ".join(dict.fromkeys(missing)) if missing else f"{len(srcs)} source rows"})
    tier3 = [s for s in srcs if s.get("tier3")]
    if tier3:
        rows.append({"check": "Story from the news is located, not verified", "pass": None,
                     "detail": "The story is a reason to read the filing; say nothing from the headline as fact."})
    hard, soft = CALL_HARD.findall(text_all), CALL_SOFT.findall(text_all)
    rows.append({"check": "No buy or sell call, no \"will go up\"", "pass": False if hard else (None if soft else True),
                 "detail": ("Remove: " + ", ".join(dict.fromkeys(hard))) if hard else (("Read again: " + ", ".join(dict.fromkeys(w.lower() for w in soft)) + " (fine if it is not advice)") if soft else "")})
    named = mentions(text_all, held)
    undisclosed = [t for t in named if f"i own {t.lower()}" not in " ".join(d["onscreen"]).lower()
                   or not all(f"i own {t.lower()}" in c.lower() for c in [*d["captions"].values(), *d["blocks"].values()])]
    rows.append({"check": "Held names disclosed (state/current.md)", "pass": not undisclosed,
                 "detail": ("Add \"i own ...\" for " + ", ".join(undisclosed)) if undisclosed else ("Disclosed: " + ", ".join(named) if named else "No held name in the post")})
    rows.append({"check": "No GBX", "pass": not GBX.search(text_all), "detail": "Nothing on GBX until it is cleared (state/current.md)" if GBX.search(text_all) else ""})
    hw = [w for w in re.findall(r"\S+", d["hook"]) if re.search(r"[A-Za-z0-9$]", w)]
    rows.append({"check": "Hook at most 8 words", "pass": len(hw) <= 8, "detail": f"{len(hw)} words"})
    upper = [s for s in [d["hook"], *d["onscreen"], *d["captions"].values()] if re.search(r"[A-Z]", re.sub(r"https?://\S+", "", s))]
    hype = HYPE.findall(text_all)
    rows.append({"check": "Lowercase, short, no hype words", "pass": not upper and not hype,
                 "detail": ("Hype: " + ", ".join(dict.fromkeys(h.lower() for h in hype)) + ". " if hype else "") + ("Capital letters in: " + "; ".join(x[:40] for x in upper[:2]) if upper else "")})
    tt = re.findall(r"#\w+", d["blocks"]["tiktok"])
    ig = re.findall(r"#\w+", d["blocks"]["instagram"])
    ok_blocks = 3 <= len(tt) <= 5 and len(ig) <= 8 and "link in bio" in d["blocks"]["instagram"]
    rows.append({"check": "TikTok 3 to 5 hashtags; Instagram up to 8 and the link in bio line", "pass": ok_blocks, "detail": f"tiktok {len(tt)}, instagram {len(ig)}"})
    nx = len(d["blocks"]["x"])
    rows.append({"check": "X post fits (280 characters)", "pass": nx <= 280,
                 "detail": f"{nx} characters" + ("; shorten the number-first caption (the X post is that caption)" if nx > 280 else "")})
    rows.append({"check": "Numbers match the current model and note version; nothing from falsified.md", "pass": None, "detail": "Owner check"})
    return rows


def refresh(d: dict, held: list) -> dict:
    """Re-run the blocks and the checks after an edit (the hook, captions, on-screen lines or sources changed)."""
    d["blocks"] = blocks(d)
    d["checks"] = check(d, held)
    return d


def slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:60].rstrip("-") or "post"


def markdown(d: dict, held: list, date: str) -> str:
    """The draft as a vault file (social/drafts/_TEMPLATE.md)."""
    plat = d.get("platform") or "tiktok"
    on = "\n".join(f"{i + 1}. {x}" for i, x in enumerate(d["onscreen"]))

    def src_row(s: dict) -> str:          # no backslash inside an f-string's braces: the Mac's Python may be older than 3.12
        derived = f" ({s['derived']})" if s.get("derived") else ""
        quote = ' "' + s["quote"][:160] + '"' if s.get("quote") else ""
        return f"| {s.get('shown') or ''} | {s.get('where') or ''}{derived}{quote} | {date} |"
    src = "\n".join(src_row(s) for s in d.get("sources") or []) or "| | | |"
    chk = "\n".join(f"| {c['check']} | {'pass' if c['pass'] is True else 'FAIL: ' + c['detail'] if c['pass'] is False else 'owner check' + (': ' + c['detail'] if c['detail'] and c['detail'] != 'Owner check' else '')} |" for c in d["checks"])
    holds = ", ".join(mentions(" ".join([d["hook"], *d["captions"].values(), d.get("ticker") or ""]), held)) or "none"
    return f"""---
status: {d.get('status') or 'DRAFT'}
platforms: {plat}
planned: {date} {d.get('when') or ''}
clip:
holds: {holds}
---

# {slug(d['hook'])}

## Hook (first 2 seconds)

{d['hook']}

## On-screen text

{on}

## Caption options

1. plain: {d['captions']['plain']}
2. number first: {d['captions']['number']}
3. question: {d['captions']['question']}

## Hashtags

{' '.join('#' + t for t in d['hashtags'])}

## Captions for cross-posting

```tiktok
{d['blocks']['tiktok']}
```

```instagram
{d['blocks']['instagram']}
```

```x
{d['blocks']['x']}
```

## Sources

| Number as shown | Source (filing page or vault file and cell or line) | As of |
|---|---|---|
{src}

## Checks

| Check | Pass or fail |
|---|---|
{chk}

Drafted in the Crawler's Posting tab; nothing was posted.
"""


class Store:
    """Drafts and the day's plan on the owner's Mac (INTAKE_DATA_DIR/posting); files are only added or replaced, never removed."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _read(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text())
        except (OSError, ValueError):
            return default

    def _write(self, name: str, data) -> None:
        p = self.root / name
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(p)

    def drafts(self) -> dict:
        return self._read("drafts.json", {})

    def put_draft(self, d: dict) -> None:
        all_ = self.drafts()
        all_[d["id"]] = d
        self._write("drafts.json", all_)

    def plan(self, day: str) -> dict:
        return self._read(f"plan-{day}.json", {})

    def put_plan(self, day: str, p: dict) -> None:
        self._write(f"plan-{day}.json", p)

    def settings(self) -> dict:
        return self._read("settings.json", {})

    def put_settings(self, s: dict) -> None:
        self._write("settings.json", s)
