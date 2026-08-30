import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.journal import evaluate_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item, watchlist_as_of


SOURCE_ID = "SRC-abcdef123456"


def new_connection(directory: str | None = None) -> sqlite3.Connection:
    connection = connect_database(
        Path(directory or tempfile.mkdtemp()) / "ledger.sqlite"
    )
    initialize_database(connection)
    return connection


def minimal_report(prose: str) -> str:
    return f"""# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-01-01`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| VT | watchlist | WATCH | limited | current_price=100 | [{SOURCE_ID}] |
**VT — WATCH**
- Reason: {prose}
## Source list
- [{SOURCE_ID}] local
Manual review required; no order was placed.
"""


def insert_direct_source(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    field: str,
    unit: str,
    observation_date: str | None = "2026-01-01",
    retrieved_at: str = "2026-01-01T00:00:00+00:00",
    instrument_identifier: str | None = "ASSET",
    instrument_identifier_type: str | None = "other",
    value: str = "100",
    currency: str | None = "JPY",
) -> None:
    connection.execute(
        """
        INSERT INTO source_records(
            id, source_name, source_url_or_identifier, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location, instrument_identifier_type, parser_version
        ) VALUES (?, 'direct', ?, ?, ?, ?, ?, ?, ?, ?, 'current', ?, ?, 'direct-v1')
        """,
        (
            source_id, source_id, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, source_id,
            instrument_identifier_type,
        ),
    )


class TwelfthRepairWatchlistTests(unittest.TestCase):
    def test_future_only_version_does_not_delete_legacy_current_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite"
            connection = connect_database(path)
            connection.execute(
                """
                CREATE TABLE watchlist (
                    id INTEGER PRIMARY KEY, identifier_value TEXT NOT NULL,
                    identifier_type TEXT NOT NULL, display_name TEXT NOT NULL,
                    asset_type TEXT NOT NULL, market TEXT NOT NULL, currency TEXT NOT NULL,
                    benchmark TEXT, benchmark_identifier_type TEXT,
                    benchmark_identifier_value TEXT, notes TEXT NOT NULL DEFAULT '',
                    observed_at TEXT,
                    UNIQUE(identifier_type, identifier_value)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE watchlist_versions (
                    id INTEGER PRIMARY KEY, identifier_value TEXT NOT NULL,
                    identifier_type TEXT NOT NULL, display_name TEXT NOT NULL,
                    asset_type TEXT NOT NULL, market TEXT NOT NULL, currency TEXT NOT NULL,
                    benchmark TEXT, benchmark_identifier_type TEXT,
                    benchmark_identifier_value TEXT, notes TEXT NOT NULL DEFAULT '',
                    effective_from TEXT NOT NULL, observed_at TEXT NOT NULL,
                    UNIQUE(identifier_type, identifier_value, effective_from)
                )
                """
            )
            connection.execute(
                """
                INSERT INTO watchlist(
                    identifier_value, identifier_type, display_name, asset_type,
                    market, currency, notes, observed_at
                ) VALUES ('LEGACY', 'other', 'Legacy current', 'ETF', 'JPX', 'JPY', 'keep', '2026-01-01T00:00:00+00:00')
                """
            )
            connection.execute(
                """
                INSERT INTO watchlist_versions(
                    identifier_value, identifier_type, display_name, asset_type,
                    market, currency, notes, effective_from, observed_at
                ) VALUES ('LEGACY', 'other', 'Future metadata', 'ETF', 'NYSE', 'USD', 'future', '2026-12-01', '2026-12-01T00:00:00+00:00')
                """
            )
            connection.commit()

            initialize_database(connection)
            initialize_database(connection)

            self.assertEqual(
                [row[0] for row in connection.execute("SELECT identifier_value FROM watchlist")],
                ["LEGACY"],
            )
            self.assertEqual(
                [row[0] for row in connection.execute("SELECT identifier_value FROM instruments")],
                ["LEGACY"],
            )
            before = watchlist_as_of(connection, as_of="2026-11-30")
            after = watchlist_as_of(connection, as_of="2026-12-01")
            self.assertEqual([row["identifier_value"] for row in before], ["LEGACY"])
            self.assertEqual(
                [row["identifier_value"] for row in after], ["LEGACY"]
            )
            self.assertEqual(after[0]["display_name"], "Future metadata")
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0],
                2,
            )
            connection.close()


