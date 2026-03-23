from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from archivist_mcp.db import connect


class DbEncryptionBehaviorTests(unittest.TestCase):
    def test_encryption_key_without_sqlcipher_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "enc.db"
            with self.assertRaises(RuntimeError):
                connect(str(db_path), encryption_key="test-key")


if __name__ == "__main__":
    unittest.main()
