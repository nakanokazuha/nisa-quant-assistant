import copy
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import render_report, validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.candidate_screening import run_screens
from nisa_quant.source_records import add_source_record
from nisa_quant.watchlist import add_watchlist_item, materialize_current, watchlist_as_of


SOURCE_ID = "SRC-abcdef123456"


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def insert_import(connection: sqlite3.Connection, name: str = "direct") -> int:
    return int(connection.execute(
        "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) "
        "VALUES (?, ?, ?, ?) RETURNING id",
        (f"{name}-hash", name, name, "2026-01-01T00:00:00+00:00"),
    ).fetchone()[0])


class WatchlistMaterializationTests(unittest.TestCase):
    def test_future_version_stays_append_only_after_reinitialization(self) -> None:
        connection = new_connection()
        add_watchlist_item(
            connection, "FUND", "other", "Current name", "ETF", "NYSE", "USD", None, "current",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        add_watchlist_item(
            connection, "FUND", "other", "Future name", "ETF", "NYSE", "EUR", None, "future",
            effective_date="2099-01-01", observed_at="2099-01-01T00:00:00+00:00",
        )

        materialize_current(connection)
        initialize_database(connection)

        materialized = connection.execute(
            "SELECT display_name, currency FROM watchlist WHERE identifier_type = 'other' AND identifier_value = 'FUND'"
        ).fetchone()
        instrument = connection.execute(
            "SELECT display_name, currency FROM instruments WHERE identifier_type = 'other' AND identifier_value = 'FUND'"
        ).fetchone()
        self.assertEqual(tuple(materialized), ("Current name", "USD"))
        self.assertEqual(tuple(instrument), ("Current name", "USD"))
        self.assertEqual(
            [(row["display_name"], row["currency"]) for row in watchlist_as_of(connection, as_of="2026-08-28")],
            [("Current name", "USD")],
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 2)
        connection.close()

    def test_empty_and_partial_version_tables_resume_legacy_migration_idempotently(self) -> None:
        connection = new_connection()
        connection.execute("DELETE FROM watchlist_versions")
        connection.execute("DELETE FROM watchlist")
        connection.execute(
            "INSERT INTO watchlist(identifier_value, identifier_type, display_name, asset_type, market, currency, notes) "
            "VALUES ('ONE', 'other', 'One', 'ETF', 'NYSE', 'USD', 'one')"
        )
        initialize_database(connection)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 1)

        connection.execute(
            "INSERT INTO watchlist(identifier_value, identifier_type, display_name, asset_type, market, currency, notes) "
            "VALUES ('TWO', 'other', 'Two', 'ETF', 'NYSE', 'USD', 'two')"
        )
        initialize_database(connection)
        initialize_database(connection)

        rows = connection.execute(
            "SELECT identifier_value, effective_from, observed_at FROM watchlist_versions ORDER BY identifier_value"
        ).fetchall()
        self.assertEqual([row[0] for row in rows], ["ONE", "TWO"])
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row[2].endswith("+00:00") for row in rows))
        self.assertTrue(all(row[1] != "0001-01-01" for row in rows))
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 2
        )
        connection.close()

    def test_legacy_effective_and_observed_columns_are_preserved(self) -> None:
        connection = connect_database(":memory:")
        connection.execute(
            """CREATE TABLE watchlist (
                id INTEGER PRIMARY KEY, identifier_value TEXT NOT NULL,
                identifier_type TEXT NOT NULL, display_name TEXT NOT NULL,
                asset_type TEXT NOT NULL, market TEXT NOT NULL, currency TEXT NOT NULL,
                benchmark TEXT, notes TEXT NOT NULL DEFAULT '',
                effective_date TEXT, observed_at TEXT,
                UNIQUE(identifier_type, identifier_value)
            )"""
        )
        connection.execute(
            "INSERT INTO watchlist(identifier_value, identifier_type, display_name, asset_type, market, currency, notes, effective_date, observed_at) "
            "VALUES ('DATED', 'other', 'Dated', 'ETF', 'NYSE', 'USD', 'legacy', '2025-01-01', '2025-02-01T12:00:00+09:00')"
        )
        initialize_database(connection)
        row = connection.execute(
            "SELECT effective_from, observed_at FROM watchlist_versions WHERE identifier_value = 'DATED'"
        ).fetchone()
        self.assertEqual(row[0], "2025-01-01")
        self.assertEqual(row[1], "2025-02-01T03:00:00+00:00")
        connection.close()


class SourceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = new_connection()
        self.account_id = self.connection.execute(
            "SELECT id FROM accounts WHERE account_type = 'NISA'"
        ).fetchone()[0]
        self.instrument_id = self.connection.execute(
            "INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) "
            "VALUES ('other', 'ASSET', 'Asset', 'JPY') RETURNING id"
        ).fetchone()[0]
        self.import_id = insert_import(self.connection)

    def tearDown(self) -> None:
        self.connection.close()

    def source(
        self,
        name: str,
        field: str,
        value: str,
        *,
        identifier: str = "ASSET",
        identifier_type: str | None = "other",
        currency: str = "JPY",
        unit: str = "price",
        freshness: str = "current",
        date_value: str = "2026-01-01",
    ) -> str:
        source_id = add_source_record(
            self.connection, source_name="direct", source_identifier=name,
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date=date_value,
            instrument_identifier=identifier, instrument_identifier_type=identifier_type,
            field=field, value=value, unit=unit, currency=currency,
            freshness_status=freshness, citation_location=name, parser_version="direct-v1",
        )
        return source_id

    def transaction(
        self,
        row_hash: str,
        transaction_type: str,
        source_id: str,
        *,
        trade_date: str = "2026-01-02",
        quantity: float = 1,
        price: float = 100,
        fee: float = 0,
        currency: str = "JPY",
        distribution: float = 0,
    ) -> None:
        self.connection.execute(
            "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, transaction_type, "
            "quantity, price, fee, currency, distribution, source_record_id, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (self.import_id, self.account_id, self.instrument_id, trade_date, transaction_type,
             quantity, price, fee, currency, distribution, source_id, row_hash),
        )

    def test_invalid_linked_sources_never_change_ledger_or_provenance(self) -> None:
        valid_buy = self.source("valid-buy", "buy", "100")
        valid_sell = self.source("valid-sell", "sell", "120")
        valid_distribution = self.source("valid-distribution", "distribution", "10", unit="total_cash")
        self.transaction("valid-buy-row", "BUY", valid_buy, quantity=2, price=100)
        self.transaction("valid-sell-row", "SELL", valid_sell, quantity=1, price=120)
        self.transaction("valid-distribution-row", "DISTRIBUTION", valid_distribution, price=0, distribution=10)

        stale = self.source("stale-buy", "buy", "100", freshness="stale")
        wrong_field = self.source("wrong-field", "price", "100")
        wrong_value = self.source("wrong-value", "buy", "999")
        wrong_identity = self.source("wrong-identity", "buy", "100", identifier="ASSET-OTHER")
        malformed_observation = self.source("malformed-observation", "buy", "100")
        malformed_trade = self.source("malformed-trade", "buy", "100")
        self.connection.execute(
            "UPDATE source_records SET observation_date = 'not-a-date' WHERE id = ?",
            (malformed_observation,),
        )
        self.transaction("stale-row", "BUY", stale)
        self.transaction("wrong-field-row", "BUY", wrong_field)
        self.transaction("wrong-value-row", "BUY", wrong_value)
        self.transaction("wrong-identity-row", "BUY", wrong_identity)
        self.transaction("malformed-observation-row", "BUY", malformed_observation)
        self.transaction("malformed-trade-row", "BUY", malformed_trade, trade_date="not-a-date")
        self.connection.commit()

        snapshot = calculate_snapshot(self.connection, as_of="2026-01-10")
        portfolio = snapshot["portfolio"]
        holding = portfolio["holdings"][0]
        self.assertEqual(holding["quantity"], 1.0)
        self.assertEqual(holding["cost_basis"], 100.0)
        self.assertEqual(portfolio["realized_pl_by_currency"], {"JPY": 20.0})
        self.assertEqual(portfolio["distributions_by_currency"], {"JPY": 10.0})
        self.assertEqual(set(holding["ledger_source_ids"]), {valid_buy, valid_sell, valid_distribution})
        warning_codes = {warning["code"] for warning in snapshot["warnings"]}
        self.assertIn("INVALID_TRANSACTION_SOURCE", warning_codes)
        self.assertIn(valid_buy, {source["id"] for source in snapshot["sources"]})
        self.assertEqual(
            self.connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 9
        )

    def test_cash_requires_cash_movement_source_and_finite_valid_dates(self) -> None:
        valid_source = add_source_record(
            self.connection, source_name="cash", source_identifier="valid-cash",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier=None, field="cash_movement", value="100", unit="total_cash",
            currency="JPY", freshness_status="observed", citation_location="valid-cash", parser_version="cash-v1",
        )
        nonfinite_source = add_source_record(
            self.connection, source_name="cash", source_identifier="nonfinite-cash",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier=None, field="cash_movement", value="1", unit="total_cash",
            currency="JPY", freshness_status="observed", citation_location="nonfinite-cash", parser_version="cash-v1",
        )
        price_source = self.source("cash-price", "price", "50", identifier="CASH-PRICE")
        self.connection.execute("UPDATE source_records SET value = 'nan' WHERE id = ?", (nonfinite_source,))
        cash_account = self.connection.execute(
            "SELECT id FROM accounts WHERE account_type = 'cash'"
        ).fetchone()[0]
        self.connection.execute(
            "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.import_id, cash_account, "2026-01-02", 100, "JPY", valid_source, "valid-cash-row"),
        )
        self.connection.execute(
            "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.import_id, cash_account, "2026-01-02", 1, "JPY", nonfinite_source, "nonfinite-cash-row"),
        )
        self.connection.execute(
            "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.import_id, cash_account, "2026-01-02", 50, "JPY", price_source, "price-cash-row"),
        )
        self.connection.execute(
            "INSERT INTO cash_movements(import_id, account_id, movement_date, amount, currency, source_record_id, row_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.import_id, cash_account, "not-a-date", 50, "JPY", valid_source, "bad-date-cash-row"),
        )
        self.connection.commit()

        portfolio = calculate_snapshot(self.connection, as_of="2026-01-10")["portfolio"]
        self.assertEqual(portfolio["cash_movements_by_currency"], {"JPY": 100.0})
        self.assertEqual(portfolio["contributions_by_currency"], {"JPY": 100.0})
        self.assertEqual(portfolio["provenance"]["contributions"]["source_ids"], [valid_source])
        self.assertIn("INVALID_CASH_MOVEMENT_SOURCE", {warning["code"] for warning in calculate_snapshot(self.connection, as_of="2026-01-10")["warnings"]})


