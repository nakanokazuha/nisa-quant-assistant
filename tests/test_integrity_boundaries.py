from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import (
    HttpResponse,
    MarketBar,
    UniverseMember,
    _parse_yahoo,
    build_history_snapshot,
    parse_sec_company_facts,
    save_history_snapshot,
)
from nisa_quant.evidence_providers import normalize_sec_company_facts
from nisa_quant.refresh_pipeline import refresh_phase3
from nisa_quant.quant_report import render_phase3_report
from nisa_quant.ranking_model import SpecializedRankingModel, rank_current_candidates
from nisa_quant.training_dataset import PanelRow, TrainingDataset, _identity, build_monthly_panel
from nisa_quant.walk_forward_evaluation import (
    BacktestResult,
    _with_backtest_identity,
    load_backtest,
    save_backtest,
    walk_forward_backtest,
)


def _targets(decision: str, *, one_month_return: float | None = 0.01) -> dict[str, object]:
    decision_date = date.fromisoformat(decision)
    one_month_endpoint = (decision_date + timedelta(days=31)).isoformat()
    values: dict[str, object] = {
        "target_1m_interval_start": decision,
        "target_1m_interval_end": one_month_endpoint,
        "target_1m_interval_semantics": "[start,end)",
        "target_1m_forward_endpoint": one_month_endpoint,
        "target_1m_return": one_month_return,
        "target_1m_benchmark_return": 0.0,
        "target_1m_excess_return": one_month_return,
        "target_1m_return_basis": "price_return",
    }
    for horizon, days in (("3m", 90), ("6m", 180), ("12m", 365)):
        endpoint = (decision_date + timedelta(days=days)).isoformat()
        values.update({
            f"target_{horizon}_forward_endpoint": endpoint,
            f"target_{horizon}_interval_end": endpoint,
            f"target_{horizon}_return_basis": "price_return",
            f"target_{horizon}_excess_return": 0.02,
            f"target_{horizon}_label_availability": "available_at_endpoint",
        })
    return values


def _row(ticker: str, decision: str, *, current_only: bool = False) -> PanelRow:
    return PanelRow(
        ticker=ticker,
        decision_date=decision,
        features={name: 0.1 for name in FEATURE_SCHEMA},
        targets=_targets(decision),
        target_metadata=(
            {"current_snapshot_only": True, "market_observation_date": decision}
            if current_only else {}
        ),
    )


def _dataset(rows: list[PanelRow], *, history_id: str = "history-critical") -> TrainingDataset:
    membership = "descriptive_survivor_selected_evidence" if any(
        row.target_metadata and row.target_metadata.get("current_snapshot_only") for row in rows
    ) else "point_in_time_membership_evidence"
    return TrainingDataset(
        rows=rows,
        dataset_id=_identity(
            rows, history_id, "^GSPC", FEATURE_SCHEMA, "price_return",
            membership_evidence_status=membership,
        ),
        history_snapshot_id=history_id,
        benchmark_ticker="^GSPC",
        feature_schema=FEATURE_SCHEMA,
        membership_evidence_status=membership,
    )


def _valid_backtest() -> BacktestResult:
    period = {
        "decision_date": "2026-01-01",
        "benchmark_return": 0.01,
        "factor_baseline_requested": 5,
        "factor_baseline_selected": 1,
        "factor_baseline_tickers": ["AAA"],
        "turnover_convention": "symmetric difference of current and prior holdings divided by current selected count",
        "top_1_selected": 1,
        "top_1_return": 0.02,
        "top_1_turnover": 1.0,
        "factor_baseline_return": 0.02,
        "factor_baseline_turnover": 1.0,
    }
    metrics = {
        "top_1_cumulative_return": 0.02,
        "top_1_benchmark_relative_return": 0.01,
        "benchmark_cumulative_return": 0.01,
        "transaction_cost_bps": 10.0,
        "turnover_convention": period["turnover_convention"],
        "portfolio_return_basis": "price_return",
        "benchmark_return_basis": "price_return",
        "portfolio_interval_convention": "non-overlapping [start,end) monthly interval ending at the next monthly decision date",
        "benchmark_relative_return": 0.01,
        "evidence_status": "point_in_time_membership_evidence",
        "performance_claims_suppressed": False,
        "performance_claims_unavailable": False,
        "benchmark_relative_claims_suppressed": False,
        "alpha_claim_suppressed": False,
        "availability_status": "available",
    }
    return _with_backtest_identity(BacktestResult(
        periods=[period], metrics=metrics, dataset_id="dataset-critical",
        history_snapshot_id="history-critical", benchmark_ticker="^GSPC",
        feature_schema=FEATURE_SCHEMA, model_artifact_id=None,
        scenarios=None, validation_dates=("2026-01-01",),
    ))