class TwelfthRepairSourceContractTests(unittest.TestCase):
    def test_source_fact_units_are_closed_and_valid_mappings_are_accepted(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        valid = (
            ("price", "price", "ASSET"),
            ("benchmark_price", "price", "BENCH"),
            ("buy", "price", "ASSET"),
            ("sell", "price", "ASSET"),
            ("distribution", "per_unit", "ASSET"),
            ("distribution", "total_cash", "ASSET"),
            ("cash_movement", "total_cash", None),
        )
        for index, (field, unit, identifier) in enumerate(valid):
            with self.subTest(field=field, unit=unit):
                source_id = add_source_record(
                    connection, source_name="direct", source_identifier=f"valid-{index}",
                    retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                    instrument_identifier=identifier, instrument_identifier_type="other" if identifier else None,
                    field=field, value="100", unit=unit, currency="JPY",
                    freshness_status="current", citation_location=f"valid-{index}", parser_version="v1",
                )
                self.assertTrue(source_id.startswith("SRC-"))

        for index, (field, unit) in enumerate(
            (
                ("price", "total_cash"), ("benchmark_price", "per_unit"),
                ("buy", "total_cash"), ("sell", "per_unit"),
                ("distribution", "price"), ("cash_movement", "amount"),
                ("price", ""), ("price", "unsupported"),
            )
        ):
            with self.subTest(field=field, unit=unit):
                with self.assertRaises(ValueError):
                    add_source_record(
                        connection, source_name="direct", source_identifier=f"invalid-{index}",
                        retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                        instrument_identifier=None if field == "cash_movement" else "ASSET",
                        instrument_identifier_type=None if field == "cash_movement" else "other",
                        field=field, value="100", unit=unit, currency="JPY",
                        freshness_status="current", citation_location=f"invalid-{index}", parser_version="v1",
                    )

    def test_mismatched_legacy_price_unit_is_excluded_from_snapshot_and_provenance(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", "BENCH", "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            benchmark_identifier_type="other",
        )
        insert_direct_source(
            connection, source_id="SRC-111111111111", field="price", unit="total_cash"
        )
        insert_direct_source(
            connection, source_id="SRC-666666666666", field="benchmark_price", unit="total_cash",
            instrument_identifier="BENCH",
        )
        insert_direct_source(
            connection, source_id="SRC-777777777777", field="distribution", unit="price"
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        item = snapshot["watchlist"][0]
        self.assertIsNone(item["latest_price"])
        self.assertEqual(item["source_ids"], [])
        self.assertEqual(item["benchmark_source_ids"], [])
        self.assertIsNone(item["distribution_amount"])
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])
        self.assertNotIn("SRC-111111111111", snapshot["portfolio"]["provenance"]["market_value"]["source_ids"])

    def test_mismatched_outcome_unit_is_rejected_even_for_direct_source_row(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        observed_source = "SRC-222222222222"
        benchmark_source = "SRC-333333333333"
        insert_direct_source(
            connection, source_id=observed_source, field="price", unit="total_cash",
            observation_date="2026-01-02", retrieved_at="2026-01-02T00:00:00+00:00",
        )
        insert_direct_source(
            connection, source_id=benchmark_source, field="benchmark_price", unit="price",
            instrument_identifier="BENCH", observation_date="2026-01-02",
            retrieved_at="2026-01-02T00:00:00+00:00",
        )
        connection.execute(
            """
            INSERT INTO recommendations(
                created_at, data_cutoff, provider, template_version, source_ids, instrument,
                label, metrics_json, reason, risk, horizon, invalidation, snapshot_json,
                snapshot_hash, identifier_type, identifier_value, benchmark_identifier_type,
                benchmark_identifier_value, currency, freshness_status
            ) VALUES ('2026-01-01T00:00:00+00:00', '2026-01-01', 'local', 'v1', '[]',
                'ASSET', 'WATCH', ?, 'reason', 'risk', 'horizon', 'invalid', '{}', 'hash',
                'other', 'ASSET', 'other', 'BENCH', 'JPY', 'current')
            """,
            (json.dumps({"current_price": 100, "benchmark_price": 100}),),
        )
        connection.commit()
        with self.assertRaises(ValueError):
            evaluate_recommendation(
                connection, 1, evaluation_date="2026-01-02",
                observed_price_source_id=observed_source,
                benchmark_price_source_id=benchmark_source,
            )


class TwelfthRepairDateTests(unittest.TestCase):
    def test_malformed_source_and_watchlist_dates_are_unavailable_not_fatal(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        insert_direct_source(
            connection, source_id="SRC-444444444444", field="price", unit="price",
            observation_date="0000",
        )
        insert_direct_source(
            connection, source_id="SRC-555555555555", field="price", unit="price",
            observation_date="2026-01-01", retrieved_at="not-a-date",
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type, market,
                currency, notes, effective_from, observed_at
            ) VALUES ('BROKEN', 'other', 'Broken', 'ETF', 'JPX', 'JPY', '', '0000', '2026-01-01T00:00:00+00:00')
            """
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])
        self.assertEqual(
            [row["identifier_value"] for row in watchlist_as_of(connection, as_of="2026-01-02")],
            ["ASSET"],
        )
        self.assertIn(
            "INVALID_SOURCE_DATE",
            {warning["code"] for warning in snapshot["warnings"]},
        )


class TwelfthRepairReportTests(unittest.TestCase):
    def test_exact_imperatives_identifiers_filenames_and_credential_shapes_are_rejected(self) -> None:
        forbidden = (
            "HOLD NOW", "WATCH NOW", "Please HOLD", "REDUCE exposure",
            "HOLD shares until review", "WATCH earnings closely", "REDUCE risk gradually",
            "h.o.l.d-now", "pLeAsE wAtCh", "rEdUcE/exposure",
            "account identifier: personal-account", "customer ID: CLIENTABC",
            "broker account: primary-broker", "portfolio identifier is RETIREMENT",
            "account: ABC12345", "alice.holdings.csv", "kevin_holdings.csv",
            "github_pat_", "xoxb-", "AIza",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])
        for phrase in ("price-history.csv is safe", "token is explanatory text"):
            with self.subTest(phrase=phrase):
                validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_structured_source_values_are_scanned_before_binding(self) -> None:
        source = {"id": SOURCE_ID, "source_name": "Please HOLD", "citation_location": "safe"}
        with self.assertRaises(ValueError):
            validate_report(minimal_report("stable evidence"), source_records=[source])


if __name__ == "__main__":
    unittest.main()
