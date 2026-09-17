from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import unquote
from unittest.mock import patch

from nisa_quant.evidence_providers import HttpResponse
from nisa_quant.historical_market_data import (
    MarketBar,
    SecFact,
    UniverseMember,
    _parse_yahoo,
    build_history_snapshot,
    build_sec_request_contract,
    fetch_history_snapshot,
)
from nisa_quant.refresh_pipeline import refresh_phase3


def _sec_contract(status: str) -> str:
    return build_sec_request_contract(
        ticker="AAA",
        cik="0000000001",
        provider="SEC XBRL Company Facts",
        source_version="companyfacts-v1",
        requested_start="2025-01-01",
        requested_end="2026-01-01",
        as_of="2026-01-01",
        retrieved_at="2026-01-01T00:00:00+00:00",
        retrieval_intent="explicit_company_facts_probe",
        retrieval_status=status,
        facts=(),
    )


def _cached_snapshot(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        request_contract="cached-contract",
        snapshot_id="history-cached-sec-r169",
        benchmark_ticker="^GSPC",
        universe=[SimpleNamespace(
            ticker="AAA", effective_from=None, effective_to=None,
            membership_status="active", lookahead_bias_status="current_snapshot_only",
            survivorship_bias_status="survivorship_risk_disclosed",
            source="fixture", source_version="sol-r169",
        )],
        coverage={
            "AAA": {"row_count": 300, "invalid_observation_count": 0, "gaps": []},
            "^GSPC": {"row_count": 300, "invalid_observation_count": 0, "gaps": []},
        },
        sec_facts=[],
        sec_request_contract=_sec_contract(status),
        ticker_failures={},
        bars_by_ticker={
            "AAA": [SimpleNamespace(observation_date="2025-01-01")],
            "^GSPC": [SimpleNamespace(observation_date="2025-01-01")],
        },
    )


