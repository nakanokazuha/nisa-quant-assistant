from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nisa_quant.historical_market_data import (
    MarketBar,
    UniverseMember,
    _content_hash,
    build_history_snapshot,
    load_history_snapshot,
)
from nisa_quant.phase3_producer import refresh_phase3


def _contract(asset_tickers: list[str]) -> str:
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)

    def epoch(value: date) -> int:
        return int(datetime.combine(value, datetime.min.time(), timezone.utc).timestamp())

    return json.dumps({
        "schema": "phase3-market-request",
        "schema_version": 1,
        "asset_tickers": asset_tickers,
        "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "period1": epoch(start),
        "period2": epoch(date(2026, 2, 1)),
        "interval": "1d",
        "events": "div,splits",
        "return_basis": "price_return",
        "timeout": 15,
        "retries": 2,
        "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))


def _member(ticker: str, *, current_only: bool = False) -> UniverseMember:
    return UniverseMember(
        ticker=ticker,
        effective_from=None,
        effective_to=None,
        membership_status="active",
        lookahead_bias_status="current_snapshot_only" if current_only else "point_in_time",
        survivorship_bias_status="survivorship_risk_disclosed" if current_only else "none",
        source="fixture",
        source_version="v1",
    )


def _bar(ticker: str, day: date, index: int = 0) -> MarketBar:
    value = 100.0 + index
    return MarketBar(
        ticker=ticker,
        observation_date=day.isoformat(),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1000.0,
        retrieved_at="2026-09-16T00:00:00+00:00",
        source="fixture",
        citation="fixture",
    )


def _short_snapshot(*, current_only: bool = False):
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(800)]
    members = [_member(ticker, current_only=current_only) for ticker in ("AAA", "BBB", "CCC", "DDD", "FAIL")]
    bars = {
        ticker: [_bar(ticker, day, index) for index, day in enumerate(days)]
        for ticker in ("AAA", "BBB", "CCC", "DDD", "^GSPC")
    }
    snapshot = build_history_snapshot(
        bars | {"FAIL": []},
        benchmark_ticker="^GSPC",
        universe=members,
        created_at="2026-09-16T00:00:00+00:00",
        ticker_failures={"FAIL": "ProviderUnavailable: no market bars"},
    )
    return snapshot, members


class HistoryUniverseBindingR127Tests(unittest.TestCase):
    def test_build_rejects_missing_extra_and_mismatched_universe_members(self) -> None:
        bars = {"AAA": [_bar("AAA", date(2026, 1, 15))], "^GSPC": [_bar("^GSPC", date(2026, 1, 15))]}
        contract = _contract(["AAA"])
        cases = (
            ("missing", []),
            ("extra", [_member("AAA"), _member("BBB")]),
            ("mismatched", [_member("BBB")]),
        )
        for label, universe in cases:
            with self.subTest(label=label), self.assertRaisesRegex(ValueError, "universe|roles"):
                build_history_snapshot(
                    bars,
                    benchmark_ticker="^GSPC",
                    universe=universe,
                    created_at="2026-09-16T00:00:00+00:00",
                    request_contract=contract,
                )

    def test_rehashed_snapshot_rejects_universe_membership_mismatch(self) -> None:
        contract = _contract(["AAA"])
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", date(2026, 1, 15))], "^GSPC": [_bar("^GSPC", date(2026, 1, 15))]},
            benchmark_ticker="^GSPC",
            universe=[_member("AAA")],
            created_at="2026-09-16T00:00:00+00:00",
            request_contract=contract,
        )
        for label, universe in (
            ("missing", []),
            ("extra", [_member("AAA"), _member("BBB")]),
            ("mismatched", [_member("BBB")]),
        ):
            forged = replace(snapshot, universe=universe, snapshot_id="")
            forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / f"snapshot-{label}.json"
                path.write_text(json.dumps(asdict(forged), sort_keys=True), encoding="utf-8")
                with self.subTest(label=label), self.assertRaisesRegex(ValueError, "universe|roles"):
                    load_history_snapshot(path, expected_request_contract=contract)


class RefreshManifestR127Tests(unittest.TestCase):
    def test_markdown_manifest_preserves_insufficient_status_and_exit_two(self) -> None:
        snapshot, members = _short_snapshot(current_only=True)
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=members,
        ), patch(
            "nisa_quant.phase3_producer.fetch_history_snapshot", return_value=snapshot,
        ):
            root = Path(directory)
            output = root / "report.md"
            status = refresh_phase3(
                as_of="2026-01-15",
                start="2024-01-01",
                end="2026-03-10",
                cache_dir=root / "cache",
                output=output,
                live=True,
                replay_only=False,
            )
            report_text = output.read_text(encoding="utf-8")
            manifest = json.loads((root / "report.md.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertIn("Status", report_text)
        self.assertEqual(manifest["report_status"], "unavailable_insufficient_data")

    def test_manifest_gaps_are_deterministic_and_keep_failures_and_panel_exclusions_separate(self) -> None:
        snapshot, members = _short_snapshot()
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=members,
        ), patch(
            "nisa_quant.phase3_producer.fetch_history_snapshot", return_value=snapshot,
        ):
            root = Path(directory)
            output = root / "report.json"
            refresh_phase3(
                as_of="2026-01-15",
                start="2024-01-01",
                end="2026-03-10",
                cache_dir=root / "cache",
                output=output,
                live=True,
                replay_only=False,
            )
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(list(manifest["gaps"]), sorted(manifest["gaps"]))
        self.assertEqual(manifest["gaps"].get("AAA", {}).get("status"), "covered")
        self.assertEqual(manifest["gaps"].get("AAA", {}).get("gap_count"), 0)
        self.assertEqual(manifest["gaps"].get("FAIL", {}).get("status"), "failed")
        self.assertEqual(manifest["gaps"].get("FAIL", {}).get("row_count"), 0)
        self.assertEqual(manifest["failures_by_ticker"], {"FAIL": "ProviderUnavailable: no market bars"})
        self.assertIn("FAIL", manifest["panel_excluded_by_ticker"])

    def test_coverage_block_keeps_zero_history_tickers_in_panel_exclusions(self) -> None:
        snapshot, members = _short_snapshot()
        blocked_snapshot = build_history_snapshot(
            {**snapshot.bars_by_ticker, "DDD": []},
            benchmark_ticker=snapshot.benchmark_ticker,
            universe=members,
            created_at=snapshot.created_at,
            ticker_failures=snapshot.ticker_failures,
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=members,
        ), patch(
            "nisa_quant.phase3_producer.fetch_history_snapshot", return_value=blocked_snapshot,
        ):
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-01-15",
                start="2024-01-01",
                end="2026-03-10",
                cache_dir=root / "cache",
                output=output,
                live=True,
                replay_only=False,
            )
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(manifest["panel_excluded_by_ticker"].get("DDD", {}).get("status"), "no_usable_market_history")
        self.assertEqual(manifest["panel_excluded_by_ticker"].get("FAIL", {}).get("status"), "no_usable_market_history")


if __name__ == "__main__":
    unittest.main()
