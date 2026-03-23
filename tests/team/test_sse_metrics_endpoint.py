from __future__ import annotations

import unittest

from archivist_mcp.sse_server import EventBus, SseApp
from archivist_mcp.team.auth import AuthContext


class SseMetricsAuthTests(unittest.TestCase):
    def test_metrics_open_when_tokens_not_configured(self) -> None:
        app = SseApp(server=None, tokens={}, event_bus=EventBus())                          
        self.assertIsNone(app.metrics_error({}))

    def test_metrics_requires_maintainer_when_tokens_enabled(self) -> None:
        writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
        maint = AuthContext(user_id="m1", role="maintainer", project_ids=("p1",))
        app = SseApp(server=None, tokens={"writer-token": writer, "maint-token": maint}, event_bus=EventBus())                          

        self.assertEqual(app.metrics_error({}), "unauthorized")
        self.assertEqual(
            app.metrics_error({"Authorization": "Bearer writer-token"}),
            "forbidden",
        )
        self.assertIsNone(app.metrics_error({"Authorization": "Bearer maint-token"}))


if __name__ == "__main__":
    unittest.main()
