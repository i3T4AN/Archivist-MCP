#!/usr/bin/env python3
"""Restore Archivist DB from a verified snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.config import load_config
from archivist_mcp.reliability.recovery import restore_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--db", default=".archivist/archivist.db")
    args = parser.parse_args()

    config = load_config()
    backup = restore_snapshot(
        Path(args.snapshot),
        Path(args.db),
        encryption_key=config.security.encryption_key,
    )
    print(f"Restore complete: {args.db}")
    if backup:
        print(f"Pre-restore backup: {backup}")


if __name__ == "__main__":
    main()
