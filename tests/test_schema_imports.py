import csv
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.broker_csv_import import CsvImportError, import_csv
from nisa_quant.source_records import import_price_fixture
from nisa_quant.database_schema import connect_database, initialize_database


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_broker.csv"


class SchemaAndImportTests(unittest.TestCase):
    def test_schema_has_required_tables_and_account_types(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "portfolio.sqlite")
            initialize_database(connection)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertTrue(
                {
                    "instruments",
                    "accounts",
                    "transactions",
                    "cash_movements",
                    "positions",
                    "source_records",
                    "imports",
                    "data_warnings",
                    "recommendations",
                    "recommendation_outcomes",
                } <= tables
            )
            account_types = {
                row[0]
                for row in connection.execute("SELECT account_type FROM accounts")
            }
            self.assertEqual(
                {
                    "NISA",
                    "taxable",
                    "general",
                    "cash",
                    "foreign_currency",
                    "unknown_review",
                },
                account_types,
            )

    def test_import_is_idempotent_and_keeps_accounts_separate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "portfolio.sqlite")
            initialize_database(connection)
            first = import_csv(connection, FIXTURE, source_name="synthetic-broker")
            second = import_csv(connection, FIXTURE, source_name="synthetic-broker")
            self.assertEqual(first.accepted_rows, 4)
            self.assertEqual(second.accepted_rows, 0)
            self.assertEqual(first.warning_count, 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0],
                3,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM cash_movements").fetchone()[0],
                1,
            )
            accounts = connection.execute(
                "SELECT a.account_type, p.quantity FROM positions p JOIN accounts a ON a.id = p.account_id ORDER BY a.account_type"
            ).fetchall()
            self.assertEqual([(row[0], row[1]) for row in accounts], [("NISA", 10.0), ("taxable", 5.0)])

    def test_ambiguous_mapping_is_rejected_without_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ambiguous.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["date", "取引日", "account", "symbol", "quantity", "price", "type"])
                writer.writerow(["2026-01-01", "2026-01-01", "NISA", "1306.T", "1", "1000", "BUY"])
            connection = connect_database(Path(directory) / "portfolio.sqlite")
            initialize_database(connection)
            with self.assertRaises(CsvImportError):
                import_csv(connection, path, source_name="ambiguous")

    def test_identifier_forms_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "portfolio.sqlite")
            initialize_database(connection)
            import_csv(connection, FIXTURE, source_name="synthetic-broker")
            rows = connection.execute(
                "SELECT identifier_type, identifier_value FROM instruments ORDER BY identifier_type, identifier_value"
            ).fetchall()
            rows = [tuple(row) for row in rows]
            self.assertIn(("jpx_code", "1306"), rows)
            self.assertNotIn(("yahoo_symbol", "1306.T"), rows)
            self.assertEqual(len(rows), 2)


if __name__ == "__main__":
    unittest.main()
