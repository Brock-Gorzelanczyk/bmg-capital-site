"""Insider trades (Form 4) and fund holdings (13F) from SEC EDGAR, written as documents the spider reads.

    parse_form4(xml) -> {owner, roles, plan, transactions}
    form4_document(company, ticker, filings, months) -> HTML     # one row per transaction, each citing its filing
    parse_13f(xml) -> [holding]                                  # one row per security (all managers summed)
    holdings_document(fund, filing, rows, prior, highlight) -> HTML

Every number in these documents is read from the filings' XML (shares, prices, values); totals are sums of
those rows and say so. Nothing is estimated: a trade without a price shows no value.
"""
from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from typing import Optional

CODES = {"P": "Open-market buy", "S": "Open-market sale", "A": "Grant or award", "M": "Exercise or conversion", "X": "Option exercise",
         "F": "Shares withheld for tax", "G": "Gift", "D": "Returned to the company", "C": "Conversion", "J": "Other",
         "W": "Will or inheritance", "I": "Discretionary", "E": "Expiration", "H": "Expiration", "K": "Equity swap",
         "L": "Small acquisition", "Z": "Voting trust", "U": "Tender of shares", "V": "Voluntary report", "O": "Option exercise"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(el, path: str):
    """Namespace-blind find: path like "reportingOwner/reportingOwnerId/rptOwnerName"."""
    cur = [el]
    for part in path.split("/"):
        nxt = []
        for c in cur:
            nxt += [x for x in list(c) if _local(x.tag) == part]
        cur = nxt
        if not cur:
            return None
    return cur[0]


def _text(el, path: str) -> str:
    x = _find(el, path) if el is not None else None
    if x is None:
        return ""
    v = _find(x, "value")
    return ((v.text if v is not None else x.text) or "").strip()


def _num(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except (TypeError, ValueError):
        return None


def parse_form4(xml_text: str) -> dict:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    owners = []
    for ro in [x for x in root if _local(x.tag) == "reportingOwner"]:
        name = _text(ro, "reportingOwnerId/rptOwnerName")
        rel = _find(ro, "reportingOwnerRelationship")
        roles = []
        if rel is not None:
            if _text(rel, "isDirector") in ("1", "true"):
                roles.append("Director")
            if _text(rel, "isOfficer") in ("1", "true"):
                roles.append(_text(rel, "officerTitle") or "Officer")
            if _text(rel, "isTenPercentOwner") in ("1", "true"):
                roles.append("10% owner")
            if _text(rel, "isOther") in ("1", "true"):
                roles.append(_text(rel, "otherText") or "Other")
        owners.append({"name": name, "roles": roles})
    notes = {(f.get("id") or ""): (f.text or "").strip() for f in root.iter() if _local(f.tag) == "footnote"}
    plan_flag = _text(root, "aff10b5One") in ("1", "true")
    txs = []
    for table, deriv in (("nonDerivativeTable", False), ("derivativeTable", True)):
        t = _find(root, table)
        if t is None:
            continue
        for tx in [x for x in t if _local(x.tag) in ("nonDerivativeTransaction", "derivativeTransaction")]:
            code = _text(tx, "transactionCoding/transactionCode")
            shares = _num(_text(tx, "transactionAmounts/transactionShares"))
            price = _num(_text(tx, "transactionAmounts/transactionPricePerShare"))
            ids = {f.get("id") for f in tx.iter() if _local(f.tag) == "footnoteId"}
            said = [notes.get(i, "") for i in ids if re.search(r"10b5-1", notes.get(i, ""), re.I)]
            plan = any(not re.search(r"\bnot\b[^.]{0,80}?10b5-1", n, re.I) for n in said) if said else None
            txs.append({"security": _text(tx, "securityTitle"), "date": _text(tx, "transactionDate"), "code": code,
                        "action": CODES.get(code, code or "Other"), "acquired": _text(tx, "transactionAmounts/transactionAcquiredDisposedCode") == "A",
                        "shares": shares, "price": price if price else None,
                        "value": round(shares * price, 2) if shares and price else None,
                        "owned_after": _num(_text(tx, "postTransactionAmounts/sharesOwnedFollowingTransaction")),
                        "direct": _text(tx, "ownershipNature/directOrIndirectOwnership") != "I", "derivative": deriv, "plan": plan})
    # the filing's 10b5-1 box says that a trade in it was made under a plan, not which: it marks the trades only when
    # no trade's own footnote says either way
    told = any(t["plan"] is not None for t in txs)
    for t in txs:
        t["plan"] = bool(t["plan"]) if told else plan_flag
    return {"issuer": _text(root, "issuer/issuerName"), "ticker": _text(root, "issuer/issuerTradingSymbol"),
            "owners": owners, "transactions": txs, "period": _text(root, "periodOfReport")}


NA = "n/a"


def _money(v: Optional[float]) -> str:
    if v is None:
        return NA
    for unit, word in ((1e9, "billion"), (1e6, "million")):
        if abs(v) >= unit:
            x = v / unit
            return f"${x:,.2f} {word}" if abs(x) < 100 else f"${x:,.1f} {word}"
    return f"${v:,.0f}"


def _int(v: Optional[float]) -> str:
    return NA if v is None else f"{v:,.0f}"


_E = html.escape
# The reader drops a document's <style> and the site's own CSS styles tables, so these documents carry no classes
# (a class can collide with the page's, as "note" did) and set the few styles that matter inline: numbers and
# dates never wrap, so a narrow column cannot split "151,610,700" or a date across lines.
_NUM = ' style="text-align:right;white-space:nowrap"'
_NOWRAP = ' style="white-space:nowrap"'
_NOTE = ' style="font-size:12px;color:#555"'
_GROUP = ' style="background:#f4f6f5;font-weight:600"'
_LINK = ' style="color:inherit;text-decoration:none;border:0"'   # the filing's link reads as the filing's text, not the site's green link


def _table(head: list, numeric: set, nowrap: set, rows: list) -> str:
    """rows: lists of cells (already escaped), or a string for a full-width row."""
    out = "<table><tr>" + "".join(f"<th{_NUM if k in numeric else _NOWRAP}>{h}</th>" for k, h in enumerate(head)) + "</tr>"
    for r in rows:
        if isinstance(r, str):
            out += f'<tr><td colspan="{len(head)}"{_GROUP}>{r}</td></tr>'
        else:
            out += "<tr>" + "".join(f"<td{_NUM if k in numeric else _NOWRAP if k in nowrap else ''}>{c}</td>" for k, c in enumerate(r)) + "</tr>"
    return out + "</table>"


def _page(title: str, marker: str, body: str) -> str:
    return (f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="bmg-doc" content="{marker}"><title>{_E(title)}</title>'
            '<style>body{font:14px/1.45 Georgia,serif;margin:24px;color:#111}table{border-collapse:collapse;margin:10px 0}'
            'td,th{border-bottom:1px solid #ccc;padding:4px 8px;text-align:left;font-size:13px}th{border-bottom:2px solid #111}</style>'
            '</head><body>' + body + "</body></html>")


def _who(name: str, role: str, n: int) -> str:
    cut = role[:n]
    cut += ")" * max(0, cut.count("(") - cut.count(")"))        # a title cut inside its own parentheses closes them
    return f"{name} ({cut})" if role else name


def form4_document(company: str, ticker: str, cik: int, filings: list, months: int) -> str:
    """filings: [{filed, accession, url, parsed}] newest first."""
    rows, groups = [], []
    # a Form 4/A restates an earlier Form 4 (the same insider or insiders, the same reporting date): only the latest
    # version is counted, so a corrected price or share count is not added on top of the original
    key = lambda f: ("; ".join(o["name"] for o in f["parsed"]["owners"]), f["parsed"].get("period") or "")
    latest = {}
    for f in filings:
        if f.get("form") == "4/A" and f["filed"] >= latest.get(key(f), ""):
            latest[key(f)] = f["filed"]
    replaced = [f for f in filings if key(f) in latest and (f.get("form") != "4/A" or f["filed"] < latest[key(f)]) and f["filed"] <= latest[key(f)]]
    filings_used = [f for f in filings if not any(f is r for r in replaced)]
    for f in filings_used:
        p = f["parsed"]
        who = "; ".join(o["name"] for o in p["owners"]) or "Unknown"
        role = "; ".join(r for o in p["owners"] for r in o["roles"])
        mine = []
        for t in p["transactions"]:
            if t["derivative"] and t["code"] not in ("M", "X", "C"):
                continue                    # option grants and their terms: not shares changing hands
            mine.append({**t, "filed": f["filed"], "accession": f["accession"], "url": f["url"], "insider": who, "role": role})
        rows += mine
        if mine:
            groups.append((f, who, role, mine))
    buys = [r for r in rows if r["code"] == "P" and not r["derivative"]]
    sells = [r for r in rows if r["code"] == "S" and not r["derivative"]]
    tot = lambda rs: sum(r["value"] for r in rs if r["value"])
    shares = lambda rs: round(sum(r["shares"] for r in rs if r["shares"]))     # whole shares, so the line adds up as printed
    people = lambda rs: len({r["insider"] for r in rs})
    net = shares(buys) - shares(sells)
    # one class of stock, or several (Class A and Class B): shares of different classes are never added together
    def cls(r):
        m = re.search(r"\bclass\s+([a-z])\b", r.get("security") or "", re.I)
        return f"Class {m[1].upper()}" if m else "Common"
    classes = sorted({cls(r) for r in buys + sells})
    planned = [r for r in sells if r["plan"]]
    span = f"{filings[-1]['filed']} to {filings[0]['filed']}" if filings else "no filings"
    summary = [
        f"Open-market purchases: {len(buys)} by {people(buys)} insider{'s' if people(buys) != 1 else ''}, {_money(tot(buys)) if buys else '$0'} in total.",
        f"Open-market sales: {len(sells)} by {people(sells)} insider{'s' if people(sells) != 1 else ''}, {_money(tot(sells)) if sells else '$0'} in total"
        + (f"; {len(planned)} of them under a Rule 10b5-1 trading plan set in advance." if sells else "."),
        (f"Open-market shares: {_int(shares(buys))} bought, {_int(shares(sells))} sold, net {_int(abs(net))}"
         + (" bought." if net > 0 else " sold." if net < 0 else ".")) if len(classes) < 2 else
        "Open-market shares by class: " + "; ".join(f"{c}: {_int(shares([r for r in buys if cls(r) == c]))} bought, "
                                                   f"{_int(shares([r for r in sells if cls(r) == c]))} sold" for c in classes) + ".",
        f"Other transactions: {sum(1 for r in rows if r['code'] == 'A')} grants or awards, {sum(1 for r in rows if r['code'] == 'F')} share withholdings for tax, "
        f"{sum(1 for r in rows if r['code'] in ('M', 'X'))} exercises or conversions of options and stock units, {sum(1 for r in rows if r['code'] == 'G')} gifts.",
    ]
    big = sorted([r for r in rows if r["value"] and r["code"] in ("P", "S")], key=lambda r: -r["value"])[:5]
    # One full-width line per filing (who filed it, their role, the filing), then its transactions: seven columns
    # fit the reading column, where eleven split words and numbers.
    table = []
    for f, who, role, mine in groups:
        table.append(f"Filed {_E(f['filed'])} by {_E(_who(who, role, 60))}, filing <a href=\"{_E(f['url'])}\"{_LINK}>{_E(f['accession'])}</a>")
        table += [[_E(r["date"] or NA), _E(r["code"] or NA), _E(r["action"] + (" (10b5-1 plan)" if r["plan"] and r["code"] == "S" else "")),
                   _int(r["shares"]), f"${r['price']:,.2f}" if r["price"] else NA, _money(r["value"]), _int(r["owned_after"])] for r in mine]
    body = (f"<h1>BMG insider trades: {_E(company)} ({_E(ticker)})</h1>"
            f"<p>Form 4 filings by {_E(company)}'s officers, directors and 10% owners from SEC EDGAR, {len(filings)} filings filed {span} "
            f"(the last {months} months, at most 60 filings). Each row is one transaction as reported, with the filing it comes from"
            + (f"; {len(replaced)} earlier filing{'s are' if len(replaced) != 1 else ' is'} left out because a Form 4/A restates {'them' if len(replaced) != 1 else 'it'}" if replaced else "")
            + ".</p>"
            "<h2>Summary</h2>" + "".join(f"<p>{_E(x)}</p>" for x in summary)
            + ("<h2>Largest open-market trades</h2>" + "".join(f"<p>{_E(_who(r['insider'], r['role'], 50))}: {_E(r['action'].lower())} of {_int(r['shares'])} shares "
                                                               f"at ${r['price']:,.2f} on {_E(r['date'])}{' (10b5-1 plan)' if r['plan'] and r['code'] == 'S' else ''}, {_money(r['value'])}. "
                                                               f"Filing {_E(r['accession'])}.</p>" for r in big) if big else "")
            + "<h2>Every transaction</h2><p>Each filing, newest first, with the transactions it reports: trade date, the SEC transaction code, "
              "shares, price per share, value (shares times price) and the shares the insider owned after the trade.</p>"
            + _table(["Trade date", "Code", "Action", "Shares", "Price", "Value", "Owned after"], {3, 4, 5, 6}, {0, 1}, table)
            + f'<p{_NOTE}>BMG note: open-market purchases are trades insiders choose to make with their own money; sales are often '
            "pre-scheduled under Rule 10b5-1 plans or made to pay taxes on vesting shares, so a sale alone says little. Values are shares times "
            "the reported price. Codes are the SEC's transaction codes. Not investment advice.</p>")
    return _page(f"BMG insider trades: {company} ({ticker})", "insider-trades", body)


# ---------------------------------------------------------------------- 13F
def parse_13f(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    agg = {}
    for it in root.iter():
        if _local(it.tag) != "infoTable":
            continue
        g = lambda name: next(((x.text or "").strip() for x in it.iter() if _local(x.tag) == name), "")
        key = (g("cusip"), g("putCall"), g("titleOfClass"))
        row = agg.setdefault(key, {"issuer": g("nameOfIssuer"), "class": g("titleOfClass"), "cusip": g("cusip"), "put_call": g("putCall"),
                                   "value": 0.0, "shares": 0.0, "kind": g("sshPrnamtType")})
        row["value"] += _num(g("value")) or 0.0
        row["shares"] += _num(g("sshPrnamt")) or 0.0
    rows = sorted(agg.values(), key=lambda r: -r["value"])
    return rows


_ABBR = {"FINL": "FINANCIAL", "AMER": "AMERICA", "HLDGS": "HOLDINGS", "HLDG": "HOLDING", "INTL": "INTERNATIONAL", "TECHNOLOGIES": "TECHNOLOGY",
         "TECH": "TECHNOLOGY", "SYS": "SYSTEMS", "COMMUNICATIONS": "COMMUNICATION", "COMMUN": "COMMUNICATION", "PPTYS": "PROPERTIES", "MTRS": "MOTORS",
         "GROUP": "", "CORPORATION": "", "CORP": "", "INC": "", "CO": "", "COMPANY": "", "LTD": "", "PLC": "", "NEW": "", "DEL": "", "THE": "",
         "CL": "", "CLASS": "", "COM": "", "SHS": "", "N.V.": "", "NV": "", "SA": "", "AG": "", "LP": "", "L.P.": "", "LLC": "", "OF": "", "&": ""}


def name_key(name: str) -> list[str]:
    words = re.findall(r"[A-Z0-9][A-Z0-9.&']*", (name or "").upper().replace("-", " "))
    out = []
    for w in words:
        w = w.strip(".'")
        w = _ABBR.get(w, w)
        if w:
            out.append(w)
    return out


def same_company(a: str, b: str) -> bool:
    ka, kb = name_key(a), name_key(b)
    if not ka or not kb or ka[0] != kb[0]:
        return False
    A, B = set(ka), set(kb)
    return len(A & B) / max(1, min(len(A), len(B))) >= 0.67


def holdings_document(fund: str, cik: int, filing: dict, rows: list, prior: Optional[dict], highlight: Optional[str]) -> str:
    """filing: {period, filed, accession, url}; prior: {period, rows} or None; highlight: a company name to look for."""
    total = sum(r["value"] for r in rows)
    pmap = {(r["cusip"], r["put_call"], r["class"]): r for r in (prior or {}).get("rows", [])}
    cmap = {(r["cusip"], r["put_call"], r["class"]) for r in rows}

    def change(r):
        p = pmap.get((r["cusip"], r["put_call"], r["class"]))
        if not prior:
            return NA
        if not p:
            return "New position"
        if not p["shares"]:
            return NA
        d = (r["shares"] / p["shares"] - 1) * 100
        return "Unchanged" if abs(d) < 0.05 else f"{'+' if d > 0 else ''}{d:.1f}% shares"
    new = [r for r in rows if prior and (r["cusip"], r["put_call"], r["class"]) not in pmap]
    gone = [p for k, p in pmap.items() if k not in cmap]
    top = rows[:50]
    hl = [r for r in rows if highlight and same_company(r["issuer"], highlight)]
    for r in hl:
        if r not in top:
            top.append(r)
    rank = {id(r): i + 1 for i, r in enumerate(rows)}
    # The class (and put or call) rides with the issuer, and the CUSIP stays out of the table: six columns fit the
    # reading column, and a nine-digit CUSIP is not a figure for the spider to lift.
    label = lambda r: r["issuer"] + (f" ({', '.join(x for x in (r['class'], r['put_call']) if x)})" if r["class"] or r["put_call"] else "")
    table = [[str(rank[id(r)]), _E(label(r)), _money(r["value"]), _int(r["shares"]), f"{(r['value'] / total * 100) if total else 0:.2f}%", _E(change(r))]
             for r in top]
    lines = [f"{len(rows)} positions worth {_money(total)} in total, as reported for the quarter ended {filing['period']} (filed {filing['filed']}).",
             f"The 10 largest positions are {sum(r['value'] for r in rows[:10]) / total * 100 if total else 0:.1f}% of the reported value."]
    if prior:
        pl = lambda n, w: f"{n} {w}{'' if n == 1 else 's'}"
        lines.append(f"Against the quarter ended {prior['period']}: {pl(len(new), 'new position')}, {pl(len(gone), 'position')} no longer reported.")
    else:
        lines.append("The manager's previous 13F-HR could not be read, so changes from the prior quarter are not shown.")
    if highlight:
        if hl:
            lines += [f"{highlight} appears in this filing as {r['issuer']} ({r['class']}): {_int(r['shares'])} shares worth {_money(r['value'])}, "
                      f"rank {rank[id(r)]} of {len(rows)}, {change(r).lower()}." for r in hl]
        else:
            lines.append(f"{highlight} does not appear in this filing (matched by issuer name).")
    body = (f"<h1>BMG fund holdings: {_E(fund)}</h1>"
            f"<p>Form 13F-HR from SEC EDGAR, CIK {cik}, accession <a href=\"{_E(filing['url'])}\"{_LINK}>{_E(filing['accession'])}</a>. "
            "A 13F lists the U.S.-listed stocks and options a large investment manager held at the quarter end; it is filed up to 45 days later, "
            "and it leaves out cash, shorts, bonds and non-U.S. holdings.</p>"
            "<h2>Summary</h2>" + "".join(f"<p>{_E(x)}</p>" for x in lines)
            + "<h2>Largest positions</h2><p>The 50 largest positions by reported value"
            + (f" (and every line for {_E(highlight)})" if hl and any(r not in rows[:50] for r in hl) else "")
            + ". Weight is the position's value over the filing's total; change compares shares with "
            + (f"the quarter ended {_E(prior['period'])}." if prior else "the prior quarter, which was not available.") + "</p>"
            + _table(["Rank", "Issuer", "Value", "Shares", "Weight", "Change"], {0, 2, 3, 4}, set(), table)
            + (("<h2>New positions</h2>" + "".join(f"<p>{_E(r['issuer'])} ({_E(r['class'])}): {_int(r['shares'])} shares, {_money(r['value'])}.</p>" for r in new[:15])) if new else "")
            + (("<h2>No longer reported</h2>" + "".join(f"<p>{_E(p['issuer'])} ({_E(p['class'])}): {_int(p['shares'])} shares in the quarter ended {_E(prior['period'])}.</p>" for p in gone[:15])) if gone else "")
            + f'<p{_NOTE}>Values and shares are summed across the manager\'s rows for each security, as reported. Weight is the position\'s value over '
              "the filing's total reported value. Not investment advice.</p>")
    return _page(f"BMG fund holdings: {fund}", "fund-holdings", body)
