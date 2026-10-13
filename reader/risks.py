"""Risk findings with evidence, category and labeled interpretation.

Sources: the Risk Factors section (Item 1A headings and their first
sentences), plus quantitative watch items found in the notes and MD&A
(customer concentration, debt maturities, legal proceedings, going concern
or material weakness language). Nothing is called "new" or "rising": that
needs a prior-period comparison this single-filing reader does not have.
Absence of a match is reported as "not found in the extracted text", never
as evidence that a risk does not exist.
"""
from __future__ import annotations

import re

CATEGORIES = [
    ("Customer or supplier concentration", r"single.source|sole.source|supplier|component|outsourc|customer concentration|key customers?|limited number of (customers|suppliers)"),
    ("Liquidity and refinancing", r"liquidity|refinanc|access to (capital|credit|financing)|credit rating|capital markets"),
    ("Debt and covenants", r"indebtedness|covenant|leverage|borrowing"),
    ("Geographic exposure", r"international|china|foreign|geopolit|tariff|trade (policy|restrictions)|outside the u\.?s"),
    ("Demand cyclicality", r"cyclical|economic conditions|recession|demand for (our|the company)|inflation|consumer spending"),
    ("Competition and pricing", r"competit|pricing pressure|downward pressure|price"),
    ("Regulation and litigation", r"regulat|litigation|legal proceedings|antitrust|government|laws|tax"),
    ("Operational disruption and cybersecurity", r"cyber|information technology|system failures?|network disruptions?|security breach|natural disaster|pandemic|disruption"),
    ("Accounting estimates and contingencies", r"estimate|impairment|goodwill|contingen|accounting"),
    ("Investment and credit exposure", r"credit risk|investment portfolio|counterpart"),
    ("People and key personnel", r"key personnel|talent|key employees|retain"),
    ("Product quality and defects", r"defect|recall|quality|warrant"),
    ("Intellectual property", r"intellectual property|patent|licens"),
]
WHY = {
    "Customer or supplier concentration": "Reliance on a limited set of suppliers or customers can raise costs or cut revenue if one relationship changes.",
    "Liquidity and refinancing": "If funding becomes harder to obtain, the company may pay more to borrow or delay spending.",
    "Debt and covenants": "Debt terms can limit flexibility, and higher rates raise interest costs.",
    "Geographic exposure": "Results in specific countries can be affected by trade rules, currency moves or local conditions.",
    "Demand cyclicality": "Revenue may fall when economic conditions weaken.",
    "Competition and pricing": "Competitive pressure can reduce prices or margins.",
    "Regulation and litigation": "Legal or regulatory outcomes can bring costs or restrict business practices.",
    "Operational disruption and cybersecurity": "Outages or breaches can interrupt sales and operations and create liabilities.",
    "Accounting estimates and contingencies": "Changes in judgments or estimates can move reported results.",
    "Investment and credit exposure": "Losses or value changes in investments or counterparties can reduce income or assets.",
    "People and key personnel": "Losing key people can affect execution.",
    "Product quality and defects": "Defects or recalls can bring repair costs and harm demand.",
    "Intellectual property": "Losing access to, or disputes over, key technology can raise costs or limit products.",
    "Other": "The filing identifies this as a risk factor.",
}
_RISKY = re.compile(r"\b(could|may|might|depend|depends|risk|risks|adversely|subject to|exposed|impact|impacted|uncertain|volatil|harm|fail|compet|affect)", re.I)
_SPECIFIC = re.compile(r"\$\s?[\d.,]+|\d+(?:\.\d+)?%|\b(?:China|Europe|Japan|India|Taiwan|Mexico|Canada|United States|U\.S\.)\b|\b(?:iPhone|Mac|iPad|railcars?|tank cars?)\b")


def _section_text(doc, item: str) -> tuple[int, int]:
    secs = doc.sections
    for i, s in enumerate(secs):
        if s.item == item:
            end = secs[i + 1].offset if i + 1 < len(secs) else len(doc.body)
            return s.offset, end
    return -1, -1