class BacktestSchemaIntegrityTests(unittest.TestCase):
    def test_recomputed_backtest_rejects_unknown_forged_metric_keys(self) -> None:
        result = _valid_backtest()
        for key in ("alpha", "annualized_return", "arbitrary_extra_metric"):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                forged = replace(result, metrics={**result.metrics, key: 0.5})
                forged = _with_backtest_identity(replace(forged, backtest_id=""))
                path = Path(directory) / "backtest.json"
                path.write_text(json.dumps(asdict(forged)), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "metric|field|schema"):
                    load_backtest(path)

    def test_recomputed_backtest_rejects_unknown_or_incomplete_period_fields(self) -> None:
        result = _valid_backtest()
        forged = replace(result, periods=[{**result.periods[0], "fabricated_return": 0.4}])
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "period|field|schema"):
            save_backtest(forged, Path(tempfile.gettempdir()) / "phase3-invalid-backtest.json")

        forged = replace(result, periods=[{
            key: value for key, value in result.periods[0].items() if key != "top_1_turnover"
        }])
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "period|top_1"):
            save_backtest(forged, Path(tempfile.gettempdir()) / "phase3-invalid-backtest.json")

    def test_recomputed_backtest_rejects_invalid_metric_and_period_types(self) -> None:
        result = _valid_backtest()
        for metrics, periods, recompute_identity in (
            ({**result.metrics, "benchmark_cumulative_return": True}, result.periods, True),
            ({**result.metrics, "benchmark_cumulative_return": float("nan")}, result.periods, False),
            (result.metrics, [{**result.periods[0], "decision_date": "not-a-date"}], True),
            (result.metrics, [{**result.periods[0], "top_1_selected": True}], True),
        ):
            with self.subTest(metrics=metrics, periods=periods):
                forged = replace(result, metrics=metrics, periods=periods)
                if recompute_identity:
                    forged = _with_backtest_identity(replace(forged, backtest_id=""))
                with self.assertRaises(ValueError):
                    save_backtest(forged, Path(tempfile.gettempdir()) / "phase3-invalid-backtest.json")

        forged = replace(result, schema_version=True)
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "schema"):
            save_backtest(forged, Path(tempfile.gettempdir()) / "phase3-invalid-backtest.json")

    def test_recomputed_backtest_rejects_nonfinite_or_unknown_scenario_fields(self) -> None:
        result = _valid_backtest()
        for scenarios in (
            {"confidence": "low", "bear": float("nan"), "base": 0.1, "bull": 0.2, "interpretation": "heuristic"},
            {"confidence": "low", "bear": None, "base": None, "bull": None, "interpretation": "heuristic", "alpha": 0.2},
        ):
            with self.subTest(scenarios=scenarios):
                forged = replace(result, scenarios=scenarios)
                with self.assertRaisesRegex(ValueError, "scenario|finite|field"):
                    save_backtest(forged, Path(tempfile.gettempdir()) / "phase3-invalid-backtest.json")

    def test_renderer_rejects_recomputed_fabricated_metric(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01", current_only=True)])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        forged = replace(backtest, metrics={**backtest.metrics, "fabricated_metric": 9.0})
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "metric|field|schema|artifact values|backtest values"):
            render_phase3_report(dataset=dataset, model=artifact, backtest=forged, output_format="json")

    def test_renderer_rejects_top_five_cumulative_return_forgery_with_recomputed_identity(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        self.assertIn("top_5_cumulative_return", backtest.metrics)
        forged = replace(
            backtest,
            metrics={**backtest.metrics, "top_5_cumulative_return": 99.0},
        )
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "metric|recomputed|period|backtest"):
            render_phase3_report(dataset=dataset, model=artifact, backtest=forged, output_format="json")

    def test_current_only_suppression_rejects_forged_cumulative_metric(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01", current_only=True)])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        forged = replace(
            backtest,
            metrics={**backtest.metrics, "top_5_cumulative_return": 99.0},
        )
        forged = _with_backtest_identity(replace(forged, backtest_id=""))
        with self.assertRaisesRegex(ValueError, "metric|recomputed|period|backtest"):
            render_phase3_report(dataset=dataset, model=artifact, backtest=forged, output_format="json")

    def test_model_training_window_selects_latest_eligible_decision_date(self) -> None:
        rows = [
            replace(_row("AAA", decision), features={name: value for name in FEATURE_SCHEMA})
            for decision, value in (
                ("2025-09-01", 1.0),
                ("2025-10-01", 2.0),
                ("2025-11-01", 3.0),
            )
        ]
        artifact = SpecializedRankingModel().fit(
            list(reversed(rows)), training_cutoff="2026-06-01", training_window=1,
        )
        self.assertEqual(artifact.training_rows, 1)
        self.assertEqual(artifact.training_window, 1)
        self.assertEqual(artifact.means, tuple(3.0 for _ in FEATURE_SCHEMA))

        default_artifact = SpecializedRankingModel().fit(rows, training_cutoff="2026-06-01")
        self.assertEqual(default_artifact.training_rows, 3)


