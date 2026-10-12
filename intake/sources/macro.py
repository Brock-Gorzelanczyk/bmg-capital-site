"""Macro sources: FRED (series DGS2/5/10/30, T10Y2Y, SOFR, DFF, BAMLC0A0CM,
BAMLH0A0HYM2, RAILFRTCARLOADSD11, RAILFRTINTERMODALD11, INDPRO), AAR weekly rail
traffic page + PDF, STB EP 724 consolidated xlsx, RSI ARCI quarterly PDF.

FRED key comes from env FRED_API_KEY (Railway variable). If missing, log and skip FRED.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path

from .. import store, http as hp

log = logging.getLogger("intake.macro")

FRED_SERIES = [
    "DGS2", "DGS5", "DGS10", "DGS30",
    "T10Y2Y", "SOFR", "DFF",
    "BAMLC0A0CM", "BAMLH0A0HYM2",
    "RAILFRTCARLOADSD11", "RAILFRTINTERMODALD11", "INDPRO",
]


async def poll_fred() -> dict:
    counts = {"series_saved": 0, "skipped": 0, "errors": []}
    run_id = store.start_run("fred")
    key = os.environ.get("FRED_API_KEY")
    if not key:
        log.warning("FRED_API_KEY not set in env; FRED poll BLOCKED (Brock to set Railway variable)")
        counts["errors"].append("FRED_API_KEY not set")
        store.end_run(run_id, ok=False, error="FRED_API_KEY not set", counts=counts)
        return counts
    try:
        for sid in FRED_SERIES:
            url = f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}&file_type=json"
            try:
                res = await hp.fetch(url)
                payload = json.loads(res.content)
                obs = payload.get("observations", [])
                raw_dir = store.DATA_DIR / "raw" / "MACRO" / "fred"
                raw_dir.mkdir(parents=True, exist_ok=True)
                csv_path = raw_dir / f"{sid}.csv"
                with csv_path.open("w", newline="") as f:
                    w = csv.writer(f)
                    w.writerow(["date", "value"])
                    for o in obs:
                        w.writerow([o.get("date"), o.get("value")])
                last = obs[-1] if obs else {}
                sha = store.sha256_bytes(csv_path.read_bytes())
                store.insert_document(
                    ticker="MACRO", doc_type="SERIES", title=sid,
                    source="fred",
                    source_url=f"https://fred.stlouisfed.org/series/{sid}",
                    published_at=last.get("date"),
                    sha256=sha, raw_path=str(csv_path.relative_to(store.DATA_DIR)),
                    text_path=None, bytes_=csv_path.stat().st_size,
                    meta={"series_id": sid, "last_date": last.get("date"), "last_value": last.get("value")},
                )
                counts["series_saved"] += 1
            except Exception as e:
                counts["errors"].append(f"{sid}: {e}")
                log.warning(f"FRED {sid} failed: {e}")
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
    return counts


AAR_LIST_URL = "https://www.aar.org/aar_news/weekly-rail-traffic-data/"


async def poll_aar() -> dict:
    counts = {"new": 0, "errors": []}
    run_id = store.start_run("aar_weekly")
    try:
        try:
            res = await hp.fetch(AAR_LIST_URL)
        except Exception as e:
            counts["errors"].append(f"list page: {e}")
            store.end_run(run_id, ok=False, error=str(e), counts=counts)
            return counts
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.content, "html.parser")
        # Latest "week ending" release link
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "aar-reports-weekly-rail-traffic-for-the-week-ending-" not in href:
                continue
            if store.already_have(source_url=href):
                break
            # Fetch the release
            try:
                page = await hp.fetch(href)
            except Exception as pe:
                counts["errors"].append(f"{href}: {pe}")
                break
            sha = store.sha256_bytes(page.content)
            raw_dir = store.DATA_DIR / "raw" / "MACRO" / "aar"
            raw_dir.mkdir(parents=True, exist_ok=True)
            name = href.rstrip("/").split("/")[-1]
            raw_path = raw_dir / f"{name}.html"
            raw_path.write_bytes(page.content)
            # Extract carloads and intermodal from prose
            txt = BeautifulSoup(page.content, "html.parser").get_text(" ", strip=True)
            carloads = None
            intermodal = None
            m = re.search(r"total U\.?S\.? weekly carloads [^\d]*([\d,]+)", txt, re.I)
            if m: carloads = m.group(1).replace(",", "")
            m = re.search(r"intermodal [^\d]*([\d,]+)", txt, re.I)
            if m: intermodal = m.group(1).replace(",", "")
            m = re.search(r"week ending ([A-Za-z]+ \d{1,2},? \d{4})", txt, re.I)
            week_ending = m.group(1) if m else None
            did = store.insert_document(
                ticker="MACRO", doc_type="RAIL_TRAFFIC",
                title=f"AAR weekly rail traffic, week ending {week_ending or 'unknown'}",
                source="aar", source_url=href,
                published_at=week_ending,
                sha256=sha, raw_path=str(raw_path.relative_to(store.DATA_DIR)),
                text_path=None, bytes_=len(page.content),
                meta={"carloads": carloads, "intermodal": intermodal},
            )
            if did:
                counts["new"] += 1
            # Follow the PDF link
            soup2 = BeautifulSoup(page.content, "html.parser")
            for a2 in soup2.find_all("a", href=True):
                h2 = a2["href"]
                if "railtraffic.pdf" in h2 and not store.already_have(source_url=h2):
                    try:
                        pdf = await hp.fetch(h2)
                        psha = store.sha256_bytes(pdf.content)
                        pdf_path = raw_dir / f"{name}.pdf"
                        pdf_path.write_bytes(pdf.content)
                        store.insert_document(
                            ticker="MACRO", doc_type="RAIL_TRAFFIC",
                            title=f"AAR weekly PDF, week ending {week_ending or 'unknown'}",
                            source="aar", source_url=h2,
                            published_at=week_ending,
                            sha256=psha, raw_path=str(pdf_path.relative_to(store.DATA_DIR)),
                            text_path=None, bytes_=len(pdf.content),
                            meta={"from_release": href},
                        )
                    except Exception as pe:
                        counts["errors"].append(f"AAR PDF {h2}: {pe}")
            break  # only latest
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
    return counts


STB_LIST_URL = "https://www.stb.gov/reports-data/rail-service-data/"


async def poll_stb() -> dict:
    counts = {"new": 0, "errors": []}
    run_id = store.start_run("stb_ep724")
    try:
        res = await hp.fetch(STB_LIST_URL)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.content, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "EP724" in href and "Consolidated" in href and href.endswith(".xlsx"):
                if store.already_have(source_url=href):
                    break
                try:
                    xl = await hp.fetch(href)
                    sha = store.sha256_bytes(xl.content)
                    raw_dir = store.DATA_DIR / "raw" / "MACRO" / "stb"
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    name = href.rsplit("/", 1)[-1]
                    raw_path = raw_dir / name
                    raw_path.write_bytes(xl.content)
                    store.insert_document(
                        ticker="MACRO", doc_type="RAIL_TRAFFIC",
                        title=f"STB EP724 Consolidated: {name}",
                        source="stb", source_url=href, published_at=None,
                        sha256=sha, raw_path=str(raw_path.relative_to(store.DATA_DIR)),
                        text_path=None, bytes_=len(xl.content), meta={},
                    )
                    counts["new"] += 1
                except Exception as e:
                    counts["errors"].append(f"STB {href}: {e}")
                break
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
    return counts


RSI_LIST_URL = "https://www.rsiweb.org/data-technical-resources/freight-car-statistics/"


async def poll_rsi() -> dict:
    counts = {"new": 0, "errors": []}
    run_id = store.start_run("rsi_arci")
    try:
        res = await hp.fetch(RSI_LIST_URL)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(res.content, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "ARCI-Summary" in href and href.endswith(".pdf"):
                if store.already_have(source_url=href):
                    break
                try:
                    pdf = await hp.fetch(href)
                    if pdf.status != 200:
                        log.warning(f"RSI PDF status {pdf.status}: likely members-only; skipping")
                        counts["errors"].append(f"members-only or auth: {href}")
                        break
                    sha = store.sha256_bytes(pdf.content)
                    raw_dir = store.DATA_DIR / "raw" / "MACRO" / "rsi"
                    raw_dir.mkdir(parents=True, exist_ok=True)
                    name = href.rsplit("/", 1)[-1]
                    raw_path = raw_dir / name
                    raw_path.write_bytes(pdf.content)
                    store.insert_document(
                        ticker="MACRO", doc_type="RAILCAR_ORDERS",
                        title=f"RSI ARCI Summary: {name}",
                        source="rsi", source_url=href, published_at=None,
                        sha256=sha, raw_path=str(raw_path.relative_to(store.DATA_DIR)),
                        text_path=None, bytes_=len(pdf.content), meta={},
                    )
                    counts["new"] += 1
                except Exception as e:
                    counts["errors"].append(f"RSI {href}: {e}")
                break
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
    return counts
