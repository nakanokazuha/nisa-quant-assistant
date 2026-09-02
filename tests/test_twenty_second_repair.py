"""Regression tests for the twenty-second release-review repair."""

from __future__ import annotations

import csv
import datetime as dt
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tests.test_nineteenth_repair import canonical_hash, minimal_report, recommendation_fixture
from tests.test_repairs import BROKER_COLUMNS

from nisa_quant.broker_csv_import import import_csv
from nisa_quant.recommendation_journal import _recommendation_contract_hash, evaluate_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.candidate_screening import run_screens
from nisa_quant.source_records import add_source_record, source_fact_contract_is_valid
from nisa_quant.watchlist import add_watchlist_item, watchlist_as_of


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def _source_id(connection: sqlite3.Connection, *, field: str, value: str) -> str:
    return connection.execute(
        "SELECT id FROM source_records WHERE field = ? AND value = ? ORDER BY id DESC LIMIT 1",
        (field, value),
    ).fetchone()[0]


class TwentySecondRecommendationContractTests(unittest.TestCase):
    def test_mutated_benchmark_cannot_select_alternate_evidence(self) -> None:
        connection, recommendation_id, observed_id, _ = recommendation_fixture()
        self.addCleanup(connection.close)
        alternate_id = add_source_record(
            connection, source_name="repair22", source_identifier="alternate-benchmark",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
            instrument_identifier="OTHER", instrument_identifier_type="other",
            field="benchmark_price", value="99", unit="price", currency="JPY",
            freshness_status="current", citation_location="repair22:alternate",
            parser_version="repair22-v1",
        )
        connection.execute(
            "UPDATE recommendations SET benchmark_identifier_value = 'OTHER' WHERE id = ?",
            (recommendation_id,),
        )
        connection.commit()

        with self.assertRaises(ValueError):
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=alternate_id,
            )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0,
        )

    def test_each_mutable_recommendation_contract_field_is_bound(self) -> None:
        mutations = (
            ("provider", "forged-provider"),
            ("template_version", "forged-template"),
            ("reason", "forged rationale"),
            ("risk", "forged risk"),
            ("horizon", "forged horizon"),
            ("invalidation", "forged invalidation"),
            ("currency", "USD"),
            ("freshness_status", "stale"),
        )
        for column, value in mutations:
            with self.subTest(column=column):
                connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
                self.addCleanup(connection.close)
                connection.execute(
                    f"UPDATE recommendations SET {column} = ? WHERE id = ?",
                    (value, recommendation_id),
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id,
                        benchmark_price_source_id=benchmark_id,
                    )

    def test_recomputed_contract_hash_cannot_authorize_provider_or_template_mutation(self) -> None:
        for column, value in (("provider", "forged-provider"), ("template_version", "forged-template")):
            with self.subTest(column=column):
                connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
                self.addCleanup(connection.close)
                row = connection.execute(
                    "SELECT * FROM recommendations WHERE id = ?", (recommendation_id,)
                ).fetchone()
                contract_hash = _recommendation_contract_hash(
                    data_cutoff=row["data_cutoff"],
                    provider=value if column == "provider" else row["provider"],
                    template_version=value if column == "template_version" else row["template_version"],
                    source_ids=json.loads(row["source_ids"]), instrument=row["instrument"],
                    label=row["label"], metrics=json.loads(row["metrics_json"]),
                    reason=row["reason"], risk=row["risk"], horizon=row["horizon"],
                    invalidation=row["invalidation"], snapshot_json=row["snapshot_json"],
                    snapshot_hash=row["snapshot_hash"], identifier_type=row["identifier_type"],
                    identifier_value=row["identifier_value"],
                    benchmark_identifier_type=row["benchmark_identifier_type"],
                    benchmark_identifier_value=row["benchmark_identifier_value"],
                    currency=row["currency"], freshness_status=row["freshness_status"],
                )
                connection.execute(
                    f"UPDATE recommendations SET {column} = ?, contract_hash = ? WHERE id = ?",
                    (value, contract_hash, recommendation_id),
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id,
                        benchmark_price_source_id=benchmark_id,
                    )


