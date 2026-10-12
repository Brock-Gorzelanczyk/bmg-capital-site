"""Vault sync (daily): copy new digests and filing text into the vault.

Writes to:
  inbox/intake/digests/<date>.md
  research/coverage/rail/<ticker>-filings/intake/<rel-path>.md

Never overwrites: new name with -1, -2, ... suffix if a file exists.

Run from the host that has both the Railway volume mounted (or synced locally)
and the vault directory accessible. Typically invoked via `scripts/local/vault_sync.sh`
after the server exports artifacts, or by a daily cron on the vault host.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger("intake.vault_sync")

DATA_DIR = Path(os.environ.get("INTAKE_DATA_DIR", "/data"))
VAULT_ROOT = Path(os.environ.get("BMG_VAULT_ROOT", str(Path.home() / "Documents" / "BMG-Capital-Vault")))


def _non_colliding(dest: Path) -> Path:
    if not dest.exists():
        return dest
    stem = dest.stem
    suf = dest.suffix
    for i in range(1, 100):
        candidate = dest.with_name(f"{stem}-{i}{suf}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"no slot for {dest}")


def sync() -> dict:
    counts = {"digests": 0, "filings": 0, "errors": []}
    # Digests -> inbox/intake/digests/
    src_digests = DATA_DIR / "digests"
    dst_digests = VAULT_ROOT / "inbox" / "intake" / "digests"
    dst_digests.mkdir(parents=True, exist_ok=True)
    if src_digests.exists():
        for p in src_digests.glob("*.md"):
            dst = _non_colliding(dst_digests / p.name)
            if not dst.exists():
                try:
                    shutil.copy2(p, dst)
                    counts["digests"] += 1
                except Exception as e:
                    counts["errors"].append(f"{p}: {e}")

    # Filing text -> research/coverage/rail/<ticker>-filings/intake/
    text_root = DATA_DIR / "text"
    if text_root.exists():
        for p in text_root.rglob("*.md"):
            parts = p.relative_to(text_root).parts
            if not parts or parts[0] == "MACRO":
                continue
            ticker = parts[0].lower()
            dst_dir = VAULT_ROOT / "research" / "coverage" / "rail" / f"{ticker}-filings" / "intake"
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = _non_colliding(dst_dir / p.name)
            if not dst.exists():
                try:
                    shutil.copy2(p, dst)
                    counts["filings"] += 1
                except Exception as e:
                    counts["errors"].append(f"{p}: {e}")
    return counts


if __name__ == "__main__":
    import json
    print(json.dumps(sync(), indent=2))
