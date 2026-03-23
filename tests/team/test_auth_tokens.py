from __future__ import annotations

import os
import unittest

from archivist_mcp.team.auth import load_token_map


class AuthTokenConfigTests(unittest.TestCase):
    def test_empty_env_returns_empty_map(self) -> None:
        old = os.environ.pop("ARCHIVIST_SSE_TOKENS", None)
        try:
            self.assertEqual(load_token_map(), {})
        finally:
            if old is not None:
                os.environ["ARCHIVIST_SSE_TOKENS"] = old

    def test_invalid_json_raises(self) -> None:
        old = os.environ.get("ARCHIVIST_SSE_TOKENS")
        os.environ["ARCHIVIST_SSE_TOKENS"] = "{bad-json"
        try:
            with self.assertRaises(ValueError):
                load_token_map()
        finally:
            if old is None:
                os.environ.pop("ARCHIVIST_SSE_TOKENS", None)
            else:
                os.environ["ARCHIVIST_SSE_TOKENS"] = old

    def test_invalid_entry_raises(self) -> None:
        old = os.environ.get("ARCHIVIST_SSE_TOKENS")
        os.environ["ARCHIVIST_SSE_TOKENS"] = '{"t1":{"user_id":"u1","role":"writer","projects":"p1"}}'
        try:
            with self.assertRaises(ValueError):
                load_token_map()
        finally:
            if old is None:
                os.environ.pop("ARCHIVIST_SSE_TOKENS", None)
            else:
                os.environ["ARCHIVIST_SSE_TOKENS"] = old


if __name__ == "__main__":
    unittest.main()
