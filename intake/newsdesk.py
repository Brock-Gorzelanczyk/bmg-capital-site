"""News desk for the local Crawler: the month's top stories for three subjects, from free sources, as headlines and
links only. No article is fetched, stored in full or republished; a story links to its publishers.

Subjects
  finance  markets, the Fed, the economy, deals and companies (Google News search, last 30 days)
  ai       building with AI: models, agents, coding tools (Google News search and Hacker News, last 30 days)
  crawler  what the Crawler can read now: earnings releases large companies filed with the SEC this month, and the
           month's stories that name a company with a ticker

A story is the group of headlines about the same thing: titles that share most of their words. Its score is a fixed
rule shown on the page:

  3 x publishers that ran it + the weight of the best publisher (0.3 to 3) + 2 if a headline is from the last 3 days
  + 2 x log2(Hacker News points) for a Hacker News story + 3 when a finance story names a well-known listed company

so a story many outlets covered ranks above one outlet's scoop, and a well-known outlet ranks above an aggregator.
Weights are BMG's own, listed in PUBLISHER_WEIGHT. Rankings are a reading aid, not a judgment of the news.
"""
from __future__ import annotations

import email.utils
import math
import re
import time
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import quote_plus
from xml.etree import ElementTree as ET

GOOGLE_NEWS = "https://news.google.com/rss/search?q={q}+when:30d&hl=en-US&gl=US&ceid=US:en"
HN = "https://hn.algolia.com/api/v1/search?tags=story&query={q}&numericFilters=created_at_i%3E{since},points%3E{points}&hitsPerPage={n}"

QUERIES = {
    "finance": ["stock market", "earnings results", "Federal Reserve rates", "Wall Street", "inflation economy", "IPO",
                "merger acquisition deal", "bank earnings"],
    "ai": ["AI agents", "OpenAI", "Anthropic Claude", "AI coding", "large language model", "Nvidia AI chips", "AI startup funding"],
    "crawler": ["10-K annual report", "quarterly results revenue guidance", "raises guidance", "cuts guidance", "SEC filing shows"],
}
HN_QUERIES = ["AI", "LLM", "Claude", "agents", "OpenAI", "GPT"]
SUBJECT_LABEL = {"finance": "Finance", "ai": "Building with AI", "crawler": "For the Crawler"}

PUBLISHER_WEIGHT = {
    "reuters": 3, "bloomberg": 3, "the wall street journal": 3, "wsj": 3, "financial times": 3, "the information": 2.5,
    "cnbc": 2.5, "associated press": 2.5, "ap news": 2.5, "the new york times": 2.5, "the washington post": 2,
    "barron's": 2, "marketwatch": 2, "axios": 2, "the verge": 2, "techcrunch": 2, "wired": 2, "ars technica": 2,
    "the economist": 2.5, "bbc": 2, "cnn": 2, "fortune": 1.5, "business insider": 1.5, "yahoo finance": 1.5,
    "the guardian": 1.5, "politico": 1.5, "semafor": 1.5, "fox business": 1.5, "forbes": 1, "investopedia": 1,
    "seeking alpha": 1, "venturebeat": 1.5, "zdnet": 1, "the register": 1, "ieee spectrum": 1.5, "mit technology review": 2,
    "hacker news": 1.5, "the motley fool": 0.5, "benzinga": 0.5, "msn": 0.5, "investing.com": 0.7, "zacks": 0.3,
    "tipranks": 0.3, "simply wall st.": 0.3, "marketbeat": 0.3, "globenewswire": 0.3, "pr newswire": 0.3, "business wire": 0.3,
}
DEFAULT_WEIGHT = 0.5

_STOP = frozenset("""a an the and or of to in on for at by with from as is are was were be been it its this that these those
after before over under into about than then new says say said will would could may might can not no up down out more
most less how why what who when where which amid vs via report reports reported week year years month today""".split())
_WORD = re.compile(r"[a-z0-9$%][a-z0-9$%.'\-]*")

