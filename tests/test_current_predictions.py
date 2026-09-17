from __future__ import annotations

import json
import hashlib
import math
import tempfile
import unittest
from dataclasses import asdict, replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.refresh_pipeline import refresh_phase3, unavailable_refresh_report
from nisa_quant.quant_report import render_phase3_report
from nisa_quant.ranking_model import (
    SpecializedRankingModel,
    current_prediction_freshness,
    rank_current_candidates,
)
from nisa_quant.training_dataset import TrainingDataset
from nisa_quant.walk_forward_evaluation import walk_forward_backtest


def _row(
    ticker: str, decision_date: str, *, current_only: bool,
    market_observation_date: str | None = "decision",
) -> SimpleNamespace:
    endpoint = (date.fromisoformat(decision_date) + timedelta(days=90)).isoformat()
    metadata = {"current_snapshot_only": current_only} if current_only else {}
    if current_only and market_observation_date is not None:
        metadata["market_observation_date"] = (
            decision_date if market_observation_date == "decision" else market_observation_date
        )
    return SimpleNamespace(
        ticker=ticker,
        decision_date=decision_date,
        features={name: 0.0 for name in FEATURE_SCHEMA},
        targets={
            "target_1m_interval_start": decision_date,
            "target_1m_interval_end": endpoint,
            "target_1m_interval_semantics": "[start,end)",
            "target_1m_forward_endpoint": endpoint,
            "target_1m_return": 0.01,
            "target_1m_benchmark_return": 0.0,
            "target_1m_excess_return": 0.01,
            "target_1m_return_basis": "price_return",
            "target_3m_forward_endpoint": endpoint,
            "target_3m_interval_end": endpoint,
            "target_3m_return_basis": "price_return",
            "target_3m_excess_return": 0.01,
            "target_3m_label_availability": "available_at_endpoint",
        },
        target_metadata=metadata,
    )


