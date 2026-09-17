from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from nisa_quant.evidence_providers import ProviderUnavailable
from nisa_quant.historical_market_data import (
    MarketBar,
    SecFact,
    UniverseMember,
    build_history_snapshot,
    build_sec_request_contract,
    parse_sec_company_facts,
)
from nisa_quant.refresh_pipeline import refresh_phase3


def _market_snapshot() -> object:
    start = date(2024, 1, 1)
    end = date(2026, 9, 16)
    bars: dict[str, list[MarketBar]] = {}
    for ticker, multiplier in (("AAA", 1.0), ("^GSPC", 0.8)):
        bars[ticker] = [
            MarketBar(
                ticker,
                (start + timedelta(days=index)).isoformat(),
                100.0 + multiplier * index,
                100.0 + multiplier * index,
                100.0 + multiplier * index,
                100.0 + multiplier * index,
                1_000.0,
                retrieved_at="2026-09-16T00:00:00+00:00",
                source="fixture-market",
                citation="fixture://sol-r154",
            )
            for index in range((end - start).days + 1)
        ]
    contract = json.dumps({
        "schema": "phase3-market-request", "schema_version": 1,
        "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "period1": 1704067200, "period2": 1789603200,
        "interval": "1d", "events": "div,splits", "return_basis": "price_return",
        "timeout": 15, "retries": 2,
        "user_agent": "nisa-quant-assistant/phase3-read-only",
        "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
    }, sort_keys=True, separators=(",", ":"))
    return build_history_snapshot(
        bars,
        benchmark_ticker="^GSPC",
        universe=[UniverseMember(
            "AAA", None, None, "active", "current_snapshot_only",
            "survivorship_risk_disclosed", "fixture", "sol-r154",
        )],
        created_at="2026-09-16T00:00:00+00:00",
        request_contract=contract,
    )


class SolR154OptionalSecTests(unittest.TestCase):
    def test_unavailable_sec_contract_does_not_block_valid_market_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch(
                "nisa_quant.refresh_pipeline.fetch_current_sp500_universe",
                return_value=[_market_snapshot().universe[0]],
            ), patch(
                "nisa_quant.refresh_pipeline.fetch_sec_company_facts_for_ticker",
                side_effect=ProviderUnavailable("SEC outage"),
            ), patch(
                "nisa_quant.refresh_pipeline.fetch_history_snapshot",
                return_value=_market_snapshot(),
            ):
                status = refresh_phase3(
                    as_of="2026-09-16", start="2024-01-01", end="2026-09-16",
                    cache_dir=root / "cache", output=output,
                    live=True, replay_only=False, limit=1,
                    sec_contact="researcher@example.com",
                )
            report = json.loads(output.read_text(encoding="utf-8"))
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertIn(status, {0, 2})
        self.assertIn(
            report["status"],
            {"available", "available_descriptive", "unavailable_insufficient_data"},
        )
        self.assertGreater(len(report["current_predictions"]), 0)
        self.assertEqual(manifest["sec_status"], "unavailable")
        self.assertEqual(manifest["sec_status_by_ticker"]["AAA"]["status"], "unavailable")
        self.assertTrue(manifest["sec_failure_evidence"])
        self.assertIn("SEC Company Facts", " ".join(manifest["failures"]))

    def test_unavailable_status_is_valid_in_a_market_history_contract(self) -> None:
        contract = build_sec_request_contract(
            ticker="AAA", cik="0000000001", provider="SEC XBRL Company Facts",
            source_version="companyfacts-v1", requested_start="2024-01-01",
            requested_end="2026-09-16", as_of="2026-09-16",
            retrieved_at="2026-09-16T00:00:00+00:00",
            retrieval_intent="explicit_company_facts_probe",
            retrieval_status="unavailable", facts=(),
        )
        self.assertEqual(json.loads(contract)["retrieval_status"], "unavailable")


class SolR154SecBindingTests(unittest.TestCase):
    def test_company_facts_accepts_only_canonical_endpoint_and_provider_identity(self) -> None:
        payload = {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {"us-gaap": {"Revenues": {"units": {"USD": [{
                "val": 10, "end": "2025-12-31", "filed": "2026-01-15",
                "form": "10-K", "accn": "0000320193-26-000001",
            }]}}}},
        }
        with self.assertRaisesRegex(ValueError, "official SEC|source URL|Company Facts"):
            parse_sec_company_facts(
                "AAPL", payload, cik="0000320193",
                retrieved_at="2026-09-16T00:00:00+00:00",
                source_url="https://www.sec.gov/not-companyfacts",
            )

        with self.assertRaisesRegex(ValueError, "ticker|issuer|CIK|identity"):
            parse_sec_company_facts(
                "MSFT", payload, cik="0000320193",
                retrieved_at="2026-09-16T00:00:00+00:00",
                source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
            )

        with self.assertRaisesRegex(ValueError, "URL CIK|response CIK|CIK"):
            parse_sec_company_facts(
                "AAPL", payload, cik="0000320193",
                retrieved_at="2026-09-16T00:00:00+00:00",
                source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json",
            )

        with self.assertRaisesRegex(ValueError, "source|provider|SEC"):
            build_history_snapshot(
                {"AAA": [], "^GSPC": []},
                universe=[UniverseMember(
                    "AAA", None, None, "active", "current_snapshot_only",
                    "survivorship_risk_disclosed", "fixture", "sol-r154",
                )],
                sec_facts=[SecFact(
                    "AAA", "us-gaap:Revenues", "USD", 10.0, None,
                    "2025-12-31", "2026-01-15", "10-K", 2025, "FY",
                    "0000320193-26-000001", None,
                    "2026-09-16T00:00:00+00:00", "fabricated-provider",
                    "https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json",
                )],
            )


if __name__ == "__main__":
    unittest.main()
