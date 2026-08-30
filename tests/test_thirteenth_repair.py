import hashlib
import json
import math
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.imports import import_csv
from nisa_quant.journal import evaluate_recommendation, record_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item


SOURCE_ID = "SRC-abcdef123456"


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
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
    unit: str | None = "price",
    instrument_identifier: str | None = "ASSET",
    instrument_identifier_type: str | None = "other",
    value: str = "100",
) -> None:
    connection.execute(
        """
        INSERT INTO source_records(
            id, source_name, source_url_or_identifier, retrieved_at, observation_date,
            instrument_identifier, field, value, unit, currency, freshness_status,
            citation_location, instrument_identifier_type, parser_version
        ) VALUES (?, 'direct', ?, '2026-01-01T00:00:00+00:00', '2026-01-01',
                  ?, ?, ?, ?, 'JPY', 'current', ?, ?, 'direct-v1')
        """,
        (
            source_id, source_id, instrument_identifier, field, value, unit,
            source_id, instrument_identifier_type,
        ),
    )


class ThirteenthRepairMigrationTests(unittest.TestCase):
    def test_partial_older_history_cannot_replace_legacy_current_projection(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute("DELETE FROM watchlist_versions")
        connection.execute("DELETE FROM watchlist")
        connection.execute("DELETE FROM instruments")
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes
            ) VALUES ('SAME', 'other', 'Current JPY', 'ETF', 'JPX', 'JPY', 'NIKKEI', 'current')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'Partial USD', 'ETF', 'NYSE', 'USD', 'SPY', 'older',
                      '2025-01-01', '2025-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'Future EUR', 'ETF', 'LSE', 'EUR', 'VUSA', 'future',
                      '2099-01-01', '2099-01-01T00:00:00+00:00')
            """
        )
        connection.commit()

        initialize_database(connection)
        initialize_database(connection)

        current = connection.execute(
            "SELECT display_name, market, currency FROM watchlist WHERE identifier_value = 'SAME'"
        ).fetchone()
        instrument = connection.execute(
            "SELECT display_name, market, currency FROM instruments WHERE identifier_value = 'SAME'"
        ).fetchone()
        self.assertEqual(tuple(current), ("Current JPY", "JPX", "JPY"))
        self.assertEqual(tuple(instrument), ("Current JPY", "JPX", "JPY"))
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 3)


class ThirteenthRepairReportTests(unittest.TestCase):
    def test_all_standalone_actions_identifiers_filenames_and_secret_prefixes_are_rejected(self) -> None:
        forbidden = (
            "HOLD", "WATCH", "REDUCE", "BUY", "SELL", "PURCHASE",
            "HOLD NOW", "Please HOLD", "We can HOLD", "WATCH earnings closely",
            "REDUCE risk gradually", "label: HOLD NOW", "recommendation: BUY",
            "account ID: ABC", "account identifier: personal-account",
            "broker: BROKER123", "broker account: primary-broker",
            "portfolio identifier is RETIREMENT", "customer ID: CLIENTABC",
            "alice.holdings.csv", "kevin_holdings.csv", "github_pat_", "glpat-",
            "xoxb-", "AIza", "rk_live_",
            "h.o.l.d-now", "pLeAsE wAtCh", "rEdUcE/exposure",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])
        for phrase in ("price-history.csv is safe", "token is explanatory text"):
            with self.subTest(phrase=phrase):
                validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_source_metadata_and_marker_mutations_cannot_bypass_safety_binding(self) -> None:
        with self.assertRaises(ValueError):
            validate_report(
                minimal_report("stable evidence"),
                source_records=[{"id": SOURCE_ID, "source_name": "We can HOLD"}],
            )
        mutated_marker = minimal_report("stable evidence") + "\nProvider/model identifier : `local`; template: `v1`.\n"
        with self.assertRaises(ValueError):
            validate_report(mutated_marker, source_records=[SOURCE_ID])


class ThirteenthRepairSourceContractTests(unittest.TestCase):
    def test_unknown_field_is_rejected_and_untyped_direct_facts_are_unavailable(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with self.assertRaises(ValueError):
            add_source_record(
                connection, source_name="direct", source_identifier="unknown-field",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="ASSET", instrument_identifier_type="other",
                field="unknown", value="100", unit="price", currency="JPY",
                freshness_status="current", citation_location="unknown", parser_version="v1",
            )
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        insert_direct_source(
            connection, source_id="SRC-111111111111", field="price",
            instrument_identifier_type=None,
        )
        insert_direct_source(
            connection, source_id="SRC-222222222222", field="unknown",
        )
        insert_direct_source(
            connection, source_id="SRC-333333333333", field="price", unit="total_cash",
        )
        connection.commit()
        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        source_ids = {source["id"] for source in snapshot["sources"]}
        self.assertNotIn("SRC-111111111111", snapshot["watchlist"][0]["source_ids"])
        self.assertNotIn("SRC-222222222222", snapshot["watchlist"][0]["source_ids"])
        self.assertNotIn("SRC-333333333333", snapshot["watchlist"][0]["source_ids"])
        self.assertNotIn("SRC-111111111111", snapshot["portfolio"]["provenance"]["market_value"]["source_ids"])
        self.assertNotIn("SRC-222222222222", source_ids)
        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])


class ThirteenthRepairOverflowTests(unittest.TestCase):
    def test_file_import_quarantines_finite_inputs_whose_cost_overflows(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.csv"
            path.write_text(
                "取引日,口座区分,銘柄コード,銘柄コード種別,銘柄名,取引区分,数量,単価,手数料,通貨,分配金,備考\n"
                "2026-01-01,NISA,BIG,other,Big,BUY,1e308,1e308,0,JPY,0,\n",
                encoding="utf-8",
            )
            result = import_csv(connection, path, source_name="oversized")
        self.assertEqual(result.accepted_rows, 0)
        self.assertGreaterEqual(result.warning_count, 1)
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)

    def test_direct_finite_inputs_never_publish_nonfinite_snapshot_values(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "BIG", "other", "Big", "ETF", "JPX", "JPY", None, "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        source_id = add_source_record(
            connection, source_name="direct", source_identifier="big-buy",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="BIG", instrument_identifier_type="other", field="buy",
            value="1e308", unit="price", currency="JPY", freshness_status="current",
            citation_location="big-buy", parser_version="v1",
        )
        import_id = int(connection.execute(
            "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('big', 'direct', 'big', '2026-01-01T00:00:00+00:00') RETURNING id"
        ).fetchone()[0])
        account_id = int(connection.execute("SELECT id FROM accounts WHERE account_type = 'NISA'").fetchone()[0])
        instrument_id = int(connection.execute(
            "SELECT id FROM instruments WHERE identifier_type = 'other' AND identifier_value = 'BIG'"
        ).fetchone()[0])
        connection.execute(
            """
            INSERT INTO transactions(
                import_id, account_id, instrument_id, trade_date, transaction_type,
                quantity, price, fee, currency, distribution, source_record_id, row_hash
            ) VALUES (?, ?, ?, '2026-01-01', 'BUY', 1e308, 1e308, 0, 'JPY', 0, ?, 'big-row')
            """,
            (import_id, account_id, instrument_id, source_id),
        )
        connection.commit()
        snapshot = calculate_snapshot(connection, as_of="2026-01-02")

        def assert_finite(value: object) -> None:
            if isinstance(value, float):
                self.assertTrue(math.isfinite(value), value)
            elif isinstance(value, dict):
                for nested in value.values():
                    assert_finite(nested)
            elif isinstance(value, (list, tuple)):
                for nested in value:
                    assert_finite(nested)

        assert_finite(snapshot)
        self.assertIn(
            "NONFINITE_DERIVED_VALUE",
            {warning["code"] for warning in snapshot["warnings"]},
        )

    def test_outcome_returns_become_unavailable_when_finite_inputs_overflow(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", "BENCH", "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            benchmark_identifier_value="BENCH",
        )
        add_source_record(
            connection, source_name="direct", source_identifier="cutoff-price",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
            value="1e-308", unit="price", currency="JPY", freshness_status="current",
            citation_location="cutoff-price", parser_version="v1",
        )
        add_source_record(
            connection, source_name="direct", source_identifier="cutoff-benchmark",
            retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
            instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
            value="1e-308", unit="price", currency="JPY", freshness_status="current",
            citation_location="cutoff-benchmark", parser_version="v1",
        )
        connection.execute(
            "INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) VALUES ('other', 'BENCH', 'Bench', 'JPY')"
        )
        observed_id = add_source_record(
            connection, source_name="direct", source_identifier="outcome-price",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
            instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
            value="1e308", unit="price", currency="JPY", freshness_status="current",
            citation_location="outcome-price", parser_version="v1",
        )
        benchmark_id = add_source_record(
            connection, source_name="direct", source_identifier="outcome-benchmark",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
            instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
            value="1e308", unit="price", currency="JPY", freshness_status="current",
            citation_location="outcome-benchmark", parser_version="v1",
        )
        valid_snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidate = run_screens(connection, valid_snapshot, as_of="2026-01-01")[0]
        recommendation_id = record_recommendation(
            connection, candidate, data_cutoff="2026-01-01", provider="local",
            template_version="v1", snapshot=valid_snapshot,
        )

        evaluate_recommendation(
            connection, recommendation_id, evaluation_date="2026-01-02",
            observed_price_source_id=observed_id,
            benchmark_price_source_id=benchmark_id,
        )
        outcome = connection.execute(
            "SELECT observed_return, benchmark_return, snapshot_json FROM recommendation_outcomes"
        ).fetchone()
        self.assertIsNone(outcome[0])
        self.assertIsNone(outcome[1])
        self.assertNotIn("Infinity", outcome[2])


if __name__ == "__main__":
    unittest.main()
