from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import (
    HttpResponse,
    UniverseMember,
    build_history_snapshot,
    fetch_history_snapshot,
    load_history_snapshot,
    save_history_snapshot,
)
from nisa_quant.phase3_reporting import render_phase3_report
from nisa_quant.ranking_model import factor_score, load_model, save_model
from nisa_quant.return_targets import calculate_forward_targets
from nisa_quant.training_dataset import TrainingDataset
from nisa_quant.training_dataset import build_monthly_panel
from nisa_quant.walk_forward_evaluation import (
    SpecializedRankingModel,
    walk_forward_backtest,
    walk_forward_splits,
)
from nisa_quant.historical_market_data import MarketBar


def bar(ticker: str, day: str, close: float) -> MarketBar:
    return MarketBar(
        ticker, day, close, close, close, close, 1000, close,
        retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://core",
    )


def complete_features(**overrides: float) -> dict[str, float]:
    values = {name: 0.0 for name in FEATURE_SCHEMA}
    values.update(overrides)
    return values


def row(
    ticker: str,
    decision_date: str,
    *,
    endpoint: str,
    target: float = 0.01,
    features: dict[str, float] | None = None,
    metadata: dict[str, object] | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        decision_date=decision_date,
        features=features or complete_features(),
        targets={
            "target_1m_interval_start": decision_date,
            "target_1m_interval_end": (date.fromisoformat(decision_date) + timedelta(days=30)).isoformat(),
            "target_1m_interval_semantics": "[start,end)",
            "target_1m_forward_endpoint": (date.fromisoformat(decision_date) + timedelta(days=30)).isoformat(),
            "target_1m_excess_return": target,
            "target_1m_return": target,
            "target_1m_benchmark_return": 0.0,
            "target_1m_return_basis": "price_return",
            "target_3m_excess_return": target,
            "target_3m_forward_endpoint": endpoint,
            "target_3m_interval_end": endpoint,
            "target_3m_return_basis": "price_return",
            "target_3m_label_availability": "available_at_endpoint",
        },
        target_metadata=metadata,
    )