class ReportContractTests(unittest.TestCase):
    def test_adversarial_actions_identifiers_filenames_and_credentials_are_rejected(self) -> None:
        def report(prose: str) -> str:
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

        forbidden = (
            "HOLD NOW", "WATCH NOW", "Please HOLD", "REDUCE exposure",
            "BUY CANDIDATE NOW", "BUY CANDIDATE tomorrow", "SELL / REDUCE CANDIDATE today",
            "h.o.l.d-now", "pLeAsE bUy-cAnDiDaTe",
            "account: ABC12345", "broker: BROKER123", "account ID is ABC12345",
            "alice.holdings.csv", "kevin_holdings.csv",
            "github_pat_abcdefghijklmnopqrstuvwxyz123456", "xoxb-12345678901234567890", "AIzaSyA123456789012345678901234567890",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(report(phrase), source_records=[SOURCE_ID])
        for phrase in ("price-history.csv is a safe fixture name", "token is explanatory text"):
            with self.subTest(phrase=phrase):
                validate_report(report(phrase), source_records=[SOURCE_ID])

    def test_renderer_markers_require_provider_and_template_even_without_structured_arguments(self) -> None:
        connection = new_connection()
        source_id = add_source_record(
            connection, source_name="local", source_identifier="renderer-source",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="VT", instrument_identifier_type="other", field="price",
            value="100", unit="price", currency="USD", freshness_status="current",
            citation_location="renderer", parser_version="v1",
        )
        add_watchlist_item(
            connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        renderer_report = render_report(snapshot, [], provider="local", template_version="v1")
        source_ids = [source_id]
        with self.assertRaises(ValueError):
            validate_report(renderer_report, source_records=source_ids)
        validate_report(
            renderer_report, source_records=source_ids, provider="local", template_version="v1",
        )
        connection.close()

    def test_structured_source_metadata_is_scanned_before_canonical_binding(self) -> None:
        connection = new_connection()
        source_id = add_source_record(
            connection, source_name="local", source_identifier="report-source",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="VT", instrument_identifier_type="other", field="price",
            value="100", unit="price", currency="USD", freshness_status="current",
            citation_location="safe", parser_version="v1",
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        canonical = render_report(snapshot, [], provider="local", template_version="v1")
        altered_snapshot = copy.deepcopy(snapshot)
        altered_snapshot["sources"][0]["source_name"] = "Please HOLD"
        altered = canonical.replace("local (report-source)", "Please HOLD (report-source)")
        with self.assertRaises(ValueError):
            validate_report(altered, snapshot=altered_snapshot, candidates=[], provider="local", template_version="v1")
        self.assertIn(source_id, {source["id"] for source in snapshot["sources"]})
        connection.close()

    def test_structured_candidate_and_citation_fields_cannot_bypass_safety(self) -> None:
        connection = new_connection()
        add_watchlist_item(
            connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        add_source_record(
            connection, source_name="local", source_identifier="candidate-source",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="VT", instrument_identifier_type="other", field="price",
            value="100", unit="price", currency="USD", freshness_status="current",
            citation_location="safe", parser_version="v1",
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidates = run_screens(connection, snapshot, as_of="2026-01-01")
        report = render_report(snapshot, candidates, provider="local", template_version="v1")
        for replacement in ("BUY NOW", "Please HOLD"):
            with self.subTest(replacement=replacement):
                altered = report.replace("| VT |", f"| {replacement} |", 1)
                if altered == report:
                    altered = report.replace("location `safe`", f"location `{replacement}`", 1)
                with self.assertRaises(ValueError):
                    validate_report(
                        altered, snapshot=snapshot, candidates=candidates,
                        provider="local", template_version="v1",
                    )
        connection.close()


if __name__ == "__main__":
    unittest.main()