def _market_snapshot_with_failed_member() -> tuple[object, list[UniverseMember]]:
    start = date(2024, 1, 1)
    days = [start + timedelta(days=index) for index in range(800)]
    members = [
        UniverseMember(
            ticker=ticker, effective_from="2024-01-01", effective_to="2026-09-16",
            membership_status="active", lookahead_bias_status="point_in_time",
            survivorship_bias_status="none", source="fixture", source_version="v1",
        )
        for ticker in ("AAA", "BBB", "CCC", "DDD", "FAIL")
    ]
    bars: dict[str, list[MarketBar]] = {}
    for ticker in ("AAA", "BBB", "CCC", "DDD", "^GSPC"):
        bars[ticker] = [MarketBar(
            ticker=ticker, observation_date=day.isoformat(),
            open=100.0 + index, high=101.0 + index, low=99.0 + index,
            close=100.0 + index, volume=1000.0,
            retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture",
        ) for index, day in enumerate(days)]
    snapshot = build_history_snapshot(
        bars | {"FAIL": []}, benchmark_ticker="^GSPC", universe=members,
        created_at="2026-09-16T00:00:00+00:00",
        ticker_failures={"FAIL": "ProviderUnavailable: no market bars"},
    )
    return snapshot, members


class MarketEligibilityTests(unittest.TestCase):
    def test_zero_history_member_is_absent_from_panel_and_current_predictions(self) -> None:
        snapshot, members = _market_snapshot_with_failed_member()
        dataset = build_monthly_panel(
            snapshot, as_of="2026-02-01", decision_dates=["2025-01-01", "2026-01-01"],
        )
        self.assertNotIn("FAIL", {row.ticker for row in dataset.rows})
        model = SpecializedRankingModel()
        artifact = model.fit(
            dataset.rows, training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        predictions = rank_current_candidates(dataset, artifact, as_of="2026-01-15")
        self.assertNotIn("FAIL", {item["ticker"] for item in predictions})

        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.refresh_pipeline.fetch_current_sp500_universe", return_value=members,
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_history_snapshot", return_value=snapshot,
        ):
            output = Path(directory) / "report.json"
            status = refresh_phase3(
                as_of="2026-01-15", start="2024-01-01", end="2026-02-01",
                cache_dir=Path(directory) / "cache", output=output,
                live=True, replay_only=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads((Path(f"{output}.manifest.json")).read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertNotIn("FAIL", {row["ticker"] for row in report["dataset"]["rows"]})
        self.assertNotIn("FAIL", {item["ticker"] for item in report["current_predictions"]})
        self.assertEqual(manifest["ticker_status"]["FAIL"], "failed")
        self.assertIn("FAIL", manifest["failures_by_ticker"])
        self.assertIn("FAIL", manifest["panel_excluded_by_ticker"])

    def test_provider_failed_member_is_absent_even_if_stale_bars_are_present(self) -> None:
        snapshot, members = _market_snapshot_with_failed_member()
        stale_bar = MarketBar(
            ticker="FAIL", observation_date="2025-12-01", open=100.0, high=100.0,
            low=100.0, close=100.0, volume=1000.0,
            retrieved_at="2026-09-16T00:00:00+00:00", source="stale", citation="stale",
        )
        failed_snapshot = build_history_snapshot(
            snapshot.bars_by_ticker | {"FAIL": [stale_bar]},
            benchmark_ticker=snapshot.benchmark_ticker, universe=members,
            created_at=snapshot.created_at,
            ticker_failures={"FAIL": "ProviderUnavailable: stale response rejected"},
        )
        dataset = build_monthly_panel(
            failed_snapshot, as_of="2026-02-01", decision_dates=["2026-01-01"],
        )
        self.assertNotIn("FAIL", {row.ticker for row in dataset.rows})


class ProviderPayloadTests(unittest.TestCase):
    def test_malformed_yahoo_chart_containers_raise_controlled_errors(self) -> None:
        payloads = (
            {"chart": {"result": []}},
            {"chart": {"result": {"meta": {}}}},
            {"chart": None},
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises((ValueError, RuntimeError)):
                _parse_yahoo(
                    "AAA", payload, "2026-01-01T00:00:00+00:00", "https://example.invalid",
                    start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                )

    def test_malformed_sec_nested_containers_raise_controlled_errors(self) -> None:
        payloads = (
            {"facts": None},
            {"facts": []},
            {"facts": {"us-gaap": []}},
            {"facts": {"us-gaap": {"Revenues": {"units": []}}}},
        )
        for payload in payloads:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                parse_sec_company_facts("AAA", payload)
        with self.assertRaises(ValueError):
            normalize_sec_company_facts(
                {"cik": "0000000001", "facts": None},
                ticker="AAA", cik="0000000001", retrieved_at="2026-01-01",
            )

    def test_malformed_provider_refresh_writes_structured_unavailable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.refresh_pipeline.fetch_current_sp500_universe", return_value=[],
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_history_snapshot",
            side_effect=ValueError("Yahoo chart result has invalid shape"),
        ):
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-01-15", start="2024-01-01", end="2026-01-01",
                cache_dir=root / "cache", output=output, live=True, replay_only=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("Yahoo chart result", report["reason"])
        self.assertEqual(manifest["report_status"], "unavailable")
        self.assertTrue(manifest["failures"])


class StatusContractTests(unittest.TestCase):
    def test_insufficient_backtest_status_is_preserved_by_bound_report(self) -> None:
        dataset = _dataset([_row("AAA", "2026-01-01")])
        backtest = walk_forward_backtest(dataset, validation_dates=["2026-01-01"])
        self.assertEqual(backtest.metrics["availability_status"], "unavailable_insufficient_data")
        report = render_phase3_report(
            dataset=dataset, model=backtest, backtest=backtest, output_format="json",
        )
        self.assertEqual(report["status"], "unavailable_insufficient_data")

    def test_producer_returns_exit_two_for_insufficient_backtest(self) -> None:
        snapshot, members = _market_snapshot_with_failed_member()
        original_backtest = walk_forward_backtest

        def insufficient_backtest(dataset: object, model: object, **_: object) -> BacktestResult:
            first_date = min(row.decision_date for row in dataset.rows)  # type: ignore[union-attr]
            return original_backtest(dataset, model=model, validation_dates=[first_date])  # type: ignore[arg-type]

        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.refresh_pipeline.fetch_current_sp500_universe", return_value=members,
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_history_snapshot", return_value=snapshot,
        ), patch(
            "nisa_quant.refresh_pipeline.walk_forward_backtest", side_effect=insufficient_backtest,
        ):
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-01-15", start="2024-01-01", end="2026-02-01",
                cache_dir=root / "cache", output=output, live=True, replay_only=False,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable_insufficient_data")
        self.assertEqual(manifest["report_status"], "unavailable_insufficient_data")

    def test_current_only_descriptive_report_keeps_predictions_and_exit_zero_contract(self) -> None:
        dataset = _dataset([_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01", current_only=True)])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        predictions = rank_current_candidates(dataset, artifact, as_of="2026-01-15")
        report = render_phase3_report(
            dataset=dataset, model=artifact, backtest=backtest,
            current_predictions=predictions, current_predictions_as_of="2026-01-15",
            output_format="json",
        )
        self.assertEqual(report["status"], "unavailable_insufficient_data")
        self.assertTrue(report["current_predictions"])
        self.assertTrue(report["performance_claims_suppressed"])
        self.assertEqual(2 if report["status"] == "unavailable_insufficient_data" else 0, 2)


if __name__ == "__main__":
    unittest.main()
