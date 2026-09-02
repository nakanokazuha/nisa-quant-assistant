"""Regression tests for the twentieth release-review repair."""

from __future__ import annotations

import os
import json
import sqlite3
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_nineteenth_repair import canonical_hash, recommendation_fixture

from nisa_quant.recommendation_journal import evaluate_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.watchlist import watchlist_as_of


ROOT = Path(__file__).resolve().parents[1]


class TwentiethRepairSyntaxTests(unittest.TestCase):
    def test_reports_imports_under_python311(self) -> None:
        python311 = shutil.which("python3.11")
        if python311 is None:
            self.skipTest("python3.11 is not installed")
        result = subprocess.run(
            [python311, "-c", "import nisa_quant.report_rendering"],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class TwentiethRepairMigrationTests(unittest.TestCase):
    def test_initialization_does_not_backdate_late_legacy_metadata(self) -> None:
        connection = connect_database(":memory:")
        self.addCleanup(connection.close)
        initialize_database(connection)
        connection.execute("ALTER TABLE watchlist ADD COLUMN effective_date TEXT")
        connection.execute("ALTER TABLE watchlist ADD COLUMN observed_at TEXT")
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, notes, effective_date, observed_at
            ) VALUES ('SAME', 'other', 'LATE LEGACY', 'ETF', 'NYSE', 'USD', NULL,
                      'late', '2026-06-01', '2026-08-30T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'EARLY VERSION', 'ETF', 'JPX', 'JPY', NULL,
                      'early', '2026-06-01', '2026-06-01T00:00:00+00:00')
            """
        )
        connection.commit()

        initialize_database(connection)

        rows = watchlist_as_of(connection, as_of="2026-06-01")
        self.assertEqual(
            [(row["display_name"], row["observed_at"]) for row in rows],
            [("EARLY VERSION", "2026-06-01T00:00:00+00:00")],
        )


def insert_invalid_unrelated_source(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    freshness_status: str,
    value: str,
) -> None:
    connection.execute(
        """
        INSERT INTO source_records(
            id, source_name, source_url_or_identifier, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location, instrument_identifier_type, parser_version
        ) VALUES (?, 'repair20', 'invalid-source', '2026-01-01T00:00:00+00:00',
                  '2026-01-01', 'UNRELATED', 'price', ?, 'price', 'JPY', ?,
                  'repair20:invalid-source', 'other', 'repair20-v1')
        """,
        (source_id, value, freshness_status),
    )


class TwentiethRepairPersistedSourceTests(unittest.TestCase):
    def test_valid_source_replay_control_still_inserts_an_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        evaluate_recommendation(
            connection,
            recommendation_id,
            evaluation_date="2026-01-02",
            observed_price_source_id=observed_id,
            benchmark_price_source_id=benchmark_id,
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0],
            1,
        )

    def test_invalid_persisted_sources_are_not_published_or_replayed(self) -> None:
        for freshness_status, value in (("unsupported", "100"), ("current", "NaN")):
            with self.subTest(freshness_status=freshness_status, value=value):
                connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
                self.addCleanup(connection.close)
                invalid_source_id = "SRC-deadbeef0001" if value == "100" else "SRC-deadbeef0002"
                insert_invalid_unrelated_source(
                    connection,
                    source_id=invalid_source_id,
                    freshness_status=freshness_status,
                    value=value,
                )
                connection.commit()

                snapshot = calculate_snapshot(connection, as_of="2026-01-01")
                self.assertNotIn(invalid_source_id, {source["id"] for source in snapshot["sources"]})
                invalid_source = dict(connection.execute(
                    "SELECT * FROM source_records WHERE id = ?", (invalid_source_id,)
                ).fetchone())
                snapshot["sources"].append(invalid_source)
                snapshot_json = json.dumps(
                    snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
                )
                connection.execute(
                    "UPDATE recommendations SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?",
                    (snapshot_json, canonical_hash(snapshot), recommendation_id),
                )
                connection.commit()
                expected_error = (
                    "sources\\[2\\]\\.freshness_status"
                    if freshness_status == "unsupported"
                    else "sources\\[2\\]\\.value"
                )
                with self.assertRaisesRegex(ValueError, expected_error):
                    evaluate_recommendation(
                        connection,
                        recommendation_id,
                        evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id,
                        benchmark_price_source_id=benchmark_id,
                    )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0],
                    0,
                )

    def test_conflicting_persisted_identity_date_facts_are_rejected(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        original_source = next(source for source in snapshot["sources"] if source["field"] == "price")
        conflicting_source = dict(original_source)
        conflicting_source.update({"id": "SRC-deadbeef0003", "value": "101"})
        snapshot["sources"].append(conflicting_source)
        connection.execute(
            "UPDATE recommendations SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?",
            (
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False),
                canonical_hash(snapshot),
                recommendation_id,
            ),
        )
        connection.commit()
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            evaluate_recommendation(
                connection,
                recommendation_id,
                evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0],
            0,
        )
