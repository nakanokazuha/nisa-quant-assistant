from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import (
    HttpResponse,
    MarketBar,
    UniverseMember,
    _parse_yahoo,
    build_history_snapshot,
    fetch_history_snapshot,
    parse_sec_company_facts,
    save_history_snapshot,
)
from nisa_quant.phase3_producer import fetch_current_sp500_universe, refresh_phase3
from nisa_quant.phase3_reporting import render_phase3_report
from nisa_quant.ranking_model import SpecializedRankingModel, rank_current_candidates
from nisa_quant.return_targets import target_label_available_by
from nisa_quant.training_dataset import PanelRow, TrainingDataset, _identity
from nisa_quant.walk_forward_evaluation import walk_forward_backtest


def _targets(decision: str, *, endpoints: dict[str, str] | None = None) -> dict[str, object]:
    decision_date = date.fromisoformat(decision)
    endpoints = endpoints or {
        "3m": (decision_date + timedelta(days=90)).isoformat(),
        "6m": (decision_date + timedelta(days=180)).isoformat(),
        "12m": (decision_date + timedelta(days=365)).isoformat(),
    }
    one_month_endpoint = (decision_date + timedelta(days=31)).isoformat()
    values: dict[str, object] = {
        "target_1m_interval_start": decision,
        "target_1m_interval_end": one_month_endpoint,
        "target_1m_interval_semantics": "[start,end)",
        "target_1m_forward_endpoint": one_month_endpoint,
        "target_1m_return": 0.01,
        "target_1m_benchmark_return": 0.0,
        "target_1m_excess_return": 0.01,
        "target_1m_return_basis": "price_return",
    }
    for horizon, endpoint in endpoints.items():
        values.update({
            f"target_{horizon}_forward_endpoint": endpoint,
            f"target_{horizon}_interval_end": endpoint,
            f"target_{horizon}_return_basis": "price_return",
            f"target_{horizon}_excess_return": 0.02,
            f"target_{horizon}_label_availability": "available_at_endpoint",
        })
    return values


def _panel_row(ticker: str, decision: str, *, current_only: bool = False) -> PanelRow:
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


def _strict_dataset(rows: list[PanelRow]) -> TrainingDataset:
    membership = "descriptive_survivor_selected_evidence" if any(
        row.target_metadata and row.target_metadata.get("current_snapshot_only") for row in rows
    ) else "point_in_time_membership_evidence"
    history_id = "history-sol-r100"
    dataset_id = _identity(
        rows, history_id, "^GSPC", FEATURE_SCHEMA, "price_return",
        membership_evidence_status=membership,
    )
    return TrainingDataset(
        rows=rows,
        dataset_id=dataset_id,
        history_snapshot_id=history_id,
        benchmark_ticker="^GSPC",
        feature_schema=FEATURE_SCHEMA,
        membership_evidence_status=membership,
    )


