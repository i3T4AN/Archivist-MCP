"""Backup/restore and startup recovery helpers."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def verify_integrity(db_path: Path, *, encryption_key: str | None = None) -> tuple[bool, str]:
    """Run SQLite integrity checks on a DB file."""
    if not db_path.exists():
        return False, f"db does not exist: {db_path}"
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            if encryption_key:
                quoted = encryption_key.replace("'", "''")
                conn.execute(f"PRAGMA key = '{quoted}';")
            quick = conn.execute("PRAGMA quick_check;").fetchone()
            if not quick or quick[0] != "ok":
                return False, f"quick_check failed: {quick[0] if quick else 'missing result'}"
            full = conn.execute("PRAGMA integrity_check;").fetchone()
            if not full or full[0] != "ok":
                return False, f"integrity_check failed: {full[0] if full else 'missing result'}"
            return True, "ok"
        finally:
            conn.close()
    except sqlite3.DatabaseError as exc:
        return False, str(exc)


def create_snapshot(
    db_path: Path,
    snapshot_dir: Path,
    *,
    encryption_key: str | None = None,
) -> Path:
    """Create a consistent SQLite snapshot using the backup API."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"archivist-{_utc_stamp()}.db"

    src = sqlite3.connect(str(db_path))
    dst = sqlite3.connect(str(snapshot_path))
    try:
        if encryption_key:
            quoted = encryption_key.replace("'", "''")
            src.execute(f"PRAGMA key = '{quoted}';")
        src.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    ok, msg = verify_integrity(snapshot_path, encryption_key=encryption_key)
    if not ok:
        snapshot_path.unlink(missing_ok=True)
        raise RuntimeError(f"snapshot integrity failed: {msg}")
    return snapshot_path


def latest_snapshot(snapshot_dir: Path) -> Path | None:
    """Get most recent snapshot path from snapshot directory."""
    if not snapshot_dir.exists():
        return None
    snaps = sorted(p for p in snapshot_dir.glob("*.db") if p.is_file())
    return snaps[-1] if snaps else None


def restore_snapshot(
    snapshot_path: Path,
    target_db_path: Path,
    *,
    encryption_key: str | None = None,
) -> Path | None:
    """Restore a snapshot onto target DB after integrity verification.

    Returns path to pre-restore backup if one was created.
    """
    if not snapshot_path.exists():
        raise FileNotFoundError(f"snapshot missing: {snapshot_path}")

    ok, msg = verify_integrity(snapshot_path, encryption_key=encryption_key)
    if not ok:
        raise RuntimeError(f"snapshot failed integrity check: {msg}")

    target_db_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path = target_db_path.with_name(f"{target_db_path.stem}.pre_restore.{_utc_stamp()}.bak")
    if target_db_path.exists():
        shutil.copy2(target_db_path, backup_path)

    shutil.copy2(snapshot_path, target_db_path)
    ok2, msg2 = verify_integrity(target_db_path, encryption_key=encryption_key)
    if not ok2:
        raise RuntimeError(f"restored target failed integrity check: {msg2}")
    return backup_path if backup_path.exists() else None


def recover_database_on_startup(
    db_path: Path,
    snapshot_dir: Path,
    auto_restore: bool,
    *,
    encryption_key: str | None = None,
) -> None:
    """Validate DB before startup and optionally auto-restore from last good snapshot."""
    ok, msg = verify_integrity(db_path, encryption_key=encryption_key)
    if ok:
        return
    if not auto_restore:
        raise RuntimeError(f"database integrity check failed: {msg}")

    snap = latest_snapshot(snapshot_dir)
    if snap is None:
        raise RuntimeError(
            f"database integrity check failed ({msg}) and no snapshots found in {snapshot_dir}"
        )
    restore_snapshot(snap, db_path, encryption_key=encryption_key)
