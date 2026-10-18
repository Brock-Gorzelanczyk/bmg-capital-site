"""A redline of two filings: every line of this year's document against last year's, section by section.

Lines are paragraphs and table rows as the reader extracted them. Within each 10-K/10-Q Item the two lists of lines
are aligned (difflib); a line only this year is added, a line only last year is deleted, and a pair of lines that
are alike (half or more of their words in common) is changed, with the words that differ marked inside it. A change
that only touches numbers is told apart from a change in wording, so a table rolled forward a year does not bury a
new sentence. Unchanged lines are counted, not repeated. Every block carries its page in each year's document.
"""
from __future__ import annotations

import bisect
import difflib
import re
from typing import Optional

_WS = re.compile(r"\s+")
_NUMTOK = re.compile(r"^[\(\-$]*[\d,.]+%?\)?$|^[\u2014\u2013-]$")


def _norm(line: str) -> str:
    return _WS.sub(" ", line.replace("’", "'").replace("“", '"').replace("”", '"').replace("\xa0", " ")).strip()


def _lines(text: str, page_starts: list) -> list:
    """(normalized line, offset, page) for every non-empty line."""
    starts = [o for o, _ in page_starts]
    pages = [p for _, p in page_starts]
    out, pos = [], 0
    for raw in text.split("\n"):
        t = _norm(raw)
        if t:
            k = bisect.bisect_right(starts, pos) - 1
            out.append((t, pos, pages[k] if k >= 0 else None))
        pos += len(raw) + 1
    return out


def _split(lines: list, sections: list) -> list:
    """[(item key, title, lines)] in document order; lines before the first Item are the cover and contents."""
    secs = sorted((s for s in sections or [] if s.get("offset") is not None), key=lambda s: s["offset"])
    out, cur = [("cover", "Cover and contents", [])], 0
    bounds = [(s["offset"], str(s["item"]).upper(), s.get("title") or "") for s in secs]
    for ln in lines:
        while cur < len(bounds) and ln[1] >= bounds[cur][0]:
            out.append((bounds[cur][1], bounds[cur][2], []))
            cur += 1
        out[-1][2].append(ln)
    return [x for x in out if x[2]]


def _numbers_only(a: str, b: str) -> bool:
    """The two lines differ only in their numbers (a table row rolled forward a year)."""
    wa = [w for w in a.split(" ") if not _NUMTOK.match(w)]
    wb = [w for w in b.split(" ") if not _NUMTOK.match(w)]
    return wa == wb


def _words(a: str, b: str) -> list:
    """Word-level marks for a changed line: [["=", text], ["-", text], ["+", text], ...]. Words are matched without
    their closing punctuation ("Ohio." and "Ohio" are the same word); a kept word is shown as this year prints it."""
    wa, wb = a.split(" "), b.split(" ")
    key = lambda w: w.rstrip(".,;:")
    sm = difflib.SequenceMatcher(None, [key(w) for w in wa], [key(w) for w in wb], autojunk=False)
    out = []
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            out.append(["=", " ".join(wb[j1:j2])])
        else:
            if i2 > i1:
                out.append(["-", " ".join(wa[i1:i2])])
            if j2 > j1:
                out.append(["+", " ".join(wb[j1:j2])])
    return out


def _alike(a: str, b: str) -> float:
    wa, wb = a.split(" "), b.split(" ")
    if not wa or not wb:
        return 0.0
    return difflib.SequenceMatcher(None, wa, wb, autojunk=False).ratio()


def _block(kind: str, now: Optional[tuple], before: Optional[tuple], words: Optional[list] = None) -> dict:
    b = {"type": kind}
    # a changed line is carried by its word marks; an added or deleted line by its text
    if now:
        b["now"] = {"offset": now[1], "page": now[2], **({} if words is not None else {"text": now[0][:4000]})}
    if before:
        b["before"] = {"offset": before[1], "page": before[2], **({} if words is not None else {"text": before[0][:4000]})}
    if words is not None:
        b["words"] = words
        b["numbers_only"] = _numbers_only(before[0], now[0])
    letters = lambda s: len(re.sub(r"[^A-Za-z]", "", s))
    b["size"] = (letters(now[0]) if now else 0) + (letters(before[0]) if before else 0) if kind != "changed" else \
        sum(len(t) for op, t in words if op != "=" and re.search(r"[A-Za-z]", t)) + (2 if not b["numbers_only"] else 0)
    return b


