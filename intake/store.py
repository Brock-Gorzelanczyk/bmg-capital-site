"""SQLite index at /data/intake.db. Schema per intake-bot-stack.md (c).

Tables:
  documents(id TEXT PRIMARY KEY, ticker, type, title, source, source_url UNIQUE,
            published_at, fetched_at, sha256, raw_path, text_path, bytes, meta_json)
  runs(id INTEGER PRIMARY KEY, job, started_at, ended_at, ok, error, counts_json)

Export: index.json rewritten after each run.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger("intake.store")

DATA_DIR = Path(os.environ.get("INTAKE_DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "intake.db"
INDEX_JSON = DATA_DIR / "index.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    ticker TEXT NOT NULL,
    type TEXT NOT NULL,
    title TEXT,
    source TEXT NOT NULL,
    source_url TEXT UNIQUE NOT NULL,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    raw_path TEXT,
    text_path TEXT,
    bytes INTEGER,
    meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_ticker ON documents(ticker);
CREATE INDEX IF NOT EXISTS idx_doc_type ON documents(type);
CREATE INDEX IF NOT EXISTS idx_doc_source ON documents(source);
CREATE INDEX IF NOT EXISTS idx_doc_sha ON documents(sha256);
CREATE INDEX IF NOT EXISTS idx_doc_pub ON documents(published_at DESC);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    ok INTEGER,
    error TEXT,
    counts_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_run_job ON runs(job, started_at DESC);
"""


def _ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "raw").mkdir(exist_ok=True)
    (DATA_DIR / "text").mkdir(exist_ok=True)
    (DATA_DIR / "digests").mkdir(exist_ok=True)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    _ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_id_from_sha(sha: str) -> str:
    return sha[:16]


def insert_document(
    *,
    ticker: str,
    doc_type: str,
    title: Optional[str],
    source: str,
    source_url: str,
    published_at: Optional[str],
    sha256: str,
    raw_path: Optional[str],
    text_path: Optional[str],
    bytes_: int,
    meta: Optional[dict] = None,
) -> Optional[str]:
    """Insert a document row. Returns doc id if newly inserted, None if duplicate
    source_url already in table."""
    with connect() as conn:
        try:
            did = doc_id_from_sha(sha256)
            conn.execute(
                """INSERT INTO documents
                   (id, ticker, type, title, source, source_url, published_at,
                    fetched_at, sha256, raw_path, text_path, bytes, meta_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (did, ticker, doc_type, title, source, source_url, published_at,
                 time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 sha256, raw_path, text_path, bytes_, json.dumps(meta or {})),
            )
            log.info(f"inserted doc id={did} ticker={ticker} type={doc_type} url={source_url}")
            return did
        except sqlite3.IntegrityError:
            log.info(f"duplicate url, skipping: {source_url}")
            return None


def already_have(source_url: Optional[str] = None, sha256: Optional[str] = None) -> bool:
    with connect() as conn:
        if source_url:
            r = conn.execute("SELECT 1 FROM documents WHERE source_url = ?", (source_url,)).fetchone()
            if r: return True
        if sha256:
            r = conn.execute("SELECT 1 FROM documents WHERE sha256 = ?", (sha256,)).fetchone()
            if r: return True
    return False


def start_run(job: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (job, started_at) VALUES (?, ?)",
            (job, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        return cur.lastrowid


def end_run(run_id: int, ok: bool, error: Optional[str] = None, counts: Optional[dict] = None) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE runs SET ended_at = ?, ok = ?, error = ?, counts_json = ? WHERE id = ?",
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             1 if ok else 0, error, json.dumps(counts or {}), run_id),
        )


def latest_run(job: str) -> Optional[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM runs WHERE job = ? ORDER BY started_at DESC LIMIT 1", (job,)
        ).fetchone()


def feed_rows(ticker: Optional[str], limit: int, types: Optional[list[str]], since: Optional[str]) -> list[dict]:
    sql = "SELECT id, ticker, type, title, source, source_url, published_at, fetched_at, text_path, bytes, meta_json FROM documents WHERE 1=1"
    args: list = []
    if ticker:
        sql += " AND ticker = ?"
        args.append(ticker)
    if types:
        placeholders = ",".join(["?"] * len(types))
        sql += f" AND type IN ({placeholders})"
        args.extend(types)
    if since:
        sql += " AND published_at >= ?"
        args.append(since)
    sql += " ORDER BY published_at DESC, fetched_at DESC LIMIT ?"
    args.append(limit)
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(d["meta_json"])
            except Exception:
                d["meta"] = {}
            d.pop("meta_json", None)
        out.append(d)
    return out


def get_doc(doc_id: str) -> Optional[dict]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if not r: return None
    d = dict(r)
    if d.get("meta_json"):
        try:
            d["meta"] = json.loads(d["meta_json"])
        except Exception:
            d["meta"] = {}
    return d


def health_summary() -> dict:
    """Latest-run per job + counts.

    Per-ticker fetchers (edgar/ir/news) write run rows with colon-suffixed
    job names like ``edgar_poll:GATX``. The health query matches by prefix
    so the canonical job buckets show the most recent per-source run.
    """
    jobs = ["edgar_poll", "ir_feeds", "news", "fred", "aar_weekly", "stb_ep724", "rsi_arci", "digest"]
    out = {"jobs": {}, "counts": {}, "db_path": str(DB_PATH), "data_dir": str(DATA_DIR)}
    with connect() as conn:
        for j in jobs:
            r = conn.execute(
                "SELECT started_at, ended_at, ok, error, job FROM runs "
                "WHERE job = ? OR job LIKE ? ORDER BY started_at DESC LIMIT 1",
                (j, f"{j}:%"),
            ).fetchone()
            out["jobs"][j] = dict(r) if r else {"started_at": None, "ok": None}
        total = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        out["counts"]["documents_total"] = total
        for t in ("8-K", "10-Q", "10-K", "FORM4", "EX-99", "PRESS_RELEASE", "DECK", "NEWS", "SERIES", "RAIL_TRAFFIC", "RAILCAR_ORDERS"):
            c = conn.execute("SELECT COUNT(*) FROM documents WHERE type=?", (t,)).fetchone()[0]
            out["counts"][t] = c
    # Disk usage
    try:
        total_sz = sum(f.stat().st_size for f in DATA_DIR.rglob("*") if f.is_file())
        out["volume_bytes"] = total_sz
    except Exception:
        out["volume_bytes"] = None
    return out


def export_index_json() -> int:
    """Rewrite /data/index.json with all document rows (minus raw_path)."""
    with connect() as conn:
        rows = conn.execute("SELECT id, ticker, type, title, source, source_url, published_at, fetched_at, sha256, text_path, bytes, meta_json FROM documents ORDER BY published_at DESC, fetched_at DESC").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get("meta_json"):
            try:
                d["meta"] = json.loads(d["meta_json"])
            except Exception:
                d["meta"] = {}
            d.pop("meta_json", None)
        out.append(d)
    INDEX_JSON.write_text(json.dumps({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "items": out}, indent=2))
    return len(out)
