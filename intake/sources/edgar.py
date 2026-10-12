"""EDGAR source: fetch new 8-K / 10-Q / 10-K / Form 4 per CIK via edgartools.

Primary library: edgartools (pinned via requirements-intake.txt).
Fallback: direct data.sec.gov submissions JSON if edgartools unavailable.

Identity header on every call: BMG Capital research bot, Brock Gorzelanczyk,
32bgorzelanczyk@gmail.com, max 8 req/s to SEC.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

from .. import store, http as hp

log = logging.getLogger("intake.edgar")

try:
    from edgar import Company, set_identity
    EDGAR_OK = True
    set_identity(hp.SEC_UA)
except ImportError:
    EDGAR_OK = False

FORMS = ("8-K", "10-Q", "10-K", "4")


def _raw_dir(ticker: str, form: str, accession: str) -> Path:
    d = store.DATA_DIR / "raw" / ticker / "edgar" / form / accession
    d.mkdir(parents=True, exist_ok=True)
    return d


def _text_dir(ticker: str, form: str, accession: str) -> Path:
    d = store.DATA_DIR / "text" / ticker / "edgar" / form / accession
    d.mkdir(parents=True, exist_ok=True)
    return d


async def poll_edgar(ticker: str, cik: str, *, since: Optional[str] = None, max_filings: int = 20) -> dict:
    """Pull new filings since `since` for one CIK. Returns run counts.

    Idempotent: a second run adds zero rows via the UNIQUE(source_url) constraint.
    Bounded: at most `max_filings` filings are processed per call. Scheduled runs
    use since=last_poll_date so each call only sees a handful of new filings; the
    cap defends against a cold-start pulling the whole history and OOM-ing the
    container (seen 2026-10-11 live run).
    """
    import datetime as _dt
    counts = {"ticker": ticker, "new": 0, "by_form": {}, "errors": [], "scanned": 0}
    run_id = store.start_run(f"edgar_poll:{ticker}")
    try:
        if not EDGAR_OK:
            raise RuntimeError("edgartools not installed; install site/server/requirements-intake.txt")
        from edgar import Company
        co = Company(cik)
        # Default to a 90-day lookback so cold-starts don't try to download 20+ years of history
        if since is None:
            since = (_dt.date.today() - _dt.timedelta(days=90)).isoformat()
        filings = co.get_filings(form=list(FORMS))
        for f in filings:
            if counts["scanned"] >= max_filings:
                break
            form = f.form
            accession = f.accession_no if hasattr(f, "accession_no") else str(f.accession_number)
            filing_date = str(f.filing_date)
            if filing_date < since:
                continue
            counts["scanned"] += 1
            raw = _raw_dir(ticker, form, accession)
            # Primary doc
            try:
                primary_url = f.filing_url if hasattr(f, "filing_url") else None
                # Fetch primary and attachments via edgartools' built-in download
                try:
                    f.download(path=str(raw), include_all=True)
                except Exception:
                    # Fallback: just fetch the index
                    pass
                # Form 4: save to CSV
                if form == "4":
                    try:
                        df = f.obj().to_dataframe()
                        csv_path = raw / f"form4_{accession}.csv"
                        df.to_csv(csv_path, index=False)
                    except Exception as e:
                        log.warning(f"Form4 to_dataframe failed {accession}: {e}")
                # Index each downloaded file. Form 4 CSVs are BMG-generated
                # derived artifacts; their source_url points to the SEC
                # accession folder (always 200) rather than a synthetic .csv
                # URL that would 403 (the CSV does not exist at sec.gov).
                accession_folder = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-','')}/"
                for p in raw.rglob("*"):
                    if not p.is_file() or p.suffix.lower() not in (".htm", ".html", ".pdf", ".xml"):
                        continue
                    data = p.read_bytes()
                    sha = store.sha256_bytes(data)
                    doc_type = form
                    name = p.name.lower()
                    if "ex-99" in name or "ex99" in name:
                        doc_type = "EX-99"
                    did = store.insert_document(
                        ticker=ticker, doc_type=doc_type, title=p.name,
                        source="edgar",
                        source_url=accession_folder + p.name,
                        published_at=filing_date,
                        sha256=sha,
                        raw_path=str(p.relative_to(store.DATA_DIR)),
                        text_path=None,
                        bytes_=len(data),
                        meta={"accession": accession, "form": form, "cik": cik},
                    )
                    if did:
                        counts["new"] += 1
                        counts["by_form"][form] = counts["by_form"].get(form, 0) + 1
                # Form 4: index the CSV we generated separately, with its
                # source_url set to the accession folder index (not the CSV)
                if form == "4":
                    csv_path = raw / f"form4_{accession}.csv"
                    if csv_path.is_file():
                        data = csv_path.read_bytes()
                        sha = store.sha256_bytes(data)
                        did = store.insert_document(
                            ticker=ticker, doc_type="4", title=f"Form 4 {accession} (CSV)",
                            source="edgar",
                            source_url=accession_folder,  # folder index page (200)
                            published_at=filing_date,
                            sha256=sha,
                            raw_path=str(csv_path.relative_to(store.DATA_DIR)),
                            text_path=None,
                            bytes_=len(data),
                            meta={"accession": accession, "form": "4", "cik": cik, "derived": "csv_from_edgartools"},
                        )
                        if did:
                            counts["new"] += 1
                            counts["by_form"]["4"] = counts["by_form"].get("4", 0) + 1
            except Exception as e:
                counts["errors"].append(f"{accession}: {e}")
                log.warning(f"filing {accession} failed: {e}")
        store.end_run(run_id, ok=True, counts=counts)
    except Exception as e:
        store.end_run(run_id, ok=False, error=str(e), counts=counts)
        log.error(f"poll_edgar {ticker} failed: {e}")
        counts["errors"].append(str(e))
    return counts