# Well-known companies named in headlines, with their tickers. Names that are ordinary words are left out (Target,
# Block, Arm, Delta, United, Apple is kept: in business headlines it is the company).
COMPANIES = {
    "Apple": "AAPL", "Microsoft": "MSFT", "Nvidia": "NVDA", "Amazon": "AMZN", "Alphabet": "GOOGL", "Google": "GOOGL",
    "Meta": "META", "Tesla": "TSLA", "Berkshire Hathaway": "BRK-B", "JPMorgan": "JPM", "JP Morgan": "JPM", "Mastercard": "MA",
    "Walmart": "WMT", "Exxon": "XOM", "ExxonMobil": "XOM", "UnitedHealth": "UNH", "Johnson & Johnson": "JNJ",
    "Procter & Gamble": "PG", "Home Depot": "HD", "Costco": "COST", "Netflix": "NFLX", "Oracle": "ORCL",
    "Salesforce": "CRM", "Adobe": "ADBE", "AMD": "AMD", "Intel": "INTC", "Broadcom": "AVGO", "Qualcomm": "QCOM",
    "Micron": "MU", "Palantir": "PLTR", "Coinbase": "COIN", "Robinhood": "HOOD", "PayPal": "PYPL", "Goldman Sachs": "GS",
    "Morgan Stanley": "MS", "Bank of America": "BAC", "Citigroup": "C", "Citi": "C", "Wells Fargo": "WFC", "BlackRock": "BLK",
    "Charles Schwab": "SCHW", "Schwab": "SCHW", "American Express": "AXP", "Boeing": "BA", "Caterpillar": "CAT",
    "Deere": "DE", "Lockheed Martin": "LMT", "Disney": "DIS", "Comcast": "CMCSA", "Verizon": "VZ", "AT&T": "T",
    "T-Mobile": "TMUS", "Pfizer": "PFE", "Merck": "MRK", "Eli Lilly": "LLY", "AbbVie": "ABBV", "Moderna": "MRNA",
    "Nike": "NKE", "Starbucks": "SBUX", "McDonald's": "MCD", "Coca-Cola": "KO", "PepsiCo": "PEP", "Pepsi": "PEP",
    "Uber": "UBER", "Airbnb": "ABNB", "Shopify": "SHOP", "Snowflake": "SNOW", "CrowdStrike": "CRWD",
    "Palo Alto Networks": "PANW", "ServiceNow": "NOW", "IBM": "IBM", "Cisco": "CSCO", "Dell": "DELL",
    "Super Micro": "SMCI", "Supermicro": "SMCI", "TSMC": "TSM", "ASML": "ASML", "Chevron": "CVX", "ConocoPhillips": "COP",
    "Ford": "F", "General Motors": "GM", "Rivian": "RIVN", "Lucid": "LCID", "Delta Air Lines": "DAL",
    "United Airlines": "UAL", "American Airlines": "AAL", "Southwest Airlines": "LUV", "FedEx": "FDX", "UPS": "UPS",
    "Union Pacific": "UNP", "CSX": "CSX", "Norfolk Southern": "NSC", "GATX": "GATX", "Trinity Industries": "TRN",
    "Greenbrier": "GBX", "Lowe's": "LOW", "CVS": "CVS", "Walgreens": "WBA", "Humana": "HUM", "Cigna": "CI",
    "Spotify": "SPOT", "Snap": "SNAP", "Pinterest": "PINS", "Reddit": "RDDT", "Roblox": "RBLX", "DoorDash": "DASH",
    "Zoom": "ZM", "Intuit": "INTU", "AppLovin": "APP", "MicroStrategy": "MSTR", "Chipotle": "CMG", "Lululemon": "LULU",
    "GameStop": "GME", "Visa": "V", "Accenture": "ACN", "Honeywell": "HON", "3M": "MMM", "Dow": "DOW",
    "Constellation Energy": "CEG", "Vistra": "VST", "NextEra": "NEE", "Duke Energy": "DUK", "Workday": "WDAY",
    "Datadog": "DDOG", "Cloudflare": "NET", "MongoDB": "MDB", "Atlassian": "TEAM", "Carvana": "CVNA", "SoFi": "SOFI",
}
_COMPANY_RX = re.compile(r"(?<![\w&])(" + "|".join(sorted((re.escape(k) for k in COMPANIES), key=len, reverse=True)) + r")(?:'s|’s)?(?![\w&])")
_TICKER_RX = re.compile(r"\((?:NYSE|NASDAQ|Nasdaq|NYSE American|AMEX)\s*:\s*([A-Z][A-Z.\-]{0,6})\)|(?<![\w$])\$([A-Z]{1,5})\b")