def _category(text: str) -> str:
    for name, pat in CATEGORIES:
        if re.search(pat, text, re.I):
            return name
    return "Other"


_SMALL = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "our", "the", "to", "with", "us", "we"}


def _title_heading(l: str) -> bool:
    """A risk heading set in Title Case with no closing period."""
    if not (15 <= len(l) <= 220) or l.endswith((".", ":", ",", ";")) or not l[:1].isupper() or re.search(r"\d{3}", l):
        return False
    if re.match(r"(item\s+1a|risk factors|summary|part\s+i\b)", l, re.I) or re.search(r"\bsummary$|\brisk factors$", l, re.I) or (re.search(r"\brisks?$", l, re.I) and (len(l.split()) <= 6 or l.isupper())):
        return False                        # the section title or a group title, not a risk
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\u2019-]*", l) if w.lower() not in _SMALL]
    return len(words) >= 3 and sum(w[0].isupper() for w in words) >= 0.8 * len(words)


def _paragraphs(text: str, base: int) -> list[tuple[int, str, bool]]:
    """Rebuild paragraphs from wrapped lines: a paragraph ends at a line that
    ends a sentence and is shorter than a full line. Returns (offset, text,
    is_subheading)."""
    lines, pos = [], 0
    for ln in text.split("\n"):
        lines.append((base + pos, ln.strip()))
        pos += len(ln) + 1
    widths = sorted(len(l) for _, l in lines if l)
    full = widths[int(len(widths) * 0.9)] * 0.82 if widths else 120
    out, cur, cur_off = [], [], None
    for off, l in lines:
        if not l:
            continue
        sub = len(l) < 90 and not l.endswith((".", ":", ",", ";")) and l[:1].isupper() and (
            (re.search(r"\brisks?$", l, re.I) and (len(l.split()) <= 6 or l.isupper()))   # "Business and Industry Risks"
            or re.match(r"risks? (related|relating|associated|specific) (to|with)\b", l, re.I))
        if sub:
            if cur:
                out.append((cur_off, " ".join(cur), False))
                cur = []
            out.append((off, l, True))
            continue
        if (not cur or cur[-1].endswith((".", ":", ";"))) and _title_heading(l):
            # "We Face Intense Competition" (Amazon): a heading on its own line after a finished
            # sentence, not a wrapped line in the middle of one
            if cur:
                out.append((cur_off, " ".join(cur), False))
                cur = []
            out.append((off, l, False))
            continue
        if not cur:
            cur_off = off
        cur.append(l)
        if l.endswith((".", ":")) and len(l) < full:
            out.append((cur_off, " ".join(cur), False))
            cur = []
    if cur:
        out.append((cur_off, " ".join(cur), False))
    return out


