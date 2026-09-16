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
    save_history_snapshot,
)
from nisa_quant.training_dataset import build_monthly_panel
from nisa_quant.phase3_producer import refresh_phase3


def _contract() -> str:
    start = date(2024, 1, 1)
    end = date(2026, 3, 31)
    return json.dumps({
        "schema": "phase3-market-request",
        "schema_version": 1,
        "asset_tickers": ["AAA"],
        "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
        "interval": "1d",
        "events": "div,splits",
        "return_basis": "price_return",
        "timeout": 15,
        "retries": 2,
        "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))


def _bar(ticker: str, day: date, *, retrieved_at: str = "2026-09-16T00:00:00+00:00") -> MarketBar:
    return MarketBar(
        ticker=ticker,
        observation_date=day.isoformat(),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000.0,
        retrieved_at=retrieved_at,
        source="fixture-market",
        citation="fixture://sol-r146",
    )


def _member(*, effective_from: str | None = "2024-01-01", effective_to: str | None = "2026-03-31", **changes: object) -> UniverseMember:
    values: dict[str, object] = {
        "ticker": "AAA",
        "effective_from": effective_from,
        "effective_to": effective_to,
        "membership_status": "active",
        "lookahead_bias_status": "point_in_time",
        "survivorship_bias_status": "none",
        "source": "fixture-universe",
        "source_version": "sol-r146",
        "source_symbol": "AAA",
    }
    values.update(changes)
    return UniverseMember(**values)


class SolR146MembershipClosureTests(unittest.TestCase):
    def test_rehashed_missing_effective_dates_are_descriptive_and_suppress_performance(self) -> None:
        start = date(2024, 1, 1)
        snapshot = build_history_snapshot(
            {
                "AAA": [_bar("AAA", start)],
                "^GSPC": [_bar("^GSPC", start)],
            },
            benchmark_ticker="^GSPC",
            universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00",
            request_contract=_contract(),
        )
        forged = replace(
            snapshot,
            universe=[_member(effective_from=None, effective_to=None)],
            snapshot_id="",
        )
        forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"history-{hashlib.sha256(snapshot.request_contract.encode()).hexdigest()[:24]}.json"
            path.write_text(json.dumps(asdict(forged), sort_keys=True), encoding="utf-8")
            loaded = load_history_snapshot(path, expected_request_contract=snapshot.request_contract)

        dataset = build_monthly_panel(loaded, as_of="2026-01-01", decision_dates=["2024-01-01"])
        self.assertEqual(dataset.membership_evidence_status, "descriptive_survivor_selected_evidence")

    def test_rehashed_arbitrary_membership_status_is_rejected(self) -> None:
        start = date(2024, 1, 1)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start)], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC", universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        forged = replace(
            snapshot,
            universe=[_member(membership_status="arbitrary")],
            snapshot_id="",
        )
        forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"history-{hashlib.sha256(snapshot.request_contract.encode()).hexdigest()[:24]}.json"
            path.write_text(json.dumps(asdict(forged), sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "membership_status|status|universe"):
                load_history_snapshot(path, expected_request_contract=snapshot.request_contract)

    def test_unclaimed_survivorship_is_descriptive_even_with_point_in_time_dates(self) -> None:
        start = date(2024, 1, 1)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start)], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC", universe=[_member(survivorship_bias_status="not_claimed")],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        dataset = build_monthly_panel(snapshot, as_of="2026-01-01", decision_dates=["2024-01-01"])
        self.assertEqual(dataset.membership_evidence_status, "descriptive_survivor_selected_evidence")

    def test_panel_rejects_snapshot_with_arbitrary_universe_status(self) -> None:
        start = date(2024, 1, 1)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start)], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC", universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        forged = replace(snapshot, universe=[_member(membership_status="arbitrary")])
        with self.assertRaisesRegex(ValueError, "membership_status|status|universe"):
            build_monthly_panel(forged, as_of="2026-01-01", decision_dates=["2024-01-01"])

    def test_manifest_does_not_advertise_partial_point_in_time_membership(self) -> None:
        start = date(2024, 1, 1)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start)], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC",
            universe=[_member(effective_from=None, effective_to=None)],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.phase3_producer.fetch_current_sp500_universe", return_value=snapshot.universe,
        ), patch(
            "nisa_quant.phase3_producer.fetch_history_snapshot", return_value=snapshot,
        ):
            root = Path(directory)
            output = root / "report.json"
            refresh_phase3(
                as_of="2026-01-02", start="2024-01-01", end="2026-03-31",
                cache_dir=root / "cache", output=output, live=True, replay_only=False, limit=1,
            )
            manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))
        self.assertFalse(manifest["point_in_time_membership"])


class SolR146MarketProvenanceClosureTests(unittest.TestCase):
    def test_provenance_free_bar_is_excluded_and_partial_snapshot_cannot_be_cached(self) -> None:
        start = date(2026, 1, 1)
        invalid = replace(_bar("AAA", start), retrieved_at="not-a-timestamp", source=" ", citation="")
        snapshot = build_history_snapshot(
            {"AAA": [invalid], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC", universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )

        self.assertEqual(snapshot.bars_by_ticker["AAA"], [])
        self.assertIn("AAA", snapshot.ticker_failures)
        self.assertEqual(snapshot.coverage["AAA"]["row_count"], 0)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "partial|replay"):
                save_history_snapshot(snapshot, Path(directory) / "history.json")
            self.assertEqual(list(Path(directory).glob("history*.json")), [])

    def test_rehashed_non_string_bar_provenance_is_rejected_without_coercion(self) -> None:
        start = date(2026, 1, 1)
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA", start)], "^GSPC": [_bar("^GSPC", start)]},
            benchmark_ticker="^GSPC", universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        forged_payload = asdict(replace(snapshot, snapshot_id=""))
        forged_payload["bars_by_ticker"]["AAA"][0]["source"] = 123
        forged_payload["bars_by_ticker"]["AAA"][0]["citation"] = None
        forged_body = json.dumps(
            {key: value for key, value in forged_payload.items() if key != "snapshot_id"},
            sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode()
        forged_payload["snapshot_id"] = f"phase3-{hashlib.sha256(forged_body).hexdigest()[:20]}"

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / f"history-{hashlib.sha256(snapshot.request_contract.encode()).hexdigest()[:24]}.json"
            path.write_text(json.dumps(forged_payload, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "source|citation|provenance|market bar"):
                load_history_snapshot(path, expected_request_contract=snapshot.request_contract)


if __name__ == "__main__":
    unittest.main()
