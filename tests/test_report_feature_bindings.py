from __future__ import annotations

import json
import unittest

from nisa_quant.feature_engineering import FEATURE_SCHEMA, build_features
from nisa_quant.historical_market_data import MarketBar, SecFact, UniverseMember, build_history_snapshot
from nisa_quant.quant_report import render_phase3_report
from nisa_quant.training_dataset import build_monthly_panel


def _dataset(*, dataset_id: str = "dataset-a", history_id: str = "history-a", schema=None, current_only: bool = False) -> dict:
    row = {"ticker": "AAA", "decision_date": "2026-01-01", "features": {}, "targets": {}}
    if current_only:
        row["target_metadata"] = {"current_snapshot_only": True}
    else:
        row["target_metadata"] = {}
    return {
        "rows": [row],
        "dataset_id": dataset_id,
        "history_snapshot_id": history_id,
        "benchmark_ticker": "^GSPC",
        "feature_schema": list(FEATURE_SCHEMA if schema is None else schema),
        "return_basis": "price_return",
    }


def _artifacts(*, current_only: bool = False, schema=None) -> tuple[dict, dict, dict]:
    dataset = _dataset(current_only=current_only, schema=schema)
    model = {
        "model_version": "model-a",
        "dataset_id": dataset["dataset_id"],
        "history_snapshot_id": dataset["history_snapshot_id"],
        "feature_schema": list(dataset["feature_schema"]),
        "return_basis": "price_return",
    }
    backtest = {
        "model_version": "model-a",
        "dataset_id": dataset["dataset_id"],
        "history_snapshot_id": dataset["history_snapshot_id"],
        "benchmark_ticker": dataset["benchmark_ticker"],
        "feature_schema": list(dataset["feature_schema"]),
        "return_basis": "price_return",
        "metrics": {
            "top_5_cumulative_return": 0.1,
            "top_5_benchmark_relative_return": 0.05,
            "alpha": 0.05,
            "excess_return": 0.05,
            "evidence_status": "descriptive_survivor_selected_evidence" if current_only else "point_in_time_membership_evidence",
            "availability_status": "available_descriptive" if current_only else "available",
        },
        "periods": [{"top_5_return": 0.1, "benchmark_return": 0.05}],
    }
    return dataset, model, backtest


class Phase3ReleaseBlockerLocalTests(unittest.TestCase):
    def test_report_rejects_model_a_with_model_b_backtest(self) -> None:
        dataset, model, backtest = _artifacts()
        backtest["model_version"] = "model-b"
        with self.assertRaisesRegex(ValueError, "model version"):
            render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="json")

    def test_report_rejects_missing_and_wrong_feature_schema_binding(self) -> None:
        dataset, model, backtest = _artifacts()
        model.pop("history_snapshot_id")
        with self.assertRaisesRegex(ValueError, "history_snapshot_id"):
            render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="json")

        _, model, backtest = _artifacts(schema=(*FEATURE_SCHEMA[:-1], "wrong_feature"))
        with self.assertRaisesRegex(ValueError, "feature schema"):
            render_phase3_report(dataset=_dataset(), model=model, backtest=backtest, output_format="json")

    def test_survivor_report_omits_all_performance_claim_fields(self) -> None:
        dataset, model, backtest = _artifacts(current_only=True)
        report = render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="json")
        metrics = report["backtest"]["metrics"]
        forbidden = {
            "top_5_cumulative_return", "top_5_benchmark_relative_return", "alpha", "excess_return",
            "top_5_return", "benchmark_return",
        }
        self.assertTrue(forbidden.isdisjoint(metrics))
        self.assertEqual(report["backtest"]["periods"], [])
        markdown = render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="markdown")
        self.assertIn("Descriptive survivor-selected evidence only", markdown)

    def test_report_uses_interval_convention_separate_from_return_basis(self) -> None:
        dataset, model, backtest = _artifacts()
        markdown = render_phase3_report(dataset=dataset, model=model, backtest=backtest, output_format="markdown")
        self.assertIn("[start,end)", markdown)
        self.assertIn("next monthly decision date", markdown)
        self.assertNotIn("Portfolio interval: `price_return`", markdown)

    def test_sec_mismatched_fiscal_duration_is_not_used_for_growth(self) -> None:
        facts = [
            SecFact("AAA", "us-gaap:Revenues", "USD", 120.0, "2025-01-01", "2025-12-31", "2026-01-15", "10-K", 2025, "FY", "acc-new", "CY2025", "2026-01-15T00:00:00+00:00", "SEC XBRL Company Facts", "sec://new"),
            SecFact("AAA", "us-gaap:Revenues", "USD", 20.0, "2025-07-01", "2025-09-30", "2025-10-20", "10-Q", 2025, "Q3", "acc-old", "CY2025Q3", "2025-10-20T00:00:00+00:00", "SEC XBRL Company Facts", "sec://old"),
        ]
        result = build_features("AAA", [MarketBar("AAA", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")], decision_date="2026-01-31", sec_facts=facts)
        self.assertIsNone(result.values["revenue_growth"])

    def test_sec_feature_keeps_source_and_context_identity(self) -> None:
        fact = SecFact("AAA", "us-gaap:Revenues", "USD", 120.0, "2025-01-01", "2025-12-31", "2026-01-15", "10-K", 2025, "FY", "acc-new", "CY2025", "2026-01-15T00:00:00+00:00", "SEC XBRL Company Facts", "sec://new")
        result = build_features("AAA", [MarketBar("AAA", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")], decision_date="2026-01-31", sec_facts=[fact])
        self.assertIn("sec://new", result.sec_source_ids)
        self.assertTrue(any("FY" in context for context in result.sec_context_ids))

    def test_sec_unsupported_typed_fact_is_rejected(self) -> None:
        fact = SecFact("AAA", "us-gaap:Assets", "USD", 120.0, None, "2025-12-31", "2026-01-15", "10-K")
        with self.assertRaisesRegex(ValueError, "unsupported SEC"):
            build_features("AAA", [], decision_date="2026-01-31", sec_facts=[fact])

    def test_dataset_preserves_sec_identity_and_survivor_status_from_history(self) -> None:
        member = UniverseMember("AAA", None, None, "active", "current_snapshot_only", "survivorship_risk_disclosed", "test universe", "v1")
        fact = SecFact("AAA", "us-gaap:Revenues", "USD", 120.0, "2025-01-01", "2025-12-31", "2026-01-15", "10-K", 2025, "FY", "acc-new", "CY2025", "2026-01-15T00:00:00+00:00", "SEC XBRL Company Facts", "sec://new")
        snapshot = build_history_snapshot(
            {"AAA": [MarketBar("AAA", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")], "^GSPC": [MarketBar("^GSPC", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")]},
            benchmark_ticker="^GSPC", universe=[member], sec_facts=[fact], created_at="2026-01-02T00:00:00+00:00",
            request_contract='{"asset_tickers":["AAA"],"benchmark_ticker":"^GSPC","return_basis":"price_return"}',
        )
        dataset = build_monthly_panel(snapshot, decision_dates=["2026-02-01"])
        self.assertEqual(dataset.membership_evidence_status, "descriptive_survivor_selected_evidence")
        self.assertIn("sec://new", dataset.sec_source_ids)
        self.assertIn("sec://new", dataset.rows[0].target_metadata["sec_source_ids"])


if __name__ == "__main__":
    unittest.main()
