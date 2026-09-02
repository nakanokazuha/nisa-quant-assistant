import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.broker_csv_import import import_csv
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.source_records import add_source_record
from nisa_quant.watchlist import add_watchlist_item, watchlist_as_of


BROKER_COLUMNS = (
    "取引日", "口座区分", "銘柄コード", "銘柄コード種別", "銘柄名", "取引区分",
    "数量", "単価", "手数料", "通貨", "分配金", "備考",
)
SOURCE_ID = "SRC-abcdef123456"


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(BROKER_COLUMNS)
        writer.writerows(rows)


def new_connection(directory: str) -> sqlite3.Connection:
    connection = connect_database(Path(directory) / "ledger.sqlite")
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


class TenthRepairTests(unittest.TestCase):
    def test_watchlist_migration_runs_once_and_does_not_leak_future_metadata(self) -> None:
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
                    UNIQUE(identifier_type, identifier_value)
                )
                """
            )
            connection.execute(
                "INSERT INTO watchlist(identifier_value, identifier_type, display_name, asset_type, market, currency, notes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("LEGACY", "other", "Legacy", "ETF", "JPX", "JPY", "preserve"),
            )
            initialize_database(connection)
            migrated = connection.execute(
                "SELECT * FROM watchlist_versions WHERE identifier_value = 'LEGACY'"
            ).fetchone()
            self.assertIsNotNone(migrated)
            self.assertTrue(migrated["observed_at"].endswith("+00:00"))
            add_watchlist_item(
                connection, "FUTURE", "other", "Future v1", "ETF", "NYSE", "USD", None, "future",
                effective_date="2026-12-01", observed_at="2026-12-01T00:00:00+00:00",
            )
            initialize_database(connection)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 2
            )
            before_future = watchlist_as_of(connection, as_of="2026-11-30")
            self.assertEqual({row["identifier_value"] for row in before_future}, {"LEGACY"})
            current = {
                row["identifier_value"]: row for row in watchlist_as_of(connection, as_of="2026-12-01")
            }
            self.assertEqual(current["FUTURE"]["display_name"], "Future v1")

    def test_csv_rejects_invalid_security_prices_and_fees_without_contamination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid-values.csv"
            write_csv(path, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "ETF", "BUY", "1", "100", "0", "JPY", "", "valid"],
                ["2026-01-02", "NISA", "1306", "jpx_code", "ETF", "BUY", "1", "0", "0", "JPY", "", "zero price"],
                ["2026-01-03", "NISA", "1306", "jpx_code", "ETF", "SELL", "1", "-1", "0", "JPY", "", "negative price"],
                ["2026-01-04", "NISA", "1306", "jpx_code", "ETF", "BUY", "1", "100", "-1", "JPY", "", "negative fee"],
            ])
            connection = new_connection(directory)
            result = import_csv(connection, path, source_name="fixture")
            self.assertEqual((result.accepted_rows, result.warning_count), (1, 3))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT quantity FROM positions").fetchone()[0], 1.0)
            warning_messages = [row[0] for row in connection.execute("SELECT message FROM data_warnings")]
            self.assertTrue(any("positive" in message for message in warning_messages))
            self.assertTrue(any("non-negative" in message for message in warning_messages))

    def test_snapshot_replay_skips_direct_invalid_buy_sell_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            account_id = connection.execute("SELECT id FROM accounts WHERE account_type = 'NISA'").fetchone()[0]
            instrument_id = connection.execute(
                "INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) VALUES ('other', 'DIRECT', 'Direct', 'JPY') RETURNING id"
            ).fetchone()[0]
            import_id = connection.execute(
                "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('direct-hash', 'direct', 'direct', '2026-01-01T00:00:00+00:00') RETURNING id"
            ).fetchone()[0]
            for number, transaction_type, price, fee in ((1, "BUY", 0, 0), (2, "SELL", 120, -1)):
                source_id = add_source_record(
                    connection, source_name="direct", source_identifier=f"direct#{number}",
                    retrieved_at=f"2026-01-0{number}T00:00:00+00:00", observation_date=f"2026-01-0{number}",
                    instrument_identifier="DIRECT", instrument_identifier_type="other",
                    field=transaction_type.lower(), value="120", unit="price", currency="JPY",
                    freshness_status="observed", citation_location=f"direct:{number}", parser_version="v1",
                )
                connection.execute(
                    "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) VALUES (?, ?, ?, ?, ?, 1, ?, ?, 'JPY', 0, ?, ?)",
                    (import_id, account_id, instrument_id, f"2026-01-0{number}", transaction_type, price, fee, source_id, f"direct-row-{number}"),
                )
            connection.commit()
            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            self.assertEqual(snapshot["portfolio"]["holdings"], [])
            self.assertIsNone(snapshot["portfolio"]["realized_pl"])
            self.assertEqual(
                {warning["code"] for warning in snapshot["warnings"]}, {"INVALID_TRANSACTION_VALUES"}
            )

    def test_legacy_position_basis_is_preserved_and_repeat_initialization_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = connect_database(Path(directory) / "legacy.sqlite")
            connection.execute("CREATE TABLE instruments (id INTEGER PRIMARY KEY, identifier_type TEXT NOT NULL, identifier_value TEXT NOT NULL, display_name TEXT NOT NULL, asset_type TEXT NOT NULL DEFAULT 'security', currency TEXT NOT NULL DEFAULT 'JPY', UNIQUE(identifier_type, identifier_value))")
            connection.execute("CREATE TABLE accounts (id INTEGER PRIMARY KEY, account_type TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL)")
            instrument_id = connection.execute("INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) VALUES ('other', 'BASIS', 'Basis', 'JPY') RETURNING id").fetchone()[0]
            account_id = connection.execute("INSERT INTO accounts(account_type, display_name) VALUES ('NISA', 'NISA') RETURNING id").fetchone()[0]
            connection.execute("CREATE TABLE positions (account_id INTEGER NOT NULL, instrument_id INTEGER NOT NULL, quantity REAL NOT NULL, cost_basis REAL NOT NULL, latest_price REAL, price_date TEXT, price_status TEXT NOT NULL DEFAULT 'unavailable', PRIMARY KEY(account_id, instrument_id))")
            connection.execute("INSERT INTO positions(account_id, instrument_id, quantity, cost_basis) VALUES (?, ?, 2, 250.5)", (account_id, instrument_id))
            initialize_database(connection)
            self.assertEqual(connection.execute("SELECT cost_basis FROM positions").fetchone()[0], 250.5)
            initialize_database(connection)
            self.assertEqual(connection.execute("SELECT cost_basis FROM positions").fetchone()[0], 250.5)
            self.assertEqual(connection.execute("SELECT cost_basis FROM positions WHERE account_id = ? AND instrument_id = ?", (account_id, instrument_id)).fetchone()[0], 250.5)
            import_id = connection.execute(
                "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('basis-hash', 'direct', 'basis', '2026-01-01T00:00:00+00:00') RETURNING id"
            ).fetchone()[0]
            source_id = add_source_record(
                connection, source_name="direct", source_identifier="basis-buy", retrieved_at="2026-01-01T00:00:00+00:00",
                observation_date="2026-01-01", instrument_identifier="BASIS", instrument_identifier_type="other",
                field="buy", value="125.25", unit="price", currency="JPY", freshness_status="observed",
                citation_location="basis-buy", parser_version="v1",
            )
            connection.execute(
                "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) VALUES (?, ?, ?, '2026-01-01', 'BUY', 2, 125.25, 0, 'JPY', 0, ?, 'basis-row')",
                (import_id, account_id, instrument_id, source_id),
            )
            connection.commit()
            holding = calculate_snapshot(connection, as_of="2026-01-01")["portfolio"]["holdings"][0]
            self.assertEqual(holding["cost_basis"], 250.5)
            connection.close()

    def test_malformed_retrieval_cash_row_cannot_contribute_or_cite_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            account_id = connection.execute("SELECT id FROM accounts WHERE account_type = 'cash'").fetchone()[0]
            import_id = connection.execute(
                "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('cash-hash', 'direct', 'cash', '2026-01-01T00:00:00+00:00') RETURNING id"
            ).fetchone()[0]
            source_id = add_source_record(
                connection, source_name="direct", source_identifier="cash#1", retrieved_at="2026-01-01T00:00:00+00:00",
                observation_date="2026-01-01", instrument_identifier=None, field="cash_movement", value="1000",
                unit="total_cash", currency="JPY", freshness_status="observed", citation_location="cash:1", parser_version="v1",
            )
            connection.execute("UPDATE source_records SET retrieved_at = 'not-a-timestamp' WHERE id = ?", (source_id,))
            connection.execute(
                "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) VALUES (?, ?, '2026-01-01', 1000, 'JPY', ?, 'cash-row')",
                (import_id, account_id, source_id),
            )
            connection.commit()
            portfolio = calculate_snapshot(connection, as_of="2026-01-02")["portfolio"]
            self.assertEqual(portfolio["contributions_by_currency"], {})
            self.assertEqual(portfolio["cash_movements_by_currency"], {})
            self.assertEqual(portfolio["contributions"], 0)
            self.assertEqual(portfolio["cash_movements"], 0)
            self.assertEqual(portfolio["provenance"]["contributions"]["source_ids"], [])

    def test_report_safety_rejects_imperatives_identifiers_and_pii_filenames(self) -> None:
        for phrase in (
            "Please BUY CANDIDATE", "BUY CANDIDATE tomorrow", "SELL / REDUCE CANDIDATE today",
            "pLeAsE bUy-cAnDiDaTe", "Please B U Y CANDIDATE", "SELL/REDUCE CANDIDATE TODAY",
            "broker identifier: BROKER123", "account ID is ABC12345", "See alice_holdings.csv",
        ):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])
        for phrase in ("price-history.csv is a safe fixture name", "token is explanatory text"):
            with self.subTest(phrase=phrase):
                validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_unrelated_currency_sources_do_not_poison_jpy_portfolio_but_relevant_mix_does(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            broker = Path(directory) / "portfolio.csv"
            write_csv(broker, [["2026-01-01", "NISA", "JPY-ASSET", "other", "JPY Asset", "BUY", "1", "100", "0", "JPY", "", "held"]])
            import_csv(connection, broker, source_name="broker")
            add_source_record(
                connection, source_name="prices", source_identifier="jpy", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="JPY-ASSET", instrument_identifier_type="other",
                field="price", value="110", unit="price", currency="JPY", freshness_status="current", citation_location="jpy", parser_version="v1",
            )
            unrelated_id = add_source_record(
                connection, source_name="prices", source_identifier="unrelated", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="UNRELATED", instrument_identifier_type="other",
                field="price", value="200", unit="price", currency="USD", freshness_status="current", citation_location="unrelated", parser_version="v1",
            )
            connection.execute("UPDATE source_records SET retrieved_at = '2026-01-02T00:00:00+00:00' WHERE field = 'buy'")
            connection.commit()
            snapshot = calculate_snapshot(connection, as_of="2026-01-02")
            self.assertEqual(snapshot["portfolio"]["market_value"], 110.0)
            self.assertEqual(snapshot["currency_context"]["currencies"], ["JPY"])
            self.assertNotIn("MIXED_CURRENCY_NO_FX", {warning["code"] for warning in snapshot["warnings"]})
            self.assertIn(unrelated_id, {source["id"] for source in snapshot["sources"]})
            cash_account = connection.execute("SELECT id FROM accounts WHERE account_type = 'cash'").fetchone()[0]
            import_id = connection.execute("SELECT id FROM imports WHERE source_name = 'broker'").fetchone()[0]
            cash_source = add_source_record(
                connection, source_name="cash", source_identifier="usd-cash", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier=None, field="cash_movement", value="1",
                unit="total_cash", currency="USD", freshness_status="observed", citation_location="usd-cash", parser_version="v1",
            )
            connection.execute("INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) VALUES (?, ?, '2026-01-02', 1, 'USD', ?, 'usd-cash-row')", (import_id, cash_account, cash_source))
            connection.commit()
            mixed = calculate_snapshot(connection, as_of="2026-01-02")
            self.assertIsNone(mixed["portfolio"]["market_value"])
            self.assertIn("MIXED_CURRENCY_NO_FX", {warning["code"] for warning in mixed["warnings"]})
            connection.close()

    def test_risk_history_keeps_same_value_across_typed_instrument_identities(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            account_id = connection.execute("SELECT id FROM accounts WHERE account_type = 'NISA'").fetchone()[0]
            import_id = connection.execute("INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('risk-hash', 'direct', 'risk', '2026-01-03T00:00:00+00:00') RETURNING id").fetchone()[0]
            for identifier_type in ("other", "isin"):
                instrument_id = connection.execute("INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) VALUES (?, 'SAME', ?, 'JPY') RETURNING id", (identifier_type, identifier_type)).fetchone()[0]
                source_id = add_source_record(
                    connection, source_name="ledger", source_identifier=f"buy-{identifier_type}", retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01", instrument_identifier="SAME", instrument_identifier_type=identifier_type, field="buy", value="100", unit="price", currency="JPY", freshness_status="observed", citation_location=f"buy-{identifier_type}", parser_version="v1",
                )
                connection.execute("INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) VALUES (?, ?, ?, '2026-01-01', 'BUY', 1, 100, 0, 'JPY', 0, ?, ?)", (import_id, account_id, instrument_id, source_id, f"risk-buy-{identifier_type}"))
                for day, price in (("2026-01-01", 100), ("2026-01-02", 120 if identifier_type == "other" else 100), ("2026-01-03", 90 if identifier_type == "other" else 100)):
                    add_source_record(
                        connection, source_name="prices", source_identifier=f"{identifier_type}-{day}", retrieved_at=f"{day}T00:00:00+00:00", observation_date=day, instrument_identifier="SAME", instrument_identifier_type=identifier_type, field="price", value=str(price), unit="price", currency="JPY", freshness_status="current", citation_location=f"{identifier_type}:{day}", parser_version="v1",
                    )
            connection.commit()
            portfolio = calculate_snapshot(connection, as_of="2026-01-03")["portfolio"]
            self.assertEqual({(holding["identifier_type"], holding["instrument"]) for holding in portfolio["holdings"]}, {("other", "SAME"), ("isin", "SAME")})
            self.assertEqual(portfolio["risk_series"]["dates"], ["2026-01-01", "2026-01-02", "2026-01-03"])
            self.assertLess(portfolio["drawdown_pct"], 0)


if __name__ == "__main__":
    unittest.main()
