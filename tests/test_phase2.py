"""Acceptance tests for the Phase 2 evidence refresh boundary."""

from __future__ import annotations

import csv
import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import nisa_quant.evidence_collection as phase2_module
from nisa_quant.evidence_collection import (
    _configured_sec_rate,
    _configured_timeout,
    _persist_refresh_scope,
    _selected_current_members,
    import_sp500_universe,
    ingest_evidence,
    phase2_evidence_report,
    refresh_market_observations,
    refresh_phase2_configured,
    refresh_phase2_fixtures,
)
from nisa_quant.evidence_providers import (
    AlphaVantageMarketProvider,
    EvidenceRecord,
    FixtureMarketProvider,
    FlowProxyObservation,
    HttpResponse,
    MARKET_FIELDS,
    MarketObservation,
    ProviderUnavailable,
    RequestRateLimiter,
    RssFeedProvider,
    RssSourceConfig,
    SECEdgarProvider,
    UrllibReadOnlyTransport,
    _NoRedirectHandler,
    contains_control_content,
    normalize_flow_proxy,
    normalize_rss_feed,
    normalize_sec_company_facts,
    normalize_sec_submissions,
    validate_public_reference,
)
from nisa_quant.database_schema import connect_database, initialize_database


UNIVERSE_COLUMNS = (
    "universe_id", "effective_date", "membership_status", "ticker", "cik",
    "issuer_name", "exchange", "source_url", "source_version", "retrieved_at",
    "lookahead_bias_status", "survivorship_bias_status",
)
MARKET_COLUMNS = (
    "ticker", "observation_date", "open", "high", "low", "close", "volume",
    "currency", "retrieved_at", "citation",
)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)


def universe_row(**overrides: str) -> dict[str, str]:
    row = {
        "universe_id": "sp500-2026-08-31",
        "effective_date": "2026-08-31",
        "membership_status": "active",
        "ticker": "ABC",
        "cik": "0000000001",
        "issuer_name": "Synthetic Corp",
        "exchange": "NYSE",
        "source_url": "https://www.spglobal.com/spdji/en/indices/equity/sp-500/",
        "source_version": "sp500-methodology-2026-06",
        "retrieved_at": "2026-08-31T12:00:00+09:00",
        "lookahead_bias_status": "point_in_time",
        "survivorship_bias_status": "survivorship_risk_disclosed",
    }
    row.update(overrides)
    return row


def market_row(**overrides: str) -> dict[str, str]:
    row = {
        "ticker": "ABC",
        "observation_date": "2026-08-31",
        "open": "100",
        "high": "105",
        "low": "99",
        "close": "104",
        "volume": "123456",
        "currency": "USD",
        "retrieved_at": "2026-09-01T01:00:00+09:00",
        "citation": "fixture:phase2-market#ABC-2026-08-31",
    }
    row.update(overrides)
    return row


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def seed_universe(connection: sqlite3.Connection, ticker: str = "ABC", cik: str = "0000000001") -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "universe.csv"
        write_csv(path, UNIVERSE_COLUMNS, [universe_row(ticker=ticker, cik=cik)])
        import_sp500_universe(connection, path, request_id=f"seed-{ticker}")


def evidence_record(**overrides: object) -> EvidenceRecord:
    values: dict[str, object] = {
        "evidence_identity": "logical-evidence-1",
        "evidence_kind": "filing",
        "evidence_subtype": "company_fact",
        "source_name": "SEC XBRL Company Facts",
        "source_identifier": "0000000001-26-000002",
        "source_url": "https://data.sec.gov/api/xbrl/companyfacts/",
        "ticker": "ABC",
        "issuer_cik": "0000000001",
        "publication_at": "2026-08-01T00:00:00+00:00",
        "period_start": "2026-04-01",
        "period_end": "2026-06-30",
        "fact_field": "Revenue",
        "fact_value": "123",
        "fact_unit": "USD",
        "topic": "reported_fact",
        "evidence_text": None,
        "source_quality": "authoritative_regulatory",
        "recency_status": "old",
        "corroboration_status": "single_source",
        "uncertainty_status": "reported_fact",
        "metadata": {"taxonomy": "us-gaap"},
        "retrieved_at": "2026-09-01T00:00:00+00:00",
        "source_version": "sec-companyfacts-v1",
        "citation": "https://www.sec.gov/Archives/edgar/data/1/000000000126000002/",
    }
    values.update(overrides)
    return EvidenceRecord(**values)  # type: ignore[arg-type]


