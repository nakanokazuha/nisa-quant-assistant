from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from nisa_quant.historical_market_data import (
    MarketBar,
    SecFact,
    UniverseMember,
    _parse_yahoo,
    _content_hash,
    _validate_sec_request_contract,
    build_history_snapshot,
    build_sec_request_contract,
    parse_sec_company_facts,
    _sec_facts_hash,
)


def _bar(ticker: str, day: date, value: float = 100.0) -> MarketBar:
    return MarketBar(
        ticker=ticker,
        observation_date=day.isoformat(),
        open=value,
        high=value,
        low=value,
        close=value,
        volume=1_000.0,
        retrieved_at="2026-09-16T00:00:00+00:00",
        source="fixture-market",
        citation="fixture://sol-r160",
    )


def _fact() -> SecFact:
    return SecFact(
        ticker="AAA",
        concept="us-gaap:Revenues",
        unit="USD",
        value=10.0,
        period_start=None,
        period_end="2025-12-31",
        filed_at="2026-01-15",
        form="10-K",
        fiscal_year=2025,
        fiscal_period="FY",
        accession="acc-sol-r160",
        frame=None,
        retrieved_at="2026-09-16T00:00:00+00:00",
        source="SEC XBRL Company Facts",
        citation="fixture-sec:sol-r160",
    )


def _sec_contract(status: str, facts: tuple[SecFact, ...]) -> str:
    contract = build_sec_request_contract(
        ticker="AAA",
        cik="0000000001",
        provider="SEC XBRL Company Facts",
        source_version="companyfacts-v1",
        requested_start="2024-01-01",
        requested_end="2026-09-16",
        as_of="2026-09-16",
        retrieved_at="2026-09-16T00:00:00+00:00",
        retrieval_intent="explicit_company_facts_probe",
        retrieval_status="bound" if status == "unavailable" else status,
        facts=facts,
    )
    if status == "unavailable":
        values = json.loads(contract)
        values["retrieval_status"] = status
        values["facts_hash"] = _sec_facts_hash(facts)
        contract = json.dumps(values, sort_keys=True, separators=(",", ":"))
    return contract


def _snapshot_with_unusable_sec_facts() -> object:
    start = date(2024, 1, 1)
    bars = {
        ticker: [_bar(ticker, start + timedelta(days=index), 100.0 + index / 100.0) for index in range(300)]
        for ticker in ("AAA", "^GSPC")
    }
    snapshot = build_history_snapshot(
        bars,
        benchmark_ticker="^GSPC",
        universe=[UniverseMember(
            "AAA", None, None, "active", "current_snapshot_only",
            "survivorship_risk_disclosed", "fixture", "sol-r160",
        )],
        created_at="2026-09-16T00:00:00+00:00",
        sec_facts=[],
        sec_request_contract=build_sec_request_contract(
            ticker="AAA", cik="0000000001", provider="SEC XBRL Company Facts",
            source_version="companyfacts-v1", requested_start="2024-01-01",
            requested_end="2026-09-16", as_of="2026-09-16",
            retrieved_at="2026-09-16T00:00:00+00:00",
            retrieval_intent="explicit_company_facts_probe", retrieval_status="unavailable", facts=(),
        ),
    )
    facts = [_fact()]
    return replace(snapshot, sec_facts=facts, sec_request_contract=_sec_contract("unavailable", tuple(facts)))


