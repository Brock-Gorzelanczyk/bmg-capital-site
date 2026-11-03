"""What the numbers mean: a headline and three to five takeaways for each document, written the way the first page of a
sell-side note reads (research/methodology/research-note-anatomy.md): the headline states the conclusion, and each point
makes one claim, gives its numbers with their pages, and says what they mean in plain words, then what to watch.

Built only from what the read already holds: the report's figures and calculations (each with its page) and the quotes
in the insights (guidance, red flags, a call's questions, insider trades, fund holdings). Connecting two figures is the
point (operating income against sales, net income against operating income, earnings per share against net income,
capital spending against operating cash flow, a segment against the whole), because that is where a number starts to
mean something. The reading is BMG's interpretation and says so; it is never a rating, a price target, or a call to buy
or sell, and nothing here is a forecast of its own (an outlook is management's, quoted)."""
from __future__ import annotations

import math
import re
from datetime import date
from typing import Optional

from .facts import fmt_value

VERSION = 1
BASIS = ("BMG's reading of the figures in this document, each linked to its page. Interpretation, not a rating, "
         "a price target or a recommendation.")

# reading order of a first page: the results, where they came from, what sits between operating profit and the
# bottom line, the outlook, cash, the balance sheet, then the disclosures
ORDER = {"flag_severe": 0, "growth": 1, "segments": 2, "below": 3, "per_share": 4, "guide": 5, "cash": 6, "invest": 7,
         "balance": 8, "roe": 9, "gains": 10, "maturity": 11, "flag": 12,
         "ins_notable": 0, "ins_direction": 1, "ins_plans": 2, "ins_stake": 3, "ins_day": 4, "ins_largest": 5, "ins_who": 6,
         "fund_concentration": 1, "fund_added": 2, "fund_cut": 3, "fund_new": 4,
         "call_guide": 1, "call_focus": 2, "call_claim": 3, "call_cast": 4}

SEVERE_FLAGS = {"going_concern", "material_weakness", "restatement", "auditor_change", "covenant", "delisting", "late_filing"}

_SUFFIX = re.compile(r"(?i)(?:[,\s]+(?:inc\.?|incorporated|corp\.?|corporation|co\.?|company|ltd\.?|limited|plc|n\.v\.|s\.a\.|"
                     r"holdings?|group|l\.p\.|lp)|\s*/[a-z]{2,3}/?|\s*&\s*co\.?)\s*$")


# ---------------------------------------------------------------------- small helpers
def _pct(cur: Optional[float], prior: Optional[float]) -> Optional[float]:
    """Growth in percent, or None where it means nothing (a missing, zero or negative base, a negative result)."""
    if cur is None or prior is None or prior <= 0 or cur < 0:
        return None
    return (cur / prior - 1) * 100


def _move(x: float, d: int = 1) -> str:
    if x is None:
        return ""
    if abs(x) < 0.05:
        return "flat"
    return f"{'up' if x > 0 else 'down'} {abs(x):.{d}f}%"


def _money(v: Optional[float]) -> str:
    return fmt_value(v, "USD") if v is not None else ""


_GENERIC = re.compile(r"^(?:Three|Six|Nine|Twelve|Fiscal|Quarter|Year|As|First|Second|Third|Fourth|Reported)\b")


def _low(s: str) -> str:
    """"Quarter ended ..." in the middle of a sentence; a month or a name keeps its capital."""
    return s[:1].lower() + s[1:] if s and _GENERIC.match(s) else s


_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _day(s: str) -> str:
    """2025-11-10 as Nov 10, 2025."""
    try:
        d = date.fromisoformat((s or "")[:10])
    except ValueError:
        return s or ""
    return f"{_MONTHS[d.month - 1]} {d.day}, {d.year}"


def poss(name: str) -> str:
    return name + ("'" if name.endswith("s") else "'s")


_ENTITY = re.compile(r"(?i)\b(?:llc|inc|corp|trust|fund|capital|partners|holdings|lp|l\.p|ltd|group|foundation|management)\b")


def person(name: str) -> str:
    """SEC's "Musk Kimbal" (last name first) as Kimbal Musk; a company or trust as it is."""
    ws = (name or "").split()
    if 2 <= len(ws) <= 3 and not _ENTITY.search(name) and all(re.fullmatch(r"[A-Za-z][A-Za-z.'\-]*", w) for w in ws):
        return " ".join(ws[1:] + ws[:1])
    return name or ""


_SMALL = {"of", "and", "the", "for", "in", "on", "at", "to", "a", "an", "&"}


def issuer(name: str) -> str:
    """A 13F issuer as written in prose: "BANK OF AMER CORP" as Bank of Amer, "APPLE INC" as Apple."""
    n = (name or "").strip()
    for _ in range(3):
        n = _SUFFIX.sub("", n).strip(" ,.")
    if n.isupper() or n.islower():
        n = " ".join(w if (k and w in _SMALL) else w.capitalize() for k, w in enumerate(n.lower().split()))
    return n or name or ""


