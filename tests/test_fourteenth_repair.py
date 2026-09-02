import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.source_records import add_source_record
from nisa_quant.watchlist import add_watchlist_item, current_materialization_date, watchlist_as_of


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


class FourteenthRepairWatchlistTests(unittest.TestCase):
    def test_same_day_partial_version_preserves_legacy_current_across_reinitialization(self) -> None:
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
            ) VALUES ('SAME', 'other', 'Legacy Current', 'ETF', 'JPX', 'JPY', 'NIKKEI', 'legacy')
            """
        )
        today = current_materialization_date()
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, benchmark, notes, effective_from, observed_at
            ) VALUES ('SAME', 'other', 'Partial Historical', 'ETF', 'NYSE', 'USD', NULL, 'partial', ?, ?)
            """,
            (today, f"{today}T00:00:00+00:00"),
        )
        connection.commit()

        initialize_database(connection)
        initialize_database(connection)

        current = connection.execute(
            "SELECT display_name, market, currency FROM watchlist WHERE identifier_value = 'SAME'"
        ).fetchone()
        point_in_time = watchlist_as_of(connection, as_of=today)
        self.assertEqual(tuple(current), ("Legacy Current", "JPX", "JPY"))
        self.assertEqual(
            [(row["display_name"], row["market"], row["currency"]) for row in point_in_time],
            [("Legacy Current", "JPX", "JPY")],
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM watchlist_versions").fetchone()[0], 1)
        self.assertEqual(
            connection.execute(
                "SELECT display_name FROM watchlist_versions WHERE identifier_value = 'SAME'"
            ).fetchone()[0],
            "Partial Historical",
        )


class FourteenthRepairReportSafetyTests(unittest.TestCase):
    def test_zero_width_actions_and_embedded_identifiers_are_rejected_without_breaking_safe_text(self) -> None:
        forbidden = (
            "H\u200bO\u200bL\u200bD now",
            "h\u200b.o\u200b.l\u200b.d now",
            "For context my account ID is ABC",
            "Use customer identifier CLIENTABC for audit",
        )
        for prose in forbidden:
            with self.subTest(prose=prose):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(prose), source_records=[SOURCE_ID])

        validate_report(minimal_report("price-history.csv is safe; token is explanatory text"), source_records=[SOURCE_ID])


class FourteenthRepairChronologyTests(unittest.TestCase):
    def test_persisted_pre_observation_retrieval_is_unavailable_and_does_not_advance_cutoffs(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        connection.execute(
            """
            INSERT INTO watchlist(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, notes
            ) VALUES ('ASSET', 'other', 'Asset', 'ETF', 'JPX', 'JPY', 'watch')
            """
        )
        connection.execute(
            """
            INSERT INTO watchlist_versions(
                identifier_value, identifier_type, display_name, asset_type,
                market, currency, notes, effective_from, observed_at
            ) VALUES ('ASSET', 'other', 'Asset', 'ETF', 'JPX', 'JPY', 'watch', '2026-01-01', '2026-01-01T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO source_records(
                id, source_name, source_url_or_identifier, retrieved_at, observation_date,
                instrument_identifier, field, value, unit, currency, freshness_status,
                citation_location, instrument_identifier_type, parser_version
            ) VALUES ('SRC-444444444444', 'direct', 'bypass', '2026-01-01T00:00:00+00:00',
                      '2026-01-02', 'ASSET', 'price', '100', 'price', 'JPY', 'current',
                      'bypass', 'other', 'direct-v1')
            """
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-03")

        item = snapshot["watchlist"][0]
        self.assertIsNone(item["latest_price"])
        self.assertNotIn("SRC-444444444444", item["source_ids"])
        self.assertNotIn("SRC-444444444444", snapshot["portfolio"]["provenance"]["market_value"]["source_ids"])
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])
        self.assertNotIn("SRC-444444444444", {source["id"] for source in snapshot["sources"]})
        self.assertIn("INVALID_SOURCE_DATE", {warning["code"] for warning in snapshot["warnings"]})
        self.assertIsNotNone(connection.execute("SELECT 1 FROM source_records WHERE id = 'SRC-444444444444'").fetchone())


class FourteenthRepairBenchmarkTests(unittest.TestCase):
    def test_same_date_typed_benchmarks_remain_independent_and_same_identity_conflicts_fail_closed(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        observation_date = "2026-01-02"
        add_watchlist_item(
            connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", "AAA", "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            benchmark_identifier_value="AAA",
        )
        add_source_record(
            connection, source_name="benchmarks", source_identifier="bench-a-1",
            retrieved_at=f"{observation_date}T00:00:00+00:00", observation_date=observation_date,
            instrument_identifier="AAA", instrument_identifier_type="other", field="benchmark_price",
            value="100", unit="price", currency="JPY", freshness_status="current",
            citation_location="bench-a-1", parser_version="v1",
        )
        add_source_record(
            connection, source_name="benchmarks", source_identifier="bench-a-2",
            retrieved_at=f"{observation_date}T00:00:00+00:00", observation_date=observation_date,
            instrument_identifier="AAA", instrument_identifier_type="other", field="benchmark_price",
            value="101", unit="price", currency="JPY", freshness_status="current",
            citation_location="bench-a-2", parser_version="v1",
        )
        add_source_record(
            connection, source_name="benchmarks", source_identifier="bench-b-1",
            retrieved_at=f"{observation_date}T00:00:00+00:00", observation_date=observation_date,
            instrument_identifier="BBB", instrument_identifier_type="yahoo_symbol", field="benchmark_price",
            value="200", unit="price", currency="JPY", freshness_status="current",
            citation_location="bench-b-1", parser_version="v1",
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of=observation_date)

        self.assertEqual(
            {(row["identifier_type"], row["instrument"], row["price"]) for row in snapshot["benchmarks"]},
            {("yahoo_symbol", "BBB", 200.0)},
        )
        self.assertIn("CONFLICTING_BENCHMARK", {warning["code"] for warning in snapshot["warnings"]})


if __name__ == "__main__":
    unittest.main()
