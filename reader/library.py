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
import threading
import time
from pathlib import Path
from typing import Optional

_HERE = Path(__file__).resolve().parent
VERSION = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(_HERE.glob("*.py")))).hexdigest()[:16]


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
            json.dump({"version": VERSION, "result": res}, fh, default=str)
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

    def lookup(self, sha: str, ticker: Optional[str]) -> Optional[dict]:
        path = self.root / f"{sha}-{(ticker or '_').upper()}.json.gz"
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            return None
        return saved["result"] if saved.get("version") == VERSION else None

    def entries(self) -> list[dict]:
        return self._index()

    def source(self, sha: str) -> Optional[tuple[bytes, str]]:
        if not all(c in "0123456789abcdef" for c in sha) or len(sha) != 64:
            return None
        src = self.root / f"{sha}.src"
        if not src.exists():
            return None
        title = next((e.get("title") for e in self._index() if e.get("sha256") == sha), None) or f"{sha[:12]}.bin"
        return src.read_bytes(), title
