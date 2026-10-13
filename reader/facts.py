"""Facts with evidence, reconciliation, checks and calculations.

A *reported* fact is one value printed in the filing: a table cell, with
its row label, column period, unit, scale, table, page and the printed
line as the excerpt. A *calculated* fact is arithmetic on cited reported
facts, with the formula and the input fact ids. Nothing here guesses:
when inputs are missing, of different periods, or the base is zero or
negative, the calculation is not published and the reason is kept.

Statuses (no numeric confidence scores):
    Source matched       the value is in the cited line, in the expected statement
    Cross-checked        also matched by an identity check or a second table
    Needs review         conflicting candidates, a failed check, or taken from a
                         secondary table because the statement did not show it
    Calculation checked  calculated from cited inputs of the same period
    Unavailable          could not be published; the reason says why
"""
from __future__ import annotations

import re
from typing import Optional

SCALE = {"": 1.0, "K": 1e3, "M": 1e6, "B": 1e9}

# key, name, statement, period kind, label patterns (most specific first), unit
METRICS = [
    ("revenue", "Revenue", "income", "flow",
     [r"^(total net sales|total revenues?|total net revenues?|total revenue|total operating revenues?)$", r"^(net sales|revenues?|net revenues?|sales|operating revenues?)$"], "USD"),
    ("cost_of_revenue", "Cost of revenue", "income", "flow",
     [r"^(total cost of sales|total cost of revenues?|total cost of revenue)$", r"^(cost of sales|cost of revenues?|cost of goods sold)$"], "USD"),
    ("gross_profit", "Gross profit", "income", "flow", [r"^(gross margin|gross profit)$", r"^margin$"], "USD"),
    ("operating_income", "Operating income", "income", "flow",
     [r"^(operating income|income from operations|operating income loss|earnings from operations|operating loss|loss from operations|total operating profit|operating profit|operating profit loss)$"], "USD"),
    ("net_income", "Net income", "income", "flow",
     [r"^net (income|earnings|loss)( loss)? attributable to (?!non ?controlling)[a-z0-9 .,&]+$", r"^(net income|net earnings|net income loss|net loss)$"], "USD"),
    ("pretax_income", "Income before income taxes", "income", "flow",
     [r"^(income|earnings) (from continuing operations )?before (provision for )?income tax(es)?[a-z ]*$", r"^income loss before income taxes$",
      r"^(loss|income loss|earnings loss) (from continuing operations )?before (provision for |benefit from )?income tax(es)?$"], "USD"),
    ("interest_expense", "Interest expense", "income", "flow",
     [r"^(interest expense|interest expense net|interest and foreign exchange|interest expense net of interest income|interest expense net of capitalized interest)$"], "USD"),
    ("gains_on_dispositions", "Gains on asset dispositions", "income", "flow",
     [r"^(net gain on asset dispositions|total gains on dispositions of property and other divestitures|net gain on disposition of equipment|"
      r"gains? on (dispositions|sales) of (property|equipment|assets)|net gains? on (sale|disposition)s? of (assets|equipment|railcars))$"], "USD"),
    ("net_income_consolidated", "Net income (consolidated, including noncontrolling interests)", "income", "flow",
     [r"^(net income|net earnings|net income loss|net loss|net earnings loss|consolidated net earnings|consolidated net income)$"], "USD"),
    ("nci_income", "Net income attributable to noncontrolling interests", "income", "flow",
     [r"^(less )?net (income|earnings|loss)( loss)? attributable to non ?controlling interests?( and redeemable non ?controlling interests?)?( in subsidiaries)?$"], "USD"),
    ("eps_basic", "Basic EPS", "income", "flow",
     [r"^basic (earnings|net income) per (common )?share$", r"^basic$", r"^basic (net income|net earnings|earnings) attributable to [a-z0-9 .,&]+$"], "USD/share"),
    ("eps_diluted", "Diluted EPS", "income", "flow",
     [r"^diluted (earnings|net income) per (common )?share$", r"^diluted$", r"^diluted (net income|net earnings|earnings) attributable to [a-z0-9 .,&]+$",
      r"^diluted (earnings|net income|net loss) per (common )?share (from|of) consolidated operations$",
      r"^(net )?(earnings|income) per (common )?share attributable to [a-z0-9 .,&]+ diluted$", r"^assuming dilution$"], "USD/share"),
    ("shares_diluted", "Diluted weighted-average shares", "income", "flow", [r"^diluted$", r"^diluted (shares|weighted average shares)$", r"^assuming dilution$"], "shares"),
    ("cash", "Cash and cash equivalents", "balance", "instant", [r"^cash and cash equivalents$", r"^cash and due from banks$"], "USD"),
    ("inventories", "Inventories", "balance", "instant", [r"^inventories$", r"^inventories net$"], "USD"),
    ("total_assets", "Total assets", "balance", "instant", [r"^total assets$"], "USD"),
    ("total_liabilities", "Total liabilities", "balance", "instant", [r"^total liabilities$"], "USD"),
    ("equity", "Shareholders' equity", "balance", "instant",
     [r"^total (share|stock)holders( s)? equity$", r"^total (?!liabilities)[a-z .,&]+ (share|stock)holders( s)? equity$", r"^total equity$"], "USD"),
    ("liab_and_equity", "Total liabilities and equity", "balance", "instant",
     [r"^total liabilities and (share|stock)holders( s)? equity$", r"^total liabilities (and|&) equity$"], "USD"),
    ("cfo", "Operating cash flow", "cashflow", "flow",
     [r"^(cash generated by operating activities|net cash (provided by|from|generated by|provided by used in|used in provided by) operating activities|net cash from operations)$",
      # only when no total line exists (a filing with no discontinued operations that still says "continuing")
      r"^net cash (provided by|from|provided by used in) operating activities (of|from) continuing operations$"], "USD"),
    ("capex", "Capital expenditures", "cashflow", "flow",
     [r"^(payments for acquisition of property plant and equipment|capital expenditures|purchases of property( plant)? and equipment|capital expenditures for property and equipment|additions to property plant and equipment|additions to property and equipment|portfolio investments and capital additions)$",
      r"^purchases of property( plant)? and equipment (excluding|net of) [a-z ,]+$"], "USD"),
    ("buybacks", "Share repurchases", "cashflow", "flow",
     [r"^(repurchases? of (common )?(stock|shares)|purchases? of treasury (stock|shares)|share repurchases?|stock repurchases?|payments for repurchase of common stock|shares repurchased|repurchases? of (common )?stock including excise tax|common stock repurchased|treasury stock repurchased)$"], "USD"),
    ("dividends", "Dividends paid", "cashflow", "flow",
     [r"^(payments for dividends and dividend equivalents|payments for dividends|dividends paid|cash dividends paid|payments of dividends|cash dividends|dividends|dividends paid to (common )?(stockholders|shareholders)|common stock (cash )?dividends paid|cash dividend payments|dividends paid on common( and preferred)? stock|dividends on common stock)$"], "USD"),
]
METRIC_NAMES = {k: n for k, n, *_ in METRICS}
DEBT_COMPONENTS = r"^(commercial paper|term debt|long term debt|current portion of long term debt|short term debt|short term borrowings|notes payable|revolving notes|senior notes|current maturities of long term debt|long term debt net|debt net|recourse|non recourse|recourse debt|non recourse debt|total non recourse|total non recourse debt|current portion of debt and finance leases|debt and finance leases net of current portion|current portion of long term debt net|long term debt net of current portion|long term debt excluding current portion|other short term debt|debt)$"
TOTAL_DEBT = r"^(total debt|total debt net|total term debt|total borrowings)$"