def risk_factors(doc) -> tuple[list[dict], str]:
    st, en = _section_text(doc, "1A")
    if st < 0:
        return [], "The Risk Factors section (Item 1A) was not located in the extracted text."
    nl = doc.body.find("\n", st, en)       # skip the "Item 1A. Risk Factors" heading line itself
    if nl > 0:
        st = nl + 1
    paras = _paragraphs(doc.body[st:en], st)
    found, group = [], ""
    for k, (off, p, is_sub) in enumerate(paras):
        if is_sub:
            group = p
            continue
        one_sentence = len(re.findall(r"[.!?](?:\s+[A-Z]|$)", p)) == 1
        nxt = paras[k + 1][1] if k + 1 < len(paras) and not paras[k + 1][2] else ""
        titled = _title_heading(p) and len(nxt) > len(p)
        if not titled and not (one_sentence and 50 <= len(p) <= 420 and p.endswith(".") and p[0].isupper() and len(nxt) > len(p)):
            continue
        if re.match(r"(the following|this section|see |as a result|references|in addition|additionally|such |these |this |while |although |for example|accordingly|however|further|moreover|also|it |there )", p, re.I) or not (_RISKY.search(p) or titled):
            continue
        sents = re.split(r"(?<=[.])\s+(?=[A-Z])", nxt)
        detail = " ".join(sents[:2])[:420]
        cat = _category(p)
        if cat == "Other":
            cat = _category(detail)
        mag = re.search(r"[^.]*?(\$\s?[\d.,]+\s?(?:billion|million)?|\d+(?:\.\d+)?%)[^.]*\.", nxt)
        found.append({
            "title": p, "group": group, "category": cat, "summary": detail,
            "magnitude": re.sub(r"\s+", " ", mag.group(0)).strip()[:300] if mag else None,
            "specificity": "Company-specific" if _SPECIFIC.search(p + " " + detail) else "Broadly worded",
            "why_it_may_matter": WHY.get(cat, WHY["Other"]), "kind": "reported", "basis": "Risk factor (a possible event, not a disclosed one)",
            "source": {**doc.location(off), "excerpt": p[:300], "offset": off},
        })
    order = list(range(len(found)))          # filing order: filers tend to put the most important first
    chosen, per = [], {}
    for i in order:
        c = found[i]["category"]
        if per.get(c, 0) >= 2:
            continue
        per[c] = per.get(c, 0) + 1
        chosen.append(i)
        if len(chosen) >= 8:
            break
    note = (f"{len(found)} risk-factor headings were identified in Item 1A; {len(chosen)} are summarised, taken in filing order with at most "
            f"two per category. Read the full section for completeness.")
    return [found[i] for i in sorted(chosen)], note


