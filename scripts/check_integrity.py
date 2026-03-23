#!/usr/bin/env python3
"""Run DB integrity check and optional auto-recovery."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.config import load_config
from archivist_mcp.reliability.recovery import recover_database_on_startup, verify_integrity


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--snapshot-dir")
    parser.add_argument("--auto-restore", action="store_true")
    args = parser.parse_args()

    config = load_config()
    db = Path(args.db)
    snap_dir = Path(args.snapshot_dir or config.reliability.snapshot_dir)
    auto_restore = args.auto_restore or config.reliability.auto_restore_on_corruption

    ok, msg = verify_integrity(db, encryption_key=config.security.encryption_key)
    print(f"integrity_before={ok} detail={msg}")
    if not ok and auto_restore:
        recover_database_on_startup(
            db,
            snap_dir,
            True,
            encryption_key=config.security.encryption_key,
        )
        ok2, msg2 = verify_integrity(db, encryption_key=config.security.encryption_key)
        print(f"integrity_after={ok2} detail={msg2}")
        if not ok2:
            raise SystemExit(2)
    elif not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