def fmt_value(v: Optional[float], unit: str) -> str:
    if v is None:
        return ""
    if unit == "USD/share":
        return f"${v:,.2f}" if v >= 0 else f"-${abs(v):,.2f}"
    if unit == "percent":
        return f"{v:.1f}%"
    if unit == "shares":
        a = abs(v)
        return (f"{a / 1e9:.2f} billion" if a >= 1e9 else f"{a / 1e6:.1f} million" if a >= 1e6 else f"{a:,.0f}") + " shares"
    a = abs(v)
    s = (f"${a / 1e12:.2f}T" if a >= 1e12 else f"${a / 1e9:.1f}B" if a >= 1e9 else f"${a / 1e6:.1f}M" if a >= 1e6
         else f"${a / 1e3:.1f}K" if a >= 1e3 else f"${a:,.0f}")
    return f"-{s}" if v < 0 else s


_MON = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _exact(v: float, unit: str) -> str:
    """Enough digits to tell two close values apart ($1,145.3M vs $1,077.2M)."""
    if unit == "USD":
        return f"${v / 1e6:,.1f}M"
    if unit == "shares":
        return f"{v / 1e6:,.2f} million shares"
    return f"{v:,.4g}"


def _period(col: dict, statement_kind: str) -> dict:
    t = col.get("type") or ""
    if statement_kind == "instant" or t == "instant":
        t = "instant"
    label = {"FY": "Fiscal year", "Q": "Three months", "6M": "Six months", "9M": "Nine months", "instant": "As of"}.get(t, "Period")
    when = col.get("label") or col.get("year")
    text = (f"FY{col['year']}" + (f" (ended {col['label']})" if col.get("end") else "")) if t == "FY" else (
        f"As of {when}" if t == "instant" else f"{label} ended {when}" if t else f"{when}")
    short = (f"FY{col['year']}" if t == "FY" else f"{when}" if t == "instant" else
             {"Q": "Q ended ", "6M": "6M ended ", "9M": "9M ended "}.get(t, "") + f"{when}")
    return {"type": t or "unknown", "end": col.get("end"), "year": col.get("year"), "label": text, "short": short}


_PART = ("affiliate", "segment", "subsidiary", "supplemental")


def _scoped(r: dict) -> bool:
    """The table reports part of the group or another entity (a segment, an
    equity-method affiliate, a subsidiary, a supplemental schedule), as read
    from its introducing sentence, note title and caption (tables.scope_of)."""
    return r.get("scope") in _PART


def _scope_label(r: dict) -> str:
    sc = r.get("scope")
    if sc in _PART:
        what = (r.get("block") or r.get("table_title") or "").strip()
        return f"{sc.capitalize()}" + (f": {what}" if what else "")
    return "Consolidated" if r.get("statement", "other") != "other" else "As labeled in the source table"


def _core(r: dict) -> bool:
    """A row of one of the company's own primary statements."""
    return r.get("statement", "other") != "other" and not _scoped(r)


def _pkey(p: dict) -> tuple:
    return (p["type"], p["end"] or p["year"])


def _match(rows: list, pats: list, with_pattern: bool = False):
    """Rows whose label matches; the first pattern that matches anything wins."""
    for pat in pats:
        rx = re.compile(pat)
        hit = [r for r in rows if rx.search(r["key"])]
        if hit:
            return (hit, pat) if with_pattern else hit
    return ([], None) if with_pattern else []