class Phase3CoreCorrectnessTests(unittest.TestCase):
    def test_adjacent_monthly_realizations_use_next_decision_endpoints(self) -> None:
        asset = [
            bar("AAA", "2026-01-01", 100),
            bar("AAA", "2026-02-01", 110),
            bar("AAA", "2026-03-01", 121),
        ]
        benchmark = [
            bar("^GSPC", "2026-01-01", 100),
            bar("^GSPC", "2026-02-01", 100),
            bar("^GSPC", "2026-03-01", 100),
        ]

        january = calculate_forward_targets(
            {"AAA": asset}, benchmark, "2026-01-01",
            next_decision_date="2026-02-01", monthly_interval=True,
        )
        february = calculate_forward_targets(
            {"AAA": asset}, benchmark, "2026-02-01",
            next_decision_date="2026-03-01", monthly_interval=True,
        )

        self.assertEqual(january["target_1m_interval_start"], "2026-01-01")
        self.assertEqual(january["target_1m_interval_end"], "2026-02-01")
        self.assertEqual(february["target_1m_interval_start"], "2026-02-01")
        self.assertEqual(february["target_1m_interval_end"], "2026-03-01")
        self.assertEqual(january["target_1m_forward_endpoint"], "2026-02-01")
        self.assertEqual(january.get("target_1m_interval_semantics"), "[start,end)")
        self.assertFalse(
            max(date.fromisoformat(january["target_1m_interval_start"]), date.fromisoformat(february["target_1m_interval_start"]))
            < min(date.fromisoformat(january["target_1m_interval_end"]), date.fromisoformat(february["target_1m_interval_end"]))
        )
        self.assertAlmostEqual(january["target_1m_return"], 0.10)
        self.assertAlmostEqual(february["target_1m_return"], 0.10)

    def test_three_month_target_records_endpoint_and_availability(self) -> None:
        asset = [bar("AAA", "2026-01-01", 100), bar("AAA", "2026-04-01", 110)]
        benchmark = [bar("^GSPC", "2026-01-01", 100), bar("^GSPC", "2026-04-01", 105)]

        targets = calculate_forward_targets({"AAA": asset}, benchmark, "2026-01-01")

        self.assertEqual(targets["target_3m_forward_endpoint"], "2026-04-01")
        self.assertEqual(targets["target_3m_interval_end"], "2026-04-01")
        self.assertEqual(targets["target_3m_label_availability"], "available_at_endpoint")
        self.assertEqual(targets["target_3m_return_basis"], "price_return")

    def test_late_observation_is_not_claimed_available_at_target_endpoint(self) -> None:
        asset = [bar("AAA", "2026-01-01", 100), bar("AAA", "2026-04-03", 110)]
        benchmark = [bar("^GSPC", "2026-01-01", 100), bar("^GSPC", "2026-04-03", 105)]

        targets = calculate_forward_targets({"AAA": asset}, benchmark, "2026-01-01")

        self.assertEqual(targets["target_3m_forward_endpoint"], "2026-04-01")
        self.assertFalse(targets["target_3m_endpoint_available"])
        self.assertEqual(targets["target_3m_label_availability"], "available_after_endpoint")
        self.assertIsNone(targets["target_3m_return"])
        self.assertIsNone(targets["target_3m_benchmark_return"])
        self.assertIsNone(targets["target_3m_excess_return"])

    def test_history_snapshot_preserves_typed_market_bars(self) -> None:
        member = UniverseMember(
            ticker="AAA", effective_from=None, effective_to=None,
            membership_status="active", lookahead_bias_status="point_in_time",
            survivorship_bias_status="survivorship_risk_disclosed",
            source="test universe", source_version="test-v1",
        )
        snapshot = build_history_snapshot(
            {"AAA": [bar("AAA", "2026-01-01", 100)], "^GSPC": [bar("^GSPC", "2026-01-01", 100)]}, benchmark_ticker="^GSPC",
            universe=[member], created_at="2026-01-02T00:00:00+00:00",
            request_contract='{"asset_tickers":["AAA"],"benchmark_ticker":"^GSPC","return_basis":"price_return"}',
        )

        self.assertIsInstance(snapshot.bars_by_ticker["AAA"][0], MarketBar)
        self.assertIsInstance(snapshot.universe[0], UniverseMember)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "snapshot.json"
            save_history_snapshot(snapshot, path)
            loaded = load_history_snapshot(path, expected_request_contract=snapshot.request_contract)

        self.assertIsInstance(loaded.bars_by_ticker["AAA"][0], MarketBar)
        self.assertIsInstance(loaded.universe[0], UniverseMember)
        self.assertNotIsInstance(loaded.bars_by_ticker["AAA"][0], dict)
        self.assertNotIsInstance(loaded.universe[0], dict)
        self.assertEqual(loaded, snapshot)
        self.assertEqual(loaded.snapshot_id, snapshot.snapshot_id)
        self.assertEqual(loaded.benchmark_ticker, snapshot.benchmark_ticker)
        self.assertEqual(loaded.request_contract, snapshot.request_contract)

        panel = build_monthly_panel(loaded, decision_dates=["2026-01-01"])
        self.assertEqual([item.ticker for item in panel.rows], ["AAA"])

    def test_walk_forward_purges_labels_ending_after_validation_date(self) -> None:
        usable = row("OLD", "2025-12-01", endpoint="2026-01-31")
        future_label = row("FUTURE", "2026-01-15", endpoint="2026-02-15")

        training, test, validation = walk_forward_splits(
            [usable, future_label], validation_dates=["2026-02-01"],
        )[0]

        self.assertEqual(validation, date(2026, 2, 1))
        self.assertEqual([item.ticker for item in training], ["OLD"])
        self.assertEqual(test, [])

    def test_walk_forward_purges_labels_not_observable_until_after_validation_date(self) -> None:
        late = row("LATE", "2025-12-01", endpoint="2026-01-31")
        late.targets.update({
            "target_3m_asset_observation_date": "2026-02-03",
            "target_3m_benchmark_observation_date": "2026-02-03",
            "target_3m_label_availability": "available_after_endpoint",
        })

        training, test, validation = walk_forward_splits(
            [late], validation_dates=["2026-02-02"],
        )[0]

        self.assertEqual(validation, date(2026, 2, 2))
        self.assertEqual(training, [])
        self.assertEqual(test, [])

    def test_walk_forward_purge_reads_serialized_fields_from_minimal_row(self) -> None:
        minimal = SimpleNamespace(
            decision_date="2025-12-01",
            targets={
                "target_3m_forward_endpoint": "2026-01-31",
                "target_3m_interval_end": "2026-01-31",
                "target_3m_asset_observation_date": "2026-02-03",
                "target_3m_benchmark_observation_date": "2026-02-03",
                "target_3m_label_availability": "available_after_endpoint",
            },
        )

        training, test, validation = walk_forward_splits(
            [minimal], validation_dates=["2026-02-02"],
        )[0]

        self.assertEqual(validation, date(2026, 2, 2))
        self.assertEqual(training, [])
        self.assertEqual(test, [])

    def test_target_label_availability_prefers_typed_metadata(self) -> None:
        item = SimpleNamespace(
            decision_date="2025-12-01",
            targets={
                "target_3m_forward_endpoint": "2026-01-31",
                "target_3m_label_availability": "available_at_endpoint",
            },
            target_metadata={
                "target_3m_forward_endpoint": "2026-01-31",
                "target_3m_label_availability": "available_after_endpoint",
                "target_3m_asset_observation_date": "2026-02-03",
                "target_3m_benchmark_observation_date": "2026-02-03",
            },
        )

        training, test, _ = walk_forward_splits(
            [item], validation_dates=["2026-02-02"],
        )[0]

        self.assertEqual(training, [])
        self.assertEqual(test, [])

    def test_model_fit_also_excludes_future_three_month_labels(self) -> None:
        usable = row("OLD", "2025-12-01", endpoint="2026-01-31")
        future_label = row("FUTURE", "2026-01-15", endpoint="2026-02-15")

        artifact = SpecializedRankingModel().fit(
            [usable, future_label], training_cutoff="2026-02-01",
            target_observable_by="2026-02-01",
        )

        self.assertEqual(artifact.training_rows, 1)

    def test_model_fits_market_only_rows_with_missing_sec_features(self) -> None:
        first_features = complete_features(momentum_3m=0.02, missingness=0.2, staleness=1.0)
        first_features["revenue"] = 100.0
        second_features = complete_features(momentum_3m=0.08, missingness=0.4, staleness=3.0)
        second_features["revenue"] = None
        training = [
            row("OBSERVED", "2025-10-01", endpoint="2026-01-01", target=0.01, features=first_features),
            row("MARKET_ONLY", "2025-11-01", endpoint="2026-02-01", target=0.08, features=second_features),
        ]

        model = SpecializedRankingModel()
        artifact = model.fit(training, training_cutoff="2026-03-01")
        prediction = model.predict(row(
            "LIVE", "2025-12-01", endpoint="2026-03-01", features=second_features,
        ))

        self.assertEqual(artifact.training_rows, 2)
        self.assertEqual(artifact.imputation_values[FEATURE_SCHEMA.index("revenue")], 100.0)
        self.assertIn("missingness", artifact.feature_schema)
        self.assertIn("staleness", artifact.feature_schema)
        self.assertIsNotNone(prediction)
        self.assertTrue(math.isfinite(float(prediction)))

    def test_current_only_membership_is_descriptive_and_has_no_performance_claim(self) -> None:
        dataset = TrainingDataset(
            rows=[
                row("TRAIN", "2025-12-01", endpoint="2026-01-31"),
                row("AAA", "2026-01-01", endpoint="2026-04-01", metadata={"current_snapshot_only": True}),
            ],
            dataset_id="dataset-current-only", history_snapshot_id="history-current-only",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )

        backtest = walk_forward_backtest(dataset, validation_dates=["2026-01-01"])
        report = render_phase3_report(
            dataset=dataset, model=backtest, backtest=backtest, output_format="json",
        )

        self.assertEqual(backtest.metrics["evidence_status"], "descriptive_survivor_selected_evidence")
        self.assertIsNone(backtest.metrics.get("benchmark_relative_return"))
        self.assertIsNone(backtest.metrics.get("alpha"))
        self.assertTrue(backtest.metrics.get("performance_claims_unavailable"))
        self.assertTrue(
            all(value is None for key, value in backtest.metrics.items()
                if ("benchmark_relative" in key.casefold() or "alpha" in key.casefold())
                and key not in {"benchmark_relative_claims_suppressed", "alpha_claim_suppressed"})
        )
        self.assertEqual(backtest.metrics["availability_status"], "unavailable_insufficient_data")
        self.assertEqual(backtest.periods, [])
        self.assertEqual(report["backtest"]["periods"], [])
        self.assertNotIn('"alpha":', json.dumps(report))

    def test_phase3_report_json_keeps_safety_and_suppresses_injected_alpha(self) -> None:
        dataset = {
            "dataset_id": "dataset-report", "history_snapshot_id": "history-report",
            "benchmark_ticker": "^GSPC", "feature_schema": list(FEATURE_SCHEMA),
            "return_basis": "price_return", "rows": [],
        }
        model = {
            "model_version": "model-report", "dataset_id": "dataset-report",
            "history_snapshot_id": "history-report", "feature_schema": list(FEATURE_SCHEMA),
            "return_basis": "price_return",
            "notes": "BUY AAA",
        }
        backtest = {
            "model_version": "model-report",
            "dataset_id": "dataset-report", "history_snapshot_id": "history-report",
            "benchmark_ticker": "^GSPC", "feature_schema": list(FEATURE_SCHEMA), "return_basis": "price_return",
            "metrics": {
                "evidence_status": "descriptive_survivor_selected_evidence",
                "performance_claims_suppressed": True, "alpha": 0.25,
                "benchmark_relative_return": 0.25,
                "availability_status": "unavailable_insufficient_data",
            }, "periods": [],
        }

        with self.assertRaisesRegex(ValueError, "action"):
            render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="json")

        model.pop("notes")
        report = render_phase3_report(
            dataset=dataset, model=model, backtest=backtest, output_format="json",
        )
        report_text = json.dumps(report)
        self.assertNotIn('"alpha":', report_text)
        self.assertNotIn('"benchmark_relative_return":', report_text)

    def test_price_return_dataset_rejects_total_return_target(self) -> None:
        item = row("AAA", "2026-01-01", endpoint="2026-04-01")
        item.targets["target_1m_return_basis"] = "total_return"
        dataset = TrainingDataset(
            rows=[item], dataset_id="dataset-mixed", history_snapshot_id="history-mixed",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )

        with self.assertRaisesRegex(ValueError, "mixed return bases"):
            walk_forward_backtest(dataset, validation_dates=["2026-01-01"])

        item.targets["target_1m_return_basis"] = "price_return"
        item.targets["target_return_basis"] = "total_return"
        with self.assertRaisesRegex(ValueError, "mixed return bases"):
            walk_forward_backtest(dataset, validation_dates=["2026-01-01"])

    def test_default_backtest_metadata_uses_canonical_price_return_basis(self) -> None:
        dataset = TrainingDataset(
            rows=[
                row("TRAIN", "2025-12-01", endpoint="2026-01-31"),
                row("TEST", "2026-02-01", endpoint="2026-05-01"),
            ],
            dataset_id="dataset-price", history_snapshot_id="history-price",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )

        result = walk_forward_backtest(dataset, validation_dates=["2026-02-01"])

        self.assertEqual(result.return_basis, "price_return")
        self.assertEqual(result.metrics["portfolio_return_basis"], "price_return")
        self.assertEqual(result.metrics["benchmark_return_basis"], "price_return")

    def test_backtest_realizes_asset_price_return_and_compounds_benchmark_price_return(self) -> None:
        training = row("TRAIN", "2025-12-01", endpoint="2026-01-31")
        test = row("TEST", "2026-02-01", endpoint="2026-05-01")
        test.targets.update({
            "target_1m_return": 0.20,
            "target_1m_benchmark_return": 0.10,
            "target_1m_excess_return": 0.10,
        })
        dataset = TrainingDataset(
            rows=[training, test], dataset_id="dataset-asset-return", history_snapshot_id="history-asset-return",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )

        result = walk_forward_backtest(dataset, validation_dates=["2026-02-01"], top_ks=(1,), transaction_cost_bps=0)

        self.assertAlmostEqual(result.periods[0]["top_1_return"], 0.20)
        self.assertAlmostEqual(result.metrics["benchmark_cumulative_return"], 0.10)

    def test_factor_baseline_ranks_deterministically_and_clamps_small_universe(self) -> None:
        training = row("TRAIN", "2025-12-01", endpoint="2026-01-31")
        weak = row("WEAK", "2026-02-01", endpoint="2026-05-01", features=complete_features(momentum_1m=0.01))
        strong = row("STRONG", "2026-02-01", endpoint="2026-05-01", features=complete_features(momentum_1m=0.10))
        dataset = TrainingDataset(
            rows=[training, weak, strong], dataset_id="dataset-factor", history_snapshot_id="history-factor",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )

        result = walk_forward_backtest(dataset, validation_dates=["2026-02-01"])

        self.assertEqual(result.periods[0]["factor_baseline_requested"], 5)
        self.assertEqual(result.periods[0]["factor_baseline_selected"], 2)
        self.assertEqual(result.periods[0].get("factor_baseline_tickers"), ["STRONG", "WEAK"])
        self.assertNotEqual(factor_score(strong), factor_score(weak))

    def test_transaction_cost_rejects_non_finite_and_negative_values(self) -> None:
        dataset = TrainingDataset(
            rows=[row("AAA", "2026-01-01", endpoint="2026-04-01")],
            dataset_id="dataset-cost", history_snapshot_id="history-cost",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        for value in (-1, math.nan, math.inf):
            with self.subTest(value=value), self.assertRaises(ValueError):
                walk_forward_backtest(dataset, validation_dates=["2026-01-01"], transaction_cost_bps=value)

    def test_backtest_requires_explicit_monthly_endpoint_and_return_basis(self) -> None:
        item = row("AAA", "2026-01-01", endpoint="2026-04-01")
        dataset = TrainingDataset(
            rows=[item], dataset_id="dataset-contract", history_snapshot_id="history-contract",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        item.targets.pop("target_1m_forward_endpoint")
        with self.assertRaisesRegex(ValueError, "1m.*endpoint"):
            walk_forward_backtest(dataset, validation_dates=["2026-01-01"])

        item.targets["target_1m_forward_endpoint"] = "2026-01-31"
        item.targets.pop("target_1m_return_basis")
        with self.assertRaisesRegex(ValueError, "return basis"):
            walk_forward_backtest(dataset, validation_dates=["2026-01-01"])

    def test_cache_key_binds_asset_and_benchmark_roles_and_tampering_is_rejected(self) -> None:
        def http_get(url: str, **_: object) -> HttpResponse:
            ticker = urlparse(url).path.rsplit("/", 1)[-1]
            response_symbol = ticker.replace("%5E", "^")
            payload = {
                "chart": {"result": [{
                    "meta": {"symbol": response_symbol},
                    "timestamp": [1767225600],
                    "indicators": {"quote": [{"open": [100], "high": [100], "low": [100], "close": [100], "volume": [1]}], "adjclose": [{"adjclose": [100]}]},
                }]},
            }
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            first = fetch_history_snapshot(
                tickers=["AAA"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-02",
                cache_dir=cache_dir, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get,
            )
            second = fetch_history_snapshot(
                tickers=["^GSPC"], benchmark_ticker="AAA", start_date="2026-01-01", end_date="2026-01-02",
                cache_dir=cache_dir, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get,
            )
            paths = sorted(cache_dir.glob("history-*.json"))
            self.assertEqual(len(paths), 2)
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            self.assertEqual(first.benchmark_ticker, "^GSPC")
            self.assertEqual(second.benchmark_ticker, "AAA")

            payload = json.loads(paths[0].read_text(encoding="utf-8"))
            payload.pop("snapshot_id")
            paths[0].write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "snapshot identity"):
                load_history_snapshot(paths[0], expected_request_contract=first.request_contract)

            renamed = cache_dir / "history-renamed.json"
            renamed.write_text(paths[1].read_text(encoding="utf-8"), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "cache filename"):
                load_history_snapshot(renamed)

            with self.assertRaisesRegex(ValueError, "roles"):
                fetch_history_snapshot(
                    tickers=["^GSPC"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-02",
                    cache_dir=cache_dir, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get,
                )

            permutation_cache = cache_dir / "permutations"
            ordered = fetch_history_snapshot(
                tickers=["AAA", "BBB"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-02",
                cache_dir=permutation_cache, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get,
            )
            permuted = fetch_history_snapshot(
                tickers=["BBB", "AAA"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-02",
                cache_dir=permutation_cache, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get,
            )
            self.assertEqual(ordered.snapshot_id, permuted.snapshot_id)
            self.assertEqual(len(list(permutation_cache.glob("history-*.json"))), 1)

    def test_saved_model_preserves_scalar_model_version(self) -> None:
        artifact = SpecializedRankingModel().fit(
            [row("AAA", "2025-12-01", endpoint="2026-01-31")],
            training_cutoff="2026-02-01",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.json"
            save_model(artifact, path)
            loaded = load_model(path)

        self.assertEqual(loaded.model_version, artifact.model_version)


if __name__ == "__main__":
    unittest.main()
