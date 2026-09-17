from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import MarketBar, UniverseMember, build_history_snapshot, save_history_snapshot
from nisa_quant.__main__ import main
from nisa_quant.refresh_pipeline import refresh_phase3
from nisa_quant.quant_report import render_phase3_report
from nisa_quant.ranking_model import RankingModelArtifact, SpecializedRankingModel, load_model, save_model
from nisa_quant.training_dataset import PanelRow, TrainingDataset, _identity
from nisa_quant.walk_forward_evaluation import (
    BacktestResult,
    _with_backtest_identity,
    load_backtest,
    save_backtest,
    walk_forward_backtest,
)


_TURNOVER = "symmetric difference of current and prior holdings divided by current selected count"
_INTERVAL = "non-overlapping [start,end) monthly interval ending at the next monthly decision date"


def _row(ticker: str, decision: str) -> PanelRow:
    day = date.fromisoformat(decision)
    endpoint = (day + timedelta(days=31)).isoformat()
    targets = {
        "target_1m_interval_start": decision,
        "target_1m_interval_end": endpoint,
        "target_1m_forward_endpoint": endpoint,
        "target_1m_interval_semantics": "[start,end)",
        "target_1m_return": 0.02,
        "target_1m_benchmark_return": 0.01,
        "target_1m_excess_return": 0.01,
        "target_1m_return_basis": "price_return",
    }
    for horizon, days in (("3m", 90), ("6m", 180), ("12m", 365)):
        target_endpoint = (day + timedelta(days=days)).isoformat()
        targets.update({
            f"target_{horizon}_forward_endpoint": target_endpoint,
            f"target_{horizon}_interval_end": target_endpoint,
            f"target_{horizon}_return_basis": "price_return",
            f"target_{horizon}_excess_return": 0.01,
            f"target_{horizon}_label_availability": "available_at_endpoint",
        })
    return PanelRow(ticker, decision, {name: 0.1 for name in FEATURE_SCHEMA}, targets, {})


def _dataset(rows: list[PanelRow]) -> TrainingDataset:
    history = "history-r115"
    return TrainingDataset(
        rows=rows,
        dataset_id=_identity(rows, history, "^GSPC", FEATURE_SCHEMA, "price_return"),
        history_snapshot_id=history,
        benchmark_ticker="^GSPC",
        feature_schema=FEATURE_SCHEMA,
    )


def _genuine_backtest() -> BacktestResult:
    dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")])
    model = SpecializedRankingModel()
    model.fit(
        [dataset.rows[0]], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
        history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
    )
    return walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"], top_ks=(1,), transaction_cost_bps=10.0)


