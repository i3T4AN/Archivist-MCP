from __future__ import annotations

import unittest

from archivist_mcp.observability.alerts import AlertConfig, AlertPipeline


class AlertPipelineTests(unittest.TestCase):
    def test_no_alert_when_disabled(self) -> None:
        pipeline = AlertPipeline(AlertConfig(enabled=False, min_calls=2, error_rate_threshold=0.5, cooldown_seconds=10))
        self.assertIsNone(pipeline.record(error=True, now=10.0))
        self.assertIsNone(pipeline.record(error=True, now=11.0))

    def test_alert_triggers_threshold_and_respects_cooldown(self) -> None:
        pipeline = AlertPipeline(AlertConfig(enabled=True, min_calls=3, error_rate_threshold=0.5, cooldown_seconds=10))
        self.assertIsNone(pipeline.record(error=True, now=10.0))
        self.assertIsNone(pipeline.record(error=False, now=11.0))
        alert = pipeline.record(error=True, now=12.0)
        self.assertIsNotNone(alert)
        assert alert is not None
        self.assertEqual(alert["window_calls"], 3)
        self.assertEqual(alert["window_errors"], 2)
        self.assertAlmostEqual(alert["error_rate"], 2 / 3, places=6)

                                              
        self.assertIsNone(pipeline.record(error=True, now=13.0))
        self.assertIsNone(pipeline.record(error=True, now=20.0))
        next_alert = pipeline.record(error=True, now=23.0)
        self.assertIsNotNone(next_alert)


if __name__ == "__main__":
    unittest.main()
