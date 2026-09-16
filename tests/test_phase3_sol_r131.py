from __future__ import annotations

import json
import math
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from nisa_quant.feature_engineering import FEATURE_SCHEMA, build_features
from nisa_quant.historical_market_data import (
    HttpResponse,
    MarketBar,
    UniverseMember,
    _parse_yahoo,
    build_history_snapshot,
    fetch_history_snapshot,
)
from nisa_quant.ranking_model import SpecializedRankingModel, rank_current_candidates
from nisa_quant.return_targets import calculate_forward_targets
from nisa_quant.training_dataset import (
    PanelRow,
    TrainingDataset,
    _identity,
    build_monthly_panel,
    save_training_dataset,
)


def _bar(ticker: str, day: str, value: float = 100.0, *, open_value: float | None = None) -> MarketBar:
    opening = value if open_value is None else open_value
    return MarketBar(
        ticker, day, opening, value, value, value, 1000.0, value,
        retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r131",
    )


def _member(ticker: str) -> UniverseMember:
    return UniverseMember(ticker, None, None, "active", "point_in_time", "none", "fixture", "v1")


def _complete_targets(decision: str) -> dict[str, object]:
    start = date.fromisoformat(decision)
    targets: dict[str, object] = {"target_return_basis": "price_return"}
    for horizon, days in (("1m", 31), ("3m", 90), ("6m", 180), ("12m", 365)):
        endpoint = (start + timedelta(days=days)).isoformat()
        prefix = f"target_{horizon}_"
        targets.update({
            f"{prefix}interval_start": decision,
            f"{prefix}interval_end": endpoint,
            f"{prefix}interval_semantics": "[start,end)",
            f"{prefix}forward_endpoint": endpoint,
            f"{prefix}return_basis": "price_return",
            f"{prefix}endpoint_available": True,
            f"{prefix}label_availability": "available_at_endpoint",
            f"{prefix}asset_observation_date": endpoint,
            f"{prefix}benchmark_observation_date": endpoint,
            f"{prefix}return": 0.10,
            f"{prefix}total_return": 0.10,
            f"{prefix}benchmark_return": 0.04,
            f"{prefix}excess_return": 0.06,
        })
    return targets


def _dataset(row: PanelRow) -> TrainingDataset:
    history_id = "history-sol-r131"
    dataset_id = _identity([row], history_id, "^GSPC", FEATURE_SCHEMA, "price_return")
    return TrainingDataset(
        rows=[row], dataset_id=dataset_id, history_snapshot_id=history_id,
        benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
    )


def _yahoo_payload(*, symbol: object = "AAA", timestamps: list[object] | None = None) -> dict[str, object]:
    stamps = timestamps or [1767225600]
    values = [100.0] * len(stamps)
    return {
        "chart": {"result": [{
            "meta": {"symbol": symbol},
            "timestamp": stamps,
            "indicators": {
                "quote": [{name: values for name in ("open", "high", "low", "close", "volume")}],
                "adjclose": [{"adjclose": values}],
            },
            "events": {"div": {}, "splits": {}},
        }]},
    }


class DatasetTargetIntegrityR131Tests(unittest.TestCase):
    def test_rehashed_dataset_rejects_forged_excess_return(self) -> None:
        row = PanelRow("AAA", "2026-01-01", {name: 0.0 for name in FEATURE_SCHEMA}, _complete_targets("2026-01-01"), {})
        forged_targets = dict(row.targets)
        forged_targets["target_3m_excess_return"] = 0.99
        forged = replace(row, targets=forged_targets)
        dataset = _dataset(forged)

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "excess|target|arithmetic"):
                save_training_dataset(dataset, Path(directory) / "dataset.json")

    def test_rehashed_dataset_rejects_inconsistent_target_metadata(self) -> None:
        row = PanelRow("AAA", "2026-01-01", {name: 0.0 for name in FEATURE_SCHEMA}, _complete_targets("2026-01-01"), {})
        forged_targets = dict(row.targets)
        forged_targets["target_6m_interval_end"] = "2026-07-02"
        dataset = _dataset(replace(row, targets=forged_targets))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "interval|endpoint|target"):
                save_training_dataset(dataset, Path(directory) / "dataset.json")

    def test_rehashed_dataset_rejects_false_after_endpoint_status(self) -> None:
        targets = _complete_targets("2026-01-01")
        targets["target_3m_label_availability"] = "available_after_endpoint"
        targets["target_3m_endpoint_available"] = False
        dataset = _dataset(PanelRow(
            "AAA", "2026-01-01", {name: 0.0 for name in FEATURE_SCHEMA}, targets, {},
        ))

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "availability|observation|endpoint"):
                save_training_dataset(dataset, Path(directory) / "dataset.json")

    def test_explicit_unavailable_target_remains_serializable(self) -> None:
        targets = _complete_targets("2026-01-01")
        for horizon in ("1m", "3m", "6m", "12m"):
            prefix = f"target_{horizon}_"
            targets.update({
                f"{prefix}endpoint_available": False,
                f"{prefix}label_availability": "unavailable_endpoint_not_observable",
                f"{prefix}asset_observation_date": None,
                f"{prefix}benchmark_observation_date": None,
                f"{prefix}return": None,
                f"{prefix}total_return": None,
                f"{prefix}benchmark_return": None,
                f"{prefix}excess_return": None,
            })
        row = PanelRow("AAA", "2026-01-01", {name: 0.0 for name in FEATURE_SCHEMA}, targets, {})
        dataset = _dataset(row)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            save_training_dataset(dataset, path)
            self.assertTrue(path.exists())