def _rehash_backtest(result: BacktestResult, **changes: object) -> dict[str, object]:
    payload = asdict(replace(result, **changes, backtest_id=""))
    digest = hashlib.sha256(json.dumps({key: value for key, value in payload.items() if key != "backtest_id"}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    payload["backtest_id"] = f"backtest-{digest[:20]}"
    return payload


def _rehash_model(artifact: object, **changes: object) -> dict[str, object]:
    payload = asdict(artifact)
    payload.update(changes)
    payload["model_id"] = ""
    payload["content_hash"] = ""
    digest = hashlib.sha256(json.dumps({key: value for key, value in payload.items() if key not in {"model_id", "content_hash"}}, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    payload["content_hash"] = digest
    payload["model_id"] = f"model-{digest[:20]}"
    return payload


def _artifact_from_payload(payload: dict[str, object]) -> RankingModelArtifact:
    return RankingModelArtifact(
        model_version=payload["model_version"], feature_schema=tuple(payload["feature_schema"]),
        training_cutoff=payload["training_cutoff"], dataset_id=payload["dataset_id"],
        history_snapshot_id=payload["history_snapshot_id"], coefficients=tuple(payload["coefficients"]),
        intercept=payload["intercept"], means=tuple(payload["means"]), scales=tuple(payload["scales"]),
        residual_std=payload["residual_std"], training_rows=payload["training_rows"],
        target_horizon=payload["target_horizon"], return_basis=payload["return_basis"],
        benchmark_ticker=payload["benchmark_ticker"], training_window=payload["training_window"],
        schema_version=payload["schema_version"], model_id=payload["model_id"],
        content_hash=payload["content_hash"], imputation_values=tuple(payload["imputation_values"]),
        missing_feature_policy=payload["missing_feature_policy"],
    )


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, allow_nan=False), encoding="utf-8")


class BacktestStatusIntegrityTests(unittest.TestCase):
    def test_rehashed_empty_survivor_backtest_is_insufficient(self) -> None:
        result = _genuine_backtest()
        metrics = {
            **result.metrics,
            "evidence_status": "descriptive_survivor_selected_evidence",
            "performance_claims_suppressed": True,
            "performance_claims_unavailable": True,
            "benchmark_relative_claims_suppressed": True,
            "alpha_claim_suppressed": True,
            "availability_status": "available_descriptive",
        }
        for key in metrics:
            if key.casefold().endswith(("cumulative_return", "benchmark_relative_return")):
                metrics[key] = None
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty-survivor.json"
            _write_json(path, _rehash_backtest(result, periods=[], metrics=metrics, validation_dates=()))
            with self.assertRaisesRegex(ValueError, "insufficient|status|period"):
                load_backtest(path)

    def test_rehashed_status_flags_and_reason_must_match_serialized_periods(self) -> None:
        result = _genuine_backtest()
        cases = (
            {**result.metrics, "availability_status": "unavailable_insufficient_data", "performance_claims_unavailable": True, "insufficient_data_reason": "forged"},
            {**result.metrics, "availability_status": "available", "performance_claims_unavailable": True},
        )
        with tempfile.TemporaryDirectory() as directory:
            for index, metrics in enumerate(cases):
                path = Path(directory) / f"backtest-{index}.json"
                _write_json(path, _rehash_backtest(result, metrics=metrics))
                with self.subTest(index=index), self.assertRaisesRegex(ValueError, "status|available|insufficient|claim"):
                    load_backtest(path)

    def test_renderer_rejects_reclassified_point_in_time_status_even_when_rehashed(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        metrics = {
            **backtest.metrics,
            "evidence_status": "descriptive_survivor_selected_evidence",
            "performance_claims_suppressed": True,
            "performance_claims_unavailable": True,
            "benchmark_relative_claims_suppressed": True,
            "alpha_claim_suppressed": True,
            "availability_status": "available_descriptive",
        }
        for key in metrics:
            if key.casefold().endswith(("cumulative_return", "benchmark_relative_return")):
                metrics[key] = None
        forged = _rehash_backtest(backtest, periods=[], metrics=metrics, validation_dates=())
        with self.assertRaisesRegex(ValueError, "evidence|status|membership|values"):
            render_phase3_report(dataset=dataset, model=artifact, backtest=forged, output_format="json")

    def test_rehashed_empty_or_zero_selected_backtest_is_insufficient(self) -> None:
        result = _genuine_backtest()
        empty_metrics = {
            **result.metrics,
            "top_1_cumulative_return": None,
            "top_1_benchmark_relative_return": None,
            "benchmark_cumulative_return": None,
            "benchmark_relative_return": None,
            "availability_status": "available",
            "performance_claims_unavailable": False,
        }
        zero_period = {**result.periods[0], "top_1_selected": 0, "top_1_return": None, "top_1_turnover": 0.0}
        zero_metrics = {
            **result.metrics,
            "top_1_cumulative_return": None,
            "top_1_benchmark_relative_return": None,
            "benchmark_relative_return": None,
            "availability_status": "available",
            "performance_claims_unavailable": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            for index, (periods, metrics) in enumerate((([], empty_metrics), ([zero_period], zero_metrics))):
                path = Path(directory) / f"insufficient-{index}.json"
                _write_json(path, _rehash_backtest(result, periods=periods, metrics=metrics, validation_dates=()))
                with self.subTest(index=index), self.assertRaisesRegex(ValueError, "insufficient|status|selected|claim"):
                    load_backtest(path)


class BacktestMetricIntegrityTests(unittest.TestCase):
    def test_rehashed_mutations_to_all_published_numeric_metrics_are_rejected(self) -> None:
        result = _genuine_backtest()
        for key in (
            "benchmark_cumulative_return", "top_1_cumulative_return",
            "top_1_benchmark_relative_return", "benchmark_relative_return",
            "transaction_cost_bps",
        ):
            original = result.metrics[key]
            metrics = {**result.metrics, key: (float(original) + 0.25) if original is not None else 0.25}
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "backtest.json"
                _write_json(path, _rehash_backtest(result, metrics=metrics))
                with self.subTest(key=key), self.assertRaisesRegex(ValueError, "metric|period|recomputed"):
                    load_backtest(path)

    def test_rehashed_impossible_period_turnover_is_rejected(self) -> None:
        result = _genuine_backtest()
        period = {**result.periods[0], "top_1_turnover": 99.0}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backtest.json"
            _write_json(path, _rehash_backtest(result, periods=[period]))
            with self.assertRaisesRegex(ValueError, "turnover|period"):
                load_backtest(path)

    def test_genuine_backtest_period_aggregation_is_accepted(self) -> None:
        result = _genuine_backtest()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "backtest.json"
            save_backtest(result, path)
            loaded = load_backtest(path)
        self.assertEqual(loaded.backtest_id, result.backtest_id)


class ModelArtifactProvenanceTests(unittest.TestCase):
    def test_rehashed_model_training_window_requires_none_or_strict_positive_integer(self) -> None:
        artifact = SpecializedRankingModel().fit([_row("AAA", "2025-09-01")], training_cutoff="2026-02-01", training_window=1)
        with tempfile.TemporaryDirectory() as directory:
            for index, value in enumerate((True, 0, -1, 1.5, "1")):
                path = Path(directory) / f"model-{index}.json"
                _write_json(path, _rehash_model(artifact, training_window=value))
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "training window"):
                    load_model(path)

    def test_bounded_training_window_keeps_truthful_training_rows(self) -> None:
        rows = [_row("A", "2025-09-01"), _row("B", "2025-10-01")]
        artifact = SpecializedRankingModel().fit(rows, training_cutoff="2026-02-01", training_window=1)
        self.assertEqual(artifact.training_window, 1)
        self.assertEqual(artifact.training_rows, 1)

    def test_renderer_rejects_rehashed_training_row_count_forgery(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        forged_model = _rehash_model(artifact, training_rows=999)
        with self.assertRaisesRegex(ValueError, "training-row|provenance|artifact"):
            render_phase3_report(dataset=dataset, model=forged_model, backtest=backtest, output_format="json")

    def test_renderer_rejects_rehashed_coefficient_mutation_after_backtest_rebuild(self) -> None:
        dataset = _dataset([
            _row("TRAIN-A", "2025-09-01"), _row("TRAIN-B", "2025-10-01"),
            _row("AAA", "2026-01-01"),
        ])
        artifact = SpecializedRankingModel().fit(
            dataset.rows[:2], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        forged_model = _rehash_model(
            artifact, coefficients=(artifact.coefficients[0] + 0.25, *artifact.coefficients[1:]),
        )
        forged_backtest = walk_forward_backtest(
            dataset, model=SpecializedRankingModel(_artifact_from_payload(forged_model)),
            validation_dates=["2026-01-01"], top_ks=(1,), transaction_cost_bps=10.0,
        )

        with self.assertRaisesRegex(ValueError, "artifact values|canonical|model artifact field|provenance"):
            render_phase3_report(
                dataset=dataset, model=forged_model, backtest=forged_backtest, output_format="json",
            )

    def test_renderer_rejects_rehashed_model_imputation_and_residual_mutations(self) -> None:
        dataset = _dataset([
            replace(_row("TRAIN-A", "2025-09-01"), features={
                **_row("TRAIN-A", "2025-09-01").features, "revenue": None,
            }),
            _row("TRAIN-B", "2025-10-01"), _row("AAA", "2026-01-01"),
        ])
        artifact = SpecializedRankingModel().fit(
            dataset.rows[:2], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        mutations = (
            ("imputation_values", {
                "imputation_values": (artifact.imputation_values[0] + 0.25, *artifact.imputation_values[1:]),
            }),
            ("residual_std", {"residual_std": artifact.residual_std + 0.25}),
        )
        for label, changes in mutations:
            with self.subTest(label=label):
                forged_model = _rehash_model(artifact, **changes)
                forged_backtest = walk_forward_backtest(
                    dataset, model=SpecializedRankingModel(_artifact_from_payload(forged_model)),
                    validation_dates=["2026-01-01"], top_ks=(1,), transaction_cost_bps=10.0,
                )
                with self.assertRaisesRegex(ValueError, "artifact values|canonical|model artifact field|provenance"):
                    render_phase3_report(
                        dataset=dataset, model=forged_model, backtest=forged_backtest,
                        output_format="json",
                    )

    def test_strict_model_rebinding_requires_typed_dataset_rows(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")])
        artifact = SpecializedRankingModel().fit(
            [dataset.rows[0]], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=SpecializedRankingModel(artifact), validation_dates=["2026-01-01"])
        weak_dataset = {
            "dataset_id": dataset.dataset_id, "history_snapshot_id": dataset.history_snapshot_id,
            "benchmark_ticker": dataset.benchmark_ticker, "feature_schema": list(FEATURE_SCHEMA),
            "return_basis": "price_return", "rows": [],
        }
        forged_model = _rehash_model(artifact, training_rows=999)
        forged_backtest = _with_backtest_identity(replace(
            backtest, model_artifact_id=forged_model["model_id"], backtest_id="",
        ))
        with self.assertRaisesRegex(ValueError, "dataset|provenance|artifact"):
            render_phase3_report(
                dataset=weak_dataset, model=forged_model,
                backtest=forged_backtest, output_format="json",
            )


class ReplayCompatibilityTests(unittest.TestCase):
    @staticmethod
    def _contract(asset_tickers: list[str], end: str = "2026-02-01") -> str:
        return json.dumps({
            "schema": "phase3-market-request", "schema_version": 1,
            "asset_tickers": asset_tickers, "benchmark_ticker": "^GSPC",
            "start_date": "2026-01-01", "end_date": end,
            "period1": 1767225600, "period2": 1769990400 if end == "2026-02-01" else 1772409600,
            "interval": "1d", "events": "div,splits", "return_basis": "price_return",
            "timeout": 15, "retries": 2,
            "user_agent": "nisa-quant-assistant/phase3-read-only",
            "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
        }, sort_keys=True, separators=(",", ":"))

    def test_replay_candidate_filter_ignores_unrelated_ranges_but_requires_exactly_one_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            matching_contract = json.dumps({
                "schema": "phase3-market-request", "schema_version": 1,
                "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
                "start_date": "2026-01-01", "end_date": "2026-02-01",
                "period1": 1767225600, "period2": 1769990400,
                "interval": "1d", "events": "div,splits", "return_basis": "price_return",
                "timeout": 15, "retries": 2,
                "user_agent": "nisa-quant-assistant/phase3-read-only",
                "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
            }, sort_keys=True, separators=(",", ":"))
            unrelated_contract = matching_contract.replace("2026-02-01", "2026-03-01").replace("1769990400", "1772409600")
            matching = cache / f"history-{hashlib.sha256(matching_contract.encode()).hexdigest()[:24]}.json"
            unrelated = cache / f"history-{hashlib.sha256(unrelated_contract.encode()).hexdigest()[:24]}.json"
            matching.write_text(json.dumps({"request_contract": matching_contract}), encoding="utf-8")
            unrelated.write_text(json.dumps({"request_contract": unrelated_contract}), encoding="utf-8")
            snapshot = SimpleNamespace(
                request_contract=matching_contract, snapshot_id="history-r115", benchmark_ticker="^GSPC",
                universe=[SimpleNamespace(ticker="AAA", lookahead_bias_status="point_in_time")],
                coverage={"AAA": {"row_count": 253}, "^GSPC": {"row_count": 253}},
                ticker_failures={}, bars_by_ticker={}, sec_facts=[],
            )
            dataset = SimpleNamespace(
                dataset_id="dataset-r115", history_snapshot_id="history-r115", benchmark_ticker="^GSPC",
                feature_schema=FEATURE_SCHEMA, rows=[], membership_evidence_status="point_in_time_membership_evidence",
            )
            artifact = SimpleNamespace(model_id="model-r115")
            backtest = SimpleNamespace(backtest_id="backtest-r115", metrics={"availability_status": "available"})
            with patch("nisa_quant.refresh_pipeline.load_history_snapshot", return_value=snapshot), \
                 patch("nisa_quant.refresh_pipeline.build_monthly_panel", return_value=dataset), \
                 patch("nisa_quant.refresh_pipeline.SpecializedRankingModel") as model_type, \
                 patch("nisa_quant.refresh_pipeline.rank_current_candidates", return_value=[]), \
                 patch("nisa_quant.refresh_pipeline.walk_forward_backtest", return_value=backtest), \
                 patch("nisa_quant.refresh_pipeline.render_phase3_report", return_value={"status": "available"}):
                model_type.return_value.artifact = artifact
                output = root / "report.json"
                status = refresh_phase3(
                    as_of="2026-02-01", start="2026-01-01", end="2026-02-01",
                    cache_dir=cache, output=output, live=False, replay_only=True,
                )
            report = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(report["status"], "available")

    def test_replay_zero_or_multiple_compatible_candidates_are_structured_unavailable(self) -> None:
        for contracts in (
            [self._contract(["AAA"], end="2026-03-01")],
            [self._contract(["AAA"]), self._contract(["BBB"])],
        ):
            with self.subTest(candidate_count=len(contracts)), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                cache = root / "cache"
                cache.mkdir()
                for contract in contracts:
                    path = cache / f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
                    path.write_text(json.dumps({"request_contract": contract}), encoding="utf-8")
                output = root / "report.json"
                status = refresh_phase3(
                    as_of="2026-02-01", start="2026-01-01", end="2026-02-01",
                    cache_dir=cache, output=output, live=False, replay_only=True,
                )
                report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("exactly one compatible", report["reason"])

    def test_replay_explicit_contract_excludes_same_range_wrong_asset_snapshot(self) -> None:
        requested = self._contract(["AAA"])
        wrong_asset = self._contract(["BBB"])
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            for contract in (requested, wrong_asset):
                path = cache / f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
                path.write_text(json.dumps({"request_contract": contract}), encoding="utf-8")
            from nisa_quant.refresh_pipeline import _find_compatible_history_cache_paths
            matches = _find_compatible_history_cache_paths(
                cache, start="2026-01-01", end="2026-02-01",
                expected_request_contract=requested,
            )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0].name, f"history-{hashlib.sha256(requested.encode()).hexdigest()[:24]}.json")


class SerializedBoundaryTests(unittest.TestCase):
    def test_renderer_rejects_missing_or_invalid_serialized_status(self) -> None:
        dataset = {
            "dataset_id": "dataset-weak", "history_snapshot_id": "history-weak",
            "benchmark_ticker": "^GSPC", "feature_schema": list(FEATURE_SCHEMA),
            "return_basis": "price_return", "rows": [],
        }
        model = {
            "model_version": "model-weak", "dataset_id": "dataset-weak",
            "history_snapshot_id": "history-weak", "feature_schema": list(FEATURE_SCHEMA),
            "return_basis": "price_return",
        }
        for metrics in (
            {"evidence_status": "point_in_time_membership_evidence"},
            {"availability_status": "forged", "evidence_status": "point_in_time_membership_evidence"},
        ):
            backtest = {
                "model_version": "model-weak", "dataset_id": "dataset-weak",
                "history_snapshot_id": "history-weak", "benchmark_ticker": "^GSPC",
                "feature_schema": list(FEATURE_SCHEMA), "return_basis": "price_return",
                "metrics": metrics, "periods": [],
            }
            with self.subTest(metrics=metrics), self.assertRaisesRegex(ValueError, "status|validated"):
                render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="json")

    def test_duplicate_serialized_periods_are_rejected_after_metric_recomputation(self) -> None:
        result = _genuine_backtest()
        period = result.periods[0]
        benchmark = period["benchmark_return"]
        top_return = period["top_1_return"]
        metrics = {
            **result.metrics,
            "benchmark_cumulative_return": (1 + benchmark) ** 2 - 1,
            "top_1_cumulative_return": (1 + top_return) ** 2 - 1,
        }
        metrics["top_1_benchmark_relative_return"] = (
            metrics["top_1_cumulative_return"] - metrics["benchmark_cumulative_return"]
        )
        metrics["benchmark_relative_return"] = metrics["top_1_benchmark_relative_return"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-periods.json"
            _write_json(path, _rehash_backtest(
                result, periods=[period, dict(period)], metrics=metrics,
                validation_dates=("2026-01-01", "2026-02-01"),
            ))
            with self.assertRaisesRegex(ValueError, "period|duplicate|order"):
                load_backtest(path)


class ProducerInputValidationTests(unittest.TestCase):
    def test_invalid_live_limit_fails_before_provider_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch(
                "nisa_quant.refresh_pipeline.fetch_current_sp500_universe",
                return_value=[],
            ) as fetch_universe:
                status = refresh_phase3(
                    as_of="2026-09-16", start="2025-01-01", end="2026-09-16",
                    cache_dir=Path(directory) / "cache", output=output,
                    live=True, replay_only=False, limit=0,
                )
            report = json.loads(output.read_text())
        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertEqual(report["reason"], "live --limit must be a positive integer")
        fetch_universe.assert_not_called()

    def test_conflicting_cli_modes_write_structured_unavailable_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            status = main([
                "phase3-refresh", "--as-of", "2026-09-16", "--start", "2025-01-01",
                "--end", "2026-09-16", "--cache-dir", str(Path(directory) / "cache"),
                "--output", str(output), "--live", "--replay-only",
            ])
            report = json.loads(output.read_text())
        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("exactly one", report["reason"])


if __name__ == "__main__":
    unittest.main()