def publisher_weight(name: str) -> float:
    n = (name or "").strip().lower()
    if n in PUBLISHER_WEIGHT:
        return PUBLISHER_WEIGHT[n]
    for k, w in PUBLISHER_WEIGHT.items():
        if k in n:
            return w
    return DEFAULT_WEIGHT


def tickers_in(title: str) -> list:
    out = []
    for m in _COMPANY_RX.finditer(title or ""):
        t = COMPANIES[m.group(1)]
        if t not in out:
            out.append(t)
    for m in _TICKER_RX.finditer(title or ""):
        t = m.group(1) or m.group(2)
        if t and t not in out:
            out.append(t)
    return out


def words(title: str) -> frozenset:
    return frozenset(w.strip(".'-") for w in _WORD.findall((title or "").lower()) if w.strip(".'-") not in _STOP and len(w.strip(".'-")) > 1)


def _date(s: str) -> Optional[str]:
    if not s:
        return None
    try:
        d = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def google_url(query: str) -> str:
    return GOOGLE_NEWS.format(q=quote_plus(query))


def hn_url(query: str, since: int, points: int = 80, n: int = 40) -> str:
    return HN.format(q=quote_plus(query), since=int(since), points=int(points), n=int(n))


def web_url(url) -> str:
    """The link if it is a web address (http or https), else "": a feed's link is never a script or a local file."""
    u = str(url or "").strip()
    return u if re.match(r"(?i)https?://[^\s/]", u) else ""


def parse_google(xml: bytes, subject: str, query: str) -> list:
    """Headlines from a Google News search feed. Google ends each title with " - Publisher"; that is split off."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return []
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = it.find("source")
        pub = (src.text or "").strip() if src is not None and src.text else ""
        if pub and title.endswith(" - " + pub):
            title = title[: -len(" - " + pub)].strip()
        elif " - " in title and not pub:
            title, pub = title.rsplit(" - ", 1)
        link = web_url(it.findtext("link"))
        when = _date(it.findtext("pubDate") or "")
        if not title or not link:
            continue
        out.append({"title": title, "url": link, "publisher": pub or "Unknown", "published": when, "subject": subject,
                    "via": "Google News", "query": query, "site": web_url(src.get("url") if src is not None else "")})
    return out


def parse_hn(data: dict, query: str) -> list:
    out = []
    for h in (data or {}).get("hits") or []:
        title = (h.get("title") or "").strip()
        if not title:
            continue
        item = f"https://news.ycombinator.com/item?id={h.get('objectID')}"
        out.append({"title": title, "url": web_url(h.get("url")) or item, "discussion": item, "publisher": "Hacker News", "published": _date(h.get("created_at") or ""),
                    "subject": "ai", "via": "Hacker News", "query": query, "points": int(h.get("points") or 0), "comments": int(h.get("num_comments") or 0)})
    return out


# a company scheduling its call, a filing notice, an analyst's price target, or a press release posted on the company's own
# site is not news to rank
_ANNOUNCE = re.compile(r"(?i)\b(?:to (?:announce|release|report|host|present|webcast)|schedules?|sets? (?:the )?date|will (?:report|announce|release|host)"
                       r"|conference call|webcast|to participate in|declares? (?:quarterly )?dividend|(?:earnings|results)(?: release)? (?:date|dates|call)\b"
                       r"|announces? (?:date|dates|timing))|\(Form [0-9A-Z][0-9A-Z\-/]*\)|\bSEC Filing \(|\bprice target\b")
_SELF_PUBLISHED = re.compile(r"(?i)investor relations|\binvestors?\b|newsroom|press release|\babout [A-Z]|news hub|\| ir\b")


def _alike(a: dict, b: dict) -> bool:
    """Two headlines about the same thing: they share at least 3 words, and the words they share include words that are
    rare in the month's headlines (a company, a person, a product), not only "earnings", "stock" and "rates"."""
    inter = a["_w"] & b["_w"]
    if len(inter) < 3:
        return False
    if a["_t"] and b["_t"] and not (a["_t"] & b["_t"]):
        return False                     # the same words about two different companies are two stories
    rare = len(inter & a["_r"])
    jac = len(inter) / len(a["_w"] | b["_w"])
    cover = len(inter) / min(len(a["_w"]), len(b["_w"]))
    return (rare >= 2 and cover >= 0.5) or (rare >= 1 and jac >= 0.6)


