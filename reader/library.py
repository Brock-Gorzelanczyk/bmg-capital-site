"""My reads: every finished read kept on the owner's Mac, so it can be reopened later.

    lib = Library(root)            # root: a folder on local disk
    lib.save(job, data, result)    # after a read finishes
    lib.lookup(sha, ticker)        # a saved result for this exact file, or None
    lib.entries()                  # the list shown under "My reads", newest first
    lib.source(sha)                # (bytes, title) of the saved file

A saved result is reused only when the reader code is unchanged since it was saved (VERSION, a
hash of reader/*.py). After any reader change the file is read again, so a reopened report never
shows numbers from older extraction rules. Nothing is ever deleted.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import threading
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
# the brain's modules (brain.py, brain_pages.py) file a finished read and never change it, so they are not part of the
# version: a change to them does not make every saved read be read again
VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(_HERE.glob("*.py")) if not p.name.startswith("brain"))).hexdigest()[:16]


class Library:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"
        self.lock = threading.Lock()

    def _index(self) -> list[dict]:
        try:
            return json.loads(self.index_path.read_text())
        except (OSError, ValueError):
            return []

    def _key(self, sha: str, ticker: Optional[str]) -> str:
        return f"{sha}:{(ticker or '').upper()}"

    def save(self, job, data: bytes, res: dict) -> None:
        rep = res.get("report") or {}
        co = rep.get("company") or {}
        cov = rep.get("coverage") or {}
        sc = rep.get("second_check") or {}
        key = self._key(job.sha256, job.ticker)
        src = self.root / f"{job.sha256}.src"
        if not src.exists():
            src.write_bytes(data)
        with gzip.open(self.root / f"{job.sha256}-{(job.ticker or '_').upper()}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump({"version": VERSION, "result": res, "stages": getattr(job, "stages", None) or {}}, fh, default=str)
        entry = {"key": key, "sha256": job.sha256, "job_ticker": job.ticker or "", "ticker": (job.ticker or co.get("ticker") or co.get("ticker_hint") or ""),
                 "company": co.get("name") or "", "form": co.get("form") or "", "period": co.get("period") or "",
                 "title": job.title, "kind": job.kind, "findings": len(rep.get("findings") or []),
                 "signals": len(rep.get("signals") or []), "checks_run": cov.get("checks_run"),
                 "checks_failed": cov.get("checks_failed"), "coverage": cov.get("status"),
                 "second_check": sc.get("status"), "flagged": len(sc.get("flagged") or []) if sc.get("status") == "done" else None,
                 "saved_at": time.strftime("%Y-%m-%d %H:%M"), "bytes": len(data)}
        with self.lock:
            idx = [e for e in self._index() if e.get("key") != key]
            idx.insert(0, entry)
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(idx, indent=1))
            tmp.replace(self.index_path)

    def _path(self, sha: str, ticker: Optional[str]) -> Optional[Path]:
        """The saved result's file, or None when the id or ticker could name a file outside the library."""
        t = (ticker or "_").upper()
        if len(sha) != 64 or not all(c in "0123456789abcdef" for c in sha) or not re.fullmatch(r"[A-Z0-9._\-]{1,16}", t) or t.strip(".") == "":
            return None
        return self.root / f"{sha}-{t}.json.gz"

    def _load(self, sha: str, ticker: Optional[str]) -> Optional[dict]:
        path = self._path(sha, ticker)
        if not path:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return None
        return saved if saved.get("version") == VERSION else None

    def lookup(self, sha: str, ticker: Optional[str]) -> Optional[dict]:
        saved = self._load(sha, ticker)
        return saved["result"] if saved else None

    def stages(self, sha: str, ticker: Optional[str]) -> dict:
        """The stage counters of the original read (pages, facts, checks ...), so a reopened read shows them."""
        saved = self._load(sha, ticker)
        return (saved or {}).get("stages") or {}

    def entries(self) -> list[dict]:
        return self._index()

    def load_any(self, sha: str, ticker: Optional[str]) -> Optional[dict]:
        """A saved result whatever reader version wrote it (its text and pages do not go stale; Ask across reads uses
        only those), or None."""
        path = self._path(sha, ticker)
        if not path:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return None
        return {"result": saved.get("result"), "current": saved.get("version") == VERSION}

    # ---- highlights and notes, kept beside the read (one JSON file per document, never deleted)
    def _notes_path(self, sha: str) -> Optional[Path]:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            return None
        return self.root / f"{sha}.notes.json"

    def notes(self, sha: str) -> list:
        p = self._notes_path(sha)
        try:
            return json.loads(p.read_text()) if p and p.exists() else []
        except (OSError, ValueError):
            return []

    def save_notes(self, sha: str, notes: list) -> bool:
        p = self._notes_path(sha)
        if not p:
            return False
        with self.lock:
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps(notes, indent=1))
            tmp.replace(p)
        return True

    def source(self, sha: str) -> Optional[tuple[bytes, str]]:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            return None
        src = self.root / f"{sha}.src"
        if not src.exists():
            return None
        title = next((e.get("title") for e in self._index() if e.get("sha256") == sha), None) or f"{sha[:12]}.bin"
        return src.read_bytes(), title
