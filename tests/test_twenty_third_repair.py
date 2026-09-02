"""Regression tests for the twenty-third release-review repair."""

from __future__ import annotations

import json
import sqlite3
import unittest

from tests.test_nineteenth_repair import canonical_hash, minimal_report, recommendation_fixture

from nisa_quant.recommendation_journal import _recommendation_contract_hash, evaluate_recommendation
from nisa_quant.report_rendering import validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.source_records import add_source_record
from nisa_quant.watchlist import watchlist_as_of


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


class TwentyThirdRecommendationIntegrityTests(unittest.TestCase):
    def test_recomputed_hash_cannot_authorize_mutated_provider_or_template_contracts(self) -> None:
        for column, contract_column, value in (
            ("provider", "provider_contract", "forged-provider"),
            ("template_version", "template_contract", "forged-template"),
        ):
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
                    f"UPDATE recommendations SET {column} = ?, {contract_column} = ?, contract_hash = ? WHERE id = ?",
                    (value, value, contract_hash, recommendation_id),
                )
                connection.commit()

                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id,
                        benchmark_price_source_id=benchmark_id,
                    )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0,
                )

    def test_valid_recommendation_integrity_control_still_accepts_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        evaluate_recommendation(
            connection, recommendation_id, evaluation_date="2026-01-02",
            observed_price_source_id=observed_id,
            benchmark_price_source_id=benchmark_id,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 1,
        )


class TwentyThirdReportSafetyTests(unittest.TestCase):
    def test_action_homoglyphs_are_rejected(self) -> None:
        for phrase in ("BΟUGHT shares", "ΒUY NOW"):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=["SRC-abcdef123456"])

    def test_general_person_like_filenames_are_rejected_and_safe_controls_remain_allowed(self) -> None:
        for filename in ("Mary Jones.csv", "Mary_prices.csv", "김철수.csv", "Juan Perez.csv"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(f"See {filename}"), source_records=["SRC-abcdef123456"])
        for filename in ("prices.csv", "価格.csv", "price-history.csv"):
            with self.subTest(filename=filename):
                validate_report(minimal_report(f"See {filename}"), source_records=["SRC-abcdef123456"])

    def test_unicode_separator_credential_variants_are_rejected(self) -> None:
        for credential in (
            "sk‐abcdefghijklmnopqrstuvwxyz123456",
            "S.K‐abcdefghijklmnopqrstuvwxyz123456",
            "s\u200bk‐abcdefghijklmnopqrstuvwxyz123456",
        ):
            with self.subTest(credential=repr(credential)):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(f"token: {credential}"), source_records=["SRC-abcdef123456"])

    def test_structured_metadata_and_citations_are_scanned_before_binding(self) -> None:
        for source in (
            {"id": "SRC-abcdef123456", "source_name": "BΟUGHT shares"},
            {"id": "SRC-abcdef123456", "citation_location": "Mary Jones.csv"},
        ):
            with self.subTest(source=source):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report("stable evidence"), source_records=[source])
        validate_report(
            minimal_report("token is explanatory text; prices.csv is generic"),
            source_records=[{"id": "SRC-abcdef123456", "source_name": "local prices.csv"}],
        )


class TwentyThirdWatchlistTests(unittest.TestCase):
    def test_equal_instants_with_different_offsets_use_deterministic_tie_breaking(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.executemany(
            """INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type, benchmark_identifier_value,
                notes, effective_from, observed_at
            ) VALUES (?, 'other', ?, 'ETF', 'JPX', 'JPY', NULL, NULL, NULL, 'watch', ?, ?)""",
            (
                ("ASSET", "second", "2026-01-01", "2026-01-01T12:00:00+09:00"),
                ("ASSET", "first", "2026-01-01", "2026-01-01T03:00:00+00:00"),
            ),
        )
        connection.commit()
        self.assertEqual(watchlist_as_of(connection, as_of="2026-01-01")[0]["display_name"], "first")

    def test_malformed_persisted_timestamp_is_excluded_fail_closed(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute(
            """INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, benchmark_identifier_type, benchmark_identifier_value,
                notes, effective_from, observed_at
            ) VALUES ('BROKEN', 'other', 'Broken', 'ETF', 'JPX', 'JPY', NULL, NULL, NULL, 'watch', '2026-01-01', 'not-a-timestamp')"""
        )
        connection.commit()
        self.assertEqual(watchlist_as_of(connection, as_of="2026-01-01"), [])


class TwentyThirdOutcomeConflictTests(unittest.TestCase):
    def test_valid_stale_disagreement_blocks_current_outcome_evidence(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        add_source_record(
            connection, source_name="repair23", source_identifier="stale-disagreement",
            retrieved_at="2026-01-02T12:00:00+00:00", observation_date="2026-01-02",
            instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
            value="109", unit="price", currency="JPY", freshness_status="stale",
            citation_location="repair23:stale", parser_version="repair23-v1",
        )
        with self.assertRaises(ValueError):
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0,
        )


if __name__ == "__main__":
    unittest.main()