class SolR160SecIsolationTests(unittest.TestCase):
    def test_sec_unavailable_accepts_facts_no(self) -> None:
        facts = (_fact(),)
        with self.assertRaisesRegex(ValueError, "does not permit SEC facts"):
            build_history_snapshot(
                {"AAA": [_bar("AAA", date(2026, 1, 1))], "^GSPC": [_bar("^GSPC", date(2026, 1, 1))]},
                benchmark_ticker="^GSPC",
                universe=[UniverseMember(
                    "AAA", None, None, "active", "current_snapshot_only",
                    "survivorship_risk_disclosed", "fixture", "sol-r160",
                )],
                sec_facts=list(facts),
                sec_request_contract=_sec_contract("unavailable", facts),
            )

    def test_replay_strips_unusable_sec_facts_before_panel_features(self) -> None:
        snapshot = _snapshot_with_unusable_sec_facts()
        observed: list[list[SecFact]] = []
        observed_snapshots: list[object] = []

        def stop_before_feature_consumption(history: object, **_: object) -> object:
            observed.append(list(getattr(history, "sec_facts")))
            observed_snapshots.append(history)
            raise ValueError("stop after panel boundary")

        from nisa_quant.phase3_producer import refresh_phase3

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch("nisa_quant.phase3_producer._find_compatible_history_cache_paths", return_value=[root / "cache.json"]), \
                    patch("nisa_quant.phase3_producer.load_history_snapshot", return_value=snapshot), \
                    patch("nisa_quant.phase3_producer.build_monthly_panel", side_effect=stop_before_feature_consumption):
                refresh_phase3(
                    as_of="2026-09-16", start="2024-01-01", end="2026-09-16",
                    cache_dir=root / "cache", output=output, replay_only=True,
                )

        self.assertEqual(observed, [[]])
        sanitized = observed_snapshots[0]
        _validate_sec_request_contract(
            sanitized.sec_request_contract, facts=sanitized.sec_facts,  # type: ignore[attr-defined]
        )
        self.assertEqual(
            sanitized.snapshot_id,  # type: ignore[attr-defined]
            f"phase3-{_content_hash(sanitized)[:20]}",
        )


class SolR160SecIdentityTests(unittest.TestCase):
    def test_sec_issuer_mismatch_with_issue_collector_is_hard_rejection(self) -> None:
        payload = {
            "cik": "0000789019",
            "ticker": "MSFT",
            "facts": {},
        }
        issues: list[str] = []
        with self.assertRaisesRegex(ValueError, "does not match"):
            parse_sec_company_facts(
                "AAPL",
                payload,
                cik="0000320193",
                retrieved_at="2026-09-16T00:00:00+00:00",
                source_url="https://data.sec.gov/api/xbrl/companyfacts/CIK0000789019.json",
                issues=issues,
            )

    def test_producer_records_identity_failure_as_failed_not_unavailable(self) -> None:
        from nisa_quant.phase3_producer import refresh_phase3

        snapshot = _snapshot_with_unusable_sec_facts()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch(
                "nisa_quant.phase3_producer.fetch_current_sp500_universe",
                return_value=[snapshot.universe[0]],
            ), patch(
                "nisa_quant.phase3_producer.fetch_sec_company_facts_for_ticker",
                side_effect=ValueError("SEC Company Facts response CIK does not match the requested CIK"),
            ), patch(
                "nisa_quant.phase3_producer.fetch_history_snapshot",
                return_value=snapshot,
            ):
                refresh_phase3(
                    as_of="2026-09-16", start="2024-01-01", end="2026-09-16",
                    cache_dir=root / "cache", output=output,
                    live=True, replay_only=False, limit=1,
                    sec_contact="researcher@example.com",
                )
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(manifest["sec_status"], "failed")
        self.assertEqual(manifest["sec_status_by_ticker"]["AAA"]["status"], "failed")
        self.assertIn("does not match", " ".join(manifest["sec_failure_evidence"]))


class SolR160YahooDividendTests(unittest.TestCase):
    def test_yahoo_dividends_event_is_retained_with_provenance(self) -> None:
        stamp = 1767225600
        payload = {
            "chart": {"result": [{
                "meta": {"symbol": "AAA"},
                "timestamp": [stamp],
                "events": {"dividends": {str(stamp): {"amount": 0.25}}, "splits": {}},
                "indicators": {"quote": [{
                    "open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000.0],
                }]},
            }]},
        }
        bars = _parse_yahoo(
            "AAA", payload, "2026-01-02T00:00:00+00:00",
            "https://query1.finance.yahoo.com/v8/finance/chart/AAA",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
        )
        self.assertEqual(bars[0].dividend, 0.25)
        self.assertIn("query1.finance.yahoo.com", bars[0].citation)
        self.assertLessEqual(date.fromisoformat(bars[0].observation_date), date.fromisoformat(bars[0].retrieved_at[:10]))


if __name__ == "__main__":
    unittest.main()
