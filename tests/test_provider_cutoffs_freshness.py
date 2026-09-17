from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import MarketBar, _parse_yahoo, build_history_snapshot, parse_sec_company_facts
from nisa_quant.ranking_model import SpecializedRankingModel, rank_current_candidates
from nisa_quant.return_targets import validate_target_contract
from nisa_quant.training_dataset import PanelRow, TrainingDataset, _identity, build_monthly_panel


def _target_row(decision: str, *, three_month_offset: int = 90) -> PanelRow:
    day = date.fromisoformat(decision)
    targets: dict[str, object] = {"target_return_basis": "price_return"}
    for horizon, days in (("1m", 31), ("3m", three_month_offset), ("6m", 180), ("12m", 365)):
        endpoint = day + timedelta(days=days)
        prefix = f"target_{horizon}_"
        targets.update({
            f"{prefix}interval_start": decision,
            f"{prefix}interval_end": endpoint.isoformat(),
            f"{prefix}interval_semantics": "[start,end)",
            f"{prefix}forward_endpoint": endpoint.isoformat(),
            f"{prefix}return_basis": "price_return",
            f"{prefix}endpoint_available": True,
            f"{prefix}label_availability": "available_at_endpoint",
            f"{prefix}asset_observation_date": endpoint.isoformat(),
            f"{prefix}benchmark_observation_date": endpoint.isoformat(),
            f"{prefix}return": 0.10,
            f"{prefix}benchmark_return": 0.04,
            f"{prefix}excess_return": 0.06,
        })
    return PanelRow(
        "AAA", decision, {name: 0.0 for name in FEATURE_SCHEMA}, targets, {},
    )


def _yahoo_payload(*, timestamps: list[int] | None = None, values: dict[str, list[float]] | None = None) -> dict[str, object]:
    stamps = timestamps or [1767225600]
    defaults = {name: [100.0] * len(stamps) for name in ("open", "high", "low", "close", "volume")}
    if values:
        defaults.update(values)
    return {"chart": {"result": [{
        "meta": {"symbol": "AAA"}, "timestamp": stamps,
        "indicators": {"quote": [defaults], "adjclose": [{"adjclose": [100.0] * len(stamps)}]},
        "events": {"div": {}, "splits": {}},
    }]}}


def _row(name: str, decision: str, *, staleness: float, observation_date: str | None) -> SimpleNamespace:
    endpoint = (date.fromisoformat(decision) + timedelta(days=90)).isoformat()
    metadata = {"market_observation_date": observation_date} if observation_date else {}
    return SimpleNamespace(
        ticker=name,
        decision_date=decision,
        features={**{feature: 0.0 for feature in FEATURE_SCHEMA}, "staleness": staleness},
        targets={
            "target_3m_forward_endpoint": endpoint,
            "target_3m_interval_end": endpoint,
            "target_3m_return_basis": "price_return",
            "target_3m_excess_return": 0.01,
            "target_3m_label_availability": "available_at_endpoint",
        },
        target_metadata=metadata,
    )


class SolR136HorizonTests(unittest.TestCase):
    def test_three_month_endpoint_must_be_exactly_documented_duration(self) -> None:
        with self.assertRaisesRegex(ValueError, "3m|horizon|endpoint"):
            validate_target_contract(_target_row("2026-01-01", three_month_offset=91))


class SolR136SecTests(unittest.TestCase):
    def test_company_facts_requires_matching_issuer_identity_and_rejects_future_chronology(self) -> None:
        valid_fact = {"val": 10, "end": "2025-12-31", "filed": "2026-01-15", "form": "10-K"}
        with self.assertRaisesRegex(ValueError, "CIK|identity"):
            parse_sec_company_facts("AAA", {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [valid_fact]}}}}})
        with self.assertRaisesRegex(ValueError, "CIK|identity"):
            parse_sec_company_facts(
                "AAA", {"cik": 2, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [valid_fact]}}}}}, cik="1",
            )
        future = {**valid_fact, "end": "2098-12-31", "filed": "2026-01-15"}
        with self.assertRaisesRegex(ValueError, "chronology|publication|future|period"):
            parse_sec_company_facts(
                "AAA", {"cik": 1, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [future]}}}}}, cik="1", retrieved_at="2026-09-16T00:00:00+00:00",
            )


class SolR136YahooTests(unittest.TestCase):
    def test_yahoo_rejects_negative_volume_duplicate_timestamps_and_impossible_ohlc(self) -> None:
        cases = (
            _yahoo_payload(values={"volume": [-1.0]}),
            _yahoo_payload(timestamps=[1767225600, 1767225600], values={name: [100.0, 100.0] for name in ("open", "high", "low", "close", "volume")}),
            _yahoo_payload(values={"open": [110.0], "high": [100.0], "low": [90.0], "close": [100.0]}),
        )
        for payload in cases:
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "volume|duplicate|timestamp|OHLC|high|low"):
                _parse_yahoo(
                    "AAA", payload, "2026-01-01T00:00:00+00:00", "https://example.invalid",
                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                )


class SolR136CutoffTests(unittest.TestCase):
    def test_explicit_decision_date_after_cutoff_is_rejected(self) -> None:
        bars = {
            "AAA": [MarketBar("AAA", "2026-01-01", 100, 100, 100, 100, 1000, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r136")],
            "^GSPC": [MarketBar("^GSPC", "2026-01-01", 100, 100, 100, 100, 1000, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r136")],
        }
        snapshot = build_history_snapshot(bars, benchmark_ticker="^GSPC", created_at="2026-01-02T00:00:00+00:00")
        with self.assertRaisesRegex(ValueError, "decision|cutoff|as_of"):
            build_monthly_panel(snapshot, as_of="2026-01-01", decision_dates=["2026-01-02"])


class SolR136FreshnessTests(unittest.TestCase):
    def test_stale_current_market_history_is_excluded_from_normal_predictions(self) -> None:
        training = _row("TRAIN", "2025-09-01", staleness=0.0, observation_date=None)
        stale = _row("AAA", "2026-01-01", staleness=0.0, observation_date="2025-12-01")
        dataset = TrainingDataset(
            rows=[training, stale],
            dataset_id="dataset-sol-r136", history_snapshot_id="history-sol-r136",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        artifact = SpecializedRankingModel().fit(
            [training], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        self.assertEqual(rank_current_candidates(dataset, artifact, as_of="2026-01-15"), [])


if __name__ == "__main__":
    unittest.main()
