from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import (
    MarketBar,
    UniverseMember,
    _content_hash,
    build_history_snapshot,
    load_history_snapshot,
)
from nisa_quant.refresh_pipeline import refresh_phase3
from nisa_quant.quant_report import render_phase3_report
from nisa_quant.ranking_model import SpecializedRankingModel
from nisa_quant.training_dataset import PanelRow, TrainingDataset, _identity
from nisa_quant.walk_forward_evaluation import (
    BacktestResult,
    _with_backtest_identity,
    walk_forward_backtest,
)


def _row(ticker: str, decision: str) -> PanelRow:
    day = date.fromisoformat(decision)
    endpoint = (day + timedelta(days=28)).isoformat()
    targets: dict[str, object] = {
        "target_1m_interval_start": decision,
        "target_1m_interval_end": endpoint,
        "target_1m_interval_semantics": "[start,end)",
        "target_1m_forward_endpoint": endpoint,
        "target_1m_return": 0.02,
        "target_1m_benchmark_return": 0.01,
        "target_1m_excess_return": 0.01,
        "target_1m_return_basis": "price_return",
    }
    for horizon, days in (("3m", 90), ("6m", 180), ("12m", 365)):
        horizon_end = (day + timedelta(days=days)).isoformat()
        targets.update({
            f"target_{horizon}_forward_endpoint": horizon_end,
            f"target_{horizon}_interval_end": horizon_end,
            f"target_{horizon}_return_basis": "price_return",
            f"target_{horizon}_excess_return": 0.01,
            f"target_{horizon}_label_availability": "available_at_endpoint",
        })
    return PanelRow(ticker, decision, {name: 0.1 for name in FEATURE_SCHEMA}, targets, {})


def _dataset() -> TrainingDataset:
    rows = [_row("TRAIN", "2025-09-01"), _row("AAA", "2026-01-01")]
    return TrainingDataset(
        rows=rows,
        dataset_id=_identity(rows, "history-r119", "^GSPC", FEATURE_SCHEMA, "price_return"),
        history_snapshot_id="history-r119",
        benchmark_ticker="^GSPC",
        feature_schema=FEATURE_SCHEMA,
    )


def _forged_backtest(result: BacktestResult, period: dict[str, object]) -> dict[str, object]:
    periods = [period]
    benchmark = period["benchmark_return"]
    top_return = period["top_1_return"]
    assert isinstance(benchmark, (int, float))
    assert isinstance(top_return, (int, float))
    benchmark_cumulative = (1 + benchmark) - 1
    top_cumulative = (1 + top_return) - 1
    metrics = {
        **result.metrics,
        "benchmark_cumulative_return": benchmark_cumulative,
        "top_1_cumulative_return": top_cumulative,
        "top_1_benchmark_relative_return": top_cumulative - benchmark_cumulative,
        "benchmark_relative_return": None,
    }
    payload = asdict(replace(
        result,
        periods=periods,
        metrics=metrics,
        backtest_id="",
    ))
    digest = hashlib.sha256(json.dumps(
        {key: value for key, value in payload.items() if key != "backtest_id"},
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()).hexdigest()
    payload["backtest_id"] = f"backtest-{digest[:20]}"
    return payload


class PeriodIntegrityR119Tests(unittest.TestCase):
    def test_renderer_rejects_rehashed_period_value_from_bound_dataset_and_model(self) -> None:
        dataset = _dataset()
        model = SpecializedRankingModel()
        artifact = model.fit(
            [dataset.rows[0]],
            training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        result = walk_forward_backtest(
            dataset, model=model, validation_dates=["2026-01-01"], top_ks=(1,),
        )
        forged_period = {**result.periods[0], "top_1_return": 0.75}
        forged = _forged_backtest(result, forged_period)

        with self.assertRaisesRegex(ValueError, "period|canonical|recomputed|match"):
            render_phase3_report(
                dataset=dataset,
                model=artifact,
                backtest=forged,
                output_format="json",
            )


def _request_contract() -> str:
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)

    def epoch(value: date) -> int:
        return int(datetime.combine(value, datetime.min.time(), timezone.utc).timestamp())

    return json.dumps({
        "schema": "phase3-market-request",
        "schema_version": 1,
        "asset_tickers": ["AAA"],
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


def _bar(ticker: str, observation_date: str) -> MarketBar:
    return MarketBar(
        ticker=ticker,
        observation_date=observation_date,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1000.0,
        retrieved_at="2026-09-16T00:00:00+00:00",
        source="fixture",
        citation="fixture",
    )


class ReplayBoundsR119Tests(unittest.TestCase):
    def test_rehashed_cache_rejects_out_of_range_asset_bar_and_producer_reports_bounds_failure(self) -> None:
        contract = _request_contract()
        snapshot = build_history_snapshot(
            {
                "AAA": [_bar("AAA", "2026-01-15")],
                "^GSPC": [_bar("^GSPC", "2026-01-15")],
            },
            benchmark_ticker="^GSPC",
            universe=[UniverseMember(
                ticker="AAA",
                effective_from=None,
                effective_to=None,
                membership_status="active",
                lookahead_bias_status="point_in_time",
                survivorship_bias_status="none",
                source="fixture",
                source_version="v1",
            )],
            created_at="2026-09-16T00:00:00+00:00",
            request_contract=contract,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / f"history-{hashlib.sha256(contract.encode()).hexdigest()[:24]}.json"
            forged = replace(
                snapshot,
                bars_by_ticker={
                    **snapshot.bars_by_ticker,
                    "AAA": [*snapshot.bars_by_ticker["AAA"], _bar("AAA", "2026-02-15")],
                },
                snapshot_id="",
            )
            forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")
            cache.write_text(
                json.dumps(asdict(forged), sort_keys=True),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "outside.*bounds|date.*range|request"):
                load_history_snapshot(cache, expected_request_contract=contract)

            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-01-31",
                start="2026-01-01",
                end="2026-01-31",
                cache_dir=root,
                output=output,
                live=False,
                replay_only=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads(Path(f"{output}.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("outside", report["reason"])
        self.assertTrue(any("outside" in failure for failure in manifest["failures"]))


if __name__ == "__main__":
    unittest.main()
