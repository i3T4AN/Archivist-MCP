from __future__ import annotations

import unittest

from archivist_mcp.sse_server import EventBus, SseApp
from archivist_mcp.team.auth import AuthContext


class SseTransportTests(unittest.TestCase):
    def test_event_bus_publish_subscribe(self) -> None:
        bus = EventBus()
        q = bus.subscribe()
        bus.publish({"event": "conflict", "project_id": "p1", "message": "x"})
        evt = q.get(timeout=1)
        self.assertEqual(evt["event"], "conflict")
        self.assertEqual(evt["project_id"], "p1")
        bus.unsubscribe(q)

    def test_auth_token_resolution(self) -> None:
        tokens = {
            "token-1": AuthContext(user_id="u1", role="writer", project_ids=("p1",)),
        }
        app = SseApp(server=None, tokens=tokens, event_bus=EventBus())                          
        ctx = app.auth({"Authorization": "Bearer token-1"})
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx.user_id, "u1")
        self.assertIsNone(app.auth({"Authorization": "Bearer bad-token"}))


if __name__ == "__main__":
    unittest.main()