class SolR100ReportIntegrityTests(unittest.TestCase):
    def test_renderer_rejects_serialized_dataset_mutation_with_original_identity(self) -> None:
        training = _panel_row("TRAIN", "2025-09-01")
        dataset = _strict_dataset([training])
        model = SpecializedRankingModel()
        artifact = model.fit(
            dataset.rows, training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        forged = replace(dataset, rows=[replace(training, features={**training.features, "momentum_1m": 99.0})])

        with self.assertRaisesRegex(ValueError, "dataset.*identity|content/hash"):
            render_phase3_report(dataset=forged, model=artifact, backtest=backtest, output_format="json")

    def test_renderer_recomputes_current_prediction_score_from_bound_model(self) -> None:
        training = _panel_row("TRAIN", "2025-09-01")
        current = _panel_row("AAA", "2026-01-01", current_only=True)
        dataset = _strict_dataset([training, current])
        model = SpecializedRankingModel()
        artifact = model.fit(
            [training], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        prediction = rank_current_candidates(dataset, artifact, as_of="2026-02-01")[0]
        forged = dict(prediction, model_score=999.0, predicted_3m_excess_return=999.0)

        with self.assertRaisesRegex(ValueError, "score|model output|prediction"):
            render_phase3_report(
                dataset=dataset, model=artifact, backtest=backtest,
                current_predictions=[forged], current_predictions_as_of="2026-02-01",
                output_format="json",
            )


class SolR100HorizonTests(unittest.TestCase):
    def test_fit_purges_future_labels_for_selected_6m_and_12m_horizons(self) -> None:
        row = SimpleNamespace(
            ticker="AAA", decision_date="2025-09-01",
            features={name: 0.1 for name in FEATURE_SCHEMA},
            targets=_targets("2025-09-01"), target_metadata=None,
        )
        for horizon in ("6m", "12m"):
            with self.subTest(horizon=horizon):
                with self.assertRaisesRegex(ValueError, "no observable"):
                    SpecializedRankingModel().fit([row], training_cutoff="2026-01-01", target_horizon=horizon)

    def test_target_label_availability_uses_the_requested_horizon(self) -> None:
        row = SimpleNamespace(
            ticker="AAA", decision_date="2025-09-01",
            features={name: 0.1 for name in FEATURE_SCHEMA},
            targets=_targets("2025-09-01"), target_metadata=None,
        )
        self.assertFalse(target_label_available_by(row, "2026-01-01", "6m"))
        self.assertFalse(target_label_available_by(row, "2026-01-01", "12m"))


class SolR100ProviderTests(unittest.TestCase):
    def test_yahoo_null_events_is_a_controlled_value_error(self) -> None:
        payload = {
            "chart": {"result": [{
                "meta": {"symbol": "AAA"}, "timestamp": [1767225600], "events": None,
                "indicators": {"quote": [{
                    "open": [100], "high": [100], "low": [100], "close": [100], "volume": [1],
                }]},
            }]},
        }
        with self.assertRaisesRegex(ValueError, "events"):
            _parse_yahoo(
                "AAA", payload, "2026-01-01T00:00:00+00:00", "https://example.invalid",
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
            )

    def test_sec_null_facts_is_a_controlled_value_error(self) -> None:
        with self.assertRaisesRegex(ValueError, "facts"):
            parse_sec_company_facts("AAA", {"facts": None})

    def test_partial_provider_snapshot_is_not_written_to_replay_cache(self) -> None:
        payload = {
            "chart": {"result": [{
                "meta": {"symbol": "^GSPC"}, "timestamp": [1767225600],
                "indicators": {"quote": [{
                    "open": [100], "high": [100], "low": [100], "close": [100], "volume": [1],
                }]},
            }]},
        }

        def http_get(url: str, **_kwargs: object) -> HttpResponse:
            if "/AAA?" in url:
                raise OSError("AAA unavailable")
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            snapshot = fetch_history_snapshot(
                tickers=["AAA"], benchmark_ticker="^GSPC", start_date="2026-01-01", end_date="2026-01-02",
                cache_dir=cache_dir, retrieved_at="2026-01-03T00:00:00+00:00", http_get=http_get, allow_partial=True,
            )
            self.assertIn("AAA", snapshot.ticker_failures)
            self.assertEqual(list(cache_dir.glob("history-*.json")), [])

    def test_full_sp500_fetch_rejects_a_truncated_100_row_table(self) -> None:
        rows = "".join(f"<tr><td>T{index:03d}</td><td>Security {index}</td></tr>" for index in range(100))
        html = f"<table class='wikitable'><tr><th>Symbol</th><th>Security</th></tr>{rows}</table>"
        with self.assertRaisesRegex(ValueError, "threshold"):
            fetch_current_sp500_universe(
                retrieved_at="2026-09-16T00:00:00+00:00",
                http_get=lambda *_args, **_kwargs: HttpResponse(200, html.encode()),
            )


def _request_contract(start: date, end: date) -> str:
    return json.dumps({
        "schema": "phase3-market-request", "schema_version": 1,
        "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
        "interval": "1d", "events": "div,splits", "return_basis": "price_return",
        "timeout": 15, "retries": 2, "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))


class SolR100ReplayTests(unittest.TestCase):
    def test_producer_invalid_range_writes_structured_unavailable_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-09-16", start="2026-02-01", end="2026-01-01",
                cache_dir=root / "cache", output=output, replay_only=True,
            )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("range", report["reason"])
        self.assertEqual(manifest["report_status"], "unavailable")

    def test_replay_range_mismatch_is_unavailable_before_cached_data_is_used(self) -> None:
        start = date(2024, 1, 1)
        end = date(2025, 1, 1)
        days = [start + timedelta(days=index) for index in range(300)]
        bars = {
            ticker: [MarketBar(
                ticker, day.isoformat(), 100.0, 100.0 + index / 100, 100.0, 100.0 + index / 100,
                1000.0, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture",
                citation="fixture",
            ) for index, day in enumerate(days)]
            for ticker in ("AAA", "^GSPC")
        }
        contract = _request_contract(start, end)
        snapshot = build_history_snapshot(
            bars, benchmark_ticker="^GSPC",
            universe=[UniverseMember("AAA", None, None, "active", "point_in_time", "none", "fixture", "v1")],
            created_at="2026-09-16T00:00:00+00:00", request_contract=contract,
        )

        with self.subTest("mismatch"):
            import tempfile
            with tempfile.TemporaryDirectory() as directory:
                cache_dir = Path(directory) / "cache"
                cache_dir.mkdir()
                cache_key = hashlib.sha256(contract.encode()).hexdigest()[:24]
                save_history_snapshot(snapshot, cache_dir / f"history-{cache_key}.json")
                output = Path(directory) / "report.json"
                status = refresh_phase3(
                    as_of="2024-10-01", start="2024-02-01", end="2025-01-01",
                    cache_dir=cache_dir, output=output, replay_only=True,
                )
                report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 2)
        self.assertEqual(report["status"], "unavailable")
        self.assertIn("range", report["reason"])


class SolR100StatusTests(unittest.TestCase):
    def test_current_only_backtest_is_successful_descriptive_exit(self) -> None:
        dataset = _strict_dataset([
            _panel_row("TRAIN", "2025-09-01"),
            _panel_row("AAA", "2026-01-01", current_only=True),
        ])
        with tempfile.TemporaryDirectory() as directory:
            dataset_path = Path(directory) / "dataset.json"
            output_path = Path(directory) / "backtest.json"
            dataset_path.write_text(json.dumps(asdict(dataset)), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, "-m", "nisa_quant", "phase3-backtest",
                 "--dataset", str(dataset_path), "--output", str(output_path),
                 "--validation-date", "2026-01-01"],
                check=False, capture_output=True, text=True,
                env={"PYTHONPATH": str(Path(__file__).parents[1] / "src")},
            )
            backtest = json.loads(output_path.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(backtest["metrics"]["availability_status"], "unavailable_insufficient_data")

    def test_release_docs_define_phase3_status_exit_and_exposed_refresh_contract(self) -> None:
        root = Path(__file__).parents[1]
        text = "\n".join((root / "docs" / name).read_text(encoding="utf-8") for name in (
            "README.md", "plan.md", "phase3-operations.md",
        ))
        for phrase in (
            "phase3-refresh", "available_descriptive", "unavailable_insufficient_data",
            "exit 0", "exit 2", "full_current_sp500", "--limit",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