class TwentySecondConflictTests(unittest.TestCase):
    def test_currency_conflict_is_excluded_from_every_published_surface(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        jpy_id = add_source_record(
            connection, source_name="repair22", source_identifier="jpy",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
            value="100", unit="price", currency="JPY", freshness_status="current",
            citation_location="repair22:jpy", parser_version="repair22-v1",
        )
        usd_id = add_source_record(
            connection, source_name="repair22", source_identifier="usd",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
            value="101", unit="price", currency="USD", freshness_status="current",
            citation_location="repair22:usd", parser_version="repair22-v1",
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]

        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])
        self.assertEqual(snapshot["watchlist"][0]["source_ids"], [])
        self.assertNotIn(jpy_id, {source["id"] for source in snapshot["sources"]})
        self.assertNotIn(usd_id, {source["id"] for source in snapshot["sources"]})
        self.assertNotIn(jpy_id, candidate["source_ids"])
        self.assertNotIn(usd_id, candidate["source_ids"])
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])
        self.assertNotIn(jpy_id, json.dumps(snapshot["portfolio"]["provenance"]))
        self.assertNotIn(usd_id, json.dumps(snapshot["portfolio"]["provenance"]))

    def test_current_and_stale_disagreement_is_not_resolved_by_freshness(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        for status, value in (("stale", "100"), ("current", "101")):
            add_source_record(
                connection, source_name="repair22", source_identifier=status,
                retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
                value=value, unit="price", currency="JPY", freshness_status=status,
                citation_location=f"repair22:{status}", parser_version="repair22-v1",
            )
        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])
        self.assertEqual(snapshot["watchlist"][0]["source_ids"], [])
        self.assertEqual(
            {source["id"] for source in snapshot["sources"]}, set(),
        )
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])
        self.assertIn("CONFLICTING_PRICE", {warning["code"] for warning in snapshot["warnings"]})

    def test_same_day_ledger_fills_are_distinct_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "ledger.sqlite")
            initialize_database(connection)
            path = Path(directory) / "fills.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(BROKER_COLUMNS)
                writer.writerow(["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "fill-a"])
                writer.writerow(["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "110", "0", "JPY", "", "fill-b"])
            import_csv(connection, path, source_name="repair22")
            snapshot = calculate_snapshot(connection, as_of=dt.datetime.now(dt.timezone.utc).date().isoformat())
            holding = snapshot["portfolio"]["holdings"][0]
            self.assertEqual(holding["quantity"], 2.0)
            self.assertEqual(holding["cost_basis"], 210.0)
            self.assertEqual(len(holding["ledger_source_ids"]), 2)
            connection.close()


class TwentySecondWatchlistTests(unittest.TestCase):
    def test_historical_omitted_observation_is_rejected(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with self.assertRaises(ValueError):
            add_watchlist_item(
                connection, "OLD", "other", "Old", "ETF", "JPX", "JPY", None, "watch",
                effective_date="2020-01-01",
            )

    def test_late_correction_same_effective_date_is_point_in_time_and_idempotent(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        first_id = add_watchlist_item(
            connection, "ASSET", "other", "Asset v1", "ETF", "JPX", "JPY", None, "old",
            effective_date="2026-01-01", observed_at="2026-01-02T00:00:00+00:00",
        )
        correction_id = add_watchlist_item(
            connection, "ASSET", "other", "Asset v2", "ETF", "JPX", "JPY", None, "corrected",
            effective_date="2026-01-01", observed_at="2026-02-01T00:00:00+00:00",
        )
        replay_id = add_watchlist_item(
            connection, "ASSET", "other", "Asset v2", "ETF", "JPX", "JPY", None, "corrected",
            effective_date="2026-01-01", observed_at="2026-02-01T00:00:00+00:00",
        )
        self.assertNotEqual(first_id, correction_id)
        self.assertEqual(correction_id, replay_id)
        self.assertEqual(watchlist_as_of(connection, as_of="2026-01-31")[0]["display_name"], "Asset v1")
        self.assertEqual(watchlist_as_of(connection, as_of="2026-02-01")[0]["display_name"], "Asset v2")
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM watchlist_versions WHERE identifier_value = 'ASSET' AND effective_from = '2026-01-01'"
            ).fetchone()[0], 2,
        )

    def test_schema_migration_removes_effective_date_uniqueness_without_loss(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("ALTER TABLE watchlist_versions RENAME TO watchlist_versions_new")
        connection.execute(
            """CREATE TABLE watchlist_versions(
                id INTEGER PRIMARY KEY, identifier_value TEXT NOT NULL, identifier_type TEXT NOT NULL,
                display_name TEXT NOT NULL, asset_type TEXT NOT NULL, market TEXT NOT NULL,
                currency TEXT NOT NULL, benchmark TEXT, benchmark_identifier_type TEXT,
                benchmark_identifier_value TEXT, notes TEXT NOT NULL, effective_from TEXT NOT NULL,
                observed_at TEXT NOT NULL, UNIQUE(identifier_type, identifier_value, effective_from)
            )"""
        )
        connection.execute(
            "INSERT INTO watchlist_versions SELECT * FROM watchlist_versions_new"
        )
        connection.execute("DROP TABLE watchlist_versions_new")
        connection.commit()
        initialize_database(connection)
        add_watchlist_item(
            connection, "ASSET", "other", "v1", "ETF", "JPX", "JPY", None, "one",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        add_watchlist_item(
            connection, "ASSET", "other", "v2", "ETF", "JPX", "JPY", None, "two",
            effective_date="2026-01-01", observed_at="2026-01-02T00:00:00+00:00",
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM watchlist_versions WHERE identifier_value = 'ASSET'").fetchone()[0], 2,
        )


class TwentySecondSourceContractTests(unittest.TestCase):
    def test_blank_direct_source_provenance_is_unavailable_everywhere(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        source_id = "SRC-deadbeef2222"
        connection.execute(
            """INSERT INTO source_records(
                id, source_name, source_url_or_identifier, retrieved_at, observation_date,
                instrument_identifier, field, value, unit, currency, freshness_status,
                citation_location, instrument_identifier_type, parser_version
            ) VALUES (?, '', '', '2026-01-01T00:00:00+00:00', '2026-01-01',
                      'ASSET', 'price', '100', 'price', 'JPY', 'current', '', 'other', '')""",
            (source_id,),
        )
        connection.commit()
        row = connection.execute("SELECT * FROM source_records WHERE id = ?", (source_id,)).fetchone()
        self.assertFalse(source_fact_contract_is_valid(row))
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        self.assertNotIn(source_id, {source["id"] for source in snapshot["sources"]})
        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])
        self.assertNotIn(source_id, json.dumps(snapshot["portfolio"]["provenance"]))


class TwentySecondSourceIdentityTests(unittest.TestCase):
    def test_delimiter_values_have_distinct_and_idempotent_source_ids(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        def add(name: str, identifier: str) -> str:
            return add_source_record(
                connection, source_name=name, source_identifier=identifier,
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="JPY", freshness_status="current",
                citation_location="repair22:source", parser_version="repair22-v1",
            )
        first = add("a|b", "c")
        second = add("a", "b|c")
        self.assertNotEqual(first, second)
        self.assertEqual(first, add("a|b", "c"))
        self.assertEqual(second, add("a", "b|c"))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)


class TwentySecondReportSafetyTests(unittest.TestCase):
    def test_past_tense_actions_and_confusable_variants_are_rejected(self) -> None:
        for phrase in (
            "Purchased shares", "Accumulated units", "Bought shares", "Sold shares",
            "Held shares", "Watches earnings", "Sells the position now", "S\u0395LL NOW",
            "P.u.r.c.h.a.s.e.d shares", "S\u200bE\u200bL\u200bL\u200b NOW",
        ):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=["SRC-abcdef123456"])

    def test_person_like_filenames_are_rejected_without_rejecting_generic_controls(self) -> None:
        for filename in ("Mary-Jones.csv", "高木太郎.csv"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(f"See {filename}"), source_records=["SRC-abcdef123456"])
        for filename in ("prices.csv", "価格.csv"):
            with self.subTest(filename=filename):
                validate_report(minimal_report(f"See {filename}"), source_records=["SRC-abcdef123456"])


if __name__ == "__main__":
    unittest.main()
