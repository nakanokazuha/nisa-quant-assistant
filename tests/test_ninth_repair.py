import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import render_report, validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import watchlist_as_of
from tests.test_time_helpers import current_utc_date


ROOT = Path(__file__).parent.parent
SOURCE_ID = "SRC-abcdef123456"


def minimal_report(prose: str) -> str:
    return f"""# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-01-01`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| VT | watchlist | WATCH | limited | current_price=100 | [{SOURCE_ID}]
**VT — WATCH**
- Reason: {prose}
## Source list
- [{SOURCE_ID}] local
Manual review required; no order was placed.
"""


class NinthRepairAccountingTests(unittest.TestCase):
    def test_direct_cross_currency_sell_is_skipped_without_false_pl_or_scalar_corruption(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(connection)
        account_id = connection.execute(
            "SELECT id FROM accounts WHERE account_type = 'NISA'"
        ).fetchone()[0]
        import_id = connection.execute(
            "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) "
            "VALUES ('direct-hash', 'direct', 'direct', '2026-01-01T00:00:00+00:00') "
            "RETURNING id"
        ).fetchone()[0]
        instrument_id = connection.execute(
            "INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) "
            "VALUES ('other', 'ASSET', 'Asset', 'USD') RETURNING id"
        ).fetchone()[0]
        for source_id, currency, value in (
            ("direct-buy-source", "USD", "100"),
            ("direct-sell-source", "JPY", "120"),
        ):
            add_source_record(
                connection, source_name="direct", source_identifier=source_id,
                retrieved_at="2026-01-03T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="ASSET", instrument_identifier_type="other",
                field="buy" if "buy" in source_id else "sell", value=value, unit="price", currency=currency,
                freshness_status="current", citation_location=source_id, parser_version="direct",
            )
            connection.execute(
                "UPDATE source_records SET id = ? WHERE source_url_or_identifier = ?",
                (source_id, source_id),
            )
        connection.execute(
            "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, "
            "transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) "
            "VALUES (?, ?, ?, '2026-01-01', 'BUY', 1, 100, 0, 'USD', 0, ?, 'direct-buy-row')",
            (import_id, account_id, instrument_id, "direct-buy-source"),
        )
        connection.execute(
            "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, "
            "transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) "
            "VALUES (?, ?, ?, '2026-01-02', 'SELL', 1, 120, 0, 'JPY', 0, ?, 'direct-sell-row')",
            (import_id, account_id, instrument_id, "direct-sell-source"),
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-12-31")
        portfolio = snapshot["portfolio"]

        self.assertEqual(portfolio["realized_pl_by_currency"], {})
        holding = portfolio["holdings"][0]
        self.assertEqual(holding["quantity"], 1.0)
        self.assertEqual(holding["cost_basis"], 100.0)
        self.assertIn("direct-sell-source", {source["id"] for source in snapshot["sources"]})
        self.assertIn("UNRECONCILED_SELL", {warning["code"] for warning in snapshot["warnings"]})
        connection.close()


class NinthRepairReportSafetyTests(unittest.TestCase):
    def test_label_containing_imperatives_and_account_identifiers_fail_in_unstructured_path(self) -> None:
        forbidden = (
            "BUY CANDIDATE NOW",
            "SELL / REDUCE CANDIDATE immediately",
            "BUY candidate immediately",
            "SELL / reduce candidate now",
            "BUY ... immediately",
            "SELL ... now",
            "account identifier: ABC12345",
            "broker account: 12345678",
            "customer identifier: CUSTOMER123",
            "portfolio identifier: PORTFOLIO123",
        )
        for prose in forbidden:
            with self.subTest(prose=prose):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(prose), source_records=[SOURCE_ID])

    def test_same_safety_rules_apply_to_structured_reports_and_renderer_output_remains_valid(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        initialize_database(connection)
        add_source_record(
            connection, source_name="local", source_identifier="report-source",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="VT", instrument_identifier_type="other", field="price",
            value="100", unit="price", currency="USD", freshness_status="current",
            citation_location="report", parser_version="v1",
        )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        report = render_report(snapshot, [], provider="local", template_version="v1")
        validate_report(report, snapshot=snapshot, candidates=[], provider="local", template_version="v1")
        for prose in ("BUY CANDIDATE NOW", "SELL / REDUCE CANDIDATE immediately", "account identifier: ABC12345"):
            with self.subTest(prose=prose):
                altered = report.replace(
                    "# NISA Quant Assistant Report\n",
                    f"# NISA Quant Assistant Report\n{prose}\n",
                    1,
                )
                with self.assertRaises(ValueError):
                    validate_report(altered, snapshot=snapshot, candidates=[], provider="local", template_version="v1")
        connection.close()


class NinthRepairMigrationTests(unittest.TestCase):
    def test_legacy_watchlist_row_is_migrated_with_aware_utc_metadata(self) -> None:
        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        connection.execute(
            """CREATE TABLE watchlist (
                id INTEGER PRIMARY KEY,
                identifier_value TEXT NOT NULL,
                identifier_type TEXT NOT NULL,
                display_name TEXT NOT NULL,
                asset_type TEXT NOT NULL,
                market TEXT NOT NULL,
                currency TEXT NOT NULL,
                benchmark TEXT,
                notes TEXT NOT NULL DEFAULT ''
            )"""
        )
        connection.execute(
            "INSERT INTO watchlist(identifier_value, identifier_type, display_name, asset_type, market, currency, benchmark, notes) "
            "VALUES ('LEGACY', 'other', 'Legacy item', 'ETF', 'NYSE', 'USD', NULL, 'legacy')"
        )

        initialize_database(connection)

        current = current_utc_date()
        rows = watchlist_as_of(connection, as_of=current)
        self.assertEqual([row["identifier_value"] for row in rows], ["LEGACY"])
        metadata = connection.execute(
            "SELECT effective_from, observed_at FROM watchlist_versions WHERE identifier_value = 'LEGACY'"
        ).fetchone()
        self.assertEqual(metadata["effective_from"], current)
        self.assertTrue(metadata["observed_at"].endswith("+00:00"))
        connection.close()


if __name__ == "__main__":
    unittest.main()
