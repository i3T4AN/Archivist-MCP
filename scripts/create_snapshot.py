#!/usr/bin/env python3
"""Create a consistent Archivist DB snapshot."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from archivist_mcp.config import load_config
from archivist_mcp.reliability.recovery import create_snapshot


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=".archivist/archivist.db")
    parser.add_argument("--snapshot-dir")
    args = parser.parse_args()

    config = load_config()
    snapshot_dir = Path(args.snapshot_dir or config.reliability.snapshot_dir)
    snapshot = create_snapshot(
        Path(args.db),
        snapshot_dir,
        encryption_key=config.security.encryption_key,
    )
    print(f"Snapshot created: {snapshot}")


if __name__ == "__main__":
    main()
