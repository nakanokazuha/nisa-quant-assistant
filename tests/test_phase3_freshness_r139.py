from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from nisa_quant.historical_market_data import MarketBar, UniverseMember, build_history_snapshot
from nisa_quant.phase3_producer import refresh_phase3


class Phase3FreshnessR139Tests(unittest.TestCase):
    def test_monthly_current_predictions_accept_latest_monthly_row_with_explicit_freshness_evidence(self) -> None:
        start = date(2023, 1, 1)
        days = [start + timedelta(days=index) for index in range((date(2026, 9, 15) - start).days + 1)]
        universe = [
            UniverseMember(
                ticker=ticker, effective_from=None, effective_to=None,
                membership_status="active", lookahead_bias_status="current_snapshot_only",
                survivorship_bias_status="survivorship_risk_disclosed", source="fixture", source_version="r139",
            )
            for ticker in ("AAA", "BBB")
        ]
        bars = {
            ticker: [
                MarketBar(
                    ticker=ticker, observation_date=day.isoformat(),
                    open=100.0 + index, high=101.0 + index, low=99.0 + index,
                    close=100.0 + index, volume=1000.0,
                    retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="r139",
                )
                for index, day in enumerate(days)
            ]
            for ticker in ("AAA", "BBB", "^GSPC")
        }
        snapshot = build_history_snapshot(
            bars, benchmark_ticker="^GSPC", universe=universe,
            created_at="2026-09-16T00:00:00+00:00",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch("nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=universe), patch(
                "nisa_quant.phase3_producer.fetch_history_snapshot", return_value=snapshot,
            ):
                status = refresh_phase3(
                    as_of="2026-09-16", start="2023-01-01", end="2026-09-15",
                    cache_dir=root / "cache", output=output,
                    live=True, replay_only=False, limit=2,
                )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertGreater(len(report["current_predictions"]), 0)
        self.assertGreater(len(manifest["current_prediction_freshness"]), 0)
        evidence = manifest["current_prediction_freshness"][0]
        self.assertEqual(evidence["decision_date"], "2026-09-01")
        self.assertEqual(evidence["status"], "fresh")
        self.assertEqual(evidence["threshold_days"], 31)
        self.assertEqual(evidence["freshness_policy"], "monthly_decision_date_31_calendar_day_allowance_v1")
        prediction_evidence = report["current_predictions"][0]["freshness_evidence"]
        self.assertEqual(prediction_evidence["status"], "fresh")
        self.assertEqual(prediction_evidence["threshold_days"], 31)
        self.assertEqual(prediction_evidence, evidence)


if __name__ == "__main__":
    unittest.main()
