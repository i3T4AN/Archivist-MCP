from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect
from archivist_mcp.migrations.runner import run_migrations
from archivist_mcp.reliability.recovery import (
    create_snapshot,
    recover_database_on_startup,
    restore_snapshot,
    verify_integrity,
)


class RecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "archivist.db"
        conn = connect(str(self.db))
        run_migrations(conn, Path("archivist_mcp/migrations/sql"))
        conn.execute("INSERT INTO projects(project_id, name) VALUES ('p1', 'Project One')")
        conn.execute("INSERT INTO users(user_id, display_name) VALUES ('u1', 'User One')")
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_snapshot_and_restore_roundtrip(self) -> None:
        conn = connect(str(self.db))
        conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('n1','p1','Entity','A','B','active','u1')"
        )
        conn.commit()
        conn.close()

        snap_dir = Path(self.tmp.name) / "snaps"
        snapshot = create_snapshot(self.db, snap_dir)

        restored = Path(self.tmp.name) / "restored.db"
        restore_snapshot(snapshot, restored)
        conn2 = connect(str(restored))
        count = conn2.execute("SELECT COUNT(*) FROM nodes WHERE project_id='p1'").fetchone()[0]
        conn2.close()
        self.assertEqual(count, 1)

    def test_crash_simulation_rolls_back_transaction(self) -> None:
        conn = connect(str(self.db))
        conn.isolation_level = None
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO nodes(node_id, project_id, type, title, content, state, created_by) VALUES ('n-crash','p1','Entity','A','B','active','u1')"
        )
        conn.execute("ROLLBACK")
        count = conn.execute("SELECT COUNT(*) FROM nodes WHERE node_id='n-crash'").fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_startup_recovery_from_bad_db_uses_snapshot(self) -> None:
        snap_dir = Path(self.tmp.name) / "snaps"
        snapshot = create_snapshot(self.db, snap_dir)
        self.assertTrue(snapshot.exists())

                                       
        self.db.write_bytes(b"not-a-sqlite-db")
        ok, _ = verify_integrity(self.db)
        self.assertFalse(ok)

        recover_database_on_startup(self.db, snap_dir, auto_restore=True)
        ok2, msg2 = verify_integrity(self.db)
        self.assertTrue(ok2, msg2)


if __name__ == "__main__":
    unittest.main()
