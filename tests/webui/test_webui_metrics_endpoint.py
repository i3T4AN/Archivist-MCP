from __future__ import annotations

import unittest

from archivist_mcp.team.auth import AuthContext
from archivist_mcp.webui_server import WebUiApp


class WebUiMetricsAuthTests(unittest.TestCase):
    def test_metrics_open_when_not_in_team_mode(self) -> None:
        app = WebUiApp(server=None, conn=None, tokens={}, team_mode=False)                          
        self.assertIsNone(app.metrics_error({}))

    def test_metrics_requires_maintainer_in_team_mode(self) -> None:
        writer = AuthContext(user_id="u1", role="writer", project_ids=("p1",))
        maint = AuthContext(user_id="m1", role="maintainer", project_ids=("p1",))
        app = WebUiApp(                          
            server=None,
            conn=None,
            tokens={"writer-token": writer, "maint-token": maint},
            team_mode=True,
        )
        self.assertEqual(app.metrics_error({}), "unauthorized")
        self.assertEqual(
            app.metrics_error({"Authorization": "Bearer writer-token"}),
            "forbidden",
        )
        self.assertIsNone(app.metrics_error({"Authorization": "Bearer maint-token"}))


if __name__ == "__main__":
    unittest.main()