class FactBook:
    def __init__(self, doc, rows: list):
        self.doc, self.rows = doc, rows
        self.facts: dict[str, dict] = {}
        self.calcs: dict[str, dict] = {}
        self.checks: list[dict] = []
        self.issues: list[dict] = []
        self.primary_periods: dict[str, list] = {}

    # ------------------------------------------------------------------
    def _fact_from(self, mkey: str, r: dict, i: int, kind_of_period: str) -> dict:
        unit = r["unit"]
        mult = SCALE.get(r["scale"], 1.0) if unit in ("USD", "shares") else 1.0
        v = r["values"][i] * mult
        per = _period(r["cols"][i], kind_of_period)
        fid = f"{mkey}:{_pkey(per)[0]}:{_pkey(per)[1]}"
        return {
            "id": fid, "metric": mkey, "name": METRIC_NAMES.get(mkey, mkey), "kind": "reported",
            "value": v, "display": fmt_value(v, unit), "unit": unit, "currency": "USD" if unit.startswith("USD") else None,
            "scale": r["scale"] or "units", "period": {**per, **({"basis": "as restated or recast in this filing"} if r.get("restated") else {})},
            "scope": _scope_label(r),
            "source": {
                "doc_sha256": self.doc.sha256, "pdf_page": r["pdf_page"], "printed_page": r["printed_page"], "section": r["section"],
                "statement": r["statement"], "table_title": r["table_title"], "row_label": r["full_label"], "column": r["cols"][i]["label"],
                "header": r["header_text"], "caption": r["caption"], "excerpt": r["line"], "raw": r["raw"][i], "offset": r["offset"],
                "table_id": r.get("table_id"), "row_index": r.get("row_index"), "col_index": i, "table_scope": r.get("scope"),
                "intro": r.get("intro") or "",
            },
            "method": "Financial statement table row" if r["statement"] != "other" else "Table row outside the primary statements",
            "status": "Source matched", "reasons": [f"Value {r['raw'][i]} appears in the cited row on {self._pg(r['pdf_page'])}."],
            "also_reported": [], "conflicts": [],
        }

    def extract(self):
        for mkey, name, stmt, pkind, pats, unit in METRICS:
            # only rows of the metric's unit compete, so "Basic" in a share
            # count table cannot shadow "Basic net income attributable to ..."
            # never another registrant's statements (a combined 10-K's subsidiary or operating partnership)
            prim = [r for r in self.rows if r["statement"] == stmt and r["unit"] == unit and not r.get("other_registrant")]
            # the company's own consolidated statement first; note tables
            # (an affiliate's condensed balance sheet) only if it has nothing
            core = [r for r in prim if _core(r)]
            hits, used = _match(core, pats, True) if core else ([], None)
            if not hits:
                hits, used = _match(prim, pats, True)
            secondary = False
            if not hits and mkey in ("total_assets", "total_liabilities", "equity", "liab_and_equity"):
                continue                 # a balance total from a note table is another entity's (VIE, affiliate, held for sale)
            if not hits:
                h2, used = _match(self.rows, pats, True)
                hits = [r for r in h2 if r["unit"] == unit and r["statement"] in (stmt, "other") and not _scoped(r)
                        and not re.search(r"segment|geograph", (r["table_title"] + " " + r["heading"]).lower())]
                secondary = bool(hits)
            if not hits:
                continue
            row = hits[0]
            periods = []
            for i in range(len(row["values"])):
                f = self._fact_from(mkey, row, i, pkind)
                if f["id"] in self.facts:
                    continue
                if secondary:
                    f["status"] = "Needs review"
                    f["reasons"].append(f"Not found in the primary {stmt} statement; taken from \"{row['table_title'] or 'a table'}\" on {self._pg(row['pdf_page'])}.")
                self._corroborate(f, [used], unit, row)   # only rows of the same definition
                self.facts[f["id"]] = f
                periods.append(f["id"])
            self.primary_periods[mkey] = periods
        self._equity_structure()
        self._net_income_scope()
        self._debt()
        self._derive_liabilities()
        self._capex_components()
        self._narrative()
        if getattr(self.doc, "xbrl", None):
            self._xbrl()
            self._derive_liabilities()       # the totals may only have come from tags
        self._order_periods()
        self._checks()
        return self

    def _order_periods(self):
        """Latest period first, whatever the column order: Amazon prints 2023 | 2024 | 2025.
        Periods of each length stay together, in the order their length first appears,
        so a 10-Q keeps its quarters ahead of its year-to-date columns."""
        def per(fid):
            x = self.facts.get(fid) or self.calcs.get(fid)
            return (x or {}).get("period") or {}
        for m, ids in self.primary_periods.items():
            kinds = list(dict.fromkeys(per(i).get("type") for i in ids))
            by_end = sorted(ids, key=lambda i: str(per(i).get("end") or per(i).get("year") or ""), reverse=True)
            self.primary_periods[m] = sorted(by_end, key=lambda i: kinds.index(per(i).get("type")))

    def _xbrl(self):
        """Reconcile table facts with inline XBRL tags; fill gaps from tags."""
        from . import xbrl
        months = {"FY": 12, "Q": 3, "6M": 6, "9M": 9}
        for f in list(self.facts.values()):
            m = f["metric"]
            if m not in xbrl.CONCEPTS or not f["period"].get("end"):
                continue
            inst = f["period"]["type"] == "instant"
            hits = xbrl.find(self.doc.xbrl, m, f["period"]["end"], inst, None if inst else months.get(f["period"]["type"]))
            if not hits:
                continue
            near = lambda h: abs(abs(h["value"]) - abs(f["value"])) <= max(abs(f["value"]) * 0.005, 0.01)
            x = next((h for h in hits if near(h)), hits[0])     # e.g. equity with or without noncontrolling interest
            if near(x):
                f["also_reported"].append({"xbrl_concept": x["concept"], "anchor": x["anchor"]})
                f["reasons"].append(f"Matches the inline XBRL tag {x['concept']} for the same period.")
                if f["status"] == "Source matched":
                    f["status"] = "Cross-checked"
            else:
                f["conflicts"].append({"value": x["value"], "display": fmt_value(x["value"], f["unit"]), "xbrl_concept": x["concept"], "anchor": x["anchor"]})
                f["status"] = "Needs review"
                f["reasons"].append(f"The inline XBRL tag {x['concept']} shows {_exact(x['value'], f['unit'])} for the same period, against "
                                    f"{_exact(f['value'], f['unit'])} in the cited row; not reconciled automatically.")
                self.issues.append({"type": "conflict", "metric": f["name"], "period": f["period"]["label"], "message": f["reasons"][-1]})
        for mkey, name, stmt, pkind, pats, unit in METRICS:
            if self.primary_periods.get(mkey) or mkey not in xbrl.CONCEPTS:
                continue
            cands = [x for x in self.doc.xbrl if x["concept"] in xbrl.CONCEPTS[mkey] and not x["dims"] and x["instant"] == (pkind == "instant")]
            if not cands:
                continue
            ends = sorted({x["end"] for x in cands if x["end"]}, reverse=True)[:2]
            ids = []
            for e in ends:
                x = next(c for c in cands if c["end"] == e)
                t = "instant" if x["instant"] else {3: "Q", 6: "6M", 9: "9M", 12: "FY"}.get(
                    (int(e[:4]) - int(x["start"][:4])) * 12 + int(e[5:7]) - int(x["start"][5:7]) if x["start"] else 12, "unknown")
                y, mo, dd = e.split("-")
                col = {"type": t, "end": e, "year": y, "label": f"{_MON[int(mo) - 1]} {int(dd)}, {y}"}
                per = _period(col, "instant" if t == "instant" else "")
                fid = f"{mkey}:{t}:{e}"
                val = -abs(x["value"]) if mkey in ("capex", "buybacks", "dividends") else x["value"]    # tagged as positive payments; shown as outflows like the statement
                pos = self.doc.body.find(x["excerpt"][:60]) if x.get("excerpt") and len(x["excerpt"]) >= 20 else -1
                pg = self.doc.page_at(pos) if pos >= 0 else None
                self.facts[fid] = {"id": fid, "metric": mkey, "name": name, "kind": "reported", "value": val, "display": fmt_value(val, unit),
                                   "unit": unit, "currency": "USD" if unit.startswith("USD") else None, "scale": "units", "period": per, "scope": "Consolidated",
                                   "source": {"doc_sha256": self.doc.sha256, "pdf_page": pg, "printed_page": self.doc.printed_page(pg) if pg else None,
                                              "section": None, "anchor": x["anchor"], "offset": pos,
                                              "statement": stmt, "table_title": "Inline XBRL", "row_label": x["concept"], "column": e, "excerpt": x["excerpt"], "raw": x["raw"]},
                                   "method": "Inline XBRL tag", "status": "Source matched",
                                   "reasons": [f"Tagged {x['concept']} in the filing's inline XBRL; the statement table row was not matched by text."],
                                   "also_reported": [], "conflicts": []}
                ids.append(fid)
            self.primary_periods[mkey] = ids

    def _pg(self, n) -> str:
        return f"{'PDF ' if self.doc.kind == 'pdf' else ''}page {n}"

    def _corroborate(self, f: dict, pats: list, unit: str, own: dict):
        """Same label and period elsewhere: equal value adds a citation, a
        different value in another primary statement or MD&A table is a conflict."""
        rx = [re.compile(p) for p in pats]
        for r in self.rows:
            if r is own or r["unit"] != unit or not any(x.search(r["key"]) for x in rx):
                continue
            if re.search(r"non ?gaap|excluding|adjusted", r["full_label"].lower()):
                continue                 # a different (non-GAAP) measure, not a restatement
            if re.search(r"segment|geograph", (r["table_title"] + " " + r["heading"]).lower()):
                continue
            for i, c in enumerate(r["cols"]):
                if c.get("year") != f["period"]["year"]:
                    continue
                ct = c.get("type") or ""
                if ct and f["period"]["type"] not in ("unknown", ct) and not (f["period"]["type"] == "instant"):
                    continue
                mult = SCALE.get(r["scale"], 1.0) if unit in ("USD", "shares") else 1.0
                v = r["values"][i] * mult
                if abs(v - f["value"]) <= max(1e-9, abs(f["value"]) * 0.0005):
                    f["also_reported"].append({"pdf_page": r["pdf_page"], "printed_page": r["printed_page"], "table_title": r["table_title"] or "untitled table", "row_label": r["full_label"]})
                elif _scoped(r):
                    continue             # a segment's or affiliate's own table: a different scope
                elif ((r["statement"] == own["statement"] and _core(r)) or
                      ((r["statement"] == "other" or not _core(r) and r["statement"] == own["statement"])
                       and abs(v - f["value"]) <= abs(f["value"]) * 0.15)):
                    # Conflicts: the same consolidated statement printed twice, or
                    # an MD&A / note table with a near value. A much smaller
                    # total there is a segment or affiliate subtotal (a
                    # different scope); another statement (the statement of
                    # equity) is a different line.
                    # A much smaller "Total revenues" in the MD&A is a segment
                    # subtotal (a different scope), not a restatement.
                    if ct and ct != f["period"]["type"]:
                        continue
                    f["conflicts"].append({"value": v, "display": fmt_value(v, unit), "raw": r["raw"][i], "scale": r["scale"], "pdf_page": r["pdf_page"], "table_title": r["table_title"] or "untitled table", "row_label": r["full_label"]})
        if f["also_reported"]:
            pages = sorted({a["pdf_page"] for a in f["also_reported"] if a["pdf_page"]})
            f["reasons"].append("Same value also printed on " + ("PDF " if self.doc.kind == "pdf" else "") + "page" + ("s " if len(pages) > 1 else " ") + ", ".join(map(str, pages)) + ".")
            f["status"] = "Cross-checked" if f["status"] == "Source matched" else f["status"]
        if f["conflicts"]:
            f["status"] = "Needs review"
            c = f["conflicts"][0]
            sw = {"K": " (in thousands)", "M": " (in millions)", "B": " (in billions)"}.get(c.get("scale") or "", "")
            f["reasons"].append(f"\"{c['row_label']}\" on {self._pg(c['pdf_page'])} prints {c['raw']}{sw} for the same period, "
                                f"against {f['source']['raw']} in the cited row; not reconciled automatically.")
            self.issues.append({"type": "conflict", "metric": f["name"], "period": f["period"]["label"], "message": f["reasons"][-1]})

    def _debt(self):
        """Total debt: a printed total if the balance sheet has one, else the
        printed debt lines summed (a calculation, with the lines listed)."""
        bal = [r for r in self.rows if r["statement"] == "balance" and r["unit"] == "USD" and not r.get("other_registrant")]
        tot = [r for r in bal if re.search(TOTAL_DEBT, r["key"])]
        if tot:
            r = tot[0]
            ids = []
            for i in range(len(r["values"])):
                f = self._fact_from("total_debt", r, i, "instant")
                f["name"] = "Total debt"
                f["definition"] = (f"The balance sheet's total line under \"{r['heading']}\" (printed without its own label)." if r.get("unlabeled_total")
                                   else f"As printed: \"{r['full_label']}\" on the balance sheet.") + " Leases and other obligations are not included unless that line includes them."
                self.facts[f["id"]] = f
                ids.append(f["id"])
            self.primary_periods["total_debt"] = ids
            return
        comp = [r for r in bal if re.search(DEBT_COMPONENTS, r["key"])]
        # only the lines of the balance sheet the totals came from: a combined filing also
        # prints a subsidiary's or operating partnership's balance sheet (NEE and FPL, Prologis Inc. and L.P.)
        ta = [self.facts[x] for x in self.primary_periods.get("total_assets", []) if x in self.facts]
        tid = ta[0]["source"].get("table_id") if ta else None
        if tid is not None and any(r.get("table_id") == tid for r in comp):
            comp = [r for r in comp if r.get("table_id") == tid]
        if not comp:
            return
        ncols = len(comp[0]["values"])
        comp = [r for r in comp if len(r["values"]) == ncols and r["cols"] == comp[0]["cols"]]
        ids = []
        for i in range(ncols):
            inputs = []
            for r in comp:
                f = self._fact_from("debt_line", r, i, "instant")
                f["id"] = f"debt_line:{r['offset']}:{i}"
                f["name"] = r["label"] + (f" ({r['heading']})" if r["heading"] and r["heading"].lower() not in r["label"].lower() else "")
                self.facts[f["id"]] = f
                inputs.append(f["id"])
            per = _period(comp[0]["cols"][i], "instant")
            total = sum(self.facts[x]["value"] for x in inputs)
            cid = f"total_debt:{_pkey(per)[0]}:{_pkey(per)[1]}"
            self.calcs[cid] = {"id": cid, "metric": "total_debt", "name": "Total debt", "kind": "calculated", "value": total,
                               "display": fmt_value(total, "USD"), "unit": "USD", "period": per,
                               "formula": " + ".join(self.facts[x]["name"] for x in inputs), "inputs": inputs,
                               "status": "Calculation checked",
                               "definition": "Sum of the debt lines printed on the balance sheet: " + ", ".join(self.facts[x]["name"] for x in inputs)
                                             + ". Leases and other obligations are not included.", "reasons": []}
            ids.append(cid)
        self.primary_periods["total_debt"] = ids

    def _net_income_scope(self):
        """State which net income each figure is. 'net_income' is the parent's
        share when the statement prints it, else the consolidated total."""
        ids = self.primary_periods.get("net_income", [])
        cons = set(self.primary_periods.get("net_income_consolidated", []))
        for fid in ids:
            f = self.facts.get(fid)
            if not f:
                continue
            if fid in cons or not re.search(r"attributable", f["source"].get("row_label", ""), re.I):
                f["name"] = "Net income"
                f["definition"] = "Net income as printed; no separate line for the parent's share was matched, so this may include noncontrolling interests."
            else:
                f["name"] = "Net income attributable to the company"
                f["definition"] = f"\"{f['source']['row_label']}\": the parent's share, excluding noncontrolling interests."

    def _equity_structure(self):
        """Parent equity and total equity, read from the balance sheet's own
        layout: the equity total printed above the noncontrolling interest row
        is the parent's, the total below it includes noncontrolling interests.
        Without a noncontrolling interest row both are the same line."""
        bal = [r for r in self.rows if r["statement"] == "balance" and _core(r) and r["unit"] == "USD"]
        nci = next((r for r in bal if re.match(r"^(total )?non ?controlling interests?( in (consolidated )?subsidiaries)?$", r["key"])), None)
        eq_ids = self.primary_periods.get("equity", [])
        if not nci:
            self.primary_periods["equity_parent"] = list(eq_ids)
            self.primary_periods["equity_total"] = list(eq_ids)
            for fid in eq_ids:
                if fid in self.facts:
                    self.facts[fid]["definition"] = "Total equity as printed; the balance sheet shows no noncontrolling interest line."
            return
        same_t = [r for r in bal if r.get("table_id") == nci.get("table_id")]
        tl = [r["row_index"] for r in same_t if re.match(r"^total liabilities$", r["key"]) and r["row_index"] < nci["row_index"]]
        floor = tl[-1] if tl else -1          # the equity section starts below total liabilities
        before = [r for r in same_t if floor < r["row_index"] < nci["row_index"] and r["key"].startswith("total")
                  and (re.search(r"equity", r["key"]) or r.get("unlabeled_total")) and not re.search(r"liabilit|debt|recourse", r["key"])]
        after = [r for r in same_t if r["row_index"] > nci["row_index"] and r["key"].startswith("total") and re.search(r"equity", r["key"])
                 and not re.search(r"liabilit", r["key"])]
        parent_row = before[-1] if before else None
        total_row = after[0] if after else None

        def facts_from(row, mkey, name, definition):
            ids = []
            for i in range(len(row["values"])):
                f = self._fact_from(mkey, row, i, "instant")
                f["name"], f["definition"] = name, definition
                self._corroborate(f, ["^" + re.escape(row["key"]) + "$"], "USD", row)
                self.facts[f["id"]] = f
                ids.append(f["id"])
            return ids
        tot_ids = facts_from(total_row, "equity_total", "Total equity (including noncontrolling interests)",
                             f"\"{total_row['full_label']}\", printed below the noncontrolling interest line.") if total_row else []
        nci_ids = facts_from(nci, "equity_nci", "Noncontrolling interests (equity)", f"\"{nci['full_label']}\" on the balance sheet.")
        if parent_row:
            par_ids = facts_from(parent_row, "equity_parent", "Equity attributable to the company",
                                 f"\"{parent_row['full_label']}\", the equity total printed above the noncontrolling interest line.")
        else:
            par_ids = []
            for t_id, n_id in zip(tot_ids, nci_ids):
                t, n = self.facts[t_id], self.facts[n_id]
                v = t["value"] - n["value"]
                cid = f"equity_parent:{_pkey(t['period'])[0]}:{_pkey(t['period'])[1]}"
                self.calcs[cid] = {"id": cid, "metric": "equity_parent", "name": "Equity attributable to the company", "kind": "calculated", "value": v,
                                   "display": fmt_value(v, "USD"), "unit": "USD", "period": t["period"], "formula": "Total equity - Noncontrolling interests",
                                   "inputs": [t_id, n_id], "status": "Calculation checked",
                                   "definition": "The parent's equity total is not printed with its own label; derived as total equity minus noncontrolling interests.", "reasons": []}
                par_ids.append(cid)
        if par_ids and not tot_ids:
            # parent total and NCI printed, no line for their sum: total equity is derived
            for p_id, n_id in zip(par_ids, nci_ids):
                p_, n = self.facts.get(p_id) or self.calcs.get(p_id), self.facts[n_id]
                if _pkey(p_["period"]) != _pkey(n["period"]):
                    continue
                v = p_["value"] + n["value"]
                cid = f"equity_total:{_pkey(p_['period'])[0]}:{_pkey(p_['period'])[1]}"
                self.calcs[cid] = {"id": cid, "metric": "equity_total", "name": "Total equity (including noncontrolling interests)", "kind": "calculated",
                                   "value": v, "display": fmt_value(v, "USD"), "unit": "USD", "period": p_["period"],
                                   "formula": "Equity attributable to the company + Noncontrolling interests", "inputs": [p_id, n_id],
                                   "status": "Calculation checked",
                                   "definition": "The balance sheet prints the parent's equity and noncontrolling interests but no total equity line; derived as their sum. "
                                                 "Redeemable noncontrolling interest presented outside equity is not included.", "reasons": []}
                tot_ids.append(cid)
        self.primary_periods["equity_parent"] = par_ids
        self.primary_periods["equity_total"] = tot_ids or list(eq_ids)
        self.primary_periods["equity_nci"] = nci_ids
        # 'equity' (shareholders' equity in the report) is the parent's share
        if par_ids:
            self.primary_periods["equity"] = par_ids

    def _derive_liabilities(self):
        """Total liabilities when the balance sheet prints no such line:
        total liabilities and equity minus total equity, as a calculation."""
        if self.primary_periods.get("total_liabilities") or not self.primary_periods.get("liab_and_equity"):
            return
        eq_rows = [r for r in self.rows if r["statement"] == "balance" and _core(r) and r["unit"] == "USD" and re.match(r"^total equity$", r["key"])]
        ids = []
        for i, le_id in enumerate(self.primary_periods["liab_and_equity"]):
            le = self.facts[le_id]
            eq = None
            if eq_rows and i < len(eq_rows[0]["values"]):
                eq = self._fact_from("total_equity", eq_rows[0], i, "instant")
                eq["name"] = "Total equity (including noncontrolling interest)"
                self.facts[eq["id"]] = eq
            else:
                eq = self.get("equity", i)
            if not eq or _pkey(eq["period"]) != _pkey(le["period"]):
                continue
            v = le["value"] - eq["value"]
            inputs, formula = [le_id, eq["id"]], f"Total liabilities and equity - {eq['name']}"
            note = "Anything presented between liabilities and equity (for example redeemable noncontrolling interest) is included."
            mez = next((r for r in self.rows if r["statement"] == "balance" and _core(r) and r["unit"] == "USD" and i < len(r["values"])
                        and re.search(r"redeemable|temporary equity|mezzanine", r["key"]) and not r["key"].startswith("total liabilities")), None)
            if mez:
                mf = self._fact_from("mezzanine", mez, i, "instant")
                if _pkey(mf["period"]) == _pkey(le["period"]):
                    mf["name"] = mez["full_label"]
                    self.facts[mf["id"]] = mf
                    v -= mf["value"]
                    inputs.append(mf["id"])
                    formula += f" - {mez['full_label']}"
                    note = f"\"{mez['full_label']}\", presented between liabilities and equity, is excluded from both."
            cid = f"total_liabilities:{_pkey(le['period'])[0]}:{_pkey(le['period'])[1]}"
            self.calcs[cid] = {"id": cid, "metric": "total_liabilities", "name": "Total liabilities", "kind": "calculated", "value": v,
                               "display": fmt_value(v, "USD"), "unit": "USD", "period": le["period"],
                               "formula": formula, "inputs": inputs, "status": "Calculation checked",
                               "definition": "The balance sheet prints no total liabilities line; derived as total liabilities and equity minus total equity. " + note,
                               "reasons": []}
            ids.append(cid)
        if ids:
            self.primary_periods["total_liabilities"] = ids

    def _capex_components(self):
        """Capital expenditures printed as several lines (e.g. "Capital
        expenditures - lease fleet" and "- operating and administrative")
        with no total: their sum, as a calculation listing the lines."""
        if self.primary_periods.get("capex"):
            return
        comp = [r for r in self.rows if r["statement"] == "cashflow" and r["unit"] == "USD" and not r.get("other_registrant")
                and re.match(r"^capital expenditures [a-z]", r["key"]) and not re.search(r"accrued|payable|non ?cash", r["key"])]
        if len(comp) < 2:
            return
        comp = [r for r in comp if r["cols"] == comp[0]["cols"] and abs(r["offset"] - comp[0]["offset"]) < 3000]
        if len(comp) < 2:
            return
        ids = []
        for i in range(len(comp[0]["values"])):
            inputs = []
            for r in comp:
                f = self._fact_from("capex_line", r, i, "flow")
                f["id"] = f"capex_line:{r['offset']}:{i}"
                f["name"] = r["label"]
                self.facts[f["id"]] = f
                inputs.append(f["id"])
            per = self.facts[inputs[0]]["period"]
            total = sum(self.facts[x]["value"] for x in inputs)
            cid = f"capex:{_pkey(per)[0]}:{_pkey(per)[1]}"
            names = [self.facts[x]["name"] for x in inputs]
            self.calcs[cid] = {"id": cid, "metric": "capex", "name": "Capital expenditures", "kind": "calculated", "value": total,
                               "display": fmt_value(total, "USD"), "unit": "USD", "period": per, "formula": " + ".join(names),
                               "inputs": inputs, "status": "Calculation checked",
                               "definition": "Sum of the capital expenditure lines printed in the cash flow statement: " + ", ".join(names) + ".", "reasons": []}
            ids.append(cid)
        self.primary_periods["capex"] = ids

    def _narrative(self):
        """Cover-page facts that live in sentences, not tables."""
        head = self.doc.body[:20000]
        m = re.search(r"aggregate market value of the (?:voting and non-voting )?(?:common )?(?:stock|equity)[^.$]{0,240}?\$\s?([\d,]+(?:\.\d+)?)\s*(billion|million|thousand)?", head, re.I | re.S)
        if m:
            v = float(m.group(1).replace(",", "")) * {"billion": 1e9, "million": 1e6, "thousand": 1e3}.get((m.group(2) or "").lower(), 1)
            sent_start = head.rfind("\n", 0, m.start()) + 1
            dm = re.search(r"as of ([A-Z][a-z]+ \d{1,2}, \d{4})", head[m.start() - 300 if m.start() > 300 else 0:m.end()])
            loc = self.doc.location(m.start())
            self.facts["market_value_nonaffiliates"] = {
                "id": "market_value_nonaffiliates", "metric": "market_value_nonaffiliates", "name": "Market value of equity held by non-affiliates",
                "kind": "reported", "value": v, "display": fmt_value(v, "USD"), "unit": "USD", "currency": "USD",
                "period": {"type": "instant", "end": None, "year": None, "label": f"As of {dm.group(1)}" if dm else "Date as stated on the cover"},
                "scope": "Non-affiliate holders only",
                "source": {"doc_sha256": self.doc.sha256, **loc, "excerpt": re.sub(r"\s+", " ", head[sent_start:m.end() + 40])[:300], "raw": m.group(0)[-40:], "offset": m.start()},
                "method": "Cover page sentence", "status": "Source matched",
                "reasons": ["Historical figure from the cover page at the stated date. It is not the current market capitalization and excludes affiliate holdings."],
                "also_reported": [], "conflicts": []}

    # ------------------------------------------------------------------
    def get(self, mkey: str, which: int = 0) -> Optional[dict]:
        ids = self.primary_periods.get(mkey, [])
        if which >= len(ids):
            return None
        return self.facts.get(ids[which]) or self.calcs.get(ids[which])

    def comparable(self, mkey: str) -> tuple[Optional[dict], Optional[dict]]:
        """Current period and the comparable prior period of the same type."""
        ids = self.primary_periods.get(mkey, [])
        items = [self.facts.get(i) or self.calcs.get(i) for i in ids]
        items = [x for x in items if x]
        if not items:
            return None, None
        cur = items[0]
        for x in items[1:]:
            if x["period"]["type"] == cur["period"]["type"]:
                return cur, x
        return cur, None

    def _check(self, name: str, lhs: Optional[dict], rhs_items: list, sign: list, tol: float = 0.005):
        if not lhs or any(x is None for x in rhs_items):
            return
        if len({_pkey(x["period"]) for x in [lhs] + rhs_items}) != 1:
            return
        rhs = sum(s * x["value"] for s, x in zip(sign, rhs_items))
        ok = abs(lhs["value"] - rhs) <= max(abs(lhs["value"]) * tol, 0.5e6 if lhs["unit"] == "USD" else 0.01)
        chk = {"name": name, "period": lhs["period"]["label"], "passed": ok,
               "detail": f"{lhs['display']} vs {fmt_value(rhs, lhs['unit'])}", "facts": [lhs["id"]] + [x["id"] for x in rhs_items]}
        self.checks.append(chk)
        for f in [lhs] + rhs_items:
            if f["id"] not in self.facts:
                continue
            if ok:
                f["reasons"].append(f"Consistent with: {name} ({chk['period']}).")
                if f["status"] == "Source matched":
                    f["status"] = "Cross-checked"
            else:
                f["status"] = "Needs review"
                f["reasons"].append(f"Check failed: {name} ({chk['detail']}).")
        if not ok:
            self.issues.append({"type": "check_failed", "metric": name, "period": chk["period"], "message": f"{name} does not hold: {chk['detail']}."})

    def _checks(self):
        for which in range(2):
            rev, cost, gp = self.get("revenue", which), self.get("cost_of_revenue", which), self.get("gross_profit", which)
            self._check("Gross profit = Revenue - Cost of revenue", gp, [rev, cost], [1, -1])
            ta, tl, eq, le = self.get("total_assets", which), self.get("total_liabilities", which), self.get("equity", which), self.get("liab_and_equity", which)
            if le:
                self._check("Total assets = Total liabilities and equity", ta, [le], [1])
            elif tl and eq:
                self._check("Total assets = Total liabilities + Shareholders' equity", ta, [tl, eq], [1, 1])
            ni, sh, eps = self.get("net_income", which), self.get("shares_diluted", which), self.get("eps_diluted", which)
            if ni and sh and eps and sh["value"]:
                if _pkey(ni["period"]) == _pkey(sh["period"]) == _pkey(eps["period"]):
                    implied = ni["value"] / sh["value"]
                    ok = abs(implied - eps["value"]) <= max(0.02, abs(eps["value"]) * 0.02)
                    self.checks.append({"name": "Diluted EPS close to Net income / Diluted shares", "period": eps["period"]["label"], "passed": ok,
                                        "detail": f"{eps['display']} vs implied ${implied:.2f}", "facts": [eps["id"], ni["id"], sh["id"]]})
                    if ok:
                        eps["reasons"].append(f"Consistent with Net income / Diluted shares (implied ${implied:.2f}).")
                        if eps["status"] == "Source matched":
                            eps["status"] = "Cross-checked"
                    else:
                        eps["status"] = "Needs review"
                        eps["reasons"].append(f"Net income / diluted shares gives ${implied:.2f}; differs from the printed EPS (attribution or share class differences can explain this).")

    # ------------------------------------------------------------------
    def calc(self, cid: str, name: str, formula: str, inputs: list, fn, unit: str, note: str = "") -> dict:
        """Publish a calculation only if every input exists and shares one period."""
        missing = [n for n, x in inputs if x is None]
        if missing:
            c = {"id": cid, "name": name, "kind": "calculated", "formula": formula, "inputs": [], "status": "Unavailable",
                 "reasons": ["Missing input: " + ", ".join(missing) + "."], "value": None, "display": ""}
        else:
            xs = [x for _, x in inputs]
            if len({_pkey(x["period"]) for x in xs}) != 1 and not note.startswith("cross-period"):
                c = {"id": cid, "name": name, "kind": "calculated", "formula": formula, "inputs": [x["id"] for x in xs], "status": "Unavailable",
                     "reasons": ["Inputs are for different periods: " + "; ".join(sorted({x["period"]["label"] for x in xs})) + "."], "value": None, "display": ""}
            else:
                v, why = fn(*[x["value"] for x in xs])
                if v is None:
                    c = {"id": cid, "name": name, "kind": "calculated", "formula": formula, "inputs": [x["id"] for x in xs],
                         "status": "Unavailable", "reasons": [why], "value": None, "display": ""}
                else:
                    c = {"id": cid, "name": name, "kind": "calculated", "formula": formula, "inputs": [x["id"] for x in xs],
                         "status": "Calculation checked", "reasons": [why] if why else [], "value": v, "display": fmt_value(v, unit),
                         "unit": unit, "period": xs[0]["period"]}
        self.calcs[cid] = c
        return c


def growth(cur: float, prior: float) -> tuple[Optional[float], str]:
    if prior == 0:
        return None, "Prior value is zero, so a percentage change is not meaningful."
    if prior < 0 or cur < 0:
        return None, "A negative base or result makes a percentage change misleading; see the absolute change."
    return (cur - prior) / prior * 100, ""


def ratio(num: float, den: float) -> tuple[Optional[float], str]:
    if den <= 0:
        return None, "Denominator is zero or negative."
    return num / den * 100, ""