class MarketValidityR131Tests(unittest.TestCase):
    def test_each_non_positive_ohlc_field_is_excluded_from_panel(self) -> None:
        for field_name in ("open", "high", "low", "close"):
            for value in (0.0, -1.0):
                with self.subTest(field_name=field_name, value=value):
                    invalid = replace(_bar("AAA", "2026-01-01"), **{field_name: value})
                    snapshot = build_history_snapshot(
                        {"AAA": [invalid], "^GSPC": [_bar("^GSPC", "2026-01-01")]},
                        benchmark_ticker="^GSPC", universe=[_member("AAA")],
                        created_at="2026-09-16T00:00:00+00:00",
                    )
                    self.assertEqual(build_monthly_panel(snapshot, decision_dates=["2026-01-01"]).rows, [])

    def test_invalid_ohlc_row_is_excluded_from_features_targets_and_panel(self) -> None:
        asset = [_bar("AAA", "2026-01-01", open_value=0.0), _bar("AAA", "2026-04-01")]
        benchmark = [_bar("^GSPC", "2026-01-01"), _bar("^GSPC", "2026-04-01", 105.0)]

        features = build_features("AAA", asset, decision_date="2026-01-01", benchmark_bars=benchmark)
        targets = calculate_forward_targets({"AAA": asset}, benchmark, "2026-01-01")
        snapshot = build_history_snapshot(
            {"AAA": asset, "^GSPC": benchmark}, benchmark_ticker="^GSPC",
            universe=[_member("AAA")], created_at="2026-09-16T00:00:00+00:00",
        )
        dataset = build_monthly_panel(snapshot, decision_dates=["2026-01-01"])

        self.assertIsNone(targets["target_3m_return"])
        self.assertIsNone(features.values["dollar_volume"])
        self.assertEqual(dataset.rows, [])

    def test_invalid_market_history_cannot_be_current_prediction(self) -> None:
        asset = [_bar("AAA", "2026-01-01", open_value=0.0)]
        training = [_bar("TRAIN", "2024-01-01"), _bar("TRAIN", "2024-03-31"), _bar("TRAIN", "2026-01-01")]
        benchmark = [_bar("^GSPC", "2024-01-01"), _bar("^GSPC", "2024-03-31"), _bar("^GSPC", "2026-01-01")]
        snapshot = build_history_snapshot(
            {"AAA": asset, "TRAIN": training, "^GSPC": benchmark}, benchmark_ticker="^GSPC",
            universe=[_member("AAA"), _member("TRAIN")], created_at="2026-09-16T00:00:00+00:00",
        )
        dataset = build_monthly_panel(snapshot, decision_dates=["2024-01-01", "2026-01-01"])
        artifact = SpecializedRankingModel().fit(
            dataset.rows, training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker="^GSPC",
        )

        self.assertEqual([item["ticker"] for item in rank_current_candidates(dataset, artifact, as_of="2026-01-15")], ["TRAIN"])


class YahooBoundaryR131Tests(unittest.TestCase):
    def test_yahoo_requires_non_empty_matching_response_identity(self) -> None:
        for symbol in (None, "", "BBB", 123):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(ValueError, "identity|symbol"):
                _parse_yahoo(
                    "AAA", _yahoo_payload(symbol=symbol), "2026-09-16T00:00:00+00:00", "https://example.invalid",
                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                )

    def test_yahoo_rejects_non_integer_or_non_finite_epoch_timestamps(self) -> None:
        for timestamp in (1767225600.5, math.nan, math.inf, -math.inf, 10**30):
            with self.subTest(timestamp=timestamp), self.assertRaisesRegex(ValueError, "timestamp|epoch"):
                _parse_yahoo(
                    "AAA", _yahoo_payload(timestamps=[timestamp]), "2026-09-16T00:00:00+00:00", "https://example.invalid",
                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                )

    def test_missing_identity_is_a_partial_failure_and_is_not_cached(self) -> None:
        def http_get(_url: str, **_: object) -> HttpResponse:
            payload = _yahoo_payload()
            payload["chart"]["result"][0]["meta"] = {}  # type: ignore[index]
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            snapshot = fetch_history_snapshot(
                tickers=["AAA"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-01",
                cache_dir=Path(directory), http_get=http_get, allow_partial=True,
            )
            self.assertEqual(set(snapshot.ticker_failures), {"AAA", "^GSPC"})
            self.assertEqual(list(Path(directory).glob("history-*.json")), [])


if __name__ == "__main__":
    unittest.main()
