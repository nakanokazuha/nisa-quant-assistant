from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nisa_quant.feature_engineering import build_features
from nisa_quant.historical_market_data import (
    HttpResponse,
    MarketBar,
    UniverseMember,
    _content_hash,
    _parse_yahoo,
    build_history_snapshot,
    fetch_history_snapshot,
    load_history_snapshot,
)
from nisa_quant.phase3_producer import refresh_phase3


def _request_contract(start: date, end: date) -> str:
    return json.dumps({
        "schema": "phase3-market-request", "schema_version": 1,
        "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
        "interval": "1d", "events": "div,splits", "return_basis": "price_return",
        "timeout": 15, "retries": 2,
        "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))


def _bar(ticker: str, observation_date: str, *, volume: float = 1000.0) -> MarketBar:
    return MarketBar(
        ticker=ticker, observation_date=observation_date,
        open=100.0, high=101.0, low=99.0, close=100.0, volume=volume,
        adjusted_close=100.0, retrieved_at="2026-09-16T00:00:00+00:00",
        source="fixture", citation="fixture",
    )


def _yahoo_payload(timestamps: list[int]) -> dict[str, object]:
    values = [100.0] * len(timestamps)
    quote = {name: values for name in ("open", "high", "low", "close", "volume")}
    return {"chart": {"result": [{
        "meta": {"symbol": "AAA"}, "timestamp": timestamps,
        "indicators": {"quote": [quote], "adjclose": [{"adjclose": values}]},
        "events": {"div": {}, "splits": {}},
    }]}}


class SolR142MarketBoundaryTests(unittest.TestCase):
    def test_rehashed_snapshot_with_invalid_ohlc_cannot_replay_or_form_panel_rows(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 31)
        contract = _request_contract(start, end)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start.isoformat())], "^GSPC": [_bar("^GSPC", start.isoformat())]},
            benchmark_ticker="^GSPC",
            universe=[UniverseMember("AAA", None, None, "active", "point_in_time", "none", "fixture", "v1")],
            created_at="2026-09-16T00:00:00+00:00", request_contract=contract,
        )
        invalid_bar = replace(snapshot.bars_by_ticker["AAA"][0], high=98.0)
        forged = replace(
            snapshot,
            bars_by_ticker={**snapshot.bars_by_ticker, "AAA": [invalid_bar]},
            snapshot_id="",
        )
        forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
            path.write_text(json.dumps(asdict(forged), sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "OHLC|high|invalid|market bar"):
                load_history_snapshot(path, expected_request_contract=contract)

    def test_distinct_intraday_epochs_normalizing_to_one_date_are_rejected(self) -> None:
        first = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        second = first + 12 * 60 * 60
        with self.assertRaisesRegex(ValueError, "duplicate|date|observation"):
            _parse_yahoo(
                "AAA", _yahoo_payload([first, second]),
                "2026-09-16T00:00:00+00:00", "https://example.invalid",
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
            )

    def test_snapshot_build_omits_duplicate_dates_after_iso_normalization_with_evidence(self) -> None:
        start = date(2026, 1, 1)
        end = date(2026, 1, 31)
        contract = _request_contract(start, end)
        snapshot = build_history_snapshot(
            {
                "AAA": [_bar("AAA", "2026-01-01"), _bar("AAA", "20260101")],
                "^GSPC": [_bar("^GSPC", "2026-01-01")],
            },
            benchmark_ticker="^GSPC",
            universe=[UniverseMember("AAA", None, None, "active", "point_in_time", "none", "fixture", "v1")],
            created_at="2026-09-16T00:00:00+00:00", request_contract=contract,
        )

        self.assertEqual([bar.observation_date for bar in snapshot.bars_by_ticker["AAA"]], ["2026-01-01"])
        self.assertEqual(snapshot.coverage["AAA"]["invalid_observation_count"], 1)
        self.assertIn("duplicate_observation_date", snapshot.coverage["AAA"]["invalid_observations"][0]["invalid_fields"])
        self.assertIn("AAA", snapshot.ticker_failures)

    def test_malformed_provider_bar_retains_invalid_observation_evidence(self) -> None:
        payload = _yahoo_payload([int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())])
        payload["chart"]["result"][0]["indicators"]["quote"][0]["high"] = [0.0]  # type: ignore[index]

        def http_get(_url: str, **_: object) -> HttpResponse:
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            snapshot = fetch_history_snapshot(
                tickers=["AAA"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-01",
                cache_dir=Path(directory), retrieved_at="2026-01-02T00:00:00+00:00",
                http_get=http_get, allow_partial=True,
            )

        self.assertEqual(snapshot.coverage["AAA"]["invalid_observation_count"], 1)
        self.assertIn("invalid_observation", snapshot.coverage["AAA"]["invalid_observations"][0]["invalid_fields"])

    def test_malformed_split_zero_denominator_is_a_controlled_value_error(self) -> None:
        payload = _yahoo_payload([int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())])
        payload["chart"]["result"][0]["events"] = {"div": {}, "splits": {"1767225600": {"numerator": 1, "denominator": "0"}}}  # type: ignore[index]

        with self.assertRaisesRegex(ValueError, "split|event|invalid"):
            _parse_yahoo(
                "AAA", payload,
                "2026-09-16T00:00:00+00:00", "https://example.invalid",
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
            )


class SolR142ZeroVolumeTests(unittest.TestCase):
    def test_zero_volume_is_a_valid_neutral_feature_series(self) -> None:
        bars = [
            _bar("AAA", "2026-01-01", volume=0.0),
            _bar("AAA", "2026-01-02", volume=0.0),
        ]
        result = build_features("AAA", bars, decision_date="2026-01-02")

        self.assertEqual(result.values["dollar_volume"], 0.0)
        self.assertEqual(result.values["volume_trend"], 0.0)


class SolR142ProducerFailureTests(unittest.TestCase):
    def test_zero_division_provider_failure_is_structured_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=[],
        ), patch(
            "nisa_quant.phase3_producer.fetch_history_snapshot",
            side_effect=ZeroDivisionError("malformed zero-volume provider data"),
        ):
            output = Path(directory) / "report.json"
            status = refresh_phase3(
                as_of="2026-09-16", start="2026-01-01", end="2026-09-16",
                cache_dir=Path(directory) / "cache", output=output,
                live=True, replay_only=False, limit=3,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(manifest["report_status"], "unavailable")
        self.assertTrue(manifest["failures"])


if __name__ == "__main__":
    unittest.main()