def stories(items: list, now: Optional[float] = None) -> list:
    """Group headlines about the same thing and score each group (the rule in the module note)."""
    now = now or time.time()
    seen, rows = set(), []
    for it in items:
        key = (it["url"], it["title"].lower())
        if key in seen or _ANNOUNCE.search(it["title"]):
            continue
        seen.add(key)
        it = dict(it)
        it["_w"] = words(it["title"])
        it["_t"] = frozenset(tickers_in(it["title"]))
        rows.append(it)
    df: dict = {}
    for it in rows:
        for w in it["_w"]:
            df[w] = df.get(w, 0) + 1
    cut = max(3, len(rows) * 0.02)
    for it in rows:
        it["_r"] = frozenset(w for w in it["_w"] if df[w] <= cut)
    # each group is compared through its first headline; only groups that share a rare word are compared at all
    index: dict = {}
    groups: list = []
    for it in sorted(rows, key=lambda x: -publisher_weight(x["publisher"])):
        cand = set()
        for w in it["_r"]:
            cand.update(index.get(w, ()))
        home = next((g for g in sorted(cand) if _alike(it, groups[g][0])), None)
        if home is None:
            home = len(groups)
            groups.append([])
            for w in it["_r"]:
                index.setdefault(w, set()).add(home)
        groups[home].append(it)
    out = []
    recent = time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(now - 3 * 86400))
    for its in groups:
        pubs = []
        for x in sorted(its, key=lambda x: -_weight(x)):
            if x["publisher"] not in pubs:
                pubs.append(x["publisher"])
        best = max(its, key=lambda x: (_weight(x), x.get("points") or 0, x.get("published") or ""))
        dates = sorted(x["published"] for x in its if x.get("published"))
        points = max((x.get("points") or 0) for x in its)
        subjects = {x["subject"] for x in its}
        tick = []
        for x in its:
            for t in tickers_in(x["title"]):
                if t not in tick:
                    tick.append(t)
        score = 3 * len(pubs) + _weight(best) + (2 if dates and dates[-1] >= recent else 0) + (2 * math.log2(points) if points > 1 else 0) \
            + (3 if tick and best["subject"] in ("finance", "crawler") else 0)       # a company people know, that the Crawler can read
        out.append({"title": best["title"], "url": best["url"], "publisher": best["publisher"], "publishers": pubs[:8], "count": len(pubs),
                    "first": dates[0] if dates else None, "latest": dates[-1] if dates else None, "subjects": sorted(subjects),
                    "subject": best["subject"], "tickers": tick,
                    "points": points or None, "comments": max((x.get("comments") or 0) for x in its) or None,
                    "discussion": next((x.get("discussion") for x in its if x.get("discussion")), None),
                    "headlines": [{"title": x["title"], "url": x["url"], "publisher": x["publisher"], "published": x.get("published")}
                                  for x in sorted(its, key=lambda x: -_weight(x))[:6]],
                    "score": round(score, 2)})
    out.sort(key=lambda s: (-s["score"], s["latest"] or ""))
    return out


def _weight(x: dict) -> float:
    return 0.2 if _SELF_PUBLISHED.search(x.get("publisher") or "") else publisher_weight(x.get("publisher") or "")


def rank_filers(hits: list, revenue: dict, limit: int = 30) -> list:
    """This month's earnings releases (8-K item 2.02) by company, largest companies first (last full year's revenue
    from SEC XBRL frames); one row per company, its latest release."""
    by: dict = {}
    for h in hits:
        if not h.get("ticker"):
            continue
        cur = by.get(h["cik"])
        if cur is None or (h.get("filed") or "") > (cur.get("filed") or ""):
            by[h["cik"]] = h
    rows = []
    for cik, h in by.items():
        rv = revenue.get(cik)
        rows.append({"title": f"{h['company']} filed its earnings release", "company": h["company"], "ticker": h["ticker"], "cik": cik,
                     "filed": h["filed"], "accession": h["accession"], "document": h["document"], "form": h["form"], "url": h["url"],
                     "revenue": rv[0] if rv else None, "revenue_year": rv[1] if rv else None, "subject": "crawler", "via": "SEC EDGAR"})
    rows.sort(key=lambda r: (-(r["revenue"] or 0), r["filed"]), reverse=False)
    return rows[:limit]