def _cut(s: str, n: int = 240) -> str:
    s = " ".join((s or "").split())
    if len(s) <= n:
        return s
    i = s.rfind(" ", 0, n - 3)
    return s[:i if i > n // 2 else n - 3].rstrip(",;: ") + "..."


def who(rep: dict, ins: dict) -> str:
    """The company's short name: "Apple", "Amazon.com", "Costco Wholesale" (an all-capitals SEC name in title case),
    else the ticker."""
    co = rep.get("company") or {}
    call = ((ins.get("call") or {}).get("company") or {}) if ins else {}
    n = (co.get("name") or co.get("name_hint") or call.get("name") or (ins.get("holdings_summary") or {}).get("manager")
         or (ins.get("summary") or {}).get("company") or "").strip()
    for _ in range(3):
        n = _SUFFIX.sub("", n).strip(" ,.")
    if n.isupper():
        n = " ".join(w if len(w) <= 4 else w.capitalize() for w in n.split())
    return n or co.get("ticker") or co.get("ticker_hint") or call.get("ticker") or ""


def when(rep: dict, ins: dict) -> str:
    end = (ins or {}).get("ending") or {}
    return _low(end.get("subtitle") or (rep.get("company") or {}).get("period") or "")


def _item(id_: str, title: str, detail: str, means: str, watch: str = "", refs=None, quotes=None, tone: str = "=",
          short: str = "", score: int = 50) -> dict:
    return {"id": id_, "title": title, "detail": detail, "means": means, "watch": watch, "refs": [r for r in (refs or []) if r],
            "quotes": [q for q in (quotes or []) if q and q.get("page")], "tone": tone, "_short": short or _low(title), "_score": score}


def _quote(x: dict, text: Optional[str] = None) -> Optional[dict]:
    if not x:
        return None
    src = x.get("source") if isinstance(x.get("source"), dict) else {}
    page = x.get("page") or src.get("pdf_page")
    off = x.get("offset") if x.get("offset") is not None else src.get("offset")
    return {"page": page, "offset": off, "text": _cut(text or x.get("text") or src.get("excerpt") or "", 160)} if page else None


# ---------------------------------------------------------------------- the figures of a report
class Book:
    def __init__(self, rep: dict):
        ev = rep.get("evidence") or {}
        self.F, self.C = ev.get("facts") or {}, ev.get("calcs") or {}
        fin = rep.get("financials") or {}
        self.rows = {}
        for r in (fin.get("performance") or []) + (fin.get("calculated") or []) + ((rep.get("position") or {}).get("rows") or []):
            if r.get("current") and r["metric"] not in self.rows:
                self.rows[r["metric"]] = r
        self.p3 = {r["metric"]: r["ids"] for r in ((rep.get("performance3") or {}).get("rows") or [])}
        self.sig = {s["id"]: s for s in rep.get("signals") or []}
        prof = " ".join(str(x) for x in ((rep.get("operating") or {}).get("profile"), (rep.get("brief") or {}).get("profile")) if x)
        self.financial = bool(re.search(r"(?i)\bbank|insur|broker|asset manag", prof))

    def get(self, i):
        return (self.F.get(i) or self.C.get(i)) if i else None

    def pair(self, metric: str):
        """(current, prior) figures of a metric, the prior only when it is the same kind of period."""
        r = self.rows.get(metric)
        cur, prior = (self.get(r.get("current")), self.get(r.get("prior"))) if r else (None, None)
        if (not cur or cur.get("value") is None) and metric in self.p3:
            ids = self.p3[metric]
            cur, prior = self.get(ids[0]), (self.get(ids[1]) if len(ids) > 1 else None)
        if not cur or cur.get("value") is None:
            return None, None
        if prior is not None and (prior.get("value") is None or (prior.get("period") or {}).get("type") != (cur.get("period") or {}).get("type")):
            prior = None
        return cur, prior

    def val(self, metric: str):
        cur, _ = self.pair(metric)
        return cur["value"] if cur else None


def _ids(*fs) -> list:
    return [f["id"] for f in fs if f and f.get("id")]


def _per(f: dict) -> str:
    p = (f or {}).get("period") or {}
    if p.get("type") == "FY" and p.get("short"):
        return p["short"]                                  # FY2025
    return _low(p.get("label") or p.get("short") or "")


def _a_year_earlier(cur: dict, prior: dict) -> str:
    try:
        a, b = date.fromisoformat(cur["period"]["end"]), date.fromisoformat(prior["period"]["end"])
        if 330 <= (a - b).days <= 400:
            return "a year earlier"
    except (KeyError, TypeError, ValueError):
        pass
    return _per(prior) or "the prior period"


# ---------------------------------------------------------------------- results: growth and where profit went
def _growth(b: Book, kind: str) -> Optional[dict]:
    """Sales growth, read against operating profit: whether the growth reached profit (the margin) is what makes it
    good or bad growth."""
    rev, rev_p = b.pair("revenue")
    if not rev or not rev_p:
        return None
    g = _pct(rev["value"], rev_p["value"])
    if g is None:
        return None
    sales = "Net revenue" if b.financial else "Sales"
    sl = sales.lower()
    if g >= 95:
        verb = "more than doubled" if g >= 105 else "doubled"
        title, short = f"{sales} {verb} to {rev['display']} ({g:+.1f}%)", f"{sl} {verb}"
    elif abs(g) < 1:
        title, short = f"{sales} flat at {rev['display']} ({g:+.1f}%)", f"{sl} flat"
    else:
        title, short = f"{sales} {_move(g)} to {rev['display']}", f"{sl} {_move(g)}"
    detail = f"{rev['name']} of {rev['display']} against {rev_p['display']} {_a_year_earlier(rev, rev_p)} ({_per(rev)})."
    oi, oi_p = b.pair("operating_income")
    om, om_p = b.pair("operating_margin")
    refs = _ids(rev, rev_p)
    h = _pct(oi["value"], oi_p["value"]) if oi and oi_p else None
    if h is not None:
        dm = (om["value"] - om_p["value"]) if om and om_p else None
        detail += f" Operating income {_move(h)} to {oi['display']}" + (f"; operating margin {om['display']}, {'up' if dm >= 0 else 'down'} from {om_p['display']}." if dm is not None else ".")
        refs += _ids(oi, oi_p)
        if h - g >= 5 and (dm is None or dm > 0.2):
            if g < 95:
                title = f"Operating income {_move(h, 0)} on {sl} {_move(g)}"
            short = f"{short} and operating income {_move(h, 0)}" if g >= 95 else f"operating income {_move(h, 0)} on {sl} {_move(g)}"
            means = ("Profit grew much faster than sales: costs rose more slowly than revenue, so more of each extra dollar of sales "
                     "reached operating profit" + (f" ({om['display']} of sales now, against {om_p['display']})." if dm is not None else "."))
            watch, tone = "Whether the wider margin holds next period; a margin that keeps widening means the growth is profitable.", "+"
        elif g - h >= 5 and (dm is None or dm < -0.2):
            if h < 0 <= g:
                title, short = f"{sales} {_move(g)} but operating income {_move(h)}", f"{short} but operating income {_move(h, 0)}"
            else:
                short = f"{short}, operating income {_move(h, 0)}"
            means = ("Costs grew faster than sales, so less of each dollar of sales reached operating profit"
                     + (f" ({om['display']} of sales, against {om_p['display']})." if dm is not None else "."))
            watch, tone = "Whether costs settle next period; a margin that keeps falling means growth is getting more expensive.", "-"
        else:
            short = f"{short} and operating income {_move(h, 0)}"
            means = (f"Operating profit moved about in step with sales ({_move(h)}), so the margin held near {om['display'] if om else 'its level'}"
                     + (": the growth reached profit at the same rate as before." if g > 0 else "."))
            watch, tone = ("The next period's sales growth: with the margin steady, profit follows sales." if g > 0 else
                           "Whether sales stabilize: with the margin steady, profit follows sales down."), ("+" if g > 0 else "-")
    else:
        ni, ni_p = b.pair("net_income")
        n = _pct(ni["value"], ni_p["value"]) if ni and ni_p else None
        if n is not None:
            detail += f" Net income {_move(n)} to {ni['display']}."
            refs += _ids(ni, ni_p)
        if g >= 25:
            means = "Growth this fast usually comes from a product cycle or a jump in demand; how long it lasts matters more than the rate itself."
        elif g >= 8:
            means = "Strong growth: the business is selling more, charging more, or both."
        elif g >= 1:
            means = "Low single-digit growth: a mature business, where profit growth has to come from margins or fewer shares."
        elif g > -1:
            means = "Sales did not move: any change in profit came from costs, not from the top line."
        else:
            means = "Shrinking sales: costs have to fall as fast to protect profit."
        tone = "+" if g >= 1 else "-" if g <= -1 else "="
        if n is not None and n < g - 5:
            title, short, tone = f"{sales} {_move(g)} but net income {_move(n)}", f"{short} but net income {_move(n)}", "-"
            means = ("Revenue grew but profit did not keep up: expenses" + (" or credit costs" if b.financial else "") + " rose faster than revenue.")
        elif n is not None and n > g + 5:
            short = f"{short} and net income {_move(n)}"
        watch = "The next period's growth rate against this one."
    return _item("growth", title, detail, means, watch, refs, tone=tone, short=short, score=95)


def _below(b: Book) -> Optional[dict]:
    """Net income against operating income: what sits between them (interest, investment gains, tax) when it moved
    the bottom line far from the business."""
    oi, oi_p = b.pair("operating_income")
    ni, ni_p = b.pair("net_income")
    if not (oi and oi_p and ni and ni_p):
        return None
    h, n = _pct(oi["value"], oi_p["value"]), _pct(ni["value"], ni_p["value"])
    if h is None or n is None or abs(n - h) < 8:
        return None
    pt, pt_p = b.pair("pretax_income")
    tx = tx_p = no = no_p = None
    if pt and pt_p and pt["value"] > 0 and pt_p["value"] > 0:
        tx, tx_p = (1 - ni["value"] / pt["value"]) * 100, (1 - ni_p["value"] / pt_p["value"]) * 100
        no, no_p = pt["value"] - oi["value"], pt_p["value"] - oi_p["value"]
    refs = _ids(oi, oi_p, ni, ni_p, pt, pt_p)
    detail = (f"Operating income {_move(h)} to {oi['display']}; net income {_move(n)} to {ni['display']}"
              + (f"; pretax income {pt['display']} against {pt_p['display']}." if pt and pt_p else "."))
    tax_moved = tx is not None and abs(tx - tx_p) >= 2
    nonop_moved = no is not None and abs(no - no_p) >= 0.05 * max(abs(oi_p["value"]), 1)
    if n > h:
        if tax_moved and tx < tx_p and (not nonop_moved or (tx_p - tx) / 100 * pt["value"] >= abs(no - no_p)):
            title = f"Net income {_move(n)}, well ahead of operating income ({_move(h)}), on a lower tax rate"
            detail += f" Taxes and other items took {tx:.1f}% of pretax income, against {tx_p:.1f}% {_a_year_earlier(pt, pt_p)}."
            means = ("Part of the earnings growth came from a lower tax bill, not from the business. The operating line is the "
                     "better guide to the trend.")
            watch = "The tax rate next period: if it goes back up, earnings growth falls back toward the operating growth."
            short = f"net income {_move(n, 0)} helped by a lower tax rate"
        elif nonop_moved and no > no_p:
            title = f"Net income {_move(n)}, ahead of operating income ({_move(h)})"
            detail += f" Items below operating income (interest, investment gains and other) came to {_money(no)}, against {_money(no_p)} {_a_year_earlier(pt, pt_p)}."
            means = "Part of the earnings growth came from outside the core business; those items do not necessarily repeat."
            watch = "Operating income, the cleaner read of the business."
            short = f"net income {_move(n, 0)} helped by non-operating items"
        else:
            title = f"Net income {_move(n)}, ahead of operating income ({_move(h)})"
            means = "Something between operating profit and the bottom line (interest, gains, taxes) added to earnings this period."
            watch = "Operating income, the cleaner read of the business."
            short = f"net income {_move(n, 0)}"
        tone = "="
    else:
        if nonop_moved and no < no_p:
            title = (f"Net income {_move(n)} though operating income rose {h:.0f}%" if h > 0 and n <= 0 else
                     f"Net income {_move(n)}, behind operating income ({_move(h)})")
            detail += f" Items below operating income (interest, investment gains and other) came to {_money(no)}, against {_money(no_p)} {_a_year_earlier(pt, pt_p)}."
            means = ("The business itself did better; the bottom line fell back because "
                     + ("last year's figure included a non-operating gain that did not repeat."
                        if no_p > 0 and no < no_p else "items below operating income cost more this time."))
            watch = "Operating income is the cleaner read of the business; items below it swing with investment values and interest."
            short = f"net income {_move(n, 0)} on a non-operating swing"
        elif tax_moved and tx > tx_p:
            title = f"Net income {_move(n)}, behind operating income ({_move(h)}), on a higher tax rate"
            detail += f" Taxes and other items took {tx:.1f}% of pretax income, against {tx_p:.1f}% {_a_year_earlier(pt, pt_p)}."
            means = "A higher tax bill, not the business, held back earnings."
            watch = "The tax rate next period."
            short = f"net income {_move(n, 0)} on a higher tax rate"
        else:
            title = f"Net income {_move(n)}, behind operating income ({_move(h)})"
            means = "Something between operating profit and the bottom line (interest, losses, taxes) took more this period."
            watch = "Operating income, the cleaner read of the business."
            short = f"net income {_move(n, 0)}"
        tone = "-" if n < 0 else "="
    return _item("below", title, detail, means, watch, refs, tone=tone, short=short, score=82 if abs(n - h) >= 10 else 62)


def _per_share(b: Book) -> Optional[dict]:
    eps, eps_p = b.pair("eps_diluted")
    ni, ni_p = b.pair("net_income")
    if not (eps and eps_p and ni and ni_p):
        return None
    e, n = _pct(eps["value"], eps_p["value"]), _pct(ni["value"], ni_p["value"])
    if e is None or n is None:
        return None
    sh = ((1 + n / 100) / (1 + e / 100) - 1) * 100            # the change in diluted shares the two growth rates imply
    if abs(sh) < 1.5:
        return None
    bb, _ = b.pair("buybacks")
    refs = _ids(eps, eps_p, ni, ni_p, bb)
    detail = (f"Diluted earnings per share {eps['display']} against {eps_p['display']} ({_move(e)}); net income {_move(n)}. "
              f"The gap implies about {abs(sh):.1f}% {'fewer' if sh < 0 else 'more'} diluted shares.")
    if sh < 0:
        title = f"Earnings per share {_move(e)}, ahead of net income ({_move(n)})"
        means = ("Buybacks shrank the share count, so each remaining share earned more"
                 + (f": the company bought back {bb['display'].lstrip('-')} of stock ({_per(bb)})." if bb and bb.get("value") else "."))
        return _item("per_share", title, detail, means, "Buybacks add to per-share growth only while they continue.", refs, tone="+",
                     short=f"earnings per share {_move(e, 1)}", score=64 if abs(sh) >= 3 else 52)
    title = f"Earnings per share {_move(e)}, behind net income ({_move(n)})"
    means = "More shares (stock pay or new issuance) spread the profit thinner, so each share's slice grew less than the company's profit."
    return _item("per_share", title, detail, means, "The share count: dilution keeps per-share growth below profit growth.", refs, tone="-",
                 short=f"earnings per share {_move(e, 1)}", score=58)


# ---------------------------------------------------------------------- cash, investment, balance sheet
def _cash(b: Book, rep: dict) -> Optional[dict]:
    s = b.sig.get("cash_conversion")
    if not s or b.financial or s.get("value") is None:
        return None
    cc = s["value"]
    cfo, ni = b.get((s.get("inputs") or [None])[0]), b.get((s.get("inputs") or [None, None])[1])
    if not cfo or not ni:
        return None
    refs = _ids(cfo, ni)
    items = [b.get(i) for i in ((rep.get("earnings_cash") or {}).get("items") or [])]
    items = [x for x in items if x and x.get("value") is not None]
    if cc < 0.8:
        gain = next((x for x in items if x["value"] < 0 and re.search(r"(?i)gain", x.get("name") or "")), None)
        noncash = next((x for x in sorted(items, key=lambda x: x["value"]) if x["value"] < 0), None)
        hint = gain or noncash
        detail = (f"Operating cash flow of {cfo['display']} against net income of {ni['display']} ({_per(cfo)})."
                  + (f" The largest line taking cash back out of profit: \"{hint['name']}\" {hint['display']}." if hint else ""))
        refs += _ids(hint)
        means = ("Profit ran ahead of cash this period. The usual reasons are money tied up in receivables or inventory, or gains "
                 "counted in profit that bring in no cash" + (" (as here)." if gain else "."))
        return _item("cash", f"Only {cc * 100:.0f}% of net income arrived as cash", detail, means,
                     "Whether cash catches up next period; a gap that keeps growing is a warning sign about the quality of earnings.",
                     refs, tone="-", short="cash lagged profit", score=72)
    if cc >= 1.3:
        dep = next((x for x in items if re.search(r"(?i)depreciation", x.get("name") or "") and x["value"] > 0), None)
        detail = (f"Operating cash flow of {cfo['display']} against net income of {ni['display']} ({_per(cfo)})."
                  + (f" Depreciation and amortization, a cost that uses no cash, was {dep['display']}." if dep else ""))
        refs += _ids(dep)
        return _item("cash", f"Cash backs the profit: operating cash flow {cc:.1f}x net income", detail,
                     "Reported profit turned into cash, and more: noncash costs such as depreciation are charged against profit but take no cash.",
                     "", refs, tone="+", short="cash flow well above profit", score=36)
    return None


def _invest(b: Book, rev_period_ok: bool = True) -> Optional[dict]:
    fcf = b.get("fcf:cur")
    cfo, _ = b.pair("cfo")
    capex, _ = b.pair("capex")
    if b.financial or not fcf or fcf.get("value") is None or not cfo or not capex:
        return None
    refs = _ids(fcf, cfo, capex)
    per = _per(fcf)
    nc, nc_p = b.get("net_cash:cur"), b.get("net_cash:prior")
    if fcf["value"] < 0:
        detail = (f"Capital spending of {capex['display'].lstrip('-')} against operating cash flow of {cfo['display']} ({per}): "
                  f"free cash flow of {fcf['display']}.")
        means = "The company is spending more on capacity than its operations bring in, a bet on future demand. The gap is paid from cash on hand or new borrowing"
        if nc and nc_p and nc.get("value") is not None and nc_p.get("value") is not None and nc["value"] < nc_p["value"] and nc["value"] < 0:
            moved, rose, mrefs = _why_net_debt_moved(b)
            means += (f": net debt is {_money(-nc['value'])} ({_per(nc)}), against {'net cash of ' + _money(nc_p['value']) if nc_p['value'] >= 0 else _money(-nc_p['value'])} "
                      f"({_per(nc_p)})" + (f", with {' and '.join(moved)}." if moved else "."))
            refs += _ids(nc, nc_p) + mrefs
        else:
            means += "."
        return _item("invest", "Spending ran ahead of cash: free cash flow was negative", detail, means,
                     "Whether the spending turns into faster sales growth, and how it is funded.", refs, tone="-",
                     short="heavy investment turned free cash flow negative", score=86)
    rev, _ = b.pair("revenue")
    margin = None
    if rev and (rev.get("period") or {}).get("end") == (fcf.get("period") or {}).get("end") and (rev.get("period") or {}).get("type") == (fcf.get("period") or {}).get("type") and rev["value"] > 0:
        margin = fcf["value"] / rev["value"] * 100
    cr = b.get("capital_returned:cur")
    pay = b.sig.get("payout_vs_fcf")
    p = pay["value"] if pay and pay.get("value") is not None else None
    detail = (f"Operating cash flow of {cfo['display']} less capital spending of {capex['display'].lstrip('-')} ({per})"
              + (f", {margin:.0f}% of sales." if margin is not None else "."))
    if cr and cr.get("value") and p is not None:
        detail += f" Buybacks and dividends: {cr['display'].lstrip('-')}, {p:.0f}% of free cash flow."
        refs += _ids(cr)
        if p > 100:
            means = ("The company handed back more cash than the business produced, so the difference came from cash on hand or "
                     "borrowing. That can run for a while, not forever.")
            watch, tone, score, short = "Payouts against free cash flow next period, and the cash and debt that fund the gap.", "=", 74, f"paid out {p:.0f}% of free cash flow"
        elif p >= 50:
            means = "Most of the cash left after investment went back to shareholders."
            watch, tone, score, short = "", "+", 56, f"free cash flow of {fcf['display']}"
        else:
            means = "Most of the cash left after investment stayed in the company, building cash or paying for other uses."
            watch, tone, score, short = "What the company does with the cash it keeps.", "+", 54, f"free cash flow of {fcf['display']}"
    else:
        means = "That is the cash left after investment: what pays for dividends, buybacks, debt repayment or acquisitions."
        watch, tone, score, short = "", "+", 46, f"free cash flow of {fcf['display']}"
    return _item("invest", f"Free cash flow of {fcf['display']}" + (f", {margin:.0f}% of sales" if margin is not None else ""), detail, means,
                 watch, refs, tone=tone, short=short, score=score)


def _why_net_debt_moved(b: Book) -> tuple:
    """Why net debt moved, from its two parts: (words, borrowing_rose, refs). Cash here is cash and cash equivalents
    only, as in the net cash calculation; marketable securities are not counted, so a fall in cash can be money moved
    into securities rather than spent."""
    d, d_p = b.pair("total_debt")
    c, c_p = b.pair("cash")
    words, rose = [], False
    if d and d_p and d_p["value"] > 0:
        x = _pct(d["value"], d_p["value"])
        if x is not None and abs(x) >= 10:
            words.append(f"total debt {_move(x, 0)} to {d['display']}")
            rose = x > 0
    if c and c_p and c_p["value"] > 0:
        y = _pct(c["value"], c_p["value"])
        if y is not None and abs(y) >= 10:
            words.append(f"cash and equivalents {_move(y, 0)} to {c['display']}")
    return words, rose, _ids(d, d_p, c, c_p)


def _balance(b: Book, invest: Optional[dict] = None) -> Optional[dict]:
    if b.financial:
        return None
    nc, nc_p = b.get("net_cash:cur"), b.get("net_cash:prior")
    if not nc or nc.get("value") is None:
        return None
    cov = b.sig.get("interest_coverage")
    lev = [s for k, s in b.sig.items() if k in ("debt_to_assets", "debt_to_equity", "equity_multiplier", "interest_coverage") and s.get("severity") in ("flag", "watch")]
    refs = _ids(nc, nc_p) + list((cov or {}).get("inputs") or [])
    note = " Cash here is cash and cash equivalents; marketable securities are not counted."
    if nc["value"] >= 0:
        detail = f"Cash and cash equivalents exceed total debt by {nc['display']} ({_per(nc)})." + note
        return _item("balance", f"Net cash of {nc['display']}: more cash than debt", detail,
                     "Debt is not a constraint: it could be repaid from cash on hand.", "", refs, tone="+",
                     short=f"net cash of {nc['display']}", score=44)
    nd = -nc["value"]
    moved, rose, mrefs = _why_net_debt_moved(b)
    refs += mrefs
    swing, score = "", 48
    if nc_p and nc_p.get("value") is not None:
        if nc_p["value"] >= 0:
            swing = f", from net cash of {nc_p['display']} ({_per(nc_p)})"
        elif nd > -nc_p["value"] * 1.1:
            swing = f", up from {_money(-nc_p['value'])} ({_per(nc_p)})"
        elif nd < -nc_p["value"] * 0.9:
            swing = f", down from {_money(-nc_p['value'])} ({_per(nc_p)})"
        if swing and "down" not in swing:
            score = 66 if rose else 50
    c = cov["value"] if cov and cov.get("value") is not None else None
    title = f"Net debt of {_money(nd)}{swing.split(' (')[0]}" + (f"; interest covered {c:.1f}x" if c is not None and c < 50 else "")
    detail = (f"Total debt exceeds cash and cash equivalents by {_money(nd)} ({_per(nc)}){swing}."
              + (f" Behind the change: {'; '.join(moved)}." if moved and swing else "")
              + (f" Pretax income plus interest is {c:.1f} times the interest bill." if c is not None else "") + note)
    fcf = b.get("fcf:cur")
    if c is not None and c >= 50:
        means, tone = "Interest is a rounding error next to profit: the debt is no burden at this level of earnings.", "="
    elif c is not None and c >= 8:
        means, tone = (f"The debt is easy to carry: interest takes about {100 / c:.0f}% of profit before interest and tax.", "=")
    elif c is not None and c >= 4:
        means, tone = (f"Interest is covered, with a thinner cushion: it takes about {100 / c:.0f}% of profit before interest and tax.", "=")
    elif c is not None:
        means, tone = (f"Interest takes about {100 / c:.0f}% of profit before interest and tax: less room if earnings fall or rates rise.", "-")
        score = max(score, 76)
    elif fcf and fcf.get("value") and fcf["value"] > 0 and (fcf.get("period") or {}).get("type") == "FY":
        yrs = nd / fcf["value"]
        refs += _ids(fcf)
        means = (f"Net debt equals about {yrs:.1f} years of free cash flow ({fcf['display']}): "
                 + ("a light load." if yrs < 2 else "a moderate load." if yrs < 5 else "a heavy load, most of a decade of free cash flow."))
        tone = "=" if yrs < 5 else "-"
    else:
        means, tone = "Debt exceeds cash on hand; how easily it is carried depends on the cash flow that services it.", "="
    if lev:
        score = max(score, 70)
    if rose and swing and "down" not in swing:
        watch = "Borrowing is rising: what the money pays for, and whether profit keeps pace."
    elif swing and "down" not in swing:
        watch = "Cash fell rather than debt rising: where the cash went (buybacks, investment, securities)."
    else:
        watch = ""
    if invest and "net debt" in invest.get("means", ""):
        score -= 20                    # the investment point already says it
    return _item("balance", title, detail, means, watch, refs, tone=tone, short=f"net debt of {_money(nd)}", score=score)


def _roe(b: Book) -> Optional[dict]:
    s = b.sig.get("roe")
    if not s or not b.financial or s.get("value") is None:
        return None
    v = s["value"]
    return _item("roe", s["headline"], s["text"], f"Each dollar of shareholders' capital earned about {v:.0f} cents in the period.",
                 "Return on equity against the prior year's: the clearest single read of how a bank is doing.", s.get("inputs"),
                 tone="=", short=f"return on equity {v:.1f}%", score=60)


def _gains(b: Book) -> Optional[dict]:
    s = b.sig.get("gains_share")
    if not s or s.get("value") is None or s["value"] < 15:
        return None
    return _item("gains", s["headline"], s["text"],
                 "Part of the profit comes from selling assets, which depends on resale prices and timing, not only on the business's own earnings.",
                 "Gains against the core earnings next period.", s.get("inputs"), tone="=", short=_low(s["headline"]), score=58)


def _maturity(b: Book) -> Optional[dict]:
    s = b.sig.get("maturity_wall")
    if not s or s.get("severity") not in ("flag", "watch"):
        return None
    return _item("maturity", s["headline"], s["text"], "That debt has to be repaid or refinanced at whatever rates apply then.",
                 "How the company plans to repay or refinance it.", s.get("inputs"), tone="-", short=_low(s["headline"]), score=70)


# ---------------------------------------------------------------------- segments: where growth and profit come from
_SKIP_SEG = re.compile(r"(?i)corporate|elimination|reconcil|unallocated|other\b|all other|intersegment")


def _period_pair(periods: list):
    """The current period and the same period a year earlier, as indexes into a segment table."""
    if not periods:
        return None, None
    cur = periods[0]
    for j, p in enumerate(periods[1:], 1):
        if p.get("months") == cur.get("months"):
            try:
                d = (date.fromisoformat(cur["end"]) - date.fromisoformat(p["end"])).days
            except (KeyError, TypeError, ValueError):
                continue
            if 330 <= d <= 400:
                return 0, j
    return 0, None


def _segments(rep: dict) -> Optional[dict]:
    sg = (rep.get("segments") or {}).get("segments") or []
    rev = next((e for e in sg if re.search(r"(?i)revenue|net sales|sales", e.get("label") or "") and not re.search(r"(?i)cost", e.get("label") or "")), None)
    if not rev:
        return None
    i, j = _period_pair(rev.get("periods") or [])
    rows = []
    for r in rev.get("rows") or []:
        vals = r.get("values") or []
        if _SKIP_SEG.search(r.get("name") or "") or len(vals) <= i or not vals[i] or (vals[i].get("value") or 0) <= 0:
            continue
        prior = vals[j] if j is not None and len(vals) > j and vals[j] and (vals[j].get("value") or 0) > 0 else None
        rows.append({"name": r["name"], "cur": vals[i], "prior": prior})
    if len(rows) < 2:
        return None
    total = sum(r["cur"]["value"] for r in rows)
    prof = next((e for e in sg if re.search(r"(?i)operating income|segment profit|operating profit", e.get("label") or "")), None)
    pi = None
    if prof and (prof.get("periods") or [{}])[0].get("end") == rev["periods"][0].get("end") and (prof.get("periods") or [{}])[0].get("months") == rev["periods"][0].get("months"):
        pi = {r["name"]: (r.get("values") or [None])[0] for r in prof.get("rows") or [] if not _SKIP_SEG.search(r.get("name") or "")}
    per = rev["periods"][i].get("label") or ""
    quotes = []
    engine = None
    if all(r["prior"] for r in rows):
        inc = sum(r["cur"]["value"] - r["prior"]["value"] for r in rows)
        g_all = (total / sum(r["prior"]["value"] for r in rows) - 1) * 100
        top = max(rows, key=lambda r: r["cur"]["value"] - r["prior"]["value"])
        g_top = (top["cur"]["value"] / top["prior"]["value"] - 1) * 100
        add = top["cur"]["value"] - top["prior"]["value"]
        if inc > 0 and add / inc >= 0.4 and g_top >= g_all + 5:
            engine = (top, g_top, add, inc, g_all)
    conc = None
    if pi:
        ptotal = sum((v or {}).get("value") or 0 for v in pi.values() if v and (v.get("value") or 0) > 0)
        if ptotal > 0:
            best = max((r for r in rows if pi.get(r["name"]) and (pi[r["name"]].get("value") or 0) > 0), key=lambda r: pi[r["name"]]["value"], default=None)
            if best:
                ps, rs = pi[best["name"]]["value"] / ptotal * 100, best["cur"]["value"] / total * 100
                if ps - rs >= 15:
                    conc = (best, ps, rs, pi[best["name"]])
    if not engine and not conc:
        return None
    if engine and conc and engine[0]["name"] == conc[0]["name"]:
        top, g_top, add, inc, g_all = engine
        _, ps, rs, pv = conc
        title = f"{top['name']} drove the growth and most of the profit"
        detail = (f"{top['name']}: sales {_move(g_top)} to {top['cur']['display']} ({per}), {add / inc * 100:.0f}% of the increase in segment sales; "
                  f"{rs:.0f}% of segment sales but {ps:.0f}% of segment operating income ({pv['display']}).")
        means = (f"The company's growth and profit both lean on one business, so {poss(top['name'])} results move the whole company. "
                 f"Its margin, about {pv['value'] / top['cur']['value'] * 100:.0f}%, is far above the rest.")
        quotes = [_seg_quote(top["cur"]), _seg_quote(pv)]
        short = f"{top['name']} {_move(g_top, 0)}"
    elif engine:
        top, g_top, add, inc, g_all = engine
        title = f"{top['name']} drove the growth: sales {_move(g_top)} to {top['cur']['display']}"
        detail = (f"{top['name']} added {_money(add)} of the {_money(inc)} increase in segment sales ({add / inc * 100:.0f}%), "
                  f"growing faster than the whole ({_move(g_all)}) ({per}).")
        means = f"Most of the company's growth came from one business; {poss(top['name'])} pace sets the pace for the whole company."
        quotes = [_seg_quote(top["cur"]), _seg_quote(top["prior"])]
        short = f"{top['name']} {_move(g_top, 0)}"
    else:
        best, ps, rs, pv = conc
        title = f"{best['name']}: {rs:.0f}% of sales, {ps:.0f}% of operating profit"
        detail = f"{best['name']} sales {best['cur']['display']} and operating income {pv['display']} ({per})."
        means = (f"Its margin, about {pv['value'] / best['cur']['value'] * 100:.0f}%, is far above the rest of the company, "
                 "so the company's profit depends mostly on it.")
        quotes = [_seg_quote(best["cur"]), _seg_quote(pv)]
        short = f"{best['name']} earns most of the profit"
    return _item("segments", title, detail, means, f"{poss((engine or conc)[0]['name'])} growth and margin next period.", [], quotes,
                 tone="=", short=short, score=76)


def _seg_quote(v: dict) -> Optional[dict]:
    return {"page": v.get("page"), "offset": None, "text": _cut(v.get("quote") or v.get("display") or "", 160)} if v and v.get("page") else None


# ---------------------------------------------------------------------- outlook (management's, quoted)
_G_USEFUL = re.compile(r"(?i)\b(?:revenue|sales|earnings|EPS|per (?:diluted )?share|income|margin|growth|capital expenditures?|capex|"
                       r"cash flow|volume|deliveries|bookings|utilization)\b")
_G_FORWARD = re.compile(r"(?i)\b(?:expect\w*|anticipat\w*|outlook|forecast\w*|project(?:s|ed|ing)?|targets?|guid(?:e|es|ing|ance)\b|"
                        r"rais\w+|lower\w*|reaffirm\w*|reiterat\w*|now sees?|on track)\b")
_G_NOISE = re.compile(r"(?i)\bFDIC\b|final rule|adopt(?:ed|ion)|accounting (?:standard|guidance)|\bASU\b|retained earnings|special assessment|"
                      r"stress (?:test|scenario)|unemployment rate|relative adverse|regulatory (?:reporting )?guidance|consistent with|"
                      r"in accordance with|intercompany")
_MONEY = r"\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?"
_SCALE = {"billion": 1e9, "million": 1e6, "thousand": 1e3, None: 1.0, "": 1.0}


def _num(s: str, unit: Optional[str]) -> float:
    return float(s.replace(",", "")) * _SCALE.get((unit or "").lower(), 1.0)


def _useful_guidance(gs: list, kind: str) -> list:
    out = []
    for g in gs or []:
        t = " ".join((g.get("text") or "").split())
        if not t or not _G_USEFUL.search(t) or not _G_FORWARD.search(t) or _G_NOISE.search(t):
            continue
        if kind == "annual" and (g.get("direction") or "stated") == "stated":
            continue                       # a 10-K's "expected" sentences are mostly accounting, not an outlook
        out.append(g)
    return out


def _rank_guidance(gs: list) -> list:
    """Raised or lowered first, then a sales outlook, then a changed one, then a kept one, then the rest."""
    def key(kv):
        k, g = kv
        d, t = g.get("direction") or "stated", g.get("text") or ""
        sales = bool(re.search(r"(?i)\b(?:revenue|net sales|sales)\b", t) and re.search(r"\$\s?\d|\d\s?%", t))
        return ({"raised": 0, "lowered": 0}.get(d, 1 if sales else {"narrowed": 2, "updated": 2, "reaffirmed": 3}.get(d, 4)), k)
    return [g for _, g in sorted(enumerate(gs), key=key)]


def _guide(gs: list, b: Optional[Book], kind: str, prefix: str = "guide") -> Optional[dict]:
    ranked = _rank_guidance(_useful_guidance(gs, kind))
    if not ranked:
        return None
    g = ranked[0]
    t = " ".join(g["text"].split())
    d = g.get("direction") or "stated"
    quotes = [_quote(g, t)]
    detail = f"\"{_cut(t, 260)}\"" + (f" ({g['speaker']})" if g.get("speaker") else "")
    also = next((x for x in ranked[1:] if (x.get("direction") or "stated") != "stated" or re.search(r"(?i)capital expenditure|capex", x.get("text") or "")), None)
    if also:
        detail += f" Also: \"{_cut(' '.join(also['text'].split()), 180)}\""
        quotes.append(_quote(also))
    means, short, title = "", "", ""
    rev, rev_p = (b.pair("revenue") if b else (None, None))
    m_rng = re.search(r"(?i)(?:revenue|sales)[^.]{0,80}?" + _MONEY + r"(?:[^.$%]{0,20}?(?:and|to|-|–)\s*" + _MONEY + r")?", t)
    m_pm = re.search(r"(?i)plus or minus\s*([\d.]+)\s*%", t)
    m_gr = re.search(r"(?i)\bgrow(?:th)?\b[^.%]{0,40}?([\d.]+)\s*%\s*(?:and|to|-|–)\s*([\d.]+)\s*%", t)
    m_eps = re.search(r"(?i)(?:earnings|EPS)[^.]{0,80}?\$\s?([\d.]+)\s*(?:to|and|-|–)\s*\$\s?([\d.]+)", t)
    if d in ("raised", "lowered"):
        title, short = f"Outlook {d}", f"outlook {d}"
        if m_eps:
            title, short = f"Outlook {d}: earnings of ${m_eps.group(1)} to ${m_eps.group(2)} a share", f"earnings outlook {d} to ${m_eps.group(1)} to ${m_eps.group(2)}"
        means = ("Management now expects more than it did before: the business is running ahead of its own plan." if d == "raised" else
                 "Management now expects less than it did before: the business is running behind its own plan.")
    elif d == "reaffirmed":
        title, short = "Outlook kept", "outlook kept"
        means = "Management kept its outlook: nothing it has seen so far changed its view."
    elif d in ("narrowed", "updated"):
        title, short = f"Outlook {d}", f"outlook {d}"
        means = "Management changed its outlook; the quote shows in which direction and by how much."
    if m_rng:
        lo = _num(m_rng.group(1), m_rng.group(2))
        hi = _num(m_rng.group(3), m_rng.group(4) or m_rng.group(2)) if m_rng.group(3) else lo
        mid = (lo + hi) / 2
        rng = _money(lo) + (f" to {_money(hi)}" if hi != lo else "") + (f" (plus or minus {m_pm.group(1)}%)" if m_pm and hi == lo else "")
        if not title:
            title, short = f"Sales outlook: {rng}", f"sales guided to {_money(mid) if hi == lo else rng}"
        gn = _pct(rev["value"], rev_p["value"]) if rev and rev_p else None
        if m_gr and gn is not None:
            g0, g1 = float(m_gr.group(1)), float(m_gr.group(2))
            gm = (g0 + g1) / 2
            cmp_ = "about the same as" if abs(gm - gn) < 2 else ("faster than" if gm > gn else "slower than")
            means = (means + " " if means else "") + (f"The midpoint, {gm:.0f}% growth, is {cmp_} this period's {gn:.1f}%: management expects growth to "
                                                      + ("hold." if cmp_.startswith("about") else "speed up." if cmp_.startswith("faster") else "slow."))
        elif rev and (rev.get("period") or {}).get("type") == "Q" and rev["value"] > 0 and 0.7 <= mid / rev["value"] <= 1.6 \
                and not re.search(r"(?i)full[- ]year|fiscal year|for the year|annual", t):
            x = (mid / rev["value"] - 1) * 100
            means = (means + " " if means else "") + (f"The midpoint, {_money(mid)}, is {abs(x):.1f}% {'above' if x >= 0 else 'below'} this quarter's sales of "
                                                      f"{rev['display']}: management expects " + ("sales to keep rising." if x > 1 else
                                                                                                  "a quarter about like this one." if x > -1 else "lower sales."))
    if not title:
        title, short = "Management's outlook", "an outlook given"
    if not means:
        means = "This is management's own number for what comes next; the next report shows whether it was met."
    return _item(prefix, title, detail, means, "The next report against this outlook.", [], quotes, tone="+" if d == "raised" else "-" if d == "lowered" else "=",
                 short=short, score=90 if kind in ("release", "transcript") else 72 if d != "stated" else 60)


# ---------------------------------------------------------------------- disclosures
def _flags(ins: dict) -> list:
    out = []
    for f in ins.get("red_flags") or []:
        if f.get("status") != "reported":
            continue
        key = f.get("key") or f.get("id") or ""
        severe = key in SEVERE_FLAGS
        qs = [_quote(q) for q in (f.get("quotes") or [])[:2]]
        detail = f"\"{_cut((f.get('quotes') or [{}])[0].get('text') or '', 240)}\"" if f.get("quotes") else ""
        out.append(_item("flag_severe" if severe else "flag", f"Disclosed: {f['title'][:1].lower() + f['title'][1:]}", detail,
                         f.get("why") or "", "Whether the next filing says more about it.", [], qs, tone="-",
                         short=f"{f['title'].lower()} disclosed", score=96 if severe else 44))
    return out


# ---------------------------------------------------------------------- the financial documents
def financial(rep: dict, ins: dict) -> list:
    b, kind = Book(rep), ins.get("kind") or "other"
    inv = _invest(b)
    cands = [_growth(b, kind), _below(b), _per_share(b), _cash(b, rep), inv, _balance(b, inv), _roe(b), _gains(b), _maturity(b),
             _segments(rep)]
    if kind in ("release", "quarterly", "annual", "current"):
        cands.append(_guide(ins.get("guidance") or [], b, kind))
    cands += _flags(ins)
    return [c for c in cands if c]


# ---------------------------------------------------------------------- insider trades (Form 4)
def _small_pct(x) -> str:
    """0.005, 0.00016: a small share of the company shown with enough decimals, never as 0.000."""
    if not x or x >= 0.001:
        return f"{x:.3f}"
    return f"{x:.{-math.floor(math.log10(x)) + 1}f}"


def _count(n: float) -> str:
    return f"{n / 1e9:.2f} billion" if n >= 1e9 else f"{n / 1e6:.1f} million" if n >= 1e6 else f"{n:,.0f}"


def _usd(s: str) -> float:
    m = re.search(r"\$?\s*([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?", s or "")
    return _num(m.group(1), m.group(2)) if m else 0.0


def insider(ins: dict) -> list:
    s = ins.get("summary") or {}
    if not s:
        return []
    buys, sells = s.get("buys") or {}, s.get("sells") or {}
    nb, ns = int(buys.get("count") or 0), int(sells.get("count") or 0)
    span = f"{_day(s.get('first'))} to {_day(s.get('last'))}" if s.get("first") else f"the last {s.get('months') or 12} months"
    largest = s.get("largest") or []
    nm = (lambda x: x or "") if s.get("names") == "readable" else person
    out = []
    lq = [_quote(x, f"{x.get('insider')} {x.get('action')} {x.get('shares')} at {x.get('price')}") for x in largest[:1]]
    so = s.get("shares_out") or {}
    ctx = (f" Those shares are {_small_pct(so['sold_pct'])}% of the {so['value']} shares outstanding ({_day(so.get('end'))})." if so.get("value") and ns else "")
    tiny = " Next to the whole company, the selling is tiny." if so.get("value") and ns and so["sold_pct"] < 0.1 else ""
    if so.get("page"):
        lq.append(_quote(so, f"Shares outstanding: {so['value']}"))
    if ns and not nb:
        out.append(_item("ins_direction", f"Insiders sold {sells.get('value')} and bought nothing in the open market",
                         f"{ns} open-market sales by {sells.get('insiders')} insiders ({sells.get('shares')} shares), {span}; no open-market purchases." + ctx,
                         "Insider selling is common and often routine (pay, taxes, diversifying), so on its own it says little. "
                         "Buying with their own money is the stronger signal, and there was none in this period." + tiny,
                         "Any open-market purchase: it would be the first in this period.", [], lq, tone="=",
                         short=f"{sells.get('value')} sold, nothing bought", score=90))
    elif nb and not ns:
        out.append(_item("ins_direction", f"Insiders bought {buys.get('value')} and sold none",
                         f"{nb} open-market purchases by {buys.get('insiders')} insiders ({buys.get('shares')} shares), {span}; no open-market sales.",
                         "Insiders putting their own money in is the more telling signal: they chose to buy at these prices.",
                         "Whether the buying continues, and by whom.", [], lq, tone="+", short=f"{buys.get('value')} bought, nothing sold", score=90))
    elif nb and ns:
        bv, sv = _usd(buys.get("value")), _usd(sells.get("value"))
        out.append(_item("ins_direction", f"Insiders bought {buys.get('value')} and sold {sells.get('value')}",
                         f"{nb} purchases by {buys.get('insiders')} insiders and {ns} sales by {sells.get('insiders')} insiders, {span}.",
                         ("Net selling, but the purchases matter more than their size suggests: buying with one's own money is the stronger signal."
                          if sv > bv else "Net buying: insiders put more money in than they took out."),
                         "Who keeps buying and who keeps selling.", [], lq, tone="+" if bv >= sv else "=",
                         short=f"{buys.get('value')} bought, {sells.get('value')} sold", score=90))
    else:
        out.append(_item("ins_direction", "No open-market buying or selling by insiders",
                         f"Only {s.get('other') or 'grants, exercises or gifts'} ({span}).",
                         "Nothing here shows an insider choosing to buy or sell at market prices.", "", [], [], tone="=",
                         short="no open-market trades", score=90))
    plan = int(sells.get("plan") or 0)
    if ns:
        share = plan / ns * 100
        if share >= 50:
            out.append(_item("ins_plans", f"{plan} of {ns} sales were under 10b5-1 plans",
                             f"{share:.0f}% of the open-market sales were made under trading plans set up in advance.",
                             "A 10b5-1 plan schedules sales ahead of time, often months earlier, so these sales say little about the sellers' view today.",
                             "Sales outside a plan: those are choices made at the time.", [], [], tone="=",
                             short=f"{plan} of {ns} sales pre-scheduled", score=70))
        else:
            out.append(_item("ins_plans", f"Most sales were not pre-scheduled ({ns - plan} of {ns})",
                             f"Only {plan} of {ns} open-market sales were under 10b5-1 trading plans.",
                             "Sales outside a plan are choices made at the time, so they are more worth noting than scheduled ones.",
                             "Whether the same insiders keep selling.", [], [], tone="-", short=f"{ns - plan} of {ns} sales discretionary", score=72))
    # the largest trade, preferring a purchase, then a sale outside a plan
    pick = next((x for x in largest if x.get("code") == "P"), None) or next((x for x in largest if x.get("code") == "S" and not x.get("plan")), None)
    if pick:
        buy = pick.get("code") == "P"
        out.append(_item("ins_largest", f"{'Largest purchase' if buy else 'Largest sale outside a plan'}: {nm(pick.get('insider'))} ({pick.get('title') or 'insider'}), {pick.get('value')}",
                         f"{pick.get('action')} of {pick.get('shares')} shares at {pick.get('price')} on {_day(pick.get('date'))} (filed {_day(pick.get('filed'))}).",
                         ("An insider buying in size with personal money is the clearest signal a Form 4 can give." if buy else
                          "A discretionary sale by someone with an inside view; one sale is not a pattern, but repeated ones by the same person are worth watching."),
                         "", [], [_quote(pick, f"{pick.get('insider')} {pick.get('action')} {pick.get('shares')}")], tone="+" if buy else "=",
                         short=f"largest {'buy' if buy else 'discretionary sale'} by {nm(pick.get('insider'))}", score=66))
    # the largest share movement that is not a trade (a grant, shares returned to the company, a gift, an exercise)
    sold_sh = _usd(sells.get("shares") or "0")
    nt = s.get("notable") or []
    if nt:
        top = max(nt, key=lambda x: _usd(x.get("shares")))
        sh = _usd(top.get("shares"))
        act = top.get("action") or ""
        verb = {"grant or award": "received a grant of", "returned to the company": "returned to the company", "gift": "gave as a gift",
                "exercise or conversion": "exercised or converted", "option exercise": "exercised options for", "shares withheld for tax": "had withheld for tax"}.get(act, act)
        lines = "; ".join(f"{nm(x.get('insider'))}: {x.get('action')}, {x.get('shares')} shares on {_day(x.get('date'))}"
                          + (f", owned after {x.get('owned_after')}" if x.get("owned_after") else "") for x in nt[:3])
        kinds = {x.get("action") for x in nt[:3]}
        why = []
        if "grant or award" in kinds:
            why.append("A grant is pay in shares, not a decision to buy.")
        if "returned to the company" in kinds:
            why.append("Shares returned to the company are not an open-market sale: usually a cancelled or forfeited award, or a sale back "
                       "to the company; the filing's footnotes say which.")
        if "gift" in kinds:
            why.append("A gift moves shares without a sale.")
        if kinds & {"exercise or conversion", "option exercise"}:
            why.append("An exercise turns an award into shares; a sale that follows is the trade to watch.")
        if kinds & {"shares withheld for tax"}:
            why.append("Shares withheld for tax pay the tax on vesting shares; no decision to sell.")
        out.append(_item("ins_notable", f"The biggest share movement was not a trade: {nm(top.get('insider'))} {verb} {_count(sh)} shares",
                         lines + ".", " ".join(why) or "Not an open-market trade.", "The filing's footnotes, for what each of these transactions was.",
                         [], [_quote(x, f"{x.get('insider')} {x.get('action')} {x.get('shares')}") for x in nt[:2]], tone="=",
                         short=f"{nm(top.get('insider'))} {verb} {_count(sh)} shares", score=93 if sh > max(sold_sh, 1) else 58))
    # how much of their own holding the biggest sellers sold
    st = s.get("stakes") or []
    if st:
        days, many = {}, {}
        for d in s.get("days") or []:                     # the largest day of each seller (the document lists them largest first)
            days.setdefault(d.get("insider"), d)
            many[d.get("insider")] = many.get(d.get("insider"), 0) + 1
        lines = "; ".join(f"{nm(x['insider'])} sold {x['shares']} shares and still held {x['held']} ({x['pct']:.1f}% sold)"
                          + (f" ({days[x['insider']]['count']} sales on {_day(days[x['insider']]['date'])})" if many.get(x["insider"]) == 1 else "") for x in st[:3])
        hi = max(st[:3], key=lambda x: x["pct"])
        if hi["pct"] >= 50:
            means = f"{nm(hi['insider'])} sold most of their holding: a far bigger step than trimming."
        elif hi["pct"] >= 15:
            means = "A meaningful slice of their holdings, though they kept most of their shares."
        else:
            means = "Each seller kept the large majority of their shares: selling a small slice of a big holding is common."
        ps = ["%.0f%%" % x["pct"] for x in st[:3]]
        pcts = ps[0] if len(ps) == 1 else ", ".join(ps[:-1]) + " and " + ps[-1]
        out.append(_item("ins_stake", f"The biggest sellers sold {pcts} of their holdings",
                         lines + ". Each share is of the shares sold plus those still held after the last sale; shares from options exercised in the "
                         "period count as sold when they were sold.", means, "Whether the same people keep selling, and how much of their holding is left.",
                         [], [_quote(x, f"{x['insider']} sold {x['shares']} shares") for x in st[:2]], tone="=",
                         short=f"sellers kept most of their shares" if hi["pct"] < 15 else f"{nm(hi['insider'])} sold {hi['pct']:.0f}% of holdings", score=80))
    # several sales on one day: one decision
    dy = s.get("days") or []
    if dy:
        d0 = max(dy, key=lambda x: _usd(x.get("value")))
        out.append(_item("ins_day", f"{nm(d0['insider'])}: {d0['count']} sales on one day, {d0['value']}",
                         f"{d0['count']} open-market sales on {_day(d0['date'])}, {d0['shares']} shares, {d0['value']}"
                         + (f"; {d0['plan']} under a 10b5-1 plan." if d0.get("plan") else "."),
                         "Many sales on one day are usually one order filled in pieces, often by a broker under a 10b5-1 plan: read them as one decision.",
                         "", [], [_quote(d0, f"{d0['insider']} {d0['count']} open-market sales")], tone="=", short="", score=62))
    # how concentrated the selling is
    per = {}
    for t in ins.get("trades") or []:
        if t.get("code") == "S":
            per[t.get("insider")] = per.get(t.get("insider"), 0.0) + _usd(t.get("value"))
    tot = sum(per.values())
    if tot > 0 and len(per) >= 2:
        name, v = max(per.items(), key=lambda kv: kv[1])
        if v / tot >= 0.6:
            out.append(_item("ins_who", f"{nm(name)} accounts for {v / tot * 100:.0f}% of the selling",
                             f"{_money(v)} of the {_money(tot)} sold in the document's sales rows.",
                             "The selling is mostly one person's; it reflects that person's situation more than a broad view inside the company.",
                             "", [], [], tone="=", short=f"selling mostly by {nm(name)}", score=60))
    return out


# ---------------------------------------------------------------------- fund holdings (13F)
def _chg(c: str) -> Optional[float]:
    m = re.match(r"([+-]\d+(?:\.\d+)?)%", c or "")
    return float(m.group(1)) if m else None


def holdings(ins: dict) -> list:
    s, hs = ins.get("holdings_summary") or {}, ins.get("holdings") or []
    if not s or not hs:
        return []
    out = []
    # one line per issuer (Alphabet's two share classes are one company)
    by = {}
    for h in hs:
        k = h.get("issuer") or ""
        v = _usd(h.get("value"))
        c = _chg(h.get("change") or "")
        e = by.setdefault(k, {"issuer": k, "value": 0.0, "added": 0.0, "cut": 0.0, "new": False, "rows": []})
        e["value"] += v
        e["rows"].append(h)
        if (h.get("change") or "").lower().startswith("new"):
            e["new"] = True
            e["added"] += v
        elif c is not None and c > -100:
            moved = v * c / (100 + c)          # the shares added or sold, at the quarter-end value
            e["added" if c > 0 else "cut"] += abs(moved)
    total = s.get("total_raw") or sum(e["value"] for e in by.values())
    top = sorted(by.values(), key=lambda e: -e["value"])
    t1 = top[0]
    tq = [_quote(t1["rows"][0], f"{t1['issuer']} {t1['rows'][0].get('value')} {t1['rows'][0].get('weight')}")]
    w10 = s.get("top10_weight") or ""
    try:
        w10n = float(w10.rstrip("%"))
    except ValueError:
        w10n = None
    if w10n is not None and w10n >= 60:
        title = f"Concentrated: the top 10 positions are {w10} of {_money(total)}"
        means = "Results ride on a handful of companies: a big move in one of them moves the whole portfolio."
        tone = "="
    elif w10n is not None and w10n < 40:
        title = f"Spread out: the top 10 positions are only {w10} of {_money(total)}"
        means = "No single holding dominates: the portfolio behaves more like a broad basket."
        tone = "="
    else:
        title = f"The top 10 positions are {w10} of {_money(total)}"
        means = "A mix of a few large bets and a longer list of smaller ones."
        tone = "="
    detail = (f"{s.get('positions')} reported positions at the quarter end ({_day(s.get('period'))}); the largest, {issuer(t1['issuer'])}, "
              f"is {t1['value'] / total * 100:.1f}% at {_money(t1['value'])}.")
    out.append(_item("fund_concentration", title, detail, means, "", [], tq, tone=tone,
                     short=f"{w10} of the portfolio in its top 10 positions", score=88))
    adds = sorted([e for e in by.values() if e["added"] > 0 and not e["new"]], key=lambda e: -e["added"])
    if adds:
        a = adds[0]
        rows = "; ".join(f"{h.get('class') or 'shares'} {h.get('change')}" for h in a["rows"] if _chg(h.get("change") or "") and _chg(h.get("change")) > 0)
        more = [issuer(e["issuer"]) for e in adds[1:3]]
        out.append(_item("fund_added", f"Biggest addition: {issuer(a['issuer'])}, about {_money(a['added'])} more",
                         f"{issuer(a['issuer'])} ({rows}), now {_money(a['value'])}, {a['value'] / total * 100:.1f}% of the portfolio."
                         + (f" Also added: {', '.join(more)}." if more else ""),
                         "This is where the manager put the most new money in the quarter. Quarter-end figures show the change, not when or "
                         "at what price the shares were bought (the dollar amount is the added shares at the quarter-end value).",
                         "The next quarter's filing: whether the position keeps growing.", [], [_quote(a["rows"][0], f"{a['issuer']} {a['rows'][0].get('change')}")],
                         tone="=", short=f"biggest addition {issuer(a['issuer'])} (about {_money(a['added'])})", score=80))
    cuts = sorted([e for e in by.values() if e["cut"] > 0], key=lambda e: -e["cut"])
    if cuts:
        c0 = cuts[0]
        more = [issuer(e["issuer"]) for e in cuts[1:3]]
        ch = "; ".join(h.get("change") for h in c0["rows"] if (_chg(h.get("change") or "") or 0) < 0)
        out.append(_item("fund_cut", f"Biggest reduction: {issuer(c0['issuer'])}, about {_money(c0['cut'])} less",
                         f"{issuer(c0['issuer'])} ({ch}), now {_money(c0['value'])}." + (f" Also reduced: {', '.join(more)}." if more else ""),
                         "Selling part of a position can mean less conviction, a better use for the money elsewhere, or simply rebalancing; "
                         "the filing does not say which.",
                         "", [], [_quote(c0["rows"][0], f"{c0['issuer']} {c0['rows'][0].get('change')}")], tone="=",
                         short=f"biggest reduction {issuer(c0['issuer'])}", score=72))
    new = [e for e in by.values() if e["new"]]
    if new and max(e["value"] for e in new) / total >= 0.0005:          # a $580K line in a $299B book is not news
        n0 = max(new, key=lambda e: e["value"])
        out.append(_item("fund_new", f"{len(new)} new position{'s' if len(new) > 1 else ''}, the largest {issuer(n0['issuer'])} ({_money(n0['value'])})",
                         f"New this quarter: {', '.join(issuer(e['issuer']) for e in new[:5])}.",
                         ("A new position is a fresh decision by the manager; a small one can be a first step or a minor holding."
                          if n0["value"] / total < 0.01 else "A new position of this size is a real bet by the manager."),
                         "Whether the new position grows next quarter.", [], [_quote(n0["rows"][0], f"{n0['issuer']} New position")], tone="=",
                         short=f"{len(new)} new position{'s' if len(new) > 1 else ''}", score=50))
    return out


# ---------------------------------------------------------------------- earnings calls
TOPICS = [
    ("lease pricing", r"\bpric\w*|lease rates?|renewal|\bLPI\b|rate increases?"),
    ("demand", r"\bdemand\b|volumes?|backlog|orders?\b|utiliz\w+|bookings"),
    ("margins", r"\bmargins?\b|\bcosts?\b|expenses?|opex|inflation"),
    ("the outlook", r"guid\w*|outlook|full[- ]year|second half|next year|targets?\b|run rate"),
    ("capital spending", r"capex|capital (?:spend\w*|expenditures?)|invest\w*"),
    ("capital returns", r"buybacks?|repurchas\w*|dividends?|capital return"),
    ("acquisitions", r"acqui\w+|integration|\bdeals?\b|\bM&A\b"),
    ("AI demand", r"\bAI\b|artificial intelligence|data cent\w+|GPUs?|compute"),
    ("China and tariffs", r"\bChina\b|tariffs?|export\w*|\btrade\b"),
    ("asset sales", r"\bgains?\b|remarket\w*|dispositions?|asset sales?|secondary market"),
    ("credit quality", r"\bcredit\b|\bloans?\b|charge-?offs?|provisions?|deposits?"),
    ("supply", r"\bsupply\b|capacity|shortages?|lead times?"),
    ("joint ventures", r"\bJVs?\b|joint ventures?|partnership"),
]


def call(ins: dict, rep: dict) -> list:
    c = ins.get("call") or {}
    out = []
    g = _guide(c.get("guidance") or [], Book(rep) if rep.get("financials") else None, "transcript", "call_guide")
    if g:
        out.append(g)
    qs = c.get("questions") or []
    if len(qs) >= 2:
        hits = []
        for name, rx in TOPICS:
            n = [q for q in qs if re.search(rx, q.get("text") or "", re.I)]
            if n:
                hits.append((len(n), name, n))
        hits.sort(key=lambda x: -x[0])
        if hits:
            top = hits[:2]
            analysts = sorted({q.get("analyst") for q in qs if q.get("analyst")})
            title = f"Analysts focused on {top[0][1]}" + (f" and {top[1][1]}" if len(top) > 1 else "")
            detail = (f"{top[0][0]} of {len(qs)} questions touched {top[0][1]}" + (f"; {top[1][0]} touched {top[1][1]}" if len(top) > 1 else "")
                      + f". {len(analysts)} analysts asked questions.")
            rx0 = dict(TOPICS)[top[0][1]]
            ex = max(top[0][2], key=lambda q: (len(re.findall(rx0, q.get("text") or "", re.I)), -abs(len(q.get("text") or "") - 160)))
            detail += f" For example, {ex.get('analyst')}" + (f" ({ex.get('firm')})" if ex.get("firm") else "") + f": \"{_cut(ex.get('text') or '', 200)}\""
            out.append(_item("call_focus", title, detail,
                             "The questions show what investors still want answered after the numbers. Management's answers on these topics "
                             "are where the call adds to the release.", "", [], [_quote(ex)], tone="=",
                             short=f"analysts focused on {top[0][1]}", score=84))
    cl = c.get("claims") or []
    if cl:
        x = cl[0]
        out.append(_item("call_claim", f"A claim to check, by {x.get('speaker')}", f"\"{_cut(x.get('text') or '', 300)}\"",
                         "A claim made on the call, not a checked figure: compare it with the filed statements before relying on it.",
                         "", [], [_quote(x)], tone="=", short="", score=40))
    sp = c.get("speakers") or []
    co = [s for s in sp if s.get("side") == "company"]
    an = [s for s in sp if s.get("side") == "analyst"]
    if co and an and len(out) < 3:
        words_co, words_an = sum(s.get("words") or 0 for s in co), sum(s.get("words") or 0 for s in an)
        lead = max(co, key=lambda s: s.get("words") or 0)
        out.append(_item("call_cast", f"{lead['name']} did most of the talking for the company",
                         f"{len(co)} company speakers ({words_co:,} words) and {len(an)} analysts ({words_an:,} words); "
                         f"{lead['name']}{' (' + lead['role'] + ')' if lead.get('role') else ''} spoke {lead.get('words'):,} words in {lead.get('turns')} turns.",
                         "Who answers tells you who owns the story; a call where one executive takes every hard question is worth noting.",
                         "", [], [], tone="=", short="", score=30))
    return out


# ---------------------------------------------------------------------- the headline and the whole
def _pick(items: list, n: int = 5) -> list:
    chosen = sorted(items, key=lambda x: -x["_score"])[:n]
    return sorted(chosen, key=lambda x: (ORDER.get(x["id"], 20), -x["_score"]))


def headline(lead: str, items: list) -> str:
    """The conclusion in one line: the two strongest points, joined by "but" when they pull opposite ways."""
    shorts = [x for x in sorted(items, key=lambda x: -x["_score"]) if x.get("_short") and (x["id"] != "flag" or x["_score"] >= 90)]
    if not shorts:
        return ""
    first = shorts[0]
    text = first["_short"]
    second = next((x for x in shorts[1:] if x["_score"] >= 60), None)
    if second:
        opposite = {first["tone"], second["tone"]} == {"+", "-"}
        joiner = ", but " if opposite and " but " not in text else ("; " if " and " in text or "," in text or " but " in text or "," in second["_short"] else " and ")
        text += joiner + second["_short"]
    return f"{lead}: {text}."


def build(rep: dict, ins: dict) -> Optional[dict]:
    kind = (ins or {}).get("kind") or "other"
    if kind == "insider":
        items = insider(ins)
        s = ins.get("summary") or {}
        lead = (f"{who(rep, ins)} insiders" if who(rep, ins) else "Insiders") + (f", {_day(s.get('first'))} to {_day(s.get('last'))}" if s.get("first") else "")
    elif kind == "holdings":
        items = holdings(ins)
        s = ins.get("holdings_summary") or {}
        lead = ", ".join(x for x in (who(rep, ins), f"quarter ended {_day(s.get('period'))}" if s.get("period") else "") if x) or "Fund holdings"
    elif kind == "transcript":
        items = call(ins, rep)
        lead = (f"{who(rep, ins)} earnings call" if who(rep, ins) else "Earnings call") + (f", {when(rep, ins)}" if when(rep, ins) else "")
    else:
        items = financial(rep, ins)
        w = when(rep, ins)
        if not w:
            rev, _ = Book(rep).pair("revenue")
            w = _per(rev) if rev else ""
        lead = ", ".join(x for x in (who(rep, ins), w) if x) or "This document"
    if not items:
        return None
    chosen = _pick(items)
    out = {"version": VERSION, "kind": kind, "headline": headline(lead, chosen), "items": [], "basis": BASIS}
    for x in chosen:
        out["items"].append({k: v for k, v in x.items() if not k.startswith("_")})
    return out
