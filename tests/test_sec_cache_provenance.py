from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from nisa_quant.feature_engineering import build_features
from nisa_quant.historical_market_data import (
    MarketBar,
    SecFact,
    UniverseMember,
    _content_hash,
    build_history_snapshot,
    load_history_snapshot,
    parse_sec_company_facts,
)
from nisa_quant.refresh_pipeline import refresh_phase3


def _contract() -> str:
    start = date(2026, 1, 1)
    end = date(2026, 1, 2)
    return json.dumps({
        "schema": "phase3-market-request", "schema_version": 1,
        "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
        "period2": int(datetime.combine(end + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
        "interval": "1d", "events": "div,splits", "return_basis": "price_return",
        "timeout": 15, "retries": 2,
        "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))


def _bar(ticker: str) -> MarketBar:
    return MarketBar(
        ticker, "2026-01-01", 100.0, 101.0, 99.0, 100.0, 1000.0,
        retrieved_at="2026-09-16T00:00:00+00:00",
        source="fixture-market", citation="fixture://sol-r150",
    )


def _member() -> UniverseMember:
    return UniverseMember(
        "AAA", None, None, "active", "current_snapshot_only",
        "survivorship_risk_disclosed", "fixture-universe", "sol-r150",
    )


class SolR150SecCacheIsolationTests(unittest.TestCase):
    def test_live_sec_not_requested_discards_embedded_older_sec_facts(self) -> None:
        stale_fact = SecFact(
            "AAA", "us-gaap:Revenues", "USD", 999999999.0, None,
            "2025-12-31", "2026-01-15", "10-K", 2025, "FY",
            "acc-stale", "CY2025", "2026-01-15T00:00:00+00:00",
            "SEC XBRL Company Facts", "fixture-sec://stale",
        )
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA")], "^GSPC": [_bar("^GSPC")]},
            benchmark_ticker="^GSPC", universe=[_member()], sec_facts=[stale_fact],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        observed_sec_facts: list[list[SecFact]] = []

        def stop_before_features(current_snapshot: object, **_: object) -> object:
            observed_sec_facts.append(list(current_snapshot.sec_facts))  # type: ignore[attr-defined]
            raise ValueError("stop after SEC isolation probe")

        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.refresh_pipeline.fetch_current_sp500_universe",
            return_value=[_member()],
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_history_snapshot",
            return_value=snapshot,
        ), patch(
            "nisa_quant.refresh_pipeline.build_monthly_panel",
            side_effect=stop_before_features,
        ), patch(
            "nisa_quant.refresh_pipeline.MIN_PHASE3_MARKET_ROWS", 0,
        ):
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-09-16", start="2026-01-01", end="2026-01-02",
                cache_dir=root / "cache", output=output,
                live=True, replay_only=False, limit=1,
            )
            manifest = json.loads((root / "report.json.manifest.json").read_text())

        self.assertEqual(status, 2)
        self.assertEqual(observed_sec_facts, [[]])
        self.assertEqual(manifest["sec_status_by_ticker"]["AAA"], "not_requested")
        self.assertNotIn("999999999", json.dumps(manifest, sort_keys=True))

    def test_live_empty_sec_probe_discards_embedded_older_sec_facts(self) -> None:
        stale_fact = SecFact(
            "AAA", "us-gaap:Revenues", "USD", 999999999.0, None,
            "2025-12-31", "2026-01-15", "10-K", 2025, "FY",
            "acc-stale", "CY2025", "2026-01-15T00:00:00+00:00",
            "SEC XBRL Company Facts", "fixture-sec://stale",
        )
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA")], "^GSPC": [_bar("^GSPC")]},
            benchmark_ticker="^GSPC", universe=[_member()], sec_facts=[stale_fact],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        observed_sec_facts: list[list[SecFact]] = []

        def stop_before_features(current_snapshot: object, **_: object) -> object:
            observed_sec_facts.append(list(current_snapshot.sec_facts))  # type: ignore[attr-defined]
            raise ValueError("stop after SEC isolation probe")

        with tempfile.TemporaryDirectory() as directory, patch(
            "nisa_quant.refresh_pipeline.fetch_current_sp500_universe",
            return_value=[_member()],
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_sec_company_facts_for_ticker",
            return_value=[],
        ), patch(
            "nisa_quant.refresh_pipeline.fetch_history_snapshot",
            return_value=snapshot,
        ), patch(
            "nisa_quant.refresh_pipeline.build_monthly_panel",
            side_effect=stop_before_features,
        ), patch(
            "nisa_quant.refresh_pipeline.MIN_PHASE3_MARKET_ROWS", 0,
        ):
            root = Path(directory)
            output = root / "report.json"
            status = refresh_phase3(
                as_of="2026-09-16", start="2026-01-01", end="2026-01-02",
                cache_dir=root / "cache", output=output,
                live=True, replay_only=False, limit=1,
                sec_contact="researcher@example.com",
            )
            manifest = json.loads((root / "report.json.manifest.json").read_text())

        self.assertEqual(status, 2)
        self.assertEqual(observed_sec_facts, [[]])
        self.assertEqual(manifest["sec_status"], "bound_no_usable_facts")
        self.assertEqual(manifest["sec_status_by_ticker"]["AAA"]["status"], "bound_no_usable_facts")
        self.assertEqual(manifest["sec_rows"], 0)


class SolR150SecProvenanceTests(unittest.TestCase):
    def test_rehashed_provenance_free_sec_fact_is_rejected_before_replay(self) -> None:
        snapshot = build_history_snapshot(
            {"AAA": [_bar("AAA")], "^GSPC": [_bar("^GSPC")]},
            benchmark_ticker="^GSPC", universe=[_member()],
            created_at="2026-09-16T00:00:00+00:00", request_contract=_contract(),
        )
        forged = replace(
            snapshot,
            sec_facts=[SecFact(
                "AAA", "us-gaap:Revenues", "USD", 999999999.0, None,
                "2025-12-31", "2026-01-15", "10-K", 2025, "FY",
                "", "CY2025", "", "SEC XBRL Company Facts", "",
            )],
            snapshot_id="",
        )
        forged = replace(forged, snapshot_id=f"phase3-{_content_hash(forged)[:20]}")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / (
                f"history-{hashlib.sha256(snapshot.request_contract.encode()).hexdigest()[:24]}.json"
            )
            path.write_text(json.dumps({
                "bars_by_ticker": {key: [bar.__dict__ if hasattr(bar, "__dict__") else {
                    "ticker": bar.ticker, "observation_date": bar.observation_date,
                    "open": bar.open, "high": bar.high, "low": bar.low,
                    "close": bar.close, "volume": bar.volume,
                    "adjusted_close": bar.adjusted_close, "dividend": bar.dividend,
                    "split_factor": bar.split_factor, "retrieved_at": bar.retrieved_at,
                    "source": bar.source, "citation": bar.citation,
                } for bar in bars] for key, bars in forged.bars_by_ticker.items()},
                "benchmark_ticker": forged.benchmark_ticker,
                "coverage": forged.coverage, "created_at": forged.created_at,
                "sec_facts": [{
                    "ticker": fact.ticker, "concept": fact.concept, "unit": fact.unit,
                    "value": fact.value, "period_start": fact.period_start,
                    "period_end": fact.period_end, "filed_at": fact.filed_at,
                    "form": fact.form, "fiscal_year": fact.fiscal_year,
                    "fiscal_period": fact.fiscal_period, "accession": fact.accession,
                    "frame": fact.frame, "retrieved_at": fact.retrieved_at,
                    "source": fact.source, "citation": fact.citation,
                } for fact in forged.sec_facts],
                "snapshot_id": forged.snapshot_id,
                "source_snapshot_ids": forged.source_snapshot_ids,
                "universe": [{
                    "ticker": member.ticker, "effective_from": member.effective_from,
                    "effective_to": member.effective_to,
                    "membership_status": member.membership_status,
                    "lookahead_bias_status": member.lookahead_bias_status,
                    "survivorship_bias_status": member.survivorship_bias_status,
                    "source": member.source, "source_version": member.source_version,
                    "source_symbol": member.source_symbol,
                } for member in forged.universe],
                "cache_kind": forged.cache_kind,
                "request_contract": forged.request_contract,
                "ticker_failures": forged.ticker_failures,
            }, sort_keys=True), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "accession|provenance|retrieved|citation"):
                load_history_snapshot(path, expected_request_contract=snapshot.request_contract)

    def test_malformed_sec_source_url_is_rejected_before_fact_creation(self) -> None:
        with self.assertRaisesRegex(ValueError, "source URL|official SEC"):
            parse_sec_company_facts(
                "AAA",
                {"cik": 1, "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{
                    "val": 10, "end": "2025-12-31", "filed": "2026-01-15",
                    "form": "10-K", "accn": "0000000001-26-000001",
                }]}}}}},
                cik="1", retrieved_at="2026-09-16T00:00:00+00:00",
                source_url="https://example.invalid/companyfacts",
            )

    def test_provenance_free_sec_fact_is_rejected_at_feature_boundary(self) -> None:
        fact = SecFact(
            "AAA", "us-gaap:Revenues", "USD", 999999999.0, None,
            "2025-12-31", "2026-01-15", "10-K", 2025, "FY",
            "", "CY2025", "", "SEC XBRL Company Facts", "",
        )
        with self.assertRaisesRegex(ValueError, "accession|provenance|retrieved|citation"):
            build_features("AAA", [_bar("AAA")], decision_date="2026-01-02", sec_facts=[fact])


if __name__ == "__main__":
    unittest.main()