class SolR169SecStatusTests(unittest.TestCase):
    def _assert_cached_status_is_preserved(self, status: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch(
                "nisa_quant.refresh_pipeline._find_compatible_history_cache_paths",
                return_value=[root / "history-cached.json"],
            ), patch(
                "nisa_quant.refresh_pipeline.load_history_snapshot",
                return_value=_cached_snapshot(status),
            ), patch(
                "nisa_quant.refresh_pipeline.build_monthly_panel",
                side_effect=ValueError("stop after SEC replay probe"),
            ):
                refresh_phase3(
                    as_of="2026-01-01", start="2025-01-01", end="2026-01-01",
                    cache_dir=root / "cache", output=output, replay_only=True,
                )
            manifest = json.loads(
                (root / "report.json.manifest.json").read_text(encoding="utf-8")
            )

        self.assertEqual(manifest["sec_status"], status)
        self.assertEqual(manifest["sec_status_by_ticker"]["AAA"]["status"], status)
        self.assertTrue(manifest["sec_failure_evidence"])

    def test_cached_unavailable_status_and_evidence_are_preserved(self) -> None:
        self._assert_cached_status_is_preserved("unavailable")

    def test_cached_failed_status_and_evidence_are_preserved(self) -> None:
        self._assert_cached_status_is_preserved("failed")

    def test_explicit_sec_probe_cannot_be_recorded_as_not_requested(self) -> None:
        with self.assertRaisesRegex(ValueError, "retrieval intent|not_requested"):
            build_sec_request_contract(
                ticker="AAA", cik="0000000001", provider="SEC XBRL Company Facts",
                source_version="companyfacts-v1", requested_start="2025-01-01",
                requested_end="2026-01-01", as_of="2026-01-01",
                retrieved_at="2026-01-01T00:00:00+00:00",
                retrieval_intent="explicit_company_facts_probe",
                retrieval_status="not_requested", facts=(),
            )

    def test_bound_sec_status_requires_facts(self) -> None:
        with self.assertRaisesRegex(ValueError, "bound|facts"):
            build_sec_request_contract(
                ticker="AAA", cik="0000000001", provider="SEC XBRL Company Facts",
                source_version="companyfacts-v1", requested_start="2025-01-01",
                requested_end="2026-01-01", as_of="2026-01-01",
                retrieved_at="2026-01-01T00:00:00+00:00",
                retrieval_intent="explicit_company_facts_probe",
                retrieval_status="bound", facts=(),
            )

    def test_stale_cached_sec_facts_are_excluded_without_blocking_market_replay(self) -> None:
        start = date(2025, 1, 1)
        day = start + timedelta(days=1)
        request_contract = json.dumps({
            "schema": "phase3-market-request", "schema_version": 1,
            "asset_tickers": ["AAA"], "benchmark_ticker": "^GSPC",
            "start_date": start.isoformat(), "end_date": "2026-01-01",
            "period1": int(datetime.combine(start, datetime.min.time(), timezone.utc).timestamp()),
            "period2": int(datetime.combine(date(2026, 1, 1) + timedelta(days=1), datetime.min.time(), timezone.utc).timestamp()),
            "interval": "1d", "events": "div,splits", "return_basis": "price_return",
            "timeout": 15, "retries": 2,
            "user_agent": "nisa-quant-assistant/phase3-read-only",
            "endpoint": "https://query1.finance.yahoo.com/v8/finance/chart",
        }, sort_keys=True, separators=(",", ":"))
        fact = SecFact(
            "AAA", "us-gaap:Revenues", "USD", 10.0, None, "2024-12-31",
            "2025-01-15", "10-K", 2024, "FY", "acc-stale-r169", None,
            "2025-01-16T00:00:00+00:00", "SEC XBRL Company Facts", "fixture-sec:stale-r169",
        )
        snapshot = build_history_snapshot(
            {
                "AAA": [MarketBar("AAA", day.isoformat(), 100.0, 101.0, 99.0, 100.0, 1_000.0, retrieved_at="2025-01-03T00:00:00+00:00", source="fixture", citation="fixture://r169")],
                "^GSPC": [MarketBar("^GSPC", day.isoformat(), 100.0, 101.0, 99.0, 100.0, 1_000.0, retrieved_at="2025-01-03T00:00:00+00:00", source="fixture", citation="fixture://r169")],
            },
            benchmark_ticker="^GSPC",
            universe=[UniverseMember("AAA", None, None, "active", "current_snapshot_only", "survivorship_risk_disclosed", "fixture", "r169")],
            sec_facts=[fact], created_at="2025-01-16T00:00:00+00:00",
            request_contract=request_contract,
            sec_request_contract=build_sec_request_contract(
                ticker="AAA", cik="0000000001", provider="SEC XBRL Company Facts",
                source_version="companyfacts-v1", requested_start=start,
                requested_end="2026-01-01", as_of="2026-01-01",
                retrieved_at="2025-01-16T00:00:00+00:00",
                retrieval_intent="explicit_company_facts_probe",
                retrieval_status="bound", facts=[fact],
            ),
        )
        observed: list[list[SecFact]] = []

        def stop_before_feature_consumption(history: object, **_: object) -> object:
            observed.append(list(history.sec_facts))  # type: ignore[attr-defined]
            raise ValueError("stop after stale SEC replay probe")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "report.json"
            with patch(
                "nisa_quant.refresh_pipeline._find_compatible_history_cache_paths",
                return_value=[root / "history-cached.json"],
            ), patch(
                "nisa_quant.refresh_pipeline.load_history_snapshot", return_value=snapshot,
            ), patch(
                "nisa_quant.refresh_pipeline.build_monthly_panel",
                side_effect=stop_before_feature_consumption,
            ), patch("nisa_quant.refresh_pipeline.MIN_PHASE3_MARKET_ROWS", 0):
                refresh_phase3(
                    as_of="2026-01-01", start="2025-01-01", end="2026-01-01",
                    cache_dir=root / "cache", output=output, replay_only=True,
                )
            manifest = json.loads((root / "report.json.manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(observed, [[]])
        self.assertEqual(manifest["sec_status"], "bound_no_usable_facts")
        self.assertIn("stale", " ".join(manifest["sec_failure_evidence"]).casefold())


def _yahoo_payload(ticker: str, *, dividend: float | None = None, split: tuple[float, float] | None = None) -> dict[str, object]:
    stamp = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
    events: dict[str, object] = {"div": {}, "splits": {}}
    if dividend is not None:
        events["div"] = {str(stamp): {"amount": dividend}}
    if split is not None:
        events["splits"] = {
            str(stamp): {"numerator": split[0], "denominator": split[1]},
        }
    return {"chart": {"result": [{
        "meta": {"symbol": ticker},
        "timestamp": [stamp],
        "indicators": {"quote": [{
            "open": [100.0], "high": [101.0], "low": [99.0],
            "close": [100.0], "volume": [1000.0],
        }]},
        "events": events,
    }]}}


class SolR169YahooCorporateActionTests(unittest.TestCase):
    def _assert_invalid_event_is_not_persisted(
        self, *, dividend: float | None = None, split: tuple[float, float] | None = None,
    ) -> None:
        def http_get(url: str, **_: object) -> HttpResponse:
            ticker = unquote(url.split("/chart/", 1)[1].split("?", 1)[0])
            payload = _yahoo_payload(ticker, dividend=dividend, split=split)
            return HttpResponse(200, json.dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            cache_dir = Path(directory)
            snapshot = fetch_history_snapshot(
                tickers=["AAA"], benchmark_ticker="^GSPC",
                start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
                cache_dir=cache_dir, retrieved_at="2026-01-02T00:00:00+00:00",
                http_get=http_get, allow_partial=True,
            )

            self.assertEqual(snapshot.bars_by_ticker["AAA"], [])
            self.assertIn("AAA", snapshot.ticker_failures)
            self.assertEqual(list(cache_dir.glob("history-*.json")), [])
            self.assertTrue(snapshot.coverage["AAA"]["invalid_observations"])

    def test_negative_dividend_is_rejected_before_bar_and_snapshot_persistence(self) -> None:
        self._assert_invalid_event_is_not_persisted(dividend=-0.25)

    def test_negative_split_ratio_is_rejected_before_bar_and_snapshot_persistence(self) -> None:
        self._assert_invalid_event_is_not_persisted(split=(-1.0, 2.0))

    def test_zero_dividend_and_positive_split_ratio_follow_existing_contract(self) -> None:
        stamp = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp())
        bars = _parse_yahoo(
            "AAA", _yahoo_payload("AAA", dividend=0.0, split=(2.0, 1.0)),
            "2026-01-02T00:00:00+00:00", "https://example.invalid",
            start_date=date(2026, 1, 1), end_date=date(2026, 1, 1),
        )
        self.assertEqual(bars[0].dividend, 0.0)
        self.assertEqual(bars[0].split_factor, 2.0)
        self.assertEqual(bars[0].observation_date, datetime.fromtimestamp(stamp, timezone.utc).date().isoformat())


if __name__ == "__main__":
    unittest.main()