def _diff_lines(a: list, b: list, blocks: list) -> int:
    """Blocks for last year's lines `a` against this year's `b`; returns the count of unchanged lines."""
    sm = difflib.SequenceMatcher(None, [x[0] for x in a], [x[0] for x in b], autojunk=len(a) + len(b) > 4000)
    same = 0
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "equal":
            same += i2 - i1
            continue
        old, new = a[i1:i2], b[j1:j2]
        if op == "replace":
            # pair each new line with the most alike old line still unpaired (in order), the rest are added or deleted
            used = set()
            k0 = 0
            for nl in new:
                best, bs = None, 0.5
                for k in range(k0, min(len(old), k0 + 12)):
                    if k in used:
                        continue
                    r = _alike(old[k][0], nl[0])
                    if r > bs:
                        best, bs = k, r
                if best is None:
                    blocks.append(_block("added", nl, None))
                    continue
                for k in range(k0, best):          # old lines skipped over were removed
                    if k not in used:
                        blocks.append(_block("deleted", None, old[k]))
                        used.add(k)
                used.add(best)
                k0 = best + 1
                blocks.append(_block("changed", nl, old[best], _words(old[best][0], nl[0])))
            for k in range(len(old)):
                if k not in used:
                    blocks.append(_block("deleted", None, old[k]))
        elif op == "delete":
            blocks.extend(_block("deleted", None, x) for x in old)
        elif op == "insert":
            blocks.extend(_block("added", x, None) for x in new)
    return same


def diff(cur_text: str, cur_pages: list, cur_sections: list, prior_text: str, prior_pages: list, prior_sections: list,
         max_blocks: int = 6000) -> dict:
    a_secs = _split(_lines(prior_text, prior_pages), prior_sections)
    b_secs = _split(_lines(cur_text, cur_pages), cur_sections)
    a_by = {k: (t, ls) for k, t, ls in a_secs}
    out, seen, total = [], set(), {"added": 0, "deleted": 0, "changed": 0, "changed_words": 0, "unchanged": 0}
    for key, title, b_lines in b_secs:
        seen.add(key)
        blocks: list = []
        if key in a_by:
            same = _diff_lines(a_by[key][1], b_lines, blocks)
        else:
            same = 0
            blocks.extend(_block("added", x, None) for x in b_lines)
        out.append({"item": key, "title": title, "blocks": blocks, "unchanged": same, "only": None if key in a_by else "now"})
    for key, title, a_lines in a_secs:
        if key not in seen:
            out.append({"item": key, "title": title, "blocks": [_block("deleted", None, x) for x in a_lines], "unchanged": 0, "only": "before"})
    n = 0
    for s in out:
        c = {"added": 0, "deleted": 0, "changed": 0, "changed_words": 0}
        for b in s["blocks"]:
            c[b["type"]] += 1
            if b["type"] == "changed" and not b["numbers_only"]:
                c["changed_words"] += 1
        s["counts"] = c
        for k in c:
            total[k] += c[k]
        total["unchanged"] += s["unchanged"]
        if n + len(s["blocks"]) > max_blocks:
            s["blocks"] = s["blocks"][:max(0, max_blocks - n)]
            s["truncated"] = True
        n += len(s["blocks"])
    # the biggest changes in wording: whole paragraphs added or removed, and sentences reworded (not number roll-forwards)
    ranked = []
    for si, s in enumerate(out):
        for bi, b in enumerate(s["blocks"]):
            if b["type"] == "changed" and b["numbers_only"]:
                continue
            ranked.append((b["size"], si, bi))
    ranked.sort(reverse=True)
    top = [{"section": si, "block": bi, "item": out[si]["item"], "type": out[si]["blocks"][bi]["type"], "size": sz} for sz, si, bi in ranked[:25]]
    return {"sections": out, "counts": total, "top": top, "truncated": any(s.get("truncated") for s in out)}


def sections_of(text: str, page_starts: list) -> list:
    """Item headings of a saved read that predates stored sections, found again in its text."""
    from . import document
    d = document.Document(sha256="", title="", kind="", pages=[], body=text, page_starts=[tuple(x) for x in page_starts])
    return [{"item": s.item, "title": s.title, "offset": s.offset, "page": s.page} for s in document._sections(text, d)]