class Phase2UniverseAndMarketTests(unittest.TestCase):
    def test_universe_records_point_in_time_metadata_and_replay_is_idempotent(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            write_csv(path, UNIVERSE_COLUMNS, [universe_row()])
            self.assertEqual(import_sp500_universe(connection, path, request_id="universe-a"), 1)
            self.assertEqual(import_sp500_universe(connection, path, request_id="universe-a"), 0)
        row = connection.execute("SELECT * FROM phase2_universe_members").fetchone()
        self.assertEqual(row["ticker"], "ABC")
        self.assertEqual(row["lookahead_bias_status"], "point_in_time")
        self.assertEqual(row["survivorship_bias_status"], "survivorship_risk_disclosed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_universe_members").fetchone()[0], 1)

    def test_universe_import_rolls_back_all_tables_after_late_invalid_row(self) -> None:
        for storage in ("memory", "file"):
            for commit in (True, False):
                with self.subTest(storage=storage, commit=commit), tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    connection = (
                        new_connection()
                        if storage == "memory"
                        else connect_database(root / "phase2.sqlite")
                    )
                    if storage == "file":
                        initialize_database(connection)
                    try:
                        malformed = root / "malformed-universe.csv"
                        write_csv(malformed, UNIVERSE_COLUMNS, [
                            universe_row(),
                            universe_row(ticker="XYZ", cik="0000000002", exchange="INVALID"),
                        ])
                        if not commit:
                            connection.execute("BEGIN")
                        with self.assertRaisesRegex(ValueError, "supported US listing venue"):
                            import_sp500_universe(
                                connection, malformed, request_id=f"atomic-invalid-{storage}-{commit}", commit=commit,
                            )

                        connection.commit()
                        for table in (
                            "phase2_universe_inputs",
                            "phase2_universe_input_members",
                            "phase2_universe_members",
                        ):
                            self.assertEqual(
                                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                0,
                                table,
                            )

                        valid = root / "valid-universe.csv"
                        write_csv(valid, UNIVERSE_COLUMNS, [universe_row()])
                        if not commit:
                            connection.execute("BEGIN")
                        self.assertEqual(
                            import_sp500_universe(
                                connection, valid, request_id=f"atomic-valid-{storage}-{commit}", commit=commit,
                            ),
                            1,
                        )
                        if not commit:
                            connection.commit()
                            connection.execute("BEGIN")
                        self.assertEqual(
                            import_sp500_universe(
                                connection, valid, request_id=f"atomic-valid-{storage}-{commit}", commit=commit,
                            ),
                            0,
                        )
                        if not commit:
                            connection.commit()
                        for table in (
                            "phase2_universe_inputs",
                            "phase2_universe_input_members",
                            "phase2_universe_members",
                        ):
                            self.assertEqual(
                                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                                1,
                                table,
                            )
                    finally:
                        connection.close()

    def test_universe_rejects_retrieval_before_effective_date(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            write_csv(path, UNIVERSE_COLUMNS, [universe_row(retrieved_at="2026-08-30T23:00:00+00:00")])
            with self.assertRaises(ValueError):
                import_sp500_universe(connection, path, request_id="bad-chronology")

    def test_market_refresh_normalizes_utc_and_rejects_partial_nonfinite_data(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market.csv"
            write_csv(path, MARKET_COLUMNS, [market_row()])
            accepted = refresh_market_observations(
                connection, FixtureMarketProvider(path), ["ABC"],
                request_id="market-a", retrieved_at="2026-09-01T00:00:00+00:00",
            )
        self.assertEqual(accepted, 5)
        row = connection.execute("SELECT * FROM phase2_market_observations WHERE field = 'close'").fetchone()
        self.assertEqual(row["retrieved_at"], "2026-08-31T16:00:00+00:00")
        self.assertEqual(row["field"], "close")

        bad_provider = lambda tickers, retrieved_at: [MarketObservation(
            ticker="ABC", observation_date="2026-09-01", values={"close": "nan"},
            currency="USD", retrieved_at=retrieved_at, citation="fixture:bad",
            provider="bad", provider_observation_id="bad-1", source_version="v1",
        )]
        self.assertEqual(
            refresh_market_observations(
                connection, bad_provider, ["ABC"], request_id="bad-market",
                retrieved_at="2026-09-01T00:00:00+00:00",
            ),
            0,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM phase2_failures WHERE failure_code = 'invalid_market_observation'").fetchone()[0],
            1,
        )

    def test_market_source_conflict_and_stale_data_are_explicit(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        observations = [
            MarketObservation(
                ticker="ABC", observation_date="2026-08-31", values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="https://example.test/a",
                provider="provider-a", provider_observation_id="a-1", source_version="v1",
                freshness_status="current",
            ),
            MarketObservation(
                ticker="ABC", observation_date="2026-08-31", values={"open": 100, "high": 105, "low": 99, "close": 105, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="https://example.test/b",
                provider="provider-b", provider_observation_id="b-1", source_version="v1",
                freshness_status="stale",
            ),
        ]
        provider = lambda tickers, retrieved_at: observations
        self.assertEqual(refresh_market_observations(
            connection, provider, ["ABC"], request_id="conflict", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 10)
        self.assertGreaterEqual(
            connection.execute("SELECT COUNT(*) FROM phase2_failures WHERE failure_code = 'source_conflict'").fetchone()[0], 1,
        )
        report = phase2_evidence_report(connection, as_of="2026-09-01")
        self.assertNotIn(
            "close",
            {row["field"] for row in report["market_observations"]},
        )

    def test_unavailable_provider_is_a_replayable_failure(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)

        class UnavailableProvider:
            def fetch_daily(self, tickers: list[str], *, retrieved_at: str) -> list[MarketObservation]:
                raise RuntimeError("not used")

        provider = UnavailableProvider()
        provider.fetch_daily = lambda tickers, *, retrieved_at: (_ for _ in ()).throw(  # type: ignore[method-assign]
            ProviderUnavailable("offline"),
        )
        self.assertEqual(refresh_market_observations(
            connection, provider, ["ABC"], request_id="offline-a", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 0)
        self.assertEqual(refresh_market_observations(
            connection, provider, ["ABC"], request_id="offline-a", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE failure_code = 'provider_unavailable'",
        ).fetchone()[0], 1)


class Phase2EvidenceTests(unittest.TestCase):
    def test_sec_provider_uses_injected_read_only_transport(self) -> None:
        calls: list[tuple[str, dict[str, str], float]] = []

        class Transport:
            def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                calls.append((url, headers, timeout))
                return HttpResponse(
                    200,
                    json.dumps({"filings": {"recent": {
                        "accessionNumber": ["0000000001-26-000001"],
                        "filingDate": ["2026-08-31"], "reportDate": ["2026-08-30"],
                        "form": ["4"], "primaryDocument": ["ownership.xml"],
                    }}}).encode(),
                )

        records = SECEdgarProvider(Transport(), user_agent="Ilham research [REDACTED]").fetch_submissions(
            "1", ticker="ABC", retrieved_at="2026-09-01T00:00:00+00:00",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(calls[0][0], "https://data.sec.gov/submissions/CIK0000000001.json")
        self.assertEqual(calls[0][1]["User-Agent"], "Ilham research [REDACTED]")
        self.assertEqual(calls[0][2], 20)

    def test_sec_filing_and_fact_metadata_is_normalized_without_invention(self) -> None:
        payload = {
            "filings": {"recent": {
                "accessionNumber": ["0000000001-26-000001"],
                "filingDate": ["2026-08-31"], "reportDate": ["2026-08-29"],
                "form": ["4"], "primaryDocument": ["ownership.xml"],
            }},
        }
        filings = normalize_sec_submissions(
            payload, ticker="ABC", cik="0000000001", retrieved_at="2026-09-01T00:00:00+00:00",
        )
        self.assertEqual(filings[0].evidence_subtype, "form_4")
        self.assertEqual(filings[0].uncertainty_status, "filing_lag")
        self.assertEqual(filings[0].ticker, "ABC")

        facts = normalize_sec_company_facts({
            "cik": "0000000001",
            "facts": {"us-gaap": {"Revenue": {"units": {"USD": [{
                "end": "2026-06-30", "val": 123, "accn": "0000000001-26-000002",
                "form": "10-Q", "filed": "2026-08-01", "fy": 2026, "fp": "Q2",
            }]}}}},
        }, ticker="ABC", cik="0000000001", retrieved_at="2026-09-01T00:00:00+00:00")
        self.assertEqual(facts[0].fact_field, "Revenue")
        self.assertEqual(facts[0].period_end, "2026-06-30")
        self.assertEqual(facts[0].fact_value, "123")

        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        self.assertEqual(ingest_evidence(connection, filings + facts, request_id="sec-a"), 2)
        self.assertEqual(ingest_evidence(connection, filings + facts, request_id="sec-a"), 0)

    def test_sec_ownership_lag_and_ambiguous_or_wrong_mapping_fail_closed(self) -> None:
        payload = {"filings": {"recent": {
            "accessionNumber": ["0000000001-26-000001"], "filingDate": ["2026-08-31"],
            "reportDate": ["2026-08-30"], "form": ["13F-HR"], "primaryDocument": ["f.xml"],
        }}}
        with self.assertRaisesRegex(ValueError, "does not match"):
            normalize_sec_submissions(payload, ticker="ABC", cik="0000000002", retrieved_at="2026-09-01T00:00:00+00:00")

        config = RssSourceConfig(
            source_name="Synthetic RSS", source_url="https://example.test/feed.xml",
            source_version="rss-v1", terms_url="https://example.test/terms",
            entity_aliases={"ABC": ("Synthetic Corp",), "XYZ": ("Synthetic Corp",)},
            content_policy="metadata_only",
        )
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        xml = b"""<rss><channel><item><guid>n-1</guid><link>https://example.test/n-1</link><title>Synthetic Corp update</title><description>text</description><pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
        with self.assertRaises(ValueError):
            normalize_rss_feed(xml, config=config, retrieved_at="2026-09-01T01:00:00+00:00")

    def test_article_deduplication_and_unsupported_flow_proxy(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        config = RssSourceConfig(
            source_name="Synthetic RSS", source_url="https://example.test/feed.xml",
            source_version="rss-v1", terms_url="https://example.test/terms",
            entity_aliases={"ABC": ("ABC",)}, content_policy="bounded_excerpt",
        )
        xml = b"""<rss><channel><item><guid>n-1</guid><link>https://example.test/n-1</link><title>ABC update</title><description>bounded evidence</description><pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>"""
        record = normalize_rss_feed(xml, config=config, retrieved_at="2026-09-01T01:00:00+00:00")[0]
        self.assertEqual(ingest_evidence(connection, [record], request_id="news-a"), 1)
        self.assertEqual(ingest_evidence(connection, [record], request_id="news-a"), 0)
        with self.assertRaises(ValueError):
            normalize_flow_proxy(FlowProxyObservation(
                ticker="ABC", proxy_type="whale_activity", observed_at="2026-09-01",
                value="1", unit="count", source_name="x", source_url="https://example.test/x",
                citation="x", retrieved_at="2026-09-01T01:00:00+00:00", source_version="v1",
            ))
        with self.assertRaises(ValueError):
            normalize_rss_feed(b"<rss><channel>", config=config, retrieved_at="2026-09-01T01:00:00+00:00")

    def test_sec_normalizers_reject_nonofficial_source_hosts(self) -> None:
        payload = {"filings": {"recent": {
            "accessionNumber": ["0000000001-26-000001"], "filingDate": ["2026-08-31"],
            "reportDate": ["2026-08-30"], "form": ["4"], "primaryDocument": ["ownership.xml"],
        }}}
        with self.assertRaisesRegex(ValueError, "official provider host"):
            normalize_sec_submissions(payload, ticker="ABC", cik="0000000001",
                                       retrieved_at="2026-09-01T00:00:00+00:00",
                                       source_url="https://evil.example/sec")


class Phase2BoundaryAndE2ETests(unittest.TestCase):
    def test_refresh_and_evidence_report_are_deterministic_and_have_no_action_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            connection = connect_database(root / "phase2.sqlite")
            initialize_database(connection)
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="e2e-a",
            )
            second = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="e2e-a",
            )
            self.assertEqual(first.accepted_market_observations, 5)
            self.assertEqual(second.accepted_market_observations, 5)
            report = phase2_evidence_report(connection, as_of="2026-09-01")
            serialized = json.dumps(report, sort_keys=True).upper()
            for forbidden in ("BUY", "HOLD", "SELL", "ORDER", "BROKER"):
                self.assertNotIn(forbidden, serialized)
            self.assertEqual(report["phase"], "phase2-evidence")
            self.assertTrue(report["snapshot_hash"])

    def test_configured_refresh_wires_market_sec_companyfacts_and_reports_missing_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "phase2.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "configured-key", "base_url": "https://www.alphavantage.co/query", "timeout_seconds": 12},
                "sec": {"user_agent": "phase2-test [REDACTED]", "submissions_base_url": "https://data.sec.gov/submissions/", "companyfacts_base_url": "https://data.sec.gov/api/xbrl/companyfacts/", "timeout_seconds": 18},
                "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                calls: list[tuple[str, float]] = []

                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    self.calls.append((url, timeout))
                    if "alphavantage" in url:
                        return HttpResponse(200, json.dumps({"Time Series (Daily)": {
                            "2026-08-31": {"1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10"},
                        }}).encode())
                    if "companyfacts" in url:
                        return HttpResponse(200, json.dumps({"cik": "0000000001", "facts": {"us-gaap": {"Revenue": {"units": {"USD": [{
                            "end": "2026-06-30", "val": 123, "accn": "0000000001-26-000002", "form": "10-Q", "filed": "2026-08-01",
                        }]}}}}}).encode())
                    return HttpResponse(200, json.dumps({"filings": {"recent": {
                        "accessionNumber": ["0000000001-26-000001"], "filingDate": ["2026-08-31"], "reportDate": ["2026-08-30"],
                        "form": ["4"], "primaryDocument": ["ownership.xml"],
                    }}}).encode())

            connection = connect_database(root / "phase2.sqlite")
            initialize_database(connection)
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe, as_of="2026-09-01",
                request_id="configured-a", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
            self.assertEqual(result.accepted_market_observations, 5)
            self.assertEqual(result.accepted_evidence, 2)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM phase2_evidence WHERE evidence_subtype = 'company_fact'",
            ).fetchone()[0], 1)
            self.assertIn(12, {timeout for _, timeout in Transport.calls})
            self.assertIn(18, {timeout for _, timeout in Transport.calls})


class Phase2ReviewRegressionTests(unittest.TestCase):
    def test_scope_cutoff_mismatch_is_rejected_before_market_or_evidence_persistence(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
        ).fetchone()[0]
        scope_id = _persist_refresh_scope(
            connection, request_id="immutable-cutoff", as_of="2026-09-01", member_ids=[member_id],
        )
        future_market = MarketObservation(
            ticker="ABC", observation_date="2026-09-02",
            values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
            currency="USD", retrieved_at="2026-09-02T00:00:00+00:00",
            citation="https://example.test/immutable-cutoff-market", provider="immutable-cutoff-provider",
            provider_observation_id="future-market", source_version="v1",
        )
        with self.assertRaisesRegex(ValueError, "immutable scope cutoff"):
            refresh_market_observations(
                connection, lambda tickers, retrieved_at: [future_market], ["ABC"],
                request_id="immutable-cutoff", retrieved_at="2026-09-02T00:00:00+00:00",
                as_of="2026-09-02", scope_id=scope_id,
            )
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_market_observations",
        ).fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_market_observation_bindings",
        ).fetchone()[0], 0)

        future_evidence = evidence_record(
            evidence_identity="immutable-cutoff-evidence", publication_at="2026-09-02T00:00:00+00:00",
            retrieved_at="2026-09-02T00:00:00+00:00",
            citation="https://example.test/immutable-cutoff-evidence",
        )
        with self.assertRaisesRegex(ValueError, "immutable scope cutoff"):
            ingest_evidence(
                connection, [future_evidence], request_id="immutable-cutoff",
                as_of="2026-09-02", scope_id=scope_id,
            )
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence",
        ).fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence_bindings",
        ).fetchone()[0], 0)

    def test_batch_local_conflicts_preserve_historical_market_and_evidence_bindings(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", cik="0000000001"),
                universe_row(ticker="XYZ", cik="0000000002", issuer_name="Other Corp"),
            ])
            import_sp500_universe(connection, universe, request_id="batch-local-universe")

        member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
        ).fetchone()[0]
        market_a_scope = _persist_refresh_scope(
            connection, request_id="batch-market-a", as_of="2026-09-01", member_ids=[member_id],
        )
        market_b_scope = _persist_refresh_scope(
            connection, request_id="batch-market-b", as_of="2026-09-01", member_ids=[member_id],
        )

        def market(close: int, observation_date: str, provider_observation_id: str) -> MarketObservation:
            return MarketObservation(
                ticker="ABC", observation_date=observation_date,
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00",
                citation="https://example.test/batch-local-market", provider="batch-local-provider",
                provider_observation_id=provider_observation_id, source_version="v1",
            )

        shared_market = market(104, "2026-08-31", "shared-market")
        conflicting_market = market(105, "2026-08-31", "shared-market")
        continuation_market = market(103, "2026-08-30", "unrelated-market")
        with patch.object(
            phase2_module, "_detect_market_conflicts", wraps=phase2_module._detect_market_conflicts,
        ) as market_detector:
            self.assertEqual(refresh_market_observations(
                connection, lambda tickers, retrieved_at: [shared_market], ["ABC"],
                request_id="batch-market-a", retrieved_at="2026-09-01T00:00:00+00:00",
                as_of="2026-09-01", scope_id=market_a_scope,
            ), 5)
            first_market_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-market-a", scope_id=market_a_scope,
            )
            original_market_ids = {
                row[0] for row in connection.execute(
                    "SELECT observation_id FROM phase2_market_observation_bindings WHERE request_id = 'batch-market-a'",
                ).fetchall()
            }
            original_market_failures = connection.execute(
                "SELECT failure_id, failure_code, ticker, observed_at FROM phase2_failures WHERE request_id = 'batch-market-a' ORDER BY failure_id",
            ).fetchall()

            self.assertEqual(refresh_market_observations(
                connection, lambda tickers, retrieved_at: [conflicting_market], ["ABC"],
                request_id="batch-market-b", retrieved_at="2026-09-01T00:00:00+00:00",
                as_of="2026-09-01", scope_id=market_b_scope,
            ), 1)
            second_market_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-market-b", scope_id=market_b_scope,
            )
            self.assertEqual(len(second_market_report["market_conflicts"]), 1)
            self.assertEqual(
                phase2_evidence_report(
                    connection, as_of="2026-09-01", request_id="batch-market-a", scope_id=market_a_scope,
                ),
                first_market_report,
            )

            self.assertEqual(refresh_market_observations(
                connection, lambda tickers, retrieved_at: [continuation_market], ["ABC"],
                request_id="batch-market-a", retrieved_at="2026-09-01T00:00:00+00:00",
                as_of="2026-09-01", scope_id=market_a_scope,
            ), 5)
            continuation_market_ids = {
                row[0] for row in connection.execute(
                    "SELECT observation_id FROM phase2_market_observations WHERE provider_observation_id = 'unrelated-market'",
                ).fetchall()
            }
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM phase2_market_observation_bindings WHERE request_id = 'batch-market-a' AND observation_id IN ({}) AND conflict_status = 'usable'".format(
                        ",".join("?" for _ in original_market_ids),
                    ), tuple(original_market_ids),
                ).fetchone()[0],
                len(original_market_ids),
            )
            self.assertEqual(
                set(market_detector.call_args.kwargs["observation_ids"]), continuation_market_ids,
            )
            self.assertEqual(connection.execute(
                "SELECT failure_id, failure_code, ticker, observed_at FROM phase2_failures WHERE request_id = 'batch-market-a' ORDER BY failure_id",
            ).fetchall(), original_market_failures)
            continued_market_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-market-a", scope_id=market_a_scope,
            )
            self.assertEqual(
                {row["observation_id"] for row in continued_market_report["market_observations"]}
                & original_market_ids,
                original_market_ids,
            )
            self.assertFalse(continued_market_report["market_conflicts"])
            market_failure_count = connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'batch-market-a'",
            ).fetchone()[0]
            self.assertEqual(refresh_market_observations(
                connection, lambda tickers, retrieved_at: [continuation_market], ["ABC"],
                request_id="batch-market-a", retrieved_at="2026-09-01T00:00:00+00:00",
                as_of="2026-09-01", scope_id=market_a_scope,
            ), 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'batch-market-a'",
            ).fetchone()[0], market_failure_count)
            self.assertEqual(
                phase2_evidence_report(
                    connection, as_of="2026-09-01", request_id="batch-market-a", scope_id=market_a_scope,
                ),
                continued_market_report,
            )

        control_market_scope = _persist_refresh_scope(
            connection, request_id="batch-market-control", as_of="2026-09-01", member_ids=[member_id],
        )
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(101, "2026-08-29", "control-market")], ["ABC"],
            request_id="batch-market-control", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01", scope_id=control_market_scope,
        ), 5)
        control_market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="batch-market-control", scope_id=control_market_scope,
        )
        self.assertEqual(control_market_report["market_conflicts"], [])
        self.assertEqual(len(control_market_report["market_observations"]), 5)

        evidence_a_scope = _persist_refresh_scope(
            connection, request_id="batch-evidence-a", as_of="2026-09-01", member_ids=[member_id],
        )
        evidence_b_scope = _persist_refresh_scope(
            connection, request_id="batch-evidence-b", as_of="2026-09-01", member_ids=[member_id],
        )
        shared_evidence = evidence_record(
            evidence_identity="shared-evidence", fact_value="123",
            citation="https://example.test/batch-local-evidence",
        )
        conflicting_evidence = evidence_record(
            evidence_identity="shared-evidence", fact_value="124",
            citation="https://example.test/batch-local-evidence",
        )
        continuation_evidence = evidence_record(
            evidence_identity="unrelated-evidence", fact_value="125",
            citation="https://example.test/batch-local-evidence-continuation",
        )
        with patch.object(
            phase2_module, "_detect_evidence_conflicts", wraps=phase2_module._detect_evidence_conflicts,
        ) as evidence_detector:
            self.assertEqual(ingest_evidence(
                connection, [shared_evidence], request_id="batch-evidence-a",
                as_of="2026-09-01", scope_id=evidence_a_scope,
            ), 1)
            first_evidence_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-evidence-a", scope_id=evidence_a_scope,
            )
            original_evidence_ids = {
                row[0] for row in connection.execute(
                    "SELECT evidence_id FROM phase2_evidence_bindings WHERE request_id = 'batch-evidence-a'",
                ).fetchall()
            }
            original_evidence_failures = connection.execute(
                "SELECT failure_id, failure_code, ticker, observed_at FROM phase2_failures WHERE request_id = 'batch-evidence-a' ORDER BY failure_id",
            ).fetchall()

            self.assertEqual(ingest_evidence(
                connection, [conflicting_evidence], request_id="batch-evidence-b",
                as_of="2026-09-01", scope_id=evidence_b_scope,
            ), 1)
            second_evidence_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-evidence-b", scope_id=evidence_b_scope,
            )
            self.assertEqual(len(second_evidence_report["conflicts"]), 1)
            self.assertEqual(
                phase2_evidence_report(
                    connection, as_of="2026-09-01", request_id="batch-evidence-a", scope_id=evidence_a_scope,
                ),
                first_evidence_report,
            )

            self.assertEqual(ingest_evidence(
                connection, [continuation_evidence], request_id="batch-evidence-a",
                as_of="2026-09-01", scope_id=evidence_a_scope,
            ), 1)
            continuation_evidence_ids = {
                row[0] for row in connection.execute(
                    "SELECT evidence_id FROM phase2_evidence WHERE evidence_identity = 'unrelated-evidence'",
                ).fetchall()
            }
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM phase2_evidence_bindings WHERE request_id = 'batch-evidence-a' AND evidence_id IN ({}) AND conflict_status = 'usable'".format(
                        ",".join("?" for _ in original_evidence_ids),
                    ), tuple(original_evidence_ids),
                ).fetchone()[0],
                len(original_evidence_ids),
            )
            self.assertEqual(
                set(evidence_detector.call_args.kwargs["evidence_ids"]), continuation_evidence_ids,
            )
            self.assertEqual(connection.execute(
                "SELECT failure_id, failure_code, ticker, observed_at FROM phase2_failures WHERE request_id = 'batch-evidence-a' ORDER BY failure_id",
            ).fetchall(), original_evidence_failures)
            continued_evidence_report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="batch-evidence-a", scope_id=evidence_a_scope,
            )
            self.assertEqual(
                {row["evidence_id"] for row in continued_evidence_report["evidence"]}
                & original_evidence_ids,
                original_evidence_ids,
            )
            self.assertFalse(continued_evidence_report["conflicts"])
            evidence_failure_count = connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'batch-evidence-a'",
            ).fetchone()[0]
            self.assertEqual(ingest_evidence(
                connection, [continuation_evidence], request_id="batch-evidence-a",
                as_of="2026-09-01", scope_id=evidence_a_scope,
            ), 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'batch-evidence-a'",
            ).fetchone()[0], evidence_failure_count)
            self.assertEqual(
                phase2_evidence_report(
                    connection, as_of="2026-09-01", request_id="batch-evidence-a", scope_id=evidence_a_scope,
                ),
                continued_evidence_report,
            )

        control_evidence_scope = _persist_refresh_scope(
            connection, request_id="batch-evidence-control", as_of="2026-09-01", member_ids=[member_id],
        )
        self.assertEqual(ingest_evidence(
            connection, [evidence_record(
                evidence_identity="control-evidence", fact_value="126",
                citation="https://example.test/batch-local-evidence-control",
            )], request_id="batch-evidence-control", as_of="2026-09-01", scope_id=control_evidence_scope,
        ), 1)
        control_evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="batch-evidence-control", scope_id=control_evidence_scope,
        )
        self.assertEqual(control_evidence_report["conflicts"], [])
        self.assertEqual(len(control_evidence_report["evidence"]), 1)

        future_scope = _persist_refresh_scope(
            connection, request_id="batch-future-evidence", as_of="2026-09-01", member_ids=[member_id],
        )
        with self.assertRaisesRegex(ValueError, "immutable scope cutoff"):
            ingest_evidence(
                connection, [evidence_record(
                    evidence_identity="shared-evidence", fact_value="999",
                    citation="https://example.test/batch-local-future",
                    publication_at="2026-09-02T00:00:00+00:00",
                    retrieved_at="2026-09-03T00:00:00+00:00",
                )], request_id="batch-future-evidence", as_of="2026-09-01", scope_id=future_scope,
            )
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence WHERE fact_value = '999'",
        ).fetchone()[0], 0)
        self.assertEqual(phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="batch-future-evidence", scope_id=future_scope,
        )["conflicts"], [])

    def test_public_reference_rejects_alternate_private_hosts_credentials_controls_and_redirects(self) -> None:
        unsafe_hosts = (
            "2130706433", "0x7f000001", "0177.0.0.1", "127.1", "localhost",
            "10.0.0.1", "169.254.1.1", "[::ffff:127.0.0.1]",
        )
        for host in unsafe_hosts:
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    validate_public_reference(f"https://{host}/feed.xml", "RSS source_url")
        for value in (
            "https://user:password@example.test/feed.xml",
            "https://example.test/feed.xml?api_key=secret",
            "https://example.test/feed.xml?side=BUY",
            "https://example.test/order/submit",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_public_reference(value, "RSS source_url")

        with self.assertRaises(ProviderUnavailable):
            _NoRedirectHandler().redirect_request(None, None, 302, "redirect", {}, "https://example.test/next")

    def test_rss_resolution_fails_closed_for_non_global_and_uncertain_hosts(self) -> None:
        config = RssSourceConfig(
            source_name="configured-rss", source_url="https://public.example/feed.xml",
            source_version="rss-v1", terms_url="https://public.example/terms",
            entity_aliases={"ABC": ("ABC",)}, content_policy="metadata_only",
        )

        class Transport:
            def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                raise AssertionError("transport must not run after failed host validation")

        with patch(
            "nisa_quant.evidence_providers.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("192.168.1.10", 443))],
        ):
            with self.assertRaises(ValueError):
                RssFeedProvider(Transport(), config).fetch(retrieved_at="2026-09-01T00:00:00+00:00")
        with patch("nisa_quant.evidence_providers.socket.getaddrinfo", side_effect=OSError("unknown host")):
            with self.assertRaises(ValueError):
                RssFeedProvider(Transport(), config).fetch(retrieved_at="2026-09-01T00:00:00+00:00")

    def test_control_scanner_rejects_normalized_control_and_credential_variants(self) -> None:
        unsafe_values = (
            "Analyst rates ABC a BUY",
            "A\u200bnalyst rates ABC a B\u200bUY",
            "Rating: BUY",
            "Consensus rating changed to SELL",
            "R\u200ba\u200bt\u200bi\u200bn\u200bg: B\u200bU\u200bY",
            "Rating%3A%20BUY",
            "Consensus%20rating%20changed%20to%20SELL",
            "https://example.test/%6f%72%64%65%72",
            "Authorization: Basic ***",
            "AWS_SECRET_ACCESS_KEY hunter2",
            "broker ref BROKER123",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                self.assertTrue(contains_control_content(value))
        self.assertFalse(contains_control_content("the issuer will buy equipment"))
        self.assertFalse(contains_control_content("a token is a generic credential concept"))

    def test_control_scanner_recurses_encoded_keys_and_nested_metadata(self) -> None:
        for value in (
            {"rating": "BUY"},
            {"nested": {"%72ating": "unknown"}},
            {"citation": "https://example.test/%256f%2572%2564%2565%2572"},
        ):
            with self.subTest(value=value):
                self.assertTrue(contains_control_content(value))

    def test_unsafe_record_fields_are_rejected_before_persistence_and_audited(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        field_values = {
            "evidence_identity": "Analyst rates ABC a BUY",
            "evidence_subtype": "Analyst rates ABC a BUY",
            "source_name": "Authorization: Basic ***",
            "source_identifier": "broker ref BROKER123",
            "fact_unit": "AWS_SECRET_ACCESS_KEY hunter2",
            "topic": "Analyst rates ABC a BUY",
            "evidence_text": "A\u200bnalyst rates ABC a B\u200bUY",
            "source_version": "broker ref BROKER123",
            "citation": "https://example.test/news?note=broker%20ref%20BROKER123",
            "metadata": {"nested": {"Authorization": "Basic ***"}},
        }
        for index, (field, value) in enumerate(field_values.items()):
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, "control content"):
                    ingest_evidence(
                        connection, [evidence_record(**{field: value})],
                        request_id=f"unsafe-field-{index}",
                    )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE failure_code = 'invalid_evidence_record'",
            ).fetchone()[0],
            len(field_values),
        )

    def test_emitted_collections_are_scanned_across_all_persisted_evidence_fields(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        original = evidence_record(evidence_text="safe issuer update")
        self.assertEqual(ingest_evidence(connection, [original], request_id="emission-fields"), 1)
        for index, column in enumerate((
            "evidence_identity", "evidence_subtype", "source_name", "source_identifier",
            "fact_unit", "topic", "evidence_text", "source_version", "citation",
        )):
            with self.subTest(column=column):
                connection.execute(
                    f"UPDATE phase2_evidence SET {column} = ? WHERE evidence_id = (SELECT evidence_id FROM phase2_evidence LIMIT 1)",
                    ("Analyst rates ABC a BUY",),
                )
                connection.commit()
                with self.assertRaisesRegex(ValueError, "control content"):
                    phase2_evidence_report(connection, as_of="2026-09-01")
                connection.execute(f"UPDATE phase2_evidence SET {column} = ?", (getattr(original, column),))
                connection.commit()

    def test_company_facts_requires_valid_top_level_cik_before_attribution(self) -> None:
        for payload in ({"facts": {}}, {"cik": "not-a-cik", "facts": {}}, {"cik": "0000000002", "facts": {}}):
            with self.subTest(payload=payload):
                issues: list[str] = []
                self.assertEqual(
                    normalize_sec_company_facts(
                        payload, ticker="ABC", cik="0000000001",
                        retrieved_at="2026-09-01T00:00:00+00:00", issues=issues,
                    ),
                    [],
                )
                self.assertTrue(any("cik" in issue.lower() for issue in issues))

    def test_latest_membership_state_removes_later_inactive_member_and_ignores_unrelated_rows(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            rows = [
                universe_row(ticker="ABC", cik="0000000001", effective_date="2026-08-30"),
                universe_row(ticker="ABC", cik="0000000001"),
                universe_row(ticker="REMOVED", cik="0000000002", effective_date="2026-08-31"),
                universe_row(ticker="REMOVED", cik="0000000002", effective_date="2026-09-01", membership_status="inactive", retrieved_at="2026-09-01T12:00:00+09:00"),
            ]
            write_csv(path, UNIVERSE_COLUMNS, rows)
            import_sp500_universe(connection, path, request_id="membership-states")
            unrelated = Path(directory) / "unrelated.csv"
            write_csv(unrelated, UNIVERSE_COLUMNS, [universe_row(ticker="UNRELATED", cik="0000000003")])
            import_sp500_universe(connection, unrelated, request_id="membership-unrelated")
            members = _selected_current_members(connection, path, as_of="2026-09-01")
        self.assertEqual(len(members), 1)
        self.assertEqual(members[0]["ticker"], "ABC")
        self.assertEqual(members[0]["effective_date"], "2026-08-31")

    def test_evidence_report_uses_latest_refresh_scope_and_excludes_prior_or_unrelated_universes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_universe = root / "first-universe.csv"
            second_universe = root / "second-universe.csv"
            first_market = root / "first-market.csv"
            second_market = root / "second-market.csv"
            write_csv(first_universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(first_market, MARKET_COLUMNS, [market_row()])
            write_csv(second_universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", cik="0000000001", effective_date="2026-09-01", membership_status="inactive", retrieved_at="2026-09-01T00:00:00+00:00"),
                universe_row(ticker="DEF", cik="0000000002", issuer_name="Second Corp"),
            ])
            write_csv(second_market, MARKET_COLUMNS, [market_row(ticker="DEF", citation="fixture:phase2-market#DEF-2026-08-31")])
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=first_universe, market_path=first_market,
                as_of="2026-09-01", request_id="scope-first",
            )
            self.assertEqual(first.status, "completed")
            second = refresh_phase2_fixtures(
                connection, universe_path=second_universe, market_path=second_market,
                as_of="2026-09-01", request_id="scope-second",
            )
            self.assertEqual(second.status, "completed")
            unrelated = root / "unrelated.csv"
            write_csv(unrelated, UNIVERSE_COLUMNS, [universe_row(ticker="ZZZ", cik="0000000003")])
            import_sp500_universe(connection, unrelated, request_id="scope-unrelated")

            report = phase2_evidence_report(connection, as_of="2026-09-01")
            self.assertEqual([row["ticker"] for row in report["universe"]], ["DEF"])
            self.assertEqual({row["ticker"] for row in report["market_observations"]}, {"DEF"})
            self.assertNotIn("ABC", json.dumps(report))
            self.assertNotIn("ZZZ", json.dumps(report))

    def test_configured_rss_aliases_and_evidence_are_limited_to_current_members(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", cik="0000000001"),
                universe_row(ticker="REMOVED", cik="0000000002", effective_date="2026-08-31"),
                universe_row(ticker="REMOVED", cik="0000000002", effective_date="2026-09-01", membership_status="inactive", retrieved_at="2026-09-01T12:00:00+09:00"),
            ])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"enabled": False},
                "sec": {"enabled": False},
                "rss": {
                    "source_name": "configured-rss", "source_url": "https://example.test/feed.xml",
                    "source_version": "rss-v1", "terms_url": "https://example.test/terms",
                    "content_policy": "metadata_only",
                    "entity_aliases": {"ABC": ["ABC"], "REMOVED": ["REMOVED"]},
                },
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, b"""<rss><channel><item><guid>removed-1</guid><link>https://example.test/removed</link><title>REMOVED update</title><description>excluded</description><pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>""")

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="rss-excluded", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)

    def test_configured_success_counts_replayed_usable_market_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "in-memory", "base_url": "https://www.alphavantage.co/query"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"Time Series (Daily)": {"2026-08-31": {
                        "1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10",
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="replay-success-a", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
            second = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="replay-success-b", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
        self.assertEqual(first.status, "completed_with_warnings")
        self.assertEqual(second.status, "completed_with_warnings")
        self.assertEqual(second.accepted_market_observations, 0)

    def test_configured_failed_attempt_does_not_reuse_same_scope_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "fixture-key", "base_url": "https://www.alphavantage.co/query"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def __init__(self, observation_date: str) -> None:
                    self.observation_date = observation_date

                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"Time Series (Daily)": {self.observation_date: {
                        "1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10",
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe, as_of="2026-09-01",
                request_id="configured-attempt-a", transport=Transport("2026-08-31"),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
            second = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe, as_of="2026-09-01",
                request_id="configured-attempt-b", transport=Transport("2026-09-02"),
                clock=lambda: "2026-09-01T00:00:01+00:00",
            )

            first_report = json.loads(connection.execute(
                "SELECT report_json FROM phase2_snapshots WHERE snapshot_id = ?", (first.snapshot_id,)
            ).fetchone()[0])
            second_row = connection.execute(
                "SELECT request_id, scope_id, report_json FROM phase2_snapshots WHERE snapshot_id = ?",
                (second.snapshot_id,),
            ).fetchone()
            second_report = json.loads(second_row["report_json"])
            self.assertEqual(first.status, "completed_with_warnings")
            self.assertEqual(len(first_report["market_observations"]), 5)
            self.assertEqual(second.status, "failed")
            self.assertEqual(second_report["market_observations"], [])
            self.assertEqual(second_report["evidence"], [])
            self.assertTrue(second_report["failures"])
            self.assertTrue(all(row["request_id"] == "configured-attempt-b" for row in second_report["failures"]))
            self.assertEqual(second_row["request_id"], second_report["request_id"])
            self.assertEqual(second_row["scope_id"], second_report["scope_id"])

    def test_identical_successful_attempts_bind_replayed_response_ids_per_request(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            news = root / "news.xml"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            news.write_text(
                "<rss><channel><item><guid>replayed-news</guid>"
                "<link>https://example.test/replayed-news</link>"
                "<title>ABC update</title><description>evidence</description>"
                "<pubDate>Tue, 01 Sep 2026 00:00:00 GMT</pubDate></item></channel></rss>",
                encoding="utf-8",
            )
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, news_path=news,
                as_of="2026-09-01", request_id="replayed-attempt-a",
            )
            second = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, news_path=news,
                as_of="2026-09-01", request_id="replayed-attempt-b",
            )

            self.assertEqual(first.status, "completed")
            self.assertEqual(second.status, "completed")
            self.assertEqual(second.accepted_market_observations, 0)
            self.assertEqual(second.accepted_evidence, 0)
            for result, request_id in ((first, "replayed-attempt-a"), (second, "replayed-attempt-b")):
                market_ids = {
                    row[0] for row in connection.execute(
                        "SELECT observation_id FROM phase2_market_observation_bindings WHERE request_id = ?",
                        (request_id,),
                    )
                }
                evidence_ids = {
                    row[0] for row in connection.execute(
                        "SELECT evidence_id FROM phase2_evidence_bindings WHERE request_id = ?",
                        (request_id,),
                    )
                }
                report = json.loads(connection.execute(
                    "SELECT report_json FROM phase2_snapshots WHERE snapshot_id = ?", (result.snapshot_id,)
                ).fetchone()[0])
                self.assertEqual(market_ids, {row["observation_id"] for row in report["market_observations"]})
                self.assertEqual(evidence_ids, {row["evidence_id"] for row in report["evidence"]})
                self.assertEqual(len(market_ids), 5)
                self.assertEqual(len(evidence_ids), 1)
            self.assertEqual(
                {
                    row[0] for row in connection.execute(
                        "SELECT observation_id FROM phase2_market_observation_bindings WHERE request_id = ?",
                        ("replayed-attempt-a",),
                    )
                },
                {
                    row[0] for row in connection.execute(
                        "SELECT observation_id FROM phase2_market_observation_bindings WHERE request_id = ?",
                        ("replayed-attempt-b",),
                    )
                },
            )

    def test_legacy_phase2_rows_receive_idempotent_legacy_bindings(self) -> None:
        connection = connect_database(":memory:")
        self.addCleanup(connection.close)
        connection.execute("""CREATE TABLE phase2_market_observations (
            observation_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, observation_date TEXT NOT NULL,
            field TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, currency TEXT NOT NULL,
            provider TEXT NOT NULL, provider_observation_id TEXT NOT NULL, source_version TEXT NOT NULL,
            citation TEXT NOT NULL, retrieved_at TEXT NOT NULL, freshness_status TEXT NOT NULL
        )""")
        connection.execute(
            """INSERT INTO phase2_market_observations VALUES
               ('legacy-market', 'ABC', '2026-08-31', 'close', '104', 'USD_per_share', 'USD',
                'fixture-market', 'legacy-row', 'v1', 'fixture:legacy',
                '2026-09-01T00:00:00+00:00', 'observed')""",
        )
        connection.execute("""CREATE TABLE phase2_evidence (
            evidence_id TEXT PRIMARY KEY, evidence_identity TEXT NOT NULL, evidence_kind TEXT NOT NULL,
            evidence_subtype TEXT NOT NULL, source_name TEXT NOT NULL, source_identifier TEXT NOT NULL,
            source_url TEXT NOT NULL, ticker TEXT, issuer_cik TEXT, publication_at TEXT,
            period_start TEXT, period_end TEXT, fact_field TEXT, fact_value TEXT, fact_unit TEXT,
            topic TEXT NOT NULL, evidence_text TEXT, source_quality TEXT NOT NULL, recency_status TEXT NOT NULL,
            corroboration_status TEXT NOT NULL, uncertainty_status TEXT NOT NULL, metadata_json TEXT NOT NULL,
            retrieved_at TEXT NOT NULL, source_version TEXT NOT NULL, citation TEXT NOT NULL
        )""")
        connection.execute(
            """INSERT INTO phase2_evidence VALUES
               ('legacy-evidence', 'legacy-logical', 'filing', 'company_fact', 'SEC', 'legacy-source',
                'https://data.sec.gov/facts', 'ABC', '0000000001', '2026-08-01T00:00:00+00:00',
                '2026-07-01', '2026-07-31', 'Revenue', '1', 'USD', 'reported_fact', NULL,
                'authoritative_regulatory', 'old', 'single_source', 'reported_fact', '{}',
                '2026-09-01T00:00:00+00:00', 'v1', 'https://data.sec.gov/facts')""",
        )
        initialize_database(connection)
        initialize_database(connection)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observation_bindings").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence_bindings").fetchone()[0], 1)

    def test_configured_stale_only_market_response_does_not_count_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "in-memory", "base_url": "https://www.alphavantage.co/query"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"Time Series (Daily)": {"2020-01-01": {
                        "1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10",
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="stale-provider", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
        self.assertEqual(result.status, "failed")

    def test_configured_rss_empty_ingestion_does_not_count_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"enabled": False}, "sec": {"enabled": False},
                "rss": {
                    "source_name": "configured-rss", "source_url": "https://example.test/feed.xml",
                    "source_version": "rss-v1", "terms_url": "https://example.test/terms",
                    "content_policy": "metadata_only", "entity_aliases": {"ABC": ["ABC"]},
                },
            }), encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            with patch("nisa_quant.evidence_collection.RssFeedProvider.fetch", return_value=[]):
                result = refresh_phase2_configured(
                    connection, config_path=config, universe_path=universe,
                    as_of="2026-09-01", request_id="empty-rss", transport=object(),
                    clock=lambda: "2026-09-01T00:00:00+00:00",
                )
        self.assertEqual(result.status, "failed")

    def test_evidence_target_requires_current_active_cutoff_state(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            write_csv(path, UNIVERSE_COLUMNS, [
                universe_row(),
                universe_row(effective_date="2026-09-01", membership_status="inactive", retrieved_at="2026-09-01T12:00:00+09:00"),
            ])
            import_sp500_universe(connection, path, request_id="inactive-target")
        with self.assertRaisesRegex(ValueError, "universe mapping"):
            ingest_evidence(connection, [evidence_record()], request_id="inactive-evidence", as_of="2026-09-01")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)

    def test_configured_conflicted_sec_response_does_not_count_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"enabled": False},
                "sec": {"user_agent": "phase2-test [REDACTED]", "max_requests_per_second": 10},
                "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    if "submissions" in url:
                        return HttpResponse(200, json.dumps({"filings": {"recent": {
                            "accessionNumber": [], "filingDate": [], "reportDate": [],
                            "form": [], "primaryDocument": [],
                        }}}).encode())
                    return HttpResponse(200, json.dumps({"cik": "0000000001", "facts": {"us-gaap": {"Revenue": {"units": {"USD": [
                        {"end": "2026-06-30", "val": 123, "accn": "0000000001-26-000002", "form": "10-Q", "filed": "2026-08-01"},
                        {"end": "2026-06-30", "val": 124, "accn": "0000000001-26-000002", "form": "10-Q", "filed": "2026-08-01"},
                    ]}}}}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="conflicted-sec", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
                monotonic_clock=lambda: 0.0, sleeper=lambda _: None,
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence WHERE conflict_status = 'conflict'").fetchone()[0], 2)

    def test_market_contract_rejects_currency_units_provider_and_citation_shape(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        invalid = {
            "currency": "EUR",
            "units": {field: ("units" if field == "volume" else "EUR_per_share") for field in ("open", "high", "low", "close", "volume")},
            "provider": "not a provider",
            "citation": "https://example.test/order?side=BUY",
        }
        for field, value in invalid.items():
            observation_values: dict[str, object] = {
                "ticker": "ABC", "observation_date": "2026-08-31",
                "values": {"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
                "currency": "USD", "retrieved_at": "2026-09-01T00:00:00+00:00",
                "citation": "https://example.test/market", "provider": "fixture-market",
                "provider_observation_id": f"bad-{field}", "source_version": "fixture-v1",
            }
            observation_values[field] = value
            observation = MarketObservation(**observation_values)
            self.assertEqual(refresh_market_observations(
                connection, lambda tickers, retrieved_at, observation=observation: [observation],
                ["ABC"], request_id=f"bad-{field}", retrieved_at="2026-09-01T00:00:00+00:00",
            ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_market_observations",
        ).fetchone()[0], 0)

    def test_sec_forms_are_text_facts_and_all_supported_forms_persist(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        forms = ("3", "4", "5", "SC 13D", "SC 13G", "13F-HR", "10-K", "10-Q", "8-K")
        payload = {"filings": {"recent": {
            "accessionNumber": [f"0000000001-26-{index:06d}" for index in range(1, len(forms) + 1)],
            "filingDate": ["2026-08-31"] * len(forms), "reportDate": ["2026-08-30"] * len(forms),
            "form": list(forms), "primaryDocument": [f"doc-{index}.xml" for index in range(len(forms))],
        }}}
        records = normalize_sec_submissions(
            payload, ticker="ABC", cik="0000000001", retrieved_at="2026-09-01T00:00:00+00:00",
        )
        self.assertEqual([record.fact_value for record in records], list(forms))
        self.assertEqual(ingest_evidence(connection, records, request_id="forms"), len(forms))
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence WHERE fact_field = 'form'",
        ).fetchone()[0], len(forms))

    def test_company_facts_reject_wrong_accession_and_ordered_periods_with_recency(self) -> None:
        issues: list[str] = []
        records = normalize_sec_company_facts({"cik": "0000000001", "facts": {"us-gaap": {"Revenue": {"units": {"USD": [
            {"end": "2026-06-30", "start": "2026-07-01", "val": 123, "accn": "0000000002-26-000002", "form": "10-Q", "filed": "2026-08-01"},
            {"end": "2026-06-30", "start": "2026-04-01", "val": 124, "accn": "0000000001-26-000003", "form": "10-Q", "filed": "2026-08-01"},
            {"end": "2026-06-30", "start": "2026-07-01", "val": 125, "accn": "0000000001-26-000004", "form": "10-Q", "filed": "2026-08-01"},
        ]}}}}}, ticker="ABC", cik="0000000001", retrieved_at="2026-09-01T00:00:00+00:00", issues=issues)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].recency_status, "old")
        self.assertTrue(any("mismatch" in issue for issue in issues))
        self.assertTrue(any("starts after" in issue for issue in issues))

    def test_malformed_numeric_evidence_is_rejected(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        with self.assertRaisesRegex(ValueError, "not numeric"):
            ingest_evidence(connection, [evidence_record(fact_value="not-a-number")], request_id="bad-number")

    def test_changed_evidence_value_provenance_and_metadata_are_conflict_only(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        original = evidence_record()
        changed = evidence_record(
            fact_value="124", citation="https://example.test/changed", metadata={"taxonomy": "ifrs-full"},
        )
        self.assertEqual(ingest_evidence(connection, [original], request_id="conflict-a"), 1)
        self.assertEqual(ingest_evidence(connection, [changed], request_id="conflict-b"), 1)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence WHERE evidence_identity = ?", (original.evidence_identity,),
        ).fetchone()[0], 2)
        with self.assertRaisesRegex(ValueError, "explicit request_id or scope_id"):
            phase2_evidence_report(connection, as_of="2026-09-01")
        first_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="conflict-a",
        )
        second_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="conflict-b",
        )
        self.assertEqual(len(first_report["evidence"]), 1)
        self.assertEqual(first_report["conflicts"], [])
        self.assertEqual(second_report["evidence"], [])
        self.assertEqual(len(second_report["conflicts"]), 1)
        self.assertTrue(all(row["conflict_status"] == "conflict" for row in second_report["conflicts"]))

    def test_safe_issuer_text_passes_but_action_and_credential_metadata_fails(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "universe.csv"
            write_csv(path, UNIVERSE_COLUMNS, [universe_row(issuer_name="Broker Holdings Inc")])
            self.assertEqual(import_sp500_universe(connection, path, request_id="safe-issuer"), 1)
        with self.assertRaisesRegex(ValueError, "control content"):
            ingest_evidence(
                connection, [evidence_record(metadata={"order_payload": {"side": "BUY"}})],
                request_id="malicious-metadata",
            )
        with self.assertRaisesRegex(ValueError, "control content"):
            ingest_evidence(
                connection, [evidence_record(citation="https://example.test/news?side=BUY")],
                request_id="malicious-citation",
            )

    def test_report_failures_use_event_time_not_wall_clock_creation_time(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)

        class Offline:
            def fetch_daily(self, tickers: list[str], *, retrieved_at: str) -> list[MarketObservation]:
                raise ProviderUnavailable("offline")

        refresh_market_observations(
            connection, Offline(), ["ABC"], request_id="historical-failure",
            retrieved_at="2020-01-02T00:00:00+00:00",
        )
        self.assertEqual(len(phase2_evidence_report(connection, as_of="2020-01-02")["failures"]), 1)
        with self.assertRaisesRegex(ValueError, "no unambiguous request-bound scope"):
            phase2_evidence_report(connection, as_of="2020-01-01")

    def test_failed_fixture_refresh_is_atomic_and_replayable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            bad_sec = root / "bad-sec.json"
            bad_sec.write_text("not-json", encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, sec_path=bad_sec,
                as_of="2026-09-01", request_id="failed-refresh",
                sec_ticker="ABC", sec_cik="0000000001",
            )
            second = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, sec_path=bad_sec,
                as_of="2026-09-01", request_id="failed-refresh",
                sec_ticker="ABC", sec_cik="0000000001",
            )
            self.assertEqual(first.status, "failed")
            self.assertEqual(first, second)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_universe_members").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'failed-refresh'",
            ).fetchone()[0], 1)

    def test_configured_malicious_market_host_never_receives_environment_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "phase2.json"
            config.write_text(json.dumps({
                "market": {
                    "provider": "alpha-vantage", "base_url": "https://private.example/query",
                    "api_key_env": "NISA_PHASE2_TEST_SECRET", "timeout_seconds": 12,
                },
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                calls: list[tuple[str, dict[str, str]]] = []

                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    self.calls.append((url, headers))
                    return HttpResponse(200, b"{}")

            previous = os.environ.get("NISA_PHASE2_TEST_SECRET")
            os.environ["NISA_PHASE2_TEST_SECRET"] = "secret-value-never-send"
            try:
                connection = new_connection()
                self.addCleanup(connection.close)
                result = refresh_phase2_configured(
                    connection, config_path=config, universe_path=universe,
                    as_of="2026-09-01", request_id="malicious-host", transport=Transport(),
                )
            finally:
                if previous is None:
                    os.environ.pop("NISA_PHASE2_TEST_SECRET", None)
                else:
                    os.environ["NISA_PHASE2_TEST_SECRET"] = previous

            self.assertEqual(result.status, "failed")
            self.assertEqual(Transport.calls, [])
            self.assertNotIn("secret-value-never-send", json.dumps(result.__dict__ if hasattr(result, "__dict__") else str(result)))
            self.assertNotIn("secret-value-never-send", json.dumps(
                [dict(row) for row in connection.execute("SELECT * FROM phase2_failures")],
            ))

    def test_malformed_market_timestamp_is_audited_at_safe_request_time(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        malformed = MarketObservation(
            ticker="ABC", observation_date="not-a-date",
            values={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            currency="USD", retrieved_at="not-a-timestamp", citation="fixture:bad-time",
            provider="fixture-market", provider_observation_id="bad-time", source_version="v1",
        )
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: [malformed], ["ABC"],
            request_id="malformed-market-time", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 0)
        failure = connection.execute(
            "SELECT * FROM phase2_failures WHERE request_id = 'malformed-market-time'",
        ).fetchone()
        self.assertIsNotNone(failure)
        self.assertEqual(failure["observed_at"], "2026-09-01T00:00:00+00:00")
        self.assertEqual(len(phase2_evidence_report(connection, as_of="2026-09-01")["failures"]), 1)

    def test_company_facts_top_level_cik_mismatch_is_rejected_without_attribution(self) -> None:
        issues: list[str] = []
        records = normalize_sec_company_facts(
            {"cik": "0000000002", "facts": {}}, ticker="ABC", cik="0000000001",
            retrieved_at="2026-09-01T00:00:00+00:00", issues=issues,
        )
        self.assertEqual(records, [])
        self.assertTrue(any("top-level" in issue.lower() and "cik" in issue.lower() for issue in issues))

    def test_company_facts_absent_accession_keeps_explicit_identity_without_invention(self) -> None:
        records = normalize_sec_company_facts(
            {"cik": "0000000001", "facts": {"us-gaap": {"Revenue": {"units": {"USD": [{
                "end": "2026-06-30", "val": 123, "filed": "2026-08-01",
            }]}}}}}, ticker="ABC", cik="0000000001",
            retrieved_at="2026-09-01T00:00:00+00:00",
        )
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].source_identifier, "Revenue:2026-06-30")
        self.assertNotIn("accn", records[0].source_identifier.lower())

    def test_evidence_scans_every_record_field_but_accepts_safe_news_prose(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        safe = evidence_record(evidence_kind="news", evidence_subtype="rss_article",
                               fact_field=None, fact_value=None, fact_unit=None,
                               evidence_text="ABC discusses its broker relationships in a routine issuer update.")
        self.assertEqual(ingest_evidence(connection, [safe], request_id="safe-prose"), 1)
        for field, value in {
            "evidence_text": "BUY NOW at market",
            "topic": "api_key=secret-value",
            "evidence_subtype": "BUY NOW at market",
            "source_identifier": "token=secret-value",
            "source_url": "https://example.test/news?side=BUY",
            "citation": "https://example.test/news?token=secret-value",
            "metadata": {"control": "Bearer secret-value"},
        }.items():
            with self.subTest(field=field):
                overrides = {field: value}
                with self.assertRaises(ValueError):
                    ingest_evidence(connection, [evidence_record(**overrides)], request_id=f"unsafe-{field}")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 1)

    def test_market_freshness_is_recomputed_and_stale_rows_are_not_reported_as_usable(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        stale = MarketObservation(
            ticker="ABC", observation_date="2020-01-01",
            values={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="fixture:stale",
            provider="fixture-market", provider_observation_id="stale", source_version="v1",
            freshness_status="current",
        )
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: [stale], ["ABC"],
            request_id="stale-label", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 5)
        row = connection.execute("SELECT freshness_status FROM phase2_market_observations LIMIT 1").fetchone()
        self.assertEqual(row["freshness_status"], "stale")
        self.assertEqual(phase2_evidence_report(connection, as_of="2026-09-01")["market_observations"], [])


class Phase2ConsolidatedRepairTests(unittest.TestCase):
    def test_market_valid_then_invalid_response_is_atomic_for_both_commit_modes(self) -> None:
        valid = MarketObservation(
            ticker="ABC", observation_date="2026-08-31",
            values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00",
            citation="https://example.test/market/valid", provider="provider-a",
            provider_observation_id="valid-row", source_version="v1",
        )
        invalid = MarketObservation(
            ticker="ABC", observation_date="2026-08-31",
            values={"open": 100, "high": 105, "low": 99, "volume": 10},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00",
            citation="https://example.test/market/invalid", provider="provider-a",
            provider_observation_id="invalid-row", source_version="v1",
        )
        for commit in (True, False):
            with self.subTest(commit=commit):
                connection = new_connection()
                try:
                    seed_universe(connection)
                    member_id = connection.execute(
                        "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
                    ).fetchone()[0]
                    scope_id = _persist_refresh_scope(
                        connection, request_id=f"market-atomic-{commit}",
                        as_of="2026-09-01", member_ids=[member_id],
                    )
                    connection.commit()
                    accepted = refresh_market_observations(
                        connection, lambda tickers, retrieved_at: [valid, invalid], ["ABC"],
                        request_id=f"market-atomic-{commit}",
                        retrieved_at="2026-09-01T00:00:00+00:00", commit=commit,
                        as_of="2026-09-01", scope_id=scope_id,
                    )
                    self.assertEqual(accepted, 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_market_observations",
                    ).fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_market_observation_bindings",
                    ).fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_failures WHERE request_id = ?",
                        (f"market-atomic-{commit}",),
                    ).fetchone()[0], 1)
                    connection.rollback()
                finally:
                    connection.close()

    def test_evidence_idle_commit_false_with_preexisting_scope_is_rollbackable(self) -> None:
        for storage in ("memory", "file"):
            with self.subTest(storage=storage), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                connection = new_connection() if storage == "memory" else connect_database(root / "phase2.sqlite")
                if storage == "file":
                    initialize_database(connection)
                try:
                    seed_universe(connection)
                    member_id = connection.execute(
                        "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
                    ).fetchone()[0]
                    scope_id = _persist_refresh_scope(
                        connection, request_id=f"evidence-idle-{storage}",
                        as_of="2026-09-01", member_ids=[member_id],
                    )
                    connection.commit()
                    self.assertEqual(ingest_evidence(
                        connection, [evidence_record()], request_id=f"evidence-idle-{storage}",
                        commit=False, as_of="2026-09-01", scope_id=scope_id,
                    ), 1)
                    connection.rollback()
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_evidence",
                    ).fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_evidence_bindings",
                    ).fetchone()[0], 0)
                finally:
                    connection.close()

    def test_evidence_scope_cutoff_is_immutable_and_later_report_is_clamped(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
        ).fetchone()[0]
        scope_id = _persist_refresh_scope(
            connection, request_id="evidence-cutoff", as_of="2026-09-01", member_ids=[member_id],
        )
        connection.commit()
        with self.assertRaisesRegex(ValueError, "immutable scope cutoff"):
            ingest_evidence(
                connection,
                [evidence_record(
                    evidence_identity="future-evidence",
                    publication_at="2026-09-02T00:00:00+00:00",
                    period_end="2026-09-01",
                    retrieved_at="2026-09-03T00:00:00+00:00",
                )],
                request_id="evidence-cutoff", as_of="2026-09-01", scope_id=scope_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence_bindings WHERE request_id = 'evidence-cutoff'",
        ).fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT failure_code FROM phase2_failures WHERE request_id = 'evidence-cutoff'",
        ).fetchone()[0], "evidence_after_scope_cutoff")
        ingest_evidence(
            connection, [evidence_record(evidence_identity="cutoff-safe")],
            request_id="evidence-cutoff", as_of="2026-09-01", scope_id=scope_id,
        )
        report = phase2_evidence_report(
            connection, as_of="2026-09-30", request_id="evidence-cutoff", scope_id=scope_id,
        )
        self.assertEqual(report["as_of"], "2026-09-01")
        self.assertEqual({row["evidence_identity"] for row in report["evidence"]}, {"cutoff-safe"})

    def test_future_evidence_cannot_create_cross_request_identity_conflict(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'",
        ).fetchone()[0]
        scope_a = _persist_refresh_scope(
            connection, request_id="future-evidence-a", as_of="2026-09-01", member_ids=[member_id],
        )
        scope_b = _persist_refresh_scope(
            connection, request_id="future-evidence-b", as_of="2026-09-01", member_ids=[member_id],
        )

        with self.assertRaisesRegex(ValueError, "immutable scope cutoff"):
            ingest_evidence(
                connection,
                [evidence_record(
                    evidence_identity="cross-request-future",
                    fact_value="999",
                    citation="https://example.test/future-evidence",
                    publication_at="2026-09-02T00:00:00+00:00",
                    retrieved_at="2026-09-03T00:00:00+00:00",
                )],
                request_id="future-evidence-a", as_of="2026-09-01", scope_id=scope_a,
            )

        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence WHERE evidence_identity = 'cross-request-future'",
        ).fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence_bindings WHERE request_id = 'future-evidence-a'",
        ).fetchone()[0], 0)
        failure = connection.execute(
            "SELECT failure_code, scope_id FROM phase2_failures WHERE request_id = 'future-evidence-a'",
        ).fetchone()
        self.assertEqual(failure["failure_code"], "evidence_after_scope_cutoff")
        self.assertEqual(failure["scope_id"], scope_a)

        valid = evidence_record(
            evidence_identity="cross-request-future", fact_value="123",
            citation="https://example.test/pre-cutoff-evidence",
            publication_at="2026-08-31T00:00:00+00:00",
            retrieved_at="2026-09-01T00:00:00+00:00",
        )
        self.assertEqual(ingest_evidence(
            connection, [valid], request_id="future-evidence-b", as_of="2026-09-01", scope_id=scope_b,
        ), 1)
        report_a = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="future-evidence-a", scope_id=scope_a,
        )
        report_b = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="future-evidence-b", scope_id=scope_b,
        )
        self.assertEqual(report_a["evidence"], [])
        self.assertEqual(report_a["conflicts"], [])
        self.assertEqual({row["evidence_identity"] for row in report_b["evidence"]}, {"cross-request-future"})
        self.assertEqual(report_b["conflicts"], [])
        self.assertFalse(any(row["failure_code"] == "evidence_conflict" for row in report_b["failures"]))

    def test_direct_market_and_evidence_scopes_use_exact_requested_members(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", cik="0000000001"),
                universe_row(ticker="XYZ", cik="0000000002", issuer_name="Other Corp"),
            ])
            import_sp500_universe(connection, universe, request_id="exact-membership")

        abc_market = MarketObservation(
            ticker="ABC", observation_date="2026-08-31",
            values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00",
            citation="https://example.test/market/abc", provider="provider-a",
            provider_observation_id="abc-row", source_version="v1",
        )
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [abc_market], ["ABC"],
            request_id="direct-abc", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01",
        )
        market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-abc",
        )
        self.assertEqual({row["ticker"] for row in market_report["universe"]}, {"ABC"})
        self.assertNotIn("XYZ", json.dumps(market_report))

        ingest_evidence(
            connection, [evidence_record(evidence_identity="abc-evidence")],
            request_id="direct-evidence-abc", as_of="2026-09-01",
        )
        evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-evidence-abc",
        )
        self.assertEqual({row["ticker"] for row in evidence_report["universe"]}, {"ABC"})
        self.assertNotIn("XYZ", json.dumps(evidence_report))

        for request_id in ("direct-abc", "direct-evidence-abc"):
            scope_id = connection.execute(
                "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = ?",
                (request_id,),
            ).fetchone()[0]
            self.assertEqual(connection.execute(
                """SELECT COUNT(*) FROM phase2_refresh_scope_members sm
                   JOIN phase2_universe_members u ON u.member_id = sm.member_id
                   WHERE sm.scope_id = ? AND u.ticker = 'XYZ'""",
                (scope_id,),
            ).fetchone()[0], 0)

    def test_request_id_safety_rejects_sensitive_shapes_before_phase2_persistence(self) -> None:
        unsafe_request_ids = (
            "token-secret",
            "credential-value",
            "broker-account-123",
            "BUY",
        )
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            for request_id in unsafe_request_ids:
                with self.subTest(request_id=request_id):
                    connection = new_connection()
                    try:
                        for operation in (
                            lambda: import_sp500_universe(connection, universe, request_id=request_id),
                            lambda: refresh_market_observations(
                                connection, lambda tickers, retrieved_at: [], ["ABC"],
                                request_id=request_id, retrieved_at="2026-09-01T00:00:00+00:00",
                            ),
                            lambda: ingest_evidence(
                                connection, [evidence_record()], request_id=request_id,
                            ),
                            lambda: phase2_evidence_report(
                                connection, as_of="2026-09-01", request_id=request_id,
                            ),
                        ):
                            with self.assertRaisesRegex(ValueError, "request_id") as raised:
                                operation()
                            self.assertNotIn(request_id, str(raised.exception))
                        for table in (
                            "phase2_refresh_scopes", "phase2_refresh_scope_members",
                            "phase2_refresh_run_scopes", "phase2_market_observations",
                            "phase2_market_observation_bindings", "phase2_evidence",
                            "phase2_evidence_bindings", "phase2_failures",
                            "phase2_refresh_runs", "phase2_snapshots",
                        ):
                            self.assertEqual(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0], 0, table)
                    finally:
                        connection.close()

    def test_configured_refresh_uses_only_current_active_cutoff_members_and_sec_rate_limit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", effective_date="2026-08-31"),
                universe_row(ticker="FUTURE", cik="0000000003", effective_date="2030-01-01", retrieved_at="2030-01-02T00:00:00+00:00"),
                universe_row(ticker="INACTIVE", cik="0000000004", membership_status="inactive"),
            ])
            config = root / "phase2.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "in-memory", "base_url": "https://www.alphavantage.co/query"},
                "sec": {"user_agent": "phase2-test [REDACTED]", "max_requests_per_second": 2},
                "rss": {"enabled": False},
            }), encoding="utf-8")

            calls: list[str] = []
            sleeps: list[float] = []

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    calls.append(url)
                    if "alphavantage" in url:
                        return HttpResponse(200, json.dumps({"Time Series (Daily)": {"2026-08-31": {
                            "1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10",
                        }}}).encode())
                    return HttpResponse(200, json.dumps({"filings": {"recent": {
                        "accessionNumber": ["0000000001-26-000001"], "filingDate": ["2026-08-31"],
                        "reportDate": ["2026-08-30"], "form": ["4"], "primaryDocument": ["ownership.xml"],
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe, as_of="2026-09-01",
                request_id="scoped-run", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
                monotonic_clock=lambda: 0.0, sleeper=sleeps.append,
            )
            self.assertEqual(result.accepted_market_observations, 5)
            self.assertTrue(all("FUTURE" not in url and "INACTIVE" not in url for url in calls))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 1)
            self.assertTrue(any(duration >= 0.49 for duration in sleeps))
            self.assertEqual(connection.execute(
                "SELECT DISTINCT retrieved_at FROM phase2_market_observations",
            ).fetchone()[0], "2026-09-01T00:00:00+00:00")

    def test_configured_refresh_with_no_usable_provider_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "phase2.json"
            config.write_text(json.dumps({
                "market": {"enabled": False}, "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="no-provider",
            )
            self.assertEqual(result.status, "failed")

    def test_report_rejects_control_content_in_an_emitted_collection(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        record = evidence_record(evidence_text="safe issuer update")
        self.assertEqual(ingest_evidence(connection, [record], request_id="report-scan"), 1)
        connection.execute(
            "UPDATE phase2_evidence SET evidence_text = 'BUY NOW at market' WHERE evidence_identity = ?",
            (record.evidence_identity,),
        )
        connection.commit()
        with self.assertRaisesRegex(ValueError, "control content"):
            phase2_evidence_report(connection, as_of="2026-09-01")

    def test_stale_evidence_is_audited_but_not_emitted_as_usable(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        stale = evidence_record(recency_status="stale")
        self.assertEqual(ingest_evidence(connection, [stale], request_id="stale-evidence"), 1)
        report = phase2_evidence_report(connection, as_of="2026-09-01")
        self.assertEqual(report["evidence"], [])
        self.assertTrue(any(row["failure_code"] == "unusable_evidence_recency" for row in report["failures"]))

    def test_market_refresh_rejects_tickers_outside_imported_universe(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        observation = MarketObservation(
            ticker="ZZZ", observation_date="2026-08-31",
            values={"open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="fixture:outside",
            provider="fixture-market", provider_observation_id="outside", source_version="v1",
        )
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: [observation], ["ZZZ"],
            request_id="outside-universe", retrieved_at="2026-09-01T00:00:00+00:00",
        ), 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT failure_code FROM phase2_failures WHERE request_id = 'outside-universe'",
        ).fetchone()[0], "market_ticker_not_in_universe")

    def test_fixture_sec_target_must_be_a_current_cutoff_member(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            future_universe = root / "future.csv"
            write_csv(future_universe, UNIVERSE_COLUMNS, [universe_row(ticker="FUTURE", cik="0000000003", effective_date="2030-01-01", retrieved_at="2030-01-02T00:00:00+00:00")])
            sec = root / "sec.json"
            sec.write_text(json.dumps({"target": {"ticker": "FUTURE", "cik": "0000000003"}, "payload": {"filings": {"recent": {
                "accessionNumber": ["0000000003-26-000001"], "filingDate": ["2026-08-31"], "reportDate": ["2026-08-30"],
                "form": ["4"], "primaryDocument": ["ownership.xml"],
            }}}}), encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            import_sp500_universe(connection, future_universe, request_id="future-seed")
            result = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, sec_path=sec,
                as_of="2026-09-01", request_id="future-sec", sec_ticker="FUTURE", sec_cik="0000000003",
            )
            self.assertEqual(result.status, "failed")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)

    def test_configured_empty_sec_responses_do_not_count_as_provider_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"enabled": False},
                "sec": {"user_agent": "phase2-test [REDACTED]", "max_requests_per_second": 10},
                "rss": {"enabled": False},
            }), encoding="utf-8")
            class EmptyTransport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"filings": {"recent": []}} if "submissions" in url else {"facts": {}}).encode())
            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe, as_of="2026-09-01",
                request_id="empty-sec", transport=EmptyTransport(),
                clock=lambda: "2026-09-01T00:00:00+00:00", monotonic_clock=lambda: 0.0, sleeper=lambda _: None,
            )
            self.assertEqual(result.status, "failed")

    def test_evidence_batch_rolls_back_prior_records_when_a_later_record_is_invalid(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        with self.assertRaises(ValueError):
            ingest_evidence(connection, [evidence_record(), evidence_record(topic="BUY NOW at market")], request_id="atomic-batch")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'atomic-batch'",
        ).fetchone()[0], 1)

    def test_control_scanner_decodes_bounded_mixed_layers_and_keeps_safe_prose(self) -> None:
        unsafe_values = (
            "Analyst rating: B&#85;Y",
            "Analyst rating: %2542%2555%2559",
            {"%&#x6f;rder%5ftype": {"%2561ction": "EXECUTE"}},
            "https://example.test/%256f%2572%2564%2565%2572s/submit",
        )
        for value in unsafe_values:
            with self.subTest(value=value):
                self.assertTrue(contains_control_content(value))
        self.assertFalse(contains_control_content("Analysts say the company will buy new equipment"))
        self.assertFalse(contains_control_content("The issuer will buy equipment"))
        self.assertTrue(contains_control_content("%25" * 200_000))

    def test_configured_future_only_market_response_does_not_count_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"provider": "alpha-vantage", "api_key": "in-memory", "base_url": "https://www.alphavantage.co/query"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"Time Series (Daily)": {"2026-09-02": {
                        "1. open": "100", "2. high": "105", "3. low": "99", "4. close": "104", "5. volume": "10",
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="future-market", transport=Transport(),
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)

    def test_configured_future_only_sec_response_does_not_count_as_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config = root / "config.json"
            config.write_text(json.dumps({
                "market": {"enabled": False},
                "sec": {"user_agent": "phase2-test [REDACTED]"}, "rss": {"enabled": False},
            }), encoding="utf-8")

            class Transport:
                def get(self, url: str, *, headers: dict[str, str], timeout: float) -> HttpResponse:
                    return HttpResponse(200, json.dumps({"filings": {"recent": {
                        "accessionNumber": ["0000000001-26-000001"], "filingDate": ["2026-09-02"],
                        "reportDate": ["2026-09-02"], "form": ["4"], "primaryDocument": ["ownership.xml"],
                    }}}).encode())

            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="future-sec", transport=Transport(),
                clock=lambda: "2026-09-03T00:00:00+00:00",
            )
        self.assertEqual(result.status, "failed")
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'future-sec' AND failure_code = 'evidence_after_scope_cutoff'",
        ).fetchone()[0], 1)
        report = phase2_evidence_report(connection, as_of="2026-09-01", scope_id=connection.execute(
            "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = 'future-sec'"
        ).fetchone()[0])
        self.assertEqual(report["evidence"], [])

    def test_urllib_transport_binds_to_one_vetted_resolution_and_never_rebinds(self) -> None:
        resolver_calls = 0
        connected: list[tuple[str, int, float]] = []

        def resolver(hostname: str) -> list[object]:
            nonlocal resolver_calls
            resolver_calls += 1
            self.assertEqual(hostname, "public.example")
            if resolver_calls > 1:
                return [__import__("ipaddress").ip_address("10.0.0.8")]
            return [__import__("ipaddress").ip_address("93.184.216.34")]

        def connector(address: object, port: int, timeout: float) -> object:
            connected.append((str(address), port, timeout))
            raise ProviderUnavailable("deterministic test connector stopped before sending")

        transport = UrllibReadOnlyTransport(
            frozenset({"public.example"}), resolver=resolver, connector=connector,
        )
        with self.assertRaises(ProviderUnavailable):
            transport.get(
                "https://public.example/feed.xml", headers={"Authorization": "Bearer never-send"}, timeout=3,
            )
        self.assertEqual(resolver_calls, 1)
        self.assertEqual(connected, [("93.184.216.34", 443, 3)])

    def test_urllib_transport_rejects_a_private_connected_peer(self) -> None:
        from nisa_quant.evidence_providers import _BoundHTTPSConnection

        class Socket:
            def getpeername(self) -> tuple[str, int]:
                return ("10.0.0.8", 443)

            def close(self) -> None:
                pass

        class Context:
            def wrap_socket(self, sock: Socket, *, server_hostname: str) -> Socket:
                raise AssertionError("TLS must not wrap an unverified peer")

        import ipaddress
        connection = _BoundHTTPSConnection(
            "public.example", ipaddress.ip_address("93.184.216.34"),
            connector=lambda address, port, timeout: Socket(), timeout=3, context=Context(),  # type: ignore[arg-type]
        )
        with self.assertRaises(ProviderUnavailable):
            connection.connect()

    def test_phase2_market_migration_resumes_each_partial_column_and_replays_legacy_row(self) -> None:
        base_columns = """
            observation_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, observation_date TEXT NOT NULL,
            field TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, currency TEXT NOT NULL,
            provider TEXT NOT NULL, provider_observation_id TEXT NOT NULL, source_version TEXT NOT NULL,
            citation TEXT NOT NULL, retrieved_at TEXT NOT NULL, freshness_status TEXT NOT NULL
        """
        for partial_columns in ("", ", observation_identity TEXT NOT NULL DEFAULT ''", ", observation_hash TEXT NOT NULL DEFAULT ''", ", conflict_status TEXT NOT NULL DEFAULT 'usable'"):
            with self.subTest(partial_columns=partial_columns):
                connection = connect_database(":memory:")
                connection.execute(f"CREATE TABLE phase2_market_observations ({base_columns}{partial_columns})")
                connection.execute("""CREATE TABLE phase2_market_observation_bindings (
                    request_id TEXT NOT NULL, observation_id TEXT NOT NULL,
                    PRIMARY KEY(request_id, observation_id)
                )""")
                for field, value in (("open", "100"), ("high", "105"), ("low", "99"), ("close", "104"), ("volume", "10")):
                    connection.execute(
                        """INSERT INTO phase2_market_observations(
                            observation_id, ticker, observation_date, field, value, unit, currency,
                            provider, provider_observation_id, source_version, citation, retrieved_at, freshness_status
                        ) VALUES (?, 'ABC', '2026-08-31', ?, ?, ?, 'USD',
                                  'fixture-market', 'legacy-row', 'v1', 'fixture:legacy',
                                  '2026-09-01T00:00:00+00:00', 'observed')""",
                        (f"legacy-{field}", field, value, "shares" if field == "volume" else "USD_per_share"),
                    )
                    connection.execute(
                        "INSERT INTO phase2_market_observation_bindings(request_id, observation_id) VALUES ('legacy-market', ?)",
                        (f"legacy-{field}",),
                    )
                initialize_database(connection)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(phase2_market_observations)")}
                self.assertTrue({"observation_identity", "observation_hash", "conflict_status"} <= columns)
                row = connection.execute("SELECT * FROM phase2_market_observations WHERE field = 'close'").fetchone()
                self.assertTrue(row["observation_identity"].startswith("[\"ABC\","))
                self.assertNotIn(":", row["observation_identity"])
                self.assertTrue(row["observation_hash"])
                self.assertEqual(row["observation_id"], "P2M-" + row["observation_hash"][:24])
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM phase2_market_observation_bindings WHERE request_id = 'legacy-market'",
                ).fetchone()[0], 5)
                seed_universe(connection)
                self.assertEqual(refresh_market_observations(
                    connection,
                    lambda tickers, retrieved_at: [MarketObservation(
                        ticker="ABC", observation_date="2026-08-31",
                        values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
                        currency="USD", retrieved_at=retrieved_at, citation="fixture:legacy",
                        provider="fixture-market", provider_observation_id="legacy-row", source_version="v1",
                    )], ["ABC"], request_id="legacy-replay", retrieved_at="2026-09-01T00:00:00+00:00",
                ), 0)
                connection.close()

    def test_phase2_evidence_migration_resumes_each_partial_column(self) -> None:
        base_columns = """
            evidence_id TEXT PRIMARY KEY, evidence_identity TEXT NOT NULL, evidence_kind TEXT NOT NULL,
            evidence_subtype TEXT NOT NULL, source_name TEXT NOT NULL, source_identifier TEXT NOT NULL,
            source_url TEXT NOT NULL, ticker TEXT, issuer_cik TEXT, publication_at TEXT,
            period_start TEXT, period_end TEXT, fact_field TEXT, fact_value TEXT, fact_unit TEXT,
            topic TEXT NOT NULL, evidence_text TEXT, source_quality TEXT NOT NULL, recency_status TEXT NOT NULL,
            corroboration_status TEXT NOT NULL, uncertainty_status TEXT NOT NULL, metadata_json TEXT NOT NULL,
            retrieved_at TEXT NOT NULL, source_version TEXT NOT NULL, citation TEXT NOT NULL
        """
        for partial_columns in ("", ", record_hash TEXT NOT NULL DEFAULT ''", ", conflict_status TEXT NOT NULL DEFAULT 'usable'"):
            with self.subTest(partial_columns=partial_columns):
                connection = connect_database(":memory:")
                connection.execute(f"CREATE TABLE phase2_evidence ({base_columns}{partial_columns}, UNIQUE(evidence_identity))")
                connection.execute(
                    """INSERT INTO phase2_evidence(
                        evidence_id, evidence_identity, evidence_kind, evidence_subtype, source_name,
                        source_identifier, source_url, ticker, issuer_cik, publication_at, period_start,
                        period_end, fact_field, fact_value, fact_unit, topic, evidence_text, source_quality,
                        recency_status, corroboration_status, uncertainty_status, metadata_json, retrieved_at,
                        source_version, citation
                    ) VALUES (
                        'legacy-evidence', 'legacy-logical', 'filing', 'company_fact',
                        'SEC', 'legacy-source', 'https://data.sec.gov/facts', 'ABC', '0000000001',
                        '2026-08-01T00:00:00+00:00', '2026-07-01', '2026-07-31', 'Revenue', '1', 'USD',
                        'reported_fact', NULL, 'authoritative_regulatory', 'old', 'single_source',
                        'reported_fact', '{}', '2026-09-01T00:00:00+00:00', 'v1',
                        'https://data.sec.gov/facts')""",
                )
                connection.execute("""CREATE TABLE phase2_evidence_bindings (
                    request_id TEXT NOT NULL, evidence_id TEXT NOT NULL,
                    PRIMARY KEY(request_id, evidence_id)
                )""")
                connection.execute(
                    "INSERT INTO phase2_evidence_bindings(request_id, evidence_id) VALUES ('legacy-evidence-request', 'legacy-evidence')",
                )
                initialize_database(connection)
                columns = {row[1] for row in connection.execute("PRAGMA table_info(phase2_evidence)")}
                self.assertTrue({"record_hash", "conflict_status"} <= columns)
                row = connection.execute("SELECT record_hash, conflict_status FROM phase2_evidence").fetchone()
                self.assertEqual(row["record_hash"], "legacy-evidence")
                self.assertEqual(row["conflict_status"], "usable")
                self.assertEqual(connection.execute(
                    "SELECT COUNT(*) FROM phase2_evidence_bindings WHERE request_id = 'legacy-evidence-request'",
                ).fetchone()[0], 1)
                connection.close()

    def test_legacy_market_identity_conflict_is_explicit(self) -> None:
        connection = connect_database(":memory:")
        connection.execute("""CREATE TABLE phase2_market_observations (
            observation_id TEXT PRIMARY KEY, ticker TEXT NOT NULL, observation_date TEXT NOT NULL,
            field TEXT NOT NULL, value TEXT NOT NULL, unit TEXT NOT NULL, currency TEXT NOT NULL,
            provider TEXT NOT NULL, provider_observation_id TEXT NOT NULL, source_version TEXT NOT NULL,
            citation TEXT NOT NULL, retrieved_at TEXT NOT NULL, freshness_status TEXT NOT NULL
        )""")
        for observation_id, value in (("legacy-a", "104"), ("legacy-b", "105")):
            connection.execute(
                """INSERT INTO phase2_market_observations VALUES (?, 'ABC', '2026-08-31', 'close', ?, 'USD_per_share', 'USD', 'fixture-market', 'same-row', 'v1', 'fixture:legacy', '2026-09-01T00:00:00+00:00', 'observed')""",
                (observation_id, value),
            )
        initialize_database(connection)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM phase2_market_observations WHERE conflict_status = 'conflict'").fetchone()[0],
            2,
        )
        connection.close()

    def test_failed_new_refresh_has_empty_new_scope_after_old_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old_universe = root / "old-universe.csv"
            old_market = root / "old-market.csv"
            new_universe = root / "new-universe.csv"
            new_market = root / "new-market.csv"
            bad_sec = root / "bad-sec.json"
            write_csv(old_universe, UNIVERSE_COLUMNS, [universe_row(ticker="ABC")])
            write_csv(old_market, MARKET_COLUMNS, [market_row(ticker="ABC")])
            write_csv(new_universe, UNIVERSE_COLUMNS, [universe_row(ticker="DEF", cik="0000000002", issuer_name="New Corp")])
            write_csv(new_market, MARKET_COLUMNS, [market_row(ticker="DEF", citation="fixture:DEF")])
            bad_sec.write_text("not-json", encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            old_result = refresh_phase2_fixtures(
                connection, universe_path=old_universe, market_path=old_market,
                as_of="2026-09-01", request_id="old-success",
            )
            self.assertEqual(old_result.status, "completed")
            new_result = refresh_phase2_fixtures(
                connection, universe_path=new_universe, market_path=new_market, sec_path=bad_sec,
                as_of="2026-09-02", request_id="new-failure",
            )
            self.assertEqual(new_result.status, "failed")
            failed_snapshot = json.loads(connection.execute(
                "SELECT report_json FROM phase2_snapshots WHERE snapshot_id = ?", (new_result.snapshot_id,)
            ).fetchone()[0])
            snapshot_row = connection.execute(
                "SELECT request_id, scope_id FROM phase2_snapshots WHERE snapshot_id = ?", (new_result.snapshot_id,)
            ).fetchone()
            self.assertEqual(snapshot_row["request_id"], "new-failure")
            self.assertEqual(snapshot_row["scope_id"], failed_snapshot["scope_id"])
            self.assertEqual(failed_snapshot["universe"], [])
            self.assertEqual(failed_snapshot["market_observations"], [])
            self.assertEqual(failed_snapshot["evidence"], [])
            self.assertTrue(failed_snapshot["failures"])
            self.assertTrue(all(row["request_id"] == "new-failure" for row in failed_snapshot["failures"]))
            self.assertNotIn("ABC", json.dumps(failed_snapshot))

    def test_disjoint_market_conflict_is_not_attributed_to_new_request(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC"),
                universe_row(ticker="XYZ", cik="0000000002", issuer_name="Other Corp"),
            ])
            import_sp500_universe(connection, universe, request_id="conflict-universe")
        member_ids = {
            row["ticker"]: row["member_id"]
            for row in connection.execute("SELECT ticker, member_id FROM phase2_universe_members")
        }
        scope_a = _persist_refresh_scope(
            connection, request_id="market-conflict-a", as_of="2026-09-01", member_ids=[member_ids["ABC"]],
        )
        scope_b = _persist_refresh_scope(
            connection, request_id="market-clean-b", as_of="2026-09-01", member_ids=[member_ids["XYZ"]],
        )
        conflicting = [
            MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00",
                citation=f"https://example.test/market/{provider}", provider=provider,
                provider_observation_id=f"{provider}-abc", source_version="v1",
            )
            for provider, close in (("provider-a", 104), ("provider-b", 105))
        ]
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: conflicting, ["ABC"],
            request_id="market-conflict-a", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=scope_a,
        )
        clean = MarketObservation(
            ticker="XYZ", observation_date="2026-08-31",
            values={"open": 200, "high": 205, "low": 199, "close": 204, "volume": 20},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="https://example.test/market/xyz",
            provider="provider-clean", provider_observation_id="clean-xyz", source_version="v1",
        )
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [clean], ["XYZ"],
            request_id="market-clean-b", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=scope_b,
        )
        report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="market-clean-b", scope_id=scope_b)
        self.assertEqual(report["market_conflicts"], [])
        self.assertEqual(report["failures"], [])
        self.assertNotIn("ABC", json.dumps(report))
        bindings = connection.execute(
            "SELECT request_id, scope_id, COUNT(*) AS count FROM phase2_market_observation_bindings GROUP BY request_id, scope_id ORDER BY request_id",
        ).fetchall()
        self.assertEqual([(row["request_id"], row["scope_id"], row["count"]) for row in bindings], [
            ("market-clean-b", scope_b, 5), ("market-conflict-a", scope_a, 10),
        ])

    def test_disjoint_evidence_conflict_is_not_attributed_to_new_request(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC"),
                universe_row(ticker="XYZ", cik="0000000002", issuer_name="Other Corp"),
            ])
            import_sp500_universe(connection, universe, request_id="conflict-universe-evidence")
        member_ids = {
            row["ticker"]: row["member_id"]
            for row in connection.execute("SELECT ticker, member_id FROM phase2_universe_members")
        }
        scope_a = _persist_refresh_scope(
            connection, request_id="evidence-conflict-a", as_of="2026-09-01", member_ids=[member_ids["ABC"]],
        )
        scope_b = _persist_refresh_scope(
            connection, request_id="evidence-clean-b", as_of="2026-09-01", member_ids=[member_ids["XYZ"]],
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="123"), evidence_record(fact_value="124", citation="https://example.test/changed")],
            request_id="evidence-conflict-a", as_of="2026-09-01", scope_id=scope_a,
        )
        ingest_evidence(
            connection, [evidence_record(
                evidence_identity="logical-evidence-xyz", ticker="XYZ", issuer_cik="0000000002",
                source_identifier="0000000002-26-000002", citation="https://example.test/xyz",
            )], request_id="evidence-clean-b", as_of="2026-09-01", scope_id=scope_b,
        )
        report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="evidence-clean-b", scope_id=scope_b)
        self.assertEqual(report["conflicts"], [])
        self.assertEqual(report["failures"], [])
        self.assertNotIn("ABC", json.dumps(report))
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_evidence_bindings WHERE request_id = ? AND scope_id = ?",
            ("evidence-clean-b", scope_b),
        ).fetchone()[0], 1)

    def test_same_request_changed_market_and_evidence_values_are_conflicts(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute("SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'").fetchone()[0]
        market_scope = _persist_refresh_scope(
            connection, request_id="same-market-request", as_of="2026-09-01", member_ids=[member_id],
        )
        market_rows = [
            MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=f"https://example.test/same/{close}",
                provider="same-request-provider", provider_observation_id=f"same-{close}", source_version="v1",
            )
            for close in (104, 105)
        ]
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market_rows[0]], ["ABC"],
            request_id="same-market-request", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=market_scope,
        )
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market_rows[1]], ["ABC"],
            request_id="same-market-request", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=market_scope,
        )
        market_report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="same-market-request", scope_id=market_scope)
        self.assertTrue(any(row["failure_code"] == "source_conflict" for row in market_report["failures"]))
        self.assertEqual(len(market_report["market_conflicts"]), 2)

        evidence_scope = _persist_refresh_scope(
            connection, request_id="same-evidence-request", as_of="2026-09-01", member_ids=[member_id],
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="123")],
            request_id="same-evidence-request", as_of="2026-09-01", scope_id=evidence_scope,
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="124", citation="https://example.test/changed-same")],
            request_id="same-evidence-request", as_of="2026-09-01", scope_id=evidence_scope,
        )
        evidence_report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="same-evidence-request", scope_id=evidence_scope)
        self.assertTrue(any(row["failure_code"] == "evidence_conflict" for row in evidence_report["failures"]))
        self.assertEqual(len(evidence_report["conflicts"]), 2)

    def test_replaying_conflicted_request_is_deterministic_without_duplicate_failures(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute("SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'").fetchone()[0]
        market_scope = _persist_refresh_scope(
            connection, request_id="replay-market", as_of="2026-09-01", member_ids=[member_id],
        )
        market_rows = [
            MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=f"https://example.test/replay/{close}",
                provider="replay-provider", provider_observation_id=f"replay-{close}", source_version="v1",
            )
            for close in (104, 105)
        ]
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: market_rows, ["ABC"],
            request_id="replay-market", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=market_scope,
        )
        first_market_failures = connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'replay-market'",
        ).fetchone()[0]
        first_market_report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="replay-market", scope_id=market_scope)
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: market_rows, ["ABC"],
            request_id="replay-market", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=market_scope,
        ), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'replay-market'",
        ).fetchone()[0], first_market_failures)
        self.assertEqual(phase2_evidence_report(connection, as_of="2026-09-01", request_id="replay-market", scope_id=market_scope), first_market_report)

        evidence_scope = _persist_refresh_scope(
            connection, request_id="replay-evidence", as_of="2026-09-01", member_ids=[member_id],
        )
        evidence_rows = [evidence_record(fact_value="123"), evidence_record(fact_value="124", citation="https://example.test/replay-evidence")]
        ingest_evidence(connection, evidence_rows, request_id="replay-evidence", as_of="2026-09-01", scope_id=evidence_scope)
        first_evidence_failures = connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'replay-evidence'",
        ).fetchone()[0]
        first_evidence_report = phase2_evidence_report(connection, as_of="2026-09-01", request_id="replay-evidence", scope_id=evidence_scope)
        self.assertEqual(ingest_evidence(connection, evidence_rows, request_id="replay-evidence", as_of="2026-09-01", scope_id=evidence_scope), 0)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_failures WHERE request_id = 'replay-evidence'",
        ).fetchone()[0], first_evidence_failures)
        self.assertEqual(phase2_evidence_report(connection, as_of="2026-09-01", request_id="replay-evidence", scope_id=evidence_scope), first_evidence_report)

    def test_scanner_fails_closed_for_oversized_and_deeply_encoded_unsafe_values(self) -> None:
        import urllib.parse

        oversized = "x" * 100_001 + " BUY NOW at market"
        self.assertTrue(contains_control_content(oversized))
        for seed in ("rating: buy", "api_key=[REDACTED]-suffix", "BUY NOW at market"):
            encoded = seed
            for _ in range(12):
                encoded = urllib.parse.quote(encoded, safe="")
            with self.subTest(seed=seed):
                self.assertTrue(contains_control_content(encoded))
        mixed = "\u200bAnalyst rating: BUY".replace(":", "&#x3a;")
        for _ in range(7):
            mixed = urllib.parse.quote(mixed, safe="")
        self.assertTrue(contains_control_content(mixed))

    def test_scanner_rejects_broker_account_client_customer_and_portfolio_identifiers(self) -> None:
        unsafe = (
            "broker account id: ABC123",
            "broker account number ABC123",
            "broker client ID=CLIENT123",
            "customer number CUSTOMER123",
            "portfolio id: PORTFOLIO123",
            "broker\u200b account\u200b id: ABC123",
        )
        for value in unsafe:
            with self.subTest(value=value):
                self.assertTrue(contains_control_content(value))
        self.assertFalse(contains_control_content("The broker's research compares portfolio accounting methods."))
        self.assertFalse(contains_control_content("broker account id: [REDACTED]"))
        self.assertTrue(contains_control_content({"broker_account_id": "ABC123"}))
        self.assertFalse(contains_control_content({"broker_account_id": "[REDACTED]"}))

    def test_direct_market_refresh_uses_latest_active_membership_as_of_and_scope(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(effective_date="2026-08-31"),
                universe_row(effective_date="2026-09-01", membership_status="inactive", retrieved_at="2026-09-01T12:00:00+00:00"),
                universe_row(ticker="XYZ", cik="0000000002"),
            ])
            import_sp500_universe(connection, universe, request_id="membership-cutoff")
            abc_member_id = connection.execute(
                "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC' AND membership_status = 'active'",
            ).fetchone()[0]
            scope_id = _persist_refresh_scope(
                connection, request_id="abc-scope", as_of="2026-09-01", member_ids=[abc_member_id],
            )
            connection.commit()
        observation = MarketObservation(
            ticker="ABC", observation_date="2026-09-01",
            values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
            currency="USD", retrieved_at="2026-09-01T13:00:00+00:00", citation="fixture:market",
            provider="fixture-market", provider_observation_id="inactive", source_version="v1",
        )
        self.assertEqual(refresh_market_observations(
            connection, lambda tickers, retrieved_at: [observation], ["ABC"],
            request_id="inactive-market", retrieved_at="2026-09-01T13:00:00+00:00", as_of="2026-09-01",
        ), 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
        xyz_observation = MarketObservation(
            ticker="XYZ", observation_date="2026-08-31",
            values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
            currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="fixture:market",
            provider="fixture-market", provider_observation_id="out-of-scope", source_version="v1",
        )
        with self.assertRaisesRegex(ValueError, "bound to a different request"):
            refresh_market_observations(
                connection, lambda tickers, retrieved_at: [xyz_observation], ["XYZ"],
                request_id="scope-mismatch", retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01", scope_id=scope_id,
            )
        with self.assertRaisesRegex(ValueError, "bound to a different request"):
            ingest_evidence(
                connection, [evidence_record()], request_id="evidence-scope-mismatch", as_of="2026-09-01", scope_id=scope_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)

    def test_snapshot_and_failures_are_bound_to_each_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            bad_sec = root / "bad-sec.json"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            bad_sec.write_text("not-json", encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, sec_path=bad_sec,
                as_of="2026-09-01", request_id="attempt-a",
            )
            second = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market, sec_path=bad_sec,
                as_of="2026-09-01", request_id="attempt-b",
            )
            self.assertEqual(first.status, "failed")
            self.assertEqual(second.status, "failed")
            self.assertNotEqual(first.snapshot_id, second.snapshot_id)
            for result, request_id in ((first, "attempt-a"), (second, "attempt-b")):
                row = connection.execute(
                    "SELECT request_id, scope_id, report_json FROM phase2_snapshots WHERE snapshot_id = ?",
                    (result.snapshot_id,),
                ).fetchone()
                report = json.loads(row["report_json"])
                self.assertEqual(row["request_id"], request_id)
                self.assertEqual(row["scope_id"], report["scope_id"])
                self.assertEqual(report["request_id"], request_id)
                self.assertTrue(report["failures"])
                self.assertTrue(all(failure["request_id"] == request_id for failure in report["failures"]))

    def test_legacy_phase2_attempt_columns_migrate_without_losing_rows(self) -> None:
        connection = connect_database(":memory:")
        self.addCleanup(connection.close)
        connection.execute("CREATE TABLE phase2_refresh_scopes (scope_id TEXT PRIMARY KEY, as_of TEXT NOT NULL)")
        connection.execute("INSERT INTO phase2_refresh_scopes VALUES ('legacy-scope', '2026-09-01')")
        connection.execute("""CREATE TABLE phase2_failures (
            failure_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, source_name TEXT NOT NULL,
            failure_code TEXT NOT NULL, message TEXT NOT NULL, ticker TEXT,
            observed_at TEXT, created_at TEXT NOT NULL
        )""")
        connection.execute("""INSERT INTO phase2_failures VALUES
            ('legacy-failure', 'legacy-request', 'phase2', 'offline', 'offline', NULL,
             '2026-09-01T00:00:00+00:00', '2026-09-01T00:00:00+00:00')""")
        connection.execute("""CREATE TABLE phase2_snapshots (
            snapshot_id TEXT PRIMARY KEY, as_of TEXT NOT NULL, created_at TEXT NOT NULL,
            snapshot_hash TEXT NOT NULL UNIQUE, report_json TEXT NOT NULL,
            no_verdict_boundary TEXT NOT NULL
        )""")
        connection.execute("""INSERT INTO phase2_snapshots VALUES
            ('legacy-snapshot', '2026-09-01', '2026-09-01T00:00:00+00:00',
             'legacy-hash', '{}', 'evidence_only')""")
        initialize_database(connection)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_refresh_scopes").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_failures").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_snapshots").fetchone()[0], 1)
        self.assertEqual(connection.execute("SELECT request_id FROM phase2_refresh_scopes").fetchone()[0], "legacy-scope")
        self.assertIn("scope_id", {row[1] for row in connection.execute("PRAGMA table_info(phase2_failures)")})
        self.assertIn("request_id", {row[1] for row in connection.execute("PRAGMA table_info(phase2_snapshots)")})
        self.assertIn("scope_id", {row[1] for row in connection.execute("PRAGMA table_info(phase2_snapshots)")})

    def test_boolean_financial_values_are_rejected_at_all_phase2_numeric_boundaries(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        base_values = {"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10}
        for field in MARKET_FIELDS:
            values = dict(base_values)
            values[field] = True
            observation = MarketObservation(
                ticker="ABC", observation_date="2026-09-01", values=values,
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation="fixture:bool",
                provider="fixture-market", provider_observation_id=f"bool-{field}", source_version="v1",
            )
            with self.subTest(field=field):
                self.assertEqual(refresh_market_observations(
                    connection, lambda tickers, retrieved_at, item=observation: [item], ["ABC"],
                    request_id=f"bool-market-{field}", retrieved_at="2026-09-01T00:00:00+00:00",
                ), 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
        issues: list[str] = []
        self.assertEqual(normalize_sec_company_facts({
            "cik": "0000000001", "facts": {"us-gaap": {"Revenue": {"units": {"USD": [{
                "end": "2026-06-30", "val": True, "filed": "2026-08-01",
            }]}}}},
        }, ticker="ABC", cik="0000000001", retrieved_at="2026-09-01T00:00:00+00:00", issues=issues), [])
        self.assertTrue(any("numeric" in issue.lower() for issue in issues))
        with self.assertRaises(ValueError):
            ingest_evidence(connection, [evidence_record(fact_value=True)], request_id="bool-fact")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            normalize_flow_proxy(FlowProxyObservation(
                ticker="ABC", proxy_type="short_interest", observed_at="2026-09-01", value=True, unit="count",  # type: ignore[arg-type]
                source_name="source", source_url="https://example.test/source", citation="fixture:source",
                retrieved_at="2026-09-01T00:00:00+00:00", source_version="v1",
            ))

    def test_cross_request_conflicts_are_bound_to_the_request_and_do_not_rewrite_prior_reports(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'"
        ).fetchone()[0]
        market_a_scope = _persist_refresh_scope(
            connection, request_id="request-a", as_of="2026-09-01", member_ids=[member_id],
        )
        market_b_scope = _persist_refresh_scope(
            connection, request_id="request-b", as_of="2026-09-01", member_ids=[member_id],
        )

        def market(close: int, citation: str) -> MarketObservation:
            return MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=citation,
                provider="shared-provider", provider_observation_id="shared-observation",
                source_version="v1",
            )

        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(104, "https://example.test/a")],
            ["ABC"], request_id="request-a", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01", scope_id=market_a_scope,
        )
        first_market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="request-a", scope_id=market_a_scope,
        )
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(105, "https://example.test/b")],
            ["ABC"], request_id="request-b", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01", scope_id=market_b_scope,
        )
        self.assertEqual(
            phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="request-a", scope_id=market_a_scope,
            ),
            first_market_report,
        )
        second_market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="request-b", scope_id=market_b_scope,
        )
        self.assertEqual(len(second_market_report["market_conflicts"]), 5)
        self.assertTrue(any(row["failure_code"] == "source_conflict" for row in second_market_report["failures"]))
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(104, "https://example.test/a")],
            ["ABC"], request_id="request-a", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01", scope_id=market_a_scope,
        )
        self.assertEqual(
            phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="request-a", scope_id=market_a_scope,
            ),
            first_market_report,
        )

        evidence_a_scope = _persist_refresh_scope(
            connection, request_id="evidence-a", as_of="2026-09-01", member_ids=[member_id],
        )
        evidence_b_scope = _persist_refresh_scope(
            connection, request_id="evidence-b", as_of="2026-09-01", member_ids=[member_id],
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="123", citation="https://example.test/evidence-a")],
            request_id="evidence-a", as_of="2026-09-01", scope_id=evidence_a_scope,
        )
        first_evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="evidence-a", scope_id=evidence_a_scope,
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="124", citation="https://example.test/evidence-b")],
            request_id="evidence-b", as_of="2026-09-01", scope_id=evidence_b_scope,
        )
        self.assertEqual(
            phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="evidence-a", scope_id=evidence_a_scope,
            ),
            first_evidence_report,
        )
        second_evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="evidence-b", scope_id=evidence_b_scope,
        )
        self.assertEqual(len(second_evidence_report["conflicts"]), 1)
        self.assertTrue(any(row["failure_code"] == "evidence_conflict" for row in second_evidence_report["failures"]))
        ingest_evidence(
            connection, [evidence_record(fact_value="123", citation="https://example.test/evidence-a")],
            request_id="evidence-a", as_of="2026-09-01", scope_id=evidence_a_scope,
        )
        self.assertEqual(
            phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="evidence-a", scope_id=evidence_a_scope,
            ),
            first_evidence_report,
        )

    def test_direct_requests_create_isolated_scopes_for_market_evidence_and_failures(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)

        def market(close: int, citation: str) -> MarketObservation:
            return MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=citation,
                provider="direct-provider", provider_observation_id="direct-observation",
                source_version="v1",
            )

        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(104, "https://example.test/direct-a")],
            ["ABC"], request_id="direct-market-a", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01",
        )
        first_market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-market-a",
        )
        market_a_scope = first_market_report["scope_id"]
        self.assertIsInstance(market_a_scope, str)
        self.assertTrue(all(row["conflict_status"] == "usable" for row in first_market_report["market_observations"]))

        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(105, "https://example.test/direct-b")],
            ["ABC"], request_id="direct-market-b", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01",
        )
        self.assertEqual(
            phase2_evidence_report(connection, as_of="2026-09-01", request_id="direct-market-a"),
            first_market_report,
        )
        second_market_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-market-b",
        )
        self.assertEqual(second_market_report["market_observations"], [])
        self.assertEqual(len(second_market_report["market_conflicts"]), 5)
        self.assertTrue(second_market_report["failures"])
        self.assertTrue(all(row["request_id"] == "direct-market-b" for row in second_market_report["failures"]))
        self.assertTrue(all(row["scope_id"] == second_market_report["scope_id"] for row in second_market_report["failures"]))
        self.assertEqual(
            connection.execute(
                "SELECT scope_id FROM phase2_refresh_scopes WHERE request_id = 'direct-market-a'",
            ).fetchone()[0],
            market_a_scope,
        )

        evidence_a = evidence_record(
            fact_value="123", citation="https://example.test/direct-evidence-a",
        )
        evidence_b = evidence_record(
            fact_value="124", citation="https://example.test/direct-evidence-b",
        )
        ingest_evidence(connection, [evidence_a], request_id="direct-evidence-a", as_of="2026-09-01")
        first_evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-evidence-a",
        )
        ingest_evidence(connection, [evidence_b], request_id="direct-evidence-b", as_of="2026-09-01")
        self.assertEqual(
            phase2_evidence_report(connection, as_of="2026-09-01", request_id="direct-evidence-a"),
            first_evidence_report,
        )
        second_evidence_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="direct-evidence-b",
        )
        self.assertEqual(second_evidence_report["evidence"], [])
        self.assertEqual(len(second_evidence_report["conflicts"]), 1)
        self.assertTrue(all(row["request_id"] == "direct-evidence-b" for row in second_evidence_report["failures"]))

        with self.assertRaisesRegex(ValueError, "explicit request_id or scope_id"):
            phase2_evidence_report(connection, as_of="2026-09-01")
        binding_rows = connection.execute(
            """SELECT request_id, scope_id FROM phase2_market_observation_bindings
               UNION ALL
               SELECT request_id, scope_id FROM phase2_evidence_bindings
               ORDER BY request_id""",
        ).fetchall()
        self.assertTrue(binding_rows)
        self.assertTrue(all(row["scope_id"] is not None for row in binding_rows))

    def test_scoped_evidence_mismatch_is_atomic_even_when_global_universe_contains_target(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "universe.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [
                universe_row(ticker="ABC", cik="0000000001"),
                universe_row(ticker="XYZ", cik="0000000002", issuer_name="Other Corp"),
            ])
            import_sp500_universe(connection, universe, request_id="scope-universe")
        abc_member_id = connection.execute(
            "SELECT member_id FROM phase2_universe_members WHERE ticker = 'ABC'"
        ).fetchone()[0]
        scope_id = _persist_refresh_scope(
            connection, request_id="abc-only", as_of="2026-09-01", member_ids=[abc_member_id],
        )
        with self.assertRaisesRegex(ValueError, "refresh scope"):
            ingest_evidence(
                connection,
                [evidence_record(ticker="XYZ", issuer_cik="0000000002")],
                request_id="abc-only", as_of="2026-09-01", scope_id=scope_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence").fetchone()[0], 0)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_evidence_bindings").fetchone()[0], 0)

    def test_refresh_replay_requires_the_same_fixture_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="fixture-replay",
            )
            replay = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="fixture-replay",
            )
            self.assertEqual(replay, first)
            write_csv(market, MARKET_COLUMNS, [market_row(close="105")])
            with self.assertRaisesRegex(ValueError, "input fingerprint mismatch"):
                refresh_phase2_fixtures(
                    connection, universe_path=universe, market_path=market,
                    as_of="2026-09-01", request_id="fixture-replay",
                )
            self.assertEqual(connection.execute(
                "SELECT COUNT(*) FROM phase2_refresh_runs WHERE request_id = 'fixture-replay'"
            ).fetchone()[0], 1)

    def test_configured_refresh_replay_fingerprint_is_safe_and_detects_config_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            config = root / "config.json"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            config.write_text(json.dumps({
                "market": {"enabled": False, "api_key": "super-secret-value"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")
            connection = new_connection()
            self.addCleanup(connection.close)
            first = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="config-replay",
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
            replay = refresh_phase2_configured(
                connection, config_path=config, universe_path=universe,
                as_of="2026-09-01", request_id="config-replay",
                clock=lambda: "2026-09-01T00:00:00+00:00",
            )
            self.assertEqual(replay, first)
            fingerprint = connection.execute(
                "SELECT input_fingerprint FROM phase2_refresh_runs WHERE request_id = 'config-replay'"
            ).fetchone()[0]
            self.assertNotIn("super-secret-value", fingerprint)
            config.write_text(json.dumps({
                "market": {"enabled": False, "timeout_seconds": 12, "api_key": "super-secret-value"},
                "sec": {"enabled": False}, "rss": {"enabled": False},
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "input fingerprint mismatch"):
                refresh_phase2_configured(
                    connection, config_path=config, universe_path=universe,
                    as_of="2026-09-01", request_id="config-replay",
                    clock=lambda: "2026-09-01T00:00:00+00:00",
                )

    def test_phase2_binding_migration_rolls_back_an_interruption_and_recovers_on_rerun(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute(
            """INSERT INTO phase2_market_observations(
                observation_id, observation_identity, observation_hash, conflict_status,
                ticker, observation_date, field, value, unit, currency, provider,
                provider_observation_id, source_version, citation, retrieved_at, freshness_status
            ) VALUES ('legacy-observation', '', '', 'usable', 'ABC', '2026-08-31', 'close',
                      '104', 'USD_per_share', 'USD', 'fixture-market', 'legacy-row', 'v1',
                      'fixture:legacy', '2026-09-01T00:00:00+00:00', 'observed')"""
        )
        connection.execute("DROP TABLE phase2_market_observation_bindings")
        connection.execute("""CREATE TABLE phase2_market_observation_bindings(
            request_id TEXT NOT NULL, observation_id TEXT NOT NULL,
            PRIMARY KEY(request_id, observation_id)
        )""")
        connection.execute(
            "INSERT INTO phase2_market_observation_bindings VALUES ('legacy-request', 'legacy-observation')"
        )
        connection.commit()
        with patch("nisa_quant.database_schema._backfill_market_identity", side_effect=RuntimeError("injected migration interruption")):
            with self.assertRaisesRegex(RuntimeError, "injected migration interruption"):
                initialize_database(connection)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_market_observation_bindings WHERE request_id = 'legacy-request'"
        ).fetchone()[0], 1)
        initialize_database(connection)
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM phase2_market_observation_bindings WHERE request_id = 'legacy-request'"
        ).fetchone()[0], 1)

    def test_boolean_provider_numeric_settings_are_rejected_before_conversion(self) -> None:
        with self.assertRaises(ValueError):
            _configured_timeout(True, 15)
        with self.assertRaises(ValueError):
            _configured_sec_rate(True)
        with self.assertRaises(ValueError):
            RequestRateLimiter(True)  # type: ignore[arg-type]
        rss_config = RssSourceConfig(
            source_name="configured-rss", source_url="https://example.test/feed.xml",
            source_version="v1", terms_url="https://example.test/terms",
            entity_aliases={"ABC": ("ABC",)}, content_policy="metadata_only",
        )
        for constructor in (
            lambda: AlphaVantageMarketProvider("key", object(), timeout_seconds=True),  # type: ignore[arg-type]
            lambda: SECEdgarProvider(object(), "agent", timeout_seconds=True),  # type: ignore[arg-type]
            lambda: RssFeedProvider(object(), rss_config, timeout_seconds=True),  # type: ignore[arg-type]
        ):
            with self.subTest(constructor=constructor):
                with self.assertRaises(ValueError):
                    constructor()

    def test_request_scope_binding_is_immutable_across_direct_then_fixture_refresh(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)

        def market(close: int, citation: str) -> MarketObservation:
            return MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=citation,
                provider="direct-provider", provider_observation_id="direct-observation",
                source_version="v1",
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market_csv = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market_csv, MARKET_COLUMNS, [market_row()])
            request_id = "immutable-direct"
            refresh_market_observations(
                connection, FixtureMarketProvider(market_csv), ["ABC"], request_id=request_id,
                retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01",
            )
            original_scope = connection.execute(
                "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = ?", (request_id,),
            ).fetchone()[0]
            result = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market_csv,
                as_of="2026-09-01", request_id=request_id,
            )

        self.assertIn(result.status, {"completed", "completed_with_warnings"})
        self.assertEqual(
            connection.execute(
                "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = ?", (request_id,),
            ).fetchone()[0],
            original_scope,
        )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM phase2_refresh_scope_members WHERE scope_id = ?", (original_scope,),
            ).fetchone()[0],
            1,
        )
        self.assertNotIn(
            "immutable-direct",
            {row[0] for row in connection.execute(
                "SELECT DISTINCT request_id FROM phase2_market_observation_bindings WHERE request_id != ?",
                (request_id,),
            ).fetchall()},
        )

    def test_request_scope_binding_rejects_failed_refresh_rebind_and_preserves_original_scope(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed_universe = root / "changed-universe.csv"
            changed_market = root / "changed-market.csv"
            bad_sec = root / "bad-sec.json"
            write_csv(changed_universe, UNIVERSE_COLUMNS, [universe_row(ticker="XYZ", cik="0000000002")])
            write_csv(changed_market, MARKET_COLUMNS, [market_row(ticker="XYZ", citation="fixture:XYZ")])
            bad_sec.write_text("not-json", encoding="utf-8")
            refresh_market_observations(
                connection, lambda tickers, retrieved_at: [MarketObservation(
                    ticker="ABC", observation_date="2026-08-31",
                    values={"open": 100, "high": 105, "low": 99, "close": 104, "volume": 10},
                    currency="USD", retrieved_at=retrieved_at, citation="https://example.test/direct",
                    provider="direct-provider", provider_observation_id="direct-row", source_version="v1",
                )], ["ABC"], request_id="immutable-failed",
                retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01",
            )
            original_scope = connection.execute(
                "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = 'immutable-failed'",
            ).fetchone()[0]
            with self.assertRaisesRegex(ValueError, "membership"):
                refresh_phase2_fixtures(
                    connection, universe_path=changed_universe, market_path=changed_market,
                    sec_path=bad_sec, as_of="2026-09-01", request_id="immutable-failed",
                )
            self.assertEqual(
                connection.execute(
                    "SELECT scope_id FROM phase2_refresh_run_scopes WHERE request_id = 'immutable-failed'",
                ).fetchone()[0],
                original_scope,
            )
            report = phase2_evidence_report(
                connection, as_of="2026-09-01", request_id="immutable-failed",
            )
            self.assertEqual({row["ticker"] for row in report["universe"]}, {"ABC"})
            self.assertNotIn("XYZ", json.dumps(report))

    def test_request_bound_replay_stays_usable_after_market_and_evidence_conflicts(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        seed_universe(connection)

        def market(close: int, citation: str) -> MarketObservation:
            return MarketObservation(
                ticker="ABC", observation_date="2026-08-31",
                values={"open": 100, "high": 105, "low": 99, "close": close, "volume": 10},
                currency="USD", retrieved_at="2026-09-01T00:00:00+00:00", citation=citation,
                provider="replay-provider", provider_observation_id=citation.rsplit("/", 1)[-1],
                source_version="v1",
            )

        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(104, "https://example.test/a")],
            ["ABC"], request_id="market-replay-a", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01",
        )
        market_a_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="market-replay-a",
        )
        market_b = lambda tickers, retrieved_at: [market(105, "https://example.test/b")]
        refresh_market_observations(
            connection, market_b, ["ABC"], request_id="market-replay-b",
            retrieved_at="2026-09-01T00:00:00+00:00", as_of="2026-09-01",
        )
        market_usable: list[bool] = []
        refresh_market_observations(
            connection, lambda tickers, retrieved_at: [market(104, "https://example.test/a")],
            ["ABC"], request_id="market-replay-a", retrieved_at="2026-09-01T00:00:00+00:00",
            as_of="2026-09-01", usable_result=market_usable,
        )
        self.assertEqual(market_usable, [True])
        self.assertEqual(
            phase2_evidence_report(connection, as_of="2026-09-01", request_id="market-replay-a"),
            market_a_report,
        )

        ingest_evidence(
            connection, [evidence_record(fact_value="123", citation="https://example.test/evidence-a")],
            request_id="evidence-replay-a", as_of="2026-09-01",
        )
        evidence_a_report = phase2_evidence_report(
            connection, as_of="2026-09-01", request_id="evidence-replay-a",
        )
        ingest_evidence(
            connection, [evidence_record(fact_value="124", citation="https://example.test/evidence-b")],
            request_id="evidence-replay-b", as_of="2026-09-01",
        )
        evidence_usable: list[bool] = []
        ingest_evidence(
            connection, [evidence_record(fact_value="123", citation="https://example.test/evidence-a")],
            request_id="evidence-replay-a", as_of="2026-09-01", usable_result=evidence_usable,
        )
        self.assertEqual(evidence_usable, [True])
        self.assertEqual(
            phase2_evidence_report(connection, as_of="2026-09-01", request_id="evidence-replay-a"),
            evidence_a_report,
        )

    def test_blank_legacy_refresh_fingerprint_is_unverifiable_and_unchanged(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="legacy-blank-fingerprint",
            )
            connection.execute(
                "UPDATE phase2_refresh_runs SET input_fingerprint = '' WHERE request_id = 'legacy-blank-fingerprint'",
            )
            connection.commit()
            with self.assertRaisesRegex(ValueError, "unverifiable"):
                refresh_phase2_fixtures(
                    connection, universe_path=universe, market_path=market,
                    as_of="2026-09-01", request_id="legacy-blank-fingerprint",
                )
            self.assertEqual(connection.execute(
                "SELECT input_fingerprint FROM phase2_refresh_runs WHERE request_id = 'legacy-blank-fingerprint'",
            ).fetchone()[0], "")

    def test_fixture_refresh_fails_without_cutoff_active_universe_and_active_control_succeeds(self) -> None:
        cases = (
            ("empty", []),
            ("future", [universe_row(effective_date="2030-01-01", retrieved_at="2030-01-02T00:00:00+00:00")]),
            ("inactive", [universe_row(membership_status="inactive")]),
        )
        for label, rows in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                universe = root / "universe.csv"
                market = root / "market.csv"
                write_csv(universe, UNIVERSE_COLUMNS, rows)
                write_csv(market, MARKET_COLUMNS, [market_row()])
                connection = new_connection()
                try:
                    result = refresh_phase2_fixtures(
                        connection, universe_path=universe, market_path=market,
                        as_of="2026-09-01", request_id=f"zero-active-{label}",
                    )
                    self.assertEqual(result.status, "failed")
                    self.assertEqual(result.accepted_market_observations, 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_universe_members").fetchone()[0], 0)
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM phase2_market_observations").fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_refresh_scope_members",
                    ).fetchone()[0], 0)
                    self.assertEqual(connection.execute(
                        "SELECT COUNT(*) FROM phase2_failures WHERE request_id = ? AND failure_code = 'refresh_failed'",
                        (f"zero-active-{label}",),
                    ).fetchone()[0], 1)
                finally:
                    connection.close()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            universe = root / "universe.csv"
            market = root / "market.csv"
            write_csv(universe, UNIVERSE_COLUMNS, [universe_row()])
            write_csv(market, MARKET_COLUMNS, [market_row()])
            connection = new_connection()
            self.addCleanup(connection.close)
            result = refresh_phase2_fixtures(
                connection, universe_path=universe, market_path=market,
                as_of="2026-09-01", request_id="zero-active-control",
            )
            self.assertEqual(result.status, "completed")
            self.assertEqual(result.accepted_market_observations, 5)


if __name__ == "__main__":
    unittest.main()