def watch_items(doc, rows: list) -> tuple[list[dict], list[str]]:
    items, searched = [], []
    body = doc.body
    sents = [(m.start(), m.group(0)) for m in re.finditer(r"[^.\n]{20,500}\.", body)]

    def first(key: str, pat: str):
        for off, s in sents:
            if key in s.lower() and re.search(pat, s, re.I):
                return off, re.sub(r"\s+", " ", s).strip()
        return None

    hit = first("%", r"(?:accounted for|represented|comprised) (?:approximately |more than |over )?\d{1,2}(?:\.\d+)?% of (?:the company.s |our )?(?:total |net )?(?:net sales|revenues?|trade receivables|receivables)")
    searched.append("customer concentration (\"accounted for N% of net sales/receivables\")")
    if hit:
        items.append({"category": "Customer or supplier concentration", "title": "Concentration disclosed", "text": hit[1][:360],
                      "why_it_may_matter": WHY["Customer or supplier concentration"], "kind": "reported", "source": {**doc.location(hit[0]), "excerpt": hit[1][:360], "offset": hit[0]}})
    # debt maturities: year rows in a debt table
    mats = [r for r in rows if re.search(r"debt|borrowing|notes", (r["table_title"] + " " + r["heading"]).lower())
            and re.fullmatch(r"(19|20)\d\d|thereafter", r["key"]) and r["unit"] == "USD"]
    searched.append("a debt maturity schedule")
    if mats:
        from .facts import SCALE, fmt_value
        lines = []
        for r in mats[:7]:
            v = r["values"][0] * SCALE.get(r["scale"], 1.0)
            lines.append(f"{r['label']}: {fmt_value(v, 'USD')}")
        items.append({"category": "Liquidity and refinancing", "title": "Scheduled debt maturities", "text": "; ".join(lines) + ".",
                      "why_it_may_matter": "Large near-term maturities must be repaid or refinanced.", "kind": "reported",
                      "source": {"pdf_page": mats[0]["pdf_page"], "printed_page": mats[0]["printed_page"], "section": mats[0]["section"],
                                 "table_title": mats[0]["table_title"], "excerpt": mats[0]["line"], "offset": mats[0]["offset"]}})
    else:
        # a schedule printed sideways: years as column headings, one row per
        # instrument and a "Total ..." row that gives each year's principal
        horiz = None
        for m in re.finditer(r"(?mi)^(?:fiscal years?\s+)?((?:20\d\d\s+){3,}20\d\d)\s+(?:20\d\d and )?thereafter(\s+total)?\s*$", body):
            ctx = body[max(0, m.start() - 500):m.start()].lower()
            if not re.search(r"principal|debt|borrowings|maturit", ctx) or re.search(r"affiliat|joint venture|unconsolidated|lease (liabilities|payments)", ctx[-300:]):
                continue
            years = re.findall(r"20\d\d", m.group(1)) + ["Thereafter"]
            cells = lambda ln: [("0" if t == "\u2014" else t) for t in re.findall(r"\(?[\d,]+(?:\.\d+)?\)?|\u2014", ln.split("  ", 1)[-1])]
            lines = body[m.end():m.end() + 6000].split("\n")[:80]
            for ln in lines:
                # a total of all commitments (Amazon: debt with interest, leases, purchase obligations) is not debt
                if re.match(r"(?i)total\b", ln) and not re.search(r"(?i)commitments|obligations|contractual", ln.split("  ", 1)[0]):
                    nums = cells(ln)
                    if len(nums) >= len(years):
                        horiz = (m, years, nums[:len(years)], ln, body.find(ln, m.end()))
                        break
            if not horiz:
                # one row for the whole schedule: "Term Debt (Principal only)  $ 875  $ —  $ 625 ..."
                for ln in lines[:6]:
                    if re.match(r"(?i)[a-z][^\d]*(debt|principal|notes|borrowings)", ln) and not re.search(r"(?i)interest|lease", ln.split("  ", 1)[0]):
                        nums = cells(ln)
                        if len(nums) >= len(years):
                            horiz = (m, years, nums[:len(years)], ln, body.find(ln, m.end()))
                            break
            if horiz:
                break
        if horiz:
            from .tables import _caption
            from .facts import SCALE, fmt_value
            m, years, nums, ln, off = horiz
            mult = SCALE.get(_caption(body, off)["scale"], 1.0)
            sched_vals = [(y, float(v.strip('()').replace(',', '')) * mult) for y, v in zip(years, nums)]
            lines = [f"{y}: {fmt_value(v, 'USD')}" for y, v in sched_vals]
            items.append({"category": "Liquidity and refinancing", "title": "Scheduled debt maturities", "text": "; ".join(lines) + ".", "schedule": sched_vals,
                          "why_it_may_matter": "Large near-term maturities must be repaid or refinanced.", "kind": "reported",
                          "source": {**doc.location(off), "excerpt": re.sub(r"\s+", " ", ln)[:300], "offset": off}})
        # a schedule printed as one run of year / amount pairs, below a debt
        # caption: "2026 $ 653.3  2027 854.5 ... Thereafter 5,605.2"
        sched = None
        for m in re.finditer(r"\b(20\d\d)\s+\$?\s?([\d,]+(?:\.\d+)?)(?:\s+(20\d\d)\s+\$?\s?[\d,]+(?:\.\d+)?){3,}(?:\s+Thereafter\s+\$?\s?[\d,]+(?:\.\d+)?)?", body):
            ctx = body[max(0, m.start() - 1500):m.start()].lower()
            # the schedule's own caption ("... debt are as follows:") decides; a wider window only when there is none
            cap_lines = [ln for ln in ctx[-500:].split("\n") if ln.strip().endswith(":") and len(ln) > 30]
            if cap_lines:
                about = cap_lines[-1]
                ok = re.search(r"debt|borrowings|notes|term loans|principal", about) and not re.search(r"lease|affiliat|joint venture|unconsolidated|equity method|investee", about)
            else:
                ok = re.search(r"debt|borrowings|notes due|term loans", ctx[-700:]) and not re.search(r"lease (liabilities|payments)|operating leases", ctx[-400:]) \
                    and not re.search(r"affiliat|joint venture|unconsolidated|equity method|investee", ctx[-300:])      # another entity's debt
            if ok:
                sched = m
                break
        hit = None if (sched or horiz) else first("matur", r"matur\w+[^.]{0,200}\$\s?[\d.,]+\s?(?:billion|million)")
        if sched and not horiz:
            from .tables import _caption
            from .facts import SCALE, fmt_value
            cap = _caption(body, sched.start())
            mult = SCALE.get(cap["scale"], 1.0)
            pairs = re.findall(r"(20\d\d|Thereafter)\s+\$?\s?([\d,]+(?:\.\d+)?)", sched.group(0))
            sched_vals = [(y, float(v.replace(',', '')) * mult) for y, v in pairs[:7]]
            lines = [f"{y}: {fmt_value(v, 'USD')}" for y, v in sched_vals]
            items.append({"category": "Liquidity and refinancing", "title": "Scheduled debt maturities", "text": "; ".join(lines) + ".", "schedule": sched_vals,
                          "why_it_may_matter": "Large near-term maturities must be repaid or refinanced.", "kind": "reported",
                          "source": {**doc.location(sched.start()), "excerpt": re.sub(r"\s+", " ", sched.group(0))[:300], "offset": sched.start()}})
        if hit:
            items.append({"category": "Liquidity and refinancing", "title": "Debt maturities", "text": hit[1][:360],
                          "why_it_may_matter": "Large near-term maturities must be repaid or refinanced.", "kind": "reported",
                          "source": {**doc.location(hit[0]), "excerpt": hit[1][:360], "offset": hit[0]}})
    st, en = _section_text(doc, "3")
    searched.append("Item 3 Legal Proceedings")
    if st >= 0:
        txt = re.sub(r"\s+", " ", body[st:en])
        txt = re.sub(r"^Item 3\.?\s*Legal Proceedings\s*", "", txt, flags=re.I)
        first_sent = re.split(r"(?<=[.])\s+(?=[A-Z])", txt)[:2]
        if first_sent and len(" ".join(first_sent)) > 40 and not re.match(r"(none|not applicable)", first_sent[0], re.I):
            items.append({"category": "Regulation and litigation", "title": "Legal proceedings", "text": " ".join(first_sent)[:420],
                          "why_it_may_matter": WHY["Regulation and litigation"], "kind": "reported", "source": {**doc.location(st), "excerpt": " ".join(first_sent)[:300], "offset": st}})
    # only an actual disclosure counts: audit-standard wording such as "the risk
    # that a material weakness exists" or "no substantial doubt" does not
    for key, title, pat in (
            ("going concern", "Going concern doubt disclosed",
             r"(?<!no )substantial doubt (?:about|regarding|as to) (?:the company's|our|its)[^.]{0,40}ability to continue as a going concern"),
            ("material weakness", "Material weakness disclosed",
             r"(?:identified|disclosed|have|has) (?:a |certain )?material weakness(?:es)?(?! exists)|material weakness(?:es)? (?:was|were|has been|have been) identified|"
             r"internal control over financial reporting (?:was|is) not effective|did not maintain effective internal control")):
        searched.append(f"\"{key}\" disclosures")
        hit = first(key, pat)
        if hit and not re.search(r"risk that a material weakness|no material weakness|not identified any material weakness|no substantial doubt", hit[1], re.I):
            items.append({"category": "Accounting estimates and contingencies", "title": title, "text": hit[1][:360],
                          "why_it_may_matter": "Auditor or management concerns about controls or continuity need direct review.", "kind": "reported",
                          "basis": "Disclosed condition", "source": {**doc.location(hit[0]), "excerpt": hit[1][:360], "offset": hit[0]}})
    for it in items:
        it.setdefault("basis", "Disclosed schedule" if it["title"] == "Scheduled debt maturities" else
                      "Cross-reference to a note" if re.search(r"(described|discussed|see) (in )?(\"?note|part ii)", it.get("text", ""), re.I) else "Disclosed in the filing")
    return items, searched
