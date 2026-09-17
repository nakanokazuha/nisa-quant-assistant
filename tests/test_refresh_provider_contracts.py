from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse
from unittest.mock import patch

from nisa_quant.feature_engineering import FEATURE_SCHEMA
from nisa_quant.historical_market_data import HttpResponse, MarketBar, SecFact, UniverseMember, build_history_snapshot, fetch_sec_company_facts_for_ticker, load_history_snapshot, parse_sec_company_facts, fetch_history_snapshot, save_history_snapshot
from nisa_quant.ranking_model import SpecializedRankingModel, load_model, save_model
from nisa_quant.training_dataset import TrainingDataset, _identity, build_monthly_panel, load_training_dataset, save_training_dataset
from nisa_quant.walk_forward_evaluation import load_backtest, save_backtest, walk_forward_backtest, walk_forward_splits
from nisa_quant.refresh_pipeline import parse_current_sp500_html
from nisa_quant.refresh_pipeline import refresh_phase3
from nisa_quant.quant_report import render_phase3_report


def _row(name: str, decision: str, endpoint: str, *, metadata: dict | None = None):
    return SimpleNamespace(
        ticker=name,
        decision_date=decision,
        features={feature: 0.1 for feature in FEATURE_SCHEMA},
        targets={
            "target_1m_interval_start": decision,
            "target_1m_interval_end": "2026-01-31",
            "target_1m_interval_semantics": "[start,end)",
            "target_1m_forward_endpoint": "2026-01-31",
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


class Phase3AdditionalReleaseBlockerTests(unittest.TestCase):
    def test_live_manifest_records_partial_market_coverage_and_blocks_below_threshold(self):
        universe = [
            UniverseMember(ticker, None, None, "active", "current_snapshot_only", "survivorship_risk_disclosed", "test", "v1")
            for ticker in ("AAA", "BBB", "CCC")
        ]
        snapshot = SimpleNamespace(
            request_contract="", snapshot_id="history-partial", benchmark_ticker="^GSPC",
            universe=universe,
            bars_by_ticker={
                "AAA": [MarketBar("AAA", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")],
                "BBB": [MarketBar("BBB", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")],
                "CCC": [],
                "^GSPC": [MarketBar("^GSPC", "2026-01-01", 1, 1, 1, 1, 1, retrieved_at="2026-09-16T00:00:00+00:00", source="fixture", citation="fixture://r146")],
            },
            coverage={
                "AAA": {"row_count": 1}, "BBB": {"row_count": 1}, "CCC": {"row_count": 0},
                "^GSPC": {"row_count": 1},
            },
            sec_facts=[],
            ticker_failures={"CCC": "Yahoo returned no chart data for CCC"},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            with patch("nisa_quant.refresh_pipeline.fetch_current_sp500_universe", return_value=universe), patch(
                "nisa_quant.refresh_pipeline.fetch_history_snapshot", return_value=snapshot,
            ):
                status = refresh_phase3(
                    as_of="2026-09-16", start="2026-01-01", end="2026-09-16",
                    cache_dir=Path(directory) / "cache", output=output,
                    live=True, replay_only=False, limit=3,
                )
            report = json.loads(output.read_text())
            manifest = json.loads((Path(directory) / "report.json.manifest.json").read_text())

        self.assertEqual(status, 2)
        self.assertIn("market coverage", report["reason"])
        self.assertEqual(manifest["ticker_status"]["CCC"], "failed")
        self.assertIn("CCC", manifest["failures_by_ticker"])

    def test_sec_ticker_mapping_is_official_and_company_facts_identity_is_checked(self):
        mapping = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        facts_payload = {"cik": 320193, "entityName": "Apple Inc.", "facts": {}}
        calls: list[str] = []

        def http_get(url: str, **_: object) -> HttpResponse:
            calls.append(url)
            return HttpResponse(200, json.dumps(mapping if url.endswith("company_tickers.json") else facts_payload).encode())

        facts = fetch_sec_company_facts_for_ticker("AAPL", contact="researcher@example.com", http_get=http_get, retrieved_at="2026-09-16T00:00:00+00:00")
        self.assertEqual(facts, [])
        self.assertEqual(calls[0], "https://www.sec.gov/files/company_tickers.json")
        self.assertTrue(calls[1].endswith("CIK0000320193.json"))

    def test_offline_producer_fixture_runs_history_dataset_model_backtest_scenarios_and_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            start = date(2023, 1, 1)
            days = [start + timedelta(days=index) for index in range(1250)]
            bars = {}
            for ticker, multiplier in (("AAA", 1.0), ("BBB", 0.9), ("^GSPC", 0.8)):
                bars[ticker] = [
                    MarketBar(ticker, day.isoformat(), 100 + multiplier * index, 100 + multiplier * index,
                              100 + multiplier * index, 100 + multiplier * index, 1000 + index,
                              100 + multiplier * index, retrieved_at="2026-09-16T00:00:00+00:00",
                              source="fixture-market", citation="tests/fixtures/offline_refresh_fixture.json")
                    for index, day in enumerate(days)
                ]
            contract = json.dumps({
                "schema": "phase3-market-request", "schema_version": 1,
                "asset_tickers": ["AAA", "BBB"], "benchmark_ticker": "^GSPC",
                "start_date": start.isoformat(), "end_date": days[-1].isoformat(),
                "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
                "period2": int(datetime.combine(days[-1] + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
                "interval": "1d", "events": "div,splits", "return_basis": "price_return",
                "timeout": 15, "retries": 2, "user_agent": "nisa-quant-assistant/phase3-read-only",
                "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
            }, sort_keys=True, separators=(",", ":"))
            history = build_history_snapshot(
                bars, benchmark_ticker="^GSPC",
                universe=[UniverseMember("AAA", None, None, "active", "point_in_time", "none", "fixture", "v1"), UniverseMember("BBB", None, None, "active", "point_in_time", "none", "fixture", "v1")],
                sec_facts=[
                    SecFact(ticker, "us-gaap:Revenues", "USD", value, None, f"{year}-12-31", f"{year + 1}-01-15", "10-K", year, "FY", f"acc-{ticker}-{year}", f"CY{year}", "2026-09-16T00:00:00+00:00", "SEC XBRL Company Facts", f"fixture-sec:{ticker}:{year}")
                    for ticker, value in (("AAA", 100.0), ("BBB", 90.0))
                    for year in (2023, 2024)
                ] + [
                    SecFact(ticker, "us-gaap:NetIncomeLoss", "USD", value / 10, None, f"{year}-12-31", f"{year + 1}-01-15", "10-K", year, "FY", f"acc-income-{ticker}-{year}", f"CY{year}", "2026-09-16T00:00:00+00:00", "SEC XBRL Company Facts", f"fixture-sec:{ticker}:income:{year}")
                    for ticker, value in (("AAA", 100.0), ("BBB", 90.0))
                    for year in (2023, 2024)
                ],
                created_at="2026-09-16T00:00:00+00:00", source_snapshot_ids=["fixture-market:AAA", "fixture-market:BBB", "fixture-market:^GSPC"], request_contract=contract,
            )
            history_path = root / "history.json"
            save_history_snapshot(history, history_path)
            loaded = load_history_snapshot(history_path, expected_request_contract=contract)
            dataset = build_monthly_panel(loaded, as_of="2026-06-01")
            model = SpecializedRankingModel()
            artifact = model.fit(dataset.rows, training_cutoff="2026-01-01", dataset_id=dataset.dataset_id, history_snapshot_id=dataset.history_snapshot_id, benchmark_ticker="^GSPC")
            backtest = walk_forward_backtest(dataset, model=model, validation_dates=["2026-01-01", "2026-02-01"], transaction_cost_bps=10)
            artifact = model.artifact
            report = render_phase3_report(dataset=dataset, model=artifact, backtest=backtest, output_format="json")
        self.assertEqual(report["artifact_binding"]["dataset_id"], dataset.dataset_id)
        self.assertEqual(report["artifact_binding"]["model_id"], artifact.model_id)
        self.assertEqual(report["artifact_binding"]["missing_feature_policy"], artifact.missing_feature_policy)
        self.assertEqual(backtest.missing_feature_policy, artifact.missing_feature_policy)
        self.assertIn("scenarios", report["backtest"])
        self.assertIn("market_source_ids", json.loads((Path(__file__).parents[0] / "fixtures" / "offline_refresh_fixture.json").read_text()))

    def test_current_sp500_parser_deduplicates_and_preserves_source_symbols(self):
        rows = "".join(f"<tr><td>{'BRK.B' if index == 0 else f'T{index:03d}'}</td><td>Security {index}</td></tr>" for index in range(120))
        html = f"<table class='wikitable sortable'><tr><th>Symbol</th><th>Security</th></tr>{rows}</table>"
        members = parse_current_sp500_html(
            html.encode(), retrieved_at="2026-09-16T00:00:00+00:00", minimum_members=100,
        )
        self.assertGreaterEqual(len(members), 100)
        self.assertEqual(members[0].source_symbol, "BRK.B")
        self.assertEqual(members[0].ticker, "BRK-B")

    def test_replay_refresh_never_falls_back_to_network_and_writes_unavailable_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-09-16", start="2025-01-01", end="2026-09-16",
                cache_dir=root / "cache", output=output, live=False, replay_only=True,
            )
            self.assertEqual(status, 2)
            report = json.loads(output.read_text())
            manifest = json.loads((root / "report.json.manifest.json").read_text())
        self.assertTrue(report["performance_claims_unavailable"])
        self.assertEqual(manifest["mode"], "replay-only")
        self.assertEqual(manifest["report_status"], "unavailable")

    def test_dataset_loader_round_trips_membership_evidence_status(self):
        dataset = TrainingDataset(
            rows=[], dataset_id="", history_snapshot_id="history-a",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
            membership_evidence_status="descriptive_survivor_selected_evidence",
        )
        dataset = TrainingDataset(
            rows=dataset.rows,
            dataset_id=_identity(dataset.rows, dataset.history_snapshot_id, dataset.benchmark_ticker,
                                  dataset.feature_schema, dataset.return_basis, dataset.sec_source_ids,
                                  dataset.sec_context_ids, dataset.membership_evidence_status),
            history_snapshot_id=dataset.history_snapshot_id,
            benchmark_ticker=dataset.benchmark_ticker, feature_schema=dataset.feature_schema,
            membership_evidence_status=dataset.membership_evidence_status,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.json"
            save_training_dataset(dataset, path)
            loaded = load_training_dataset(path)
        self.assertEqual(loaded.membership_evidence_status, dataset.membership_evidence_status)

    def test_direct_fit_derives_exclusive_observable_cutoff(self):
        usable = _row("OLD", "2025-12-01", "2026-01-31")
        late = _row(
            "LATE", "2025-11-01", "2026-01-31",
            metadata={
                "target_3m_forward_endpoint": "2026-01-31",
                "target_3m_label_availability": "available_after_endpoint",
                "target_3m_asset_observation_date": "2026-02-03",
                "target_3m_benchmark_observation_date": "2026-02-03",
            },
        )
        artifact = SpecializedRankingModel().fit(
            [usable, late], training_cutoff="2026-02-01",
        )
        self.assertEqual(artifact.training_rows, 1)

    def test_duplicate_validation_dates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate validation"):
            walk_forward_splits(
                [_row("AAA", "2025-12-01", "2026-01-31")],
                validation_dates=["2026-02-01", "2026-02-01"],
            )

    def test_nested_target_metadata_return_basis_is_validated(self):
        item = _row("AAA", "2025-12-01", "2026-01-31", metadata={"target_1m_return_basis": "total_return"})
        dataset = TrainingDataset(
            rows=[item], dataset_id="dataset-a", history_snapshot_id="history-a",
            benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
        )
        with self.assertRaisesRegex(ValueError, "return bases"):
            walk_forward_backtest(dataset, validation_dates=["2025-12-01"])

    def test_missing_sec_filing_date_is_unavailable(self):
        issues: list[str] = []
        facts = parse_sec_company_facts(
            "AAA",
            {"facts": {"us-gaap": {"Revenues": {"units": {"USD": [{
                "val": 10, "end": "2025-12-31", "form": "10-K",
            }]}}}}},
            issues=issues,
        )
        self.assertEqual(facts, [])
        self.assertTrue(any("filing" in issue.lower() for issue in issues))

    def test_yahoo_response_arrays_and_requested_bounds_are_validated(self):
        def http_get(url: str, **_: object) -> HttpResponse:
            ticker = urlparse(url).path.rsplit("/", 1)[-1]
            payload = {"chart": {"result": [{
                "meta": {"symbol": ticker},
                "timestamp": [1767225600, 1767312000],
                "indicators": {"quote": [{
                    "open": [100], "high": [100, 101], "low": [100, 101],
                    "close": [100, 101], "volume": [1, 2],
                }], "adjclose": [{"adjclose": [100, 101]}]},
            }]}}
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "length"):
                fetch_history_snapshot(
                    tickers=["AAA"], benchmark_ticker="^GSPC",
                    start_date="2026-01-01", end_date="2026-01-02",
                    cache_dir=Path(directory), http_get=http_get,
                )

    def test_model_and_backtest_loaders_reject_tampered_content(self):
        artifact = SpecializedRankingModel().fit(
            [_row("AAA", "2025-12-01", "2026-01-31")], training_cutoff="2026-02-01",
        )
        with tempfile.TemporaryDirectory() as directory:
            model_path = Path(directory) / "model.json"
            save_model(artifact, model_path)
            payload = json.loads(model_path.read_text())
            payload["intercept"] = 99
            model_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "identity|hash"):
                load_model(model_path)

            dataset = TrainingDataset(
                rows=[_row("AAA", "2025-12-01", "2026-01-31")], dataset_id="dataset-a",
                history_snapshot_id="history-a", benchmark_ticker="^GSPC", feature_schema=FEATURE_SCHEMA,
            )
            result = walk_forward_backtest(dataset, validation_dates=["2025-12-01"])
            backtest_path = Path(directory) / "backtest.json"
            save_backtest(result, backtest_path)
            payload = json.loads(backtest_path.read_text())
            payload["metrics"]["transaction_cost_bps"] = 99
            backtest_path.write_text(json.dumps(payload))
            with self.assertRaisesRegex(ValueError, "identity|hash"):
                load_backtest(backtest_path)


if __name__ == "__main__":
    unittest.main()
