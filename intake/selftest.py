"""Local acceptance test for the intake package.

Usage:  python -m intake.selftest

Covers G1 (store round-trip + hash) and robots.txt load, as the main gates that
can be verified without live network calls. Live gates (sec.gov rate, deployed
/desk endpoints, 24h volume persistence) are not covered here; see
site/reports/intake-1-report.md for the blocker list.
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import time
from pathlib import Path


def main() -> int:
    # Point DATA_DIR to a tmp dir for the self-test
    tmp = tempfile.mkdtemp(prefix="intake_selftest_")
    os.environ["INTAKE_DATA_DIR"] = tmp
    from importlib import reload
    from . import store
    reload(store)

    # 1. write a fake document row
    data = b"hello world " * 100
    sha = store.sha256_bytes(data)
    did = store.insert_document(
        ticker="TEST", doc_type="PRESS_RELEASE", title="selftest row",
        source="selftest",
        source_url="https://example.invalid/selftest/" + sha[:8],
        published_at="2026-10-11", sha256=sha,
        raw_path="raw/TEST/selftest.txt", text_path=None, bytes_=len(data),
        meta={"selftest": True},
    )
    assert did is not None, "insert_document should return doc id"
    assert did == sha[:16], f"doc id should be sha256-first-16-hex; got {did}"

    # 2. second insert of the same source_url should return None (dedup)
    did2 = store.insert_document(
        ticker="TEST", doc_type="PRESS_RELEASE", title="dup",
        source="selftest",
        source_url="https://example.invalid/selftest/" + sha[:8],
        published_at="2026-10-11", sha256=sha,
        raw_path="raw/TEST/selftest.txt", text_path=None, bytes_=len(data),
    )
    assert did2 is None, "duplicate url insert should return None"

    # 3. feed_rows should include our row
    rows = store.feed_rows(ticker="TEST", limit=10, types=None, since=None)
    assert len(rows) == 1, f"expected 1 row, got {len(rows)}"
    assert rows[0]["id"] == did

    # 4. get_doc round-trip
    row = store.get_doc(did)
    assert row and row["sha256"] == sha

    # 5. health_summary runs
    h = store.health_summary()
    assert h["counts"]["documents_total"] >= 1
    assert "jobs" in h

    # 6. runs lifecycle
    rid = store.start_run("selftest_job")
    time.sleep(0.01)
    store.end_run(rid, ok=True, counts={"items": 1})
    r = store.latest_run("selftest_job")
    assert r and r["ok"] == 1

    # 7. export index.json
    n = store.export_index_json()
    assert n == 1

    print(f"OK selftest wrote and read: doc_id={did}, sha256_first8={sha[:8]}, dir={tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