class Phase3CurrentPredictionsR97Tests(unittest.TestCase):
    def test_missing_market_observation_date_cannot_be_fresh_at_later_as_of(self) -> None:
        training = _row("TRAIN", "2025-09-01", current_only=False)
        current = _row("AAA", "2026-01-01", current_only=True, market_observation_date=None)
        dataset = TrainingDataset(
            rows=[training, current],
            dataset_id="dataset-current-predictions-freshness-r165-regression",
            history_snapshot_id="history-current-predictions-freshness-r165-regression",
            benchmark_ticker="^GSPC",
            feature_schema=FEATURE_SCHEMA,
            membership_evidence_status="descriptive_survivor_selected_evidence",
        )
        artifact = SpecializedRankingModel().fit(
            [training], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )

        evidence = current_prediction_freshness(current, as_of="2026-09-16")

        self.assertEqual(evidence["status"], "unavailable")
        self.assertIsNone(evidence["market_observation_date"])
        self.assertNotIn("staleness_days", evidence)
        self.assertEqual(evidence["evidence_source"], "target_metadata.market_observation_date")
        self.assertIn("feature staleness cannot establish absolute freshness", evidence["reason"])
        self.assertEqual(rank_current_candidates(dataset, artifact, as_of="2026-09-16"), [])

    def test_current_only_rows_are_ranked_and_serialized_without_actions(self) -> None:
        rows = [
            _row("TRAIN", "2025-09-01", current_only=False),
            _row("BBB", "2026-01-01", current_only=True),
            _row("AAA", "2026-01-01", current_only=True),
            _row("FUTURE", "2026-02-01", current_only=True),
        ]
        dataset = TrainingDataset(
            rows=rows,
            dataset_id="dataset-current-predictions-r97",
            history_snapshot_id="history-current-predictions-r97",
            benchmark_ticker="^GSPC",
            feature_schema=FEATURE_SCHEMA,
            membership_evidence_status="descriptive_survivor_selected_evidence",
        )
        artifact = SpecializedRankingModel().fit(
            [rows[0]], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )
        current_predictions = rank_current_candidates(
            dataset, artifact, as_of="2026-01-15",
        )
        backtest = walk_forward_backtest(
            dataset, validation_dates=["2026-01-01"],
        )

        self.assertEqual([item["ticker"] for item in current_predictions], ["AAA", "BBB"])
        self.assertEqual([item["rank"] for item in current_predictions], [1, 2])
        for item in current_predictions:
            self.assertTrue(math.isfinite(item["model_score"]))
            self.assertEqual(item["predicted_3m_excess_return"], item["model_score"])
            self.assertEqual(item["target_horizon"], "3m")
            self.assertEqual(item["model_id"], artifact.model_id)
            self.assertEqual(item["dataset_id"], dataset.dataset_id)
            self.assertEqual(item["history_snapshot_id"], dataset.history_snapshot_id)
            self.assertEqual(item["benchmark"], "^GSPC")
            self.assertEqual(item["return_basis"], "price_return")
            self.assertEqual(item["decision_date"], "2026-01-01")
            self.assertEqual(item["evidence_status"], "descriptive_survivor_selected_evidence")

        report = render_phase3_report(
            dataset=dataset, model=artifact, backtest=backtest,
            current_predictions=current_predictions, current_predictions_as_of="2026-01-15",
            output_format="json",
        )
        report_json = json.dumps(report, sort_keys=True)
        report_markdown = render_phase3_report(
            dataset=dataset, model=artifact, backtest=backtest,
            current_predictions=current_predictions, current_predictions_as_of="2026-01-15",
            output_format="markdown",
        )
        self.assertEqual(report["current_predictions"], current_predictions)
        self.assertEqual(report["descriptive_current_survivor_ranking"], True)
        self.assertEqual(report["performance_claims_suppressed"], True)
        self.assertEqual(report["status"], "unavailable_insufficient_data")
        self.assertEqual(report["counter_evidence"], "not_calculated")
        self.assertEqual(report["invalidation"], "not_calculated")
        self.assertEqual(report["backtest"]["periods"], [])
        for serialized in (report_json, report_markdown):
            self.assertNotRegex(serialized, r"\b(?:BUY|HOLD|SELL)\b")
            self.assertNotRegex(serialized, r"\b(?:BUY|HOLD|SELL)\s+(?:NOW|IMMEDIATELY|TODAY)\b")

    def test_current_ranking_skips_unconvertible_feature_rows(self) -> None:
        training = _row("TRAIN", "2025-09-01", current_only=False)
        valid = _row("AAA", "2026-01-01", current_only=True)
        invalid = _row("BAD", "2026-01-01", current_only=True)
        invalid.features["revenue"] = 10**1000
        dataset = TrainingDataset(
            rows=[training, valid, invalid],
            dataset_id="dataset-current-predictions-invalid-r97",
            history_snapshot_id="history-current-predictions-invalid-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
            membership_evidence_status="descriptive_survivor_selected_evidence",
        )
        artifact = SpecializedRankingModel().fit(
            [training], training_cutoff="2026-01-01",
            dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker,
        )

        predictions = rank_current_candidates(dataset, artifact, as_of="2026-01-15")

        self.assertEqual([item["ticker"] for item in predictions], ["AAA"])

    def test_renderer_rejects_historical_current_prediction(self) -> None:
        rows = [_row("TRAIN", "2025-09-01", current_only=False), _row("AAA", "2026-01-01", current_only=True)]
        dataset = TrainingDataset(
            rows=rows, dataset_id="dataset-current-predictions-latest-r97",
            history_snapshot_id="history-current-predictions-latest-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
            membership_evidence_status="descriptive_survivor_selected_evidence",
        )
        artifact = SpecializedRankingModel().fit(
            [rows[0]], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        predictions = rank_current_candidates(dataset, artifact, as_of="2026-01-15")
        historical = dict(predictions[0])
        historical.update(ticker="TRAIN", decision_date="2025-09-01")
        backtest = walk_forward_backtest(dataset, validation_dates=["2026-01-01"])

        with self.assertRaisesRegex(ValueError, "latest"):
            render_phase3_report(
                dataset=dataset, model=artifact, backtest=backtest,
                current_predictions=[historical], current_predictions_as_of="2026-01-15",
                output_format="json",
            )

    def test_current_ranking_rejects_tampered_artifact_identity(self) -> None:
        row = _row("TRAIN", "2025-09-01", current_only=False)
        dataset = TrainingDataset(
            rows=[row], dataset_id="dataset-current-predictions-hash-r97",
            history_snapshot_id="history-current-predictions-hash-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        artifact = SpecializedRankingModel().fit(
            [row], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )

        with self.assertRaisesRegex(ValueError, "identity"):
            rank_current_candidates(
                dataset, replace(artifact, model_id="model-forged", content_hash="0" * 64), as_of="2026-01-01",
            )

    def test_current_ranking_rejects_non_three_month_artifact(self) -> None:
        row = _row("TRAIN", "2025-09-01", current_only=False)
        row.targets.update({
            "target_6m_excess_return": 0.02, "target_6m_forward_endpoint": "2025-12-01",
            "target_6m_interval_end": "2025-12-01", "target_6m_return_basis": "price_return",
            "target_6m_label_availability": "available_at_endpoint",
        })
        dataset = TrainingDataset(
            rows=[row], dataset_id="dataset-current-predictions-horizon-r97",
            history_snapshot_id="history-current-predictions-horizon-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA, target_horizon="6m",
        )
        artifact = SpecializedRankingModel().fit(
            [row], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
            target_horizon="6m",
        )

        with self.assertRaisesRegex(ValueError, "3m"):
            rank_current_candidates(dataset, artifact, as_of="2026-01-01")

    def test_unavailable_report_rejects_action_like_reason(self) -> None:
        with self.assertRaisesRegex(ValueError, "action"):
            unavailable_refresh_report(
                reason="BUY AAA", manifest={"failures": ["BUY AAA"]}, output_format="json",
            )

    def test_walk_forward_preserves_current_fit_artifact_binding(self) -> None:
        training = _row("TRAIN", "2025-09-01", current_only=False)
        later = _row("LATER", "2026-01-01", current_only=False)
        dataset = TrainingDataset(
            rows=[training, later], dataset_id="dataset-current-predictions-fold-r97",
            history_snapshot_id="history-current-predictions-fold-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        model = SpecializedRankingModel()
        current_artifact = model.fit(
            dataset.rows, training_cutoff="2026-03-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )

        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-02-01"])

        self.assertEqual(model.artifact, current_artifact)
        self.assertEqual(backtest.model_artifact_id, current_artifact.model_id)
        self.assertEqual(backtest.training_cutoff, current_artifact.training_cutoff)

    def test_replay_manifest_preserves_cached_sec_status(self) -> None:
        snapshot = SimpleNamespace(
            request_contract="cached-contract", snapshot_id="history-cached-sec-r97",
            benchmark_ticker="^GSPC", universe=[], coverage={}, sec_facts=[object()],
            ticker_failures={}, bars_by_ticker={},
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "cache"
            cache.mkdir()
            (cache / "history-cached.json").write_text(
                json.dumps({"request_contract": json.dumps({
                    "start_date": "2025-01-01", "end_date": "2026-01-01",
                })}),
                encoding="utf-8",
            )
            output = root / "report.json"
            with patch("nisa_quant.refresh_pipeline.load_history_snapshot", return_value=snapshot):
                refresh_phase3(
                    as_of="2026-01-01", start="2025-01-01", end="2026-01-01",
                    cache_dir=cache, output=output, live=False, replay_only=True,
                )
            manifest = json.loads(
                (root / "report.json.manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["sec_status"], "cached_partial")
        self.assertEqual(manifest["sec_rows"], 1)

    def test_renderer_cannot_downgrade_real_artifact_to_weak_binding(self) -> None:
        row = _row("TRAIN", "2025-09-01", current_only=False)
        dataset = TrainingDataset(
            rows=[row], dataset_id="dataset-current-predictions-strict-r97",
            history_snapshot_id="history-current-predictions-strict-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        artifact = SpecializedRankingModel().fit(
            [row], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=SpecializedRankingModel(), validation_dates=["2026-01-01"])
        model_payload = asdict(artifact)
        model_payload.pop("model_id")
        model_payload.pop("content_hash")

        with self.assertRaisesRegex(ValueError, "model artifact field"):
            render_phase3_report(
                dataset=dataset, model=model_payload, backtest=backtest, output_format="json",
            )

    def test_renderer_rejects_malformed_values_even_with_recomputed_hash(self) -> None:
        row = _row("TRAIN", "2025-09-01", current_only=False)
        dataset = TrainingDataset(
            rows=[row], dataset_id="dataset-current-predictions-types-r97",
            history_snapshot_id="history-current-predictions-types-r97",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        model = SpecializedRankingModel()
        artifact = model.fit(
            [row], training_cutoff="2026-01-01", dataset_id=dataset.dataset_id,
            history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker=dataset.benchmark_ticker,
        )
        backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01"])
        model_payload = asdict(artifact)
        model_payload["coefficients"] = ["malformed"] * len(artifact.coefficients)
        model_payload.pop("model_id")
        model_payload.pop("content_hash")
        model_hash = hashlib.sha256(
            json.dumps(model_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        model_payload["model_id"] = f"model-{model_hash[:20]}"
        model_payload["content_hash"] = model_hash
        backtest_payload = asdict(backtest)
        backtest_payload["model_artifact_id"] = model_payload["model_id"]
        backtest_payload.pop("backtest_id")
        backtest_hash = hashlib.sha256(
            json.dumps(backtest_payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()
        backtest_payload["backtest_id"] = f"backtest-{backtest_hash[:20]}"

        with self.assertRaisesRegex(ValueError, "values|non-finite|vector"):
            render_phase3_report(
                dataset=dataset, model=model_payload, backtest=backtest_payload, output_format="json",
            )


if __name__ == "__main__":
    unittest.main()
