import csv
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.broker_csv_import import import_csv
from nisa_quant.recommendation_journal import evaluate_recommendation, record_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import render_report, validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.candidate_screening import run_screens
from nisa_quant.source_records import add_source_record, import_distribution_fixture, import_price_fixture
from tests.test_time_helpers import current_utc_date


BROKER_COLUMNS = (
    "取引日", "口座区分", "銘柄コード", "銘柄コード種別", "銘柄名", "取引区分",
    "数量", "単価", "手数料", "通貨", "分配金", "備考",
)
PRICE_COLUMNS = (
    "identifier", "identifier_type", "observation_date", "price", "currency",
    "retrieved_at", "freshness_status", "citation_location", "benchmark",
)
DISTRIBUTION_COLUMNS = (
    "identifier", "identifier_type", "observation_date", "distribution_amount", "unit",
    "currency", "retrieved_at", "freshness_status", "citation_location",
)


def write_csv(path: Path, columns: tuple[str, ...], rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        writer.writerows(rows)


def new_connection(directory: str) -> sqlite3.Connection:
    connection = connect_database(Path(directory) / "ledger.sqlite")
    initialize_database(connection)
    return connection


def backdate_sources(connection: sqlite3.Connection, retrieved_at: str) -> None:
    connection.execute("UPDATE source_records SET retrieved_at = ?", (retrieved_at,))
    connection.commit()


class TemporalAndImportRepairTests(unittest.TestCase):
    def test_retrieval_timestamp_uses_utc_calendar_date_for_availability(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            same_day_id = add_source_record(
                connection, source_name="prices", source_identifier="same-day",
                retrieved_at="2026-01-01T23:30:00+00:00", observation_date="2026-01-01",
                instrument_identifier="SAME", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="prices:same-day", parser_version="v1",
            )
            next_utc_day_id = add_source_record(
                connection, source_name="prices", source_identifier="negative-offset",
                retrieved_at="2026-01-01T23:30:00-12:00", observation_date="2026-01-01",
                instrument_identifier="NEXT", instrument_identifier_type="other", field="price",
                value="101", unit="price", currency="USD", freshness_status="current",
                citation_location="prices:negative-offset", parser_version="v1",
            )
            connection.commit()

            jan_first = calculate_snapshot(connection, as_of="2026-01-01")
            jan_second = calculate_snapshot(connection, as_of="2026-01-02")

            self.assertEqual(len(jan_first["sources"]), 1)
            self.assertEqual(jan_first["sources"][0]["id"], same_day_id)
            self.assertEqual(
                {source["id"] for source in jan_second["sources"]},
                {same_day_id, next_utc_day_id},
            )
            self.assertEqual(
                connection.execute(
                    "SELECT retrieved_at FROM source_records WHERE id = ?", (next_utc_day_id,)
                ).fetchone()[0],
                "2026-01-02T11:30:00+00:00",
            )
            connection.close()

    def test_benchmark_alias_requires_explicit_typed_link(self) -> None:
        from nisa_quant.database_schema import link_instruments
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "1306", "jpx_code", "TOPIX ETF", "ETF", "JPX", "JPY", "TOPIX", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            for day, price in (("2026-01-01", "100"), ("2026-01-02", "110")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"1306#{day}",
                    retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                    instrument_identifier="1306", instrument_identifier_type="jpx_code",
                    field="price", value=price, unit="price", currency="JPY",
                    freshness_status="current", citation_location=f"prices:{day}", parser_version="v1",
                )
                add_source_record(
                    connection, source_name="prices", source_identifier=f"topix#{day}",
                    retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                    instrument_identifier="TOPIX.BENCHMARK", instrument_identifier_type="other",
                    field="benchmark_price", value=price, unit="price", currency="JPY",
                    freshness_status="current", citation_location=f"prices:topix:{day}", parser_version="v1",
                )
            connection.commit()
            without_link = calculate_snapshot(connection, as_of="2026-01-02")["watchlist"][0]
            self.assertEqual(without_link["benchmark"], "TOPIX")
            self.assertIsNone(without_link["benchmark_return_pct"])
            self.assertIn("MISSING_BENCHMARK", {item["code"] for item in calculate_snapshot(connection, as_of="2026-01-02")["warnings"]})

            for identifier in ("TOPIX", "TOPIX.BENCHMARK"):
                connection.execute(
                    "INSERT INTO instruments(identifier_type, identifier_value, display_name, currency) VALUES (?, ?, ?, ?)",
                    ("other", identifier, identifier, "JPY"),
                )
            link_instruments(
                connection,
                from_identifier_type="other", from_identifier_value="TOPIX",
                to_identifier_type="other", to_identifier_value="TOPIX.BENCHMARK",
                link_type="explicit_benchmark_alias",
            )
            linked = calculate_snapshot(connection, as_of="2026-01-02")["watchlist"][0]
            self.assertEqual(linked["benchmark_identifier_type"], "other")
            self.assertIsNotNone(linked["benchmark_return_pct"])
            connection.close()

    def test_missing_benchmark_type_is_unavailable_even_when_value_matches(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            )
            for day, price in (("2026-01-01", "100"), ("2026-01-02", "110")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"vt#{day}",
                    retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                    instrument_identifier="VT", instrument_identifier_type="other", field="price",
                    value=price, unit="price", currency="USD", freshness_status="current",
                    citation_location=f"prices:{day}", parser_version="v1",
                )
                add_source_record(
                    connection, source_name="prices", source_identifier=f"msci#{day}",
                    retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                    instrument_identifier="MSCI", instrument_identifier_type="other", field="benchmark_price",
                    value=price, unit="price", currency="USD", freshness_status="current",
                    citation_location=f"prices:msci:{day}", parser_version="v1",
                )
            connection.commit()
            connection.execute(
                "UPDATE watchlist_versions SET benchmark_identifier_type = NULL WHERE identifier_value = 'VT'"
            )
            connection.commit()
            item = calculate_snapshot(connection, as_of="2026-01-02")["watchlist"][0]
            self.assertEqual(item["benchmark"], "UNDECLARED")
            self.assertIsNone(item["benchmark_return_pct"])
            connection.close()

    def test_source_fixture_rejects_blank_provenance_fields(self) -> None:
        for field_index in (6, 7, 8):
            with self.subTest(field_index=field_index), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "invalid-prices.csv"
                row = [
                    "1306", "jpx_code", "2026-01-01", "100", "JPY",
                    "2026-01-01T00:00:00+00:00", "current", "prices:1", "no",
                ]
                row[field_index] = ""
                write_csv(path, PRICE_COLUMNS, [row])
                connection = new_connection(directory)
                with self.assertRaises(ValueError):
                    import_price_fixture(connection, path, source_name="prices")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
                connection.close()

    def test_add_source_record_rejects_retrieval_before_observation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            with self.assertRaises(ValueError):
                add_source_record(
                    connection, source_name="prices", source_identifier="bad", retrieved_at="2026-01-01T00:00:00+00:00",
                    observation_date="2026-01-02", instrument_identifier="VT", instrument_identifier_type="other",
                    field="price", value="100", unit="price", currency="USD", freshness_status="current",
                    citation_location="prices:bad", parser_version="v1",
                )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
            connection.close()

    def test_distribution_fixture_is_strict_and_keeps_source_provenance(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            path = Path(__file__).parent / "fixtures" / "synthetic_distributions.csv"
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            )
            add_source_record(
                connection, source_name="prices", source_identifier="VT#price",
                retrieved_at="2026-06-30T00:00:00+00:00", observation_date="2026-06-30",
                instrument_identifier="VT", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="prices:VT", parser_version="v1",
            )
            result = import_distribution_fixture(connection, path, source_name="local-distributions")
            self.assertEqual(result.accepted_rows, 2)
            self.assertEqual(
                connection.execute("SELECT field, unit, citation_location FROM source_records ORDER BY observation_date").fetchall()[0][0],
                "distribution",
            )
            snapshot = calculate_snapshot(connection, as_of="2026-07-01")
            item = snapshot["watchlist"][0]
            self.assertEqual(item["distribution_amount"], 0.85)
            self.assertAlmostEqual(item["distribution_change_pct"], 6.25)
            self.assertAlmostEqual(item["distribution_yield_pct"], 0.85)
            self.assertEqual(len(item["distribution_source_ids"]), 2)
            self.assertEqual(snapshot["data_cutoffs"]["distributions"], "2026-06-30")
            self.assertTrue(all(source_id.startswith("SRC-") for source_id in item["distribution_source_ids"]))
            connection.close()

    def test_distribution_metrics_exclude_future_and_warn_on_insufficient_history(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "distributions.csv"
            write_csv(path, DISTRIBUTION_COLUMNS, [
                ["VT", "other", "2026-01-01", "1.00", "per_unit", "USD", "2026-01-02T00:00:00+00:00", "current", "dist:1"],
                ["VT", "other", "2026-03-01", "2.00", "per_unit", "USD", "2026-03-02T00:00:00+00:00", "current", "dist:future"],
            ])
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            )
            import_distribution_fixture(connection, path, source_name="local-distributions")
            snapshot = calculate_snapshot(connection, as_of="2026-01-15")
            item = snapshot["watchlist"][0]
            self.assertEqual(item["distribution_amount"], 1.0)
            self.assertIsNone(item["distribution_change_pct"])
            self.assertIsNone(item["distribution_yield_pct"])
            self.assertEqual(item["distribution_source_ids"], [item["distribution_source_ids"][0]])
            self.assertIn("INSUFFICIENT_DISTRIBUTION_HISTORY", {warning["code"] for warning in snapshot["warnings"]})
            self.assertNotEqual(snapshot["data_cutoffs"]["distributions"], "2026-03-01")
            connection.close()

    def test_report_rejects_unsupported_action_prose_but_allows_explanatory_token(self) -> None:
        base = """# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-01-01`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| VT | watchlist | WATCH | limited | current_price=100 | [SRC-abcdef123456] |
**VT — WATCH**
- Reason: {prose}
## Source list
- [SRC-abcdef123456] local
Manual review required; no order was placed.
"""
        for prose in ("STRONG BUY", "BUY NOW", "STRONG SELL", "SELL NOW", "GUARANTEED RETURN", "MAYBE ACTION"):
            with self.subTest(prose=prose):
                with self.assertRaises(ValueError):
                    validate_report(base.format(prose=prose), source_records=["SRC-abcdef123456"])
        validate_report(
            base.format(prose="The token field is explanatory and contains no credential."),
            source_records=["SRC-abcdef123456"],
        )


    def test_future_retrieval_excludes_past_trade_and_undated_future_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "past trade",
            ]])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            connection.execute(
                "UPDATE source_records SET retrieved_at = ?",
                ("2026-02-02T00:00:00+00:00",),
            )
            connection.execute(
                "INSERT INTO data_warnings(warning_code, message, created_at, observation_date) VALUES (?, ?, ?, ?)",
                ("FUTURE_WARNING", "not available yet", "2026-02-02T00:00:00+00:00", None),
            )
            connection.commit()

            snapshot = calculate_snapshot(connection, as_of="2026-01-31")

            self.assertEqual(snapshot["portfolio"]["holdings"], [])
            self.assertNotIn("FUTURE_WARNING", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()

    def test_non_finite_broker_quantity_is_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "non-finite.csv"
            write_csv(path, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "NaN", "100", "0", "JPY", "", "not finite",
            ]])
            connection = new_connection(directory)

            result = import_csv(connection, path, source_name="fixture")

            self.assertEqual(result.accepted_rows, 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            self.assertIn("MALFORMED_ROW", {row[0] for row in connection.execute("SELECT warning_code FROM data_warnings")})
            connection.close()

    def test_non_finite_price_is_rejected_without_a_source_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "non-finite-prices.csv"
            write_csv(path, PRICE_COLUMNS, [[
                "1306", "jpx_code", "2026-01-01", "Infinity", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:2", "no",
            ]])
            connection = new_connection(directory)

            with self.assertRaises(ValueError):
                import_price_fixture(connection, path, source_name="prices")

            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
            connection.close()

    def test_mixed_currency_snapshot_is_unavailable_without_fx(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "mixed.csv"
            write_csv(broker, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "jpy"],
                ["2026-01-01", "NISA", "VT", "other", "Global ETF", "BUY", "1", "200", "0", "USD", "", "usd"],
            ])
            prices = root / "mixed-prices.csv"
            write_csv(prices, PRICE_COLUMNS, [
                ["1306", "jpx_code", "2026-01-02", "110", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:2", "no"],
                ["VT", "other", "2026-01-02", "210", "USD", "2026-01-02T00:00:00+00:00", "current", "prices:3", "no"],
            ])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            import_price_fixture(connection, prices, source_name="prices")
            backdate_sources(connection, "2026-01-03T00:00:00+00:00")

            snapshot = calculate_snapshot(connection, as_of="2026-01-03")

            self.assertIsNone(snapshot["portfolio"]["market_value"])
            self.assertIsNone(snapshot["portfolio"]["unrealized_pl"])
            self.assertIn("MIXED_CURRENCY_NO_FX", {warning["code"] for warning in snapshot["warnings"]})
            self.assertEqual(snapshot["portfolio"]["market_value_by_currency"], {"JPY": 110.0, "USD": 210.0})
            connection.close()

    def test_explicit_typed_instrument_link_is_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "links.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "jpx"],
                ["2026-01-02", "NISA", "1306.T", "yahoo_symbol", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "yahoo"],
            ])
            import_csv(connection, path, source_name="fixture")

            import nisa_quant.database_schema as schema

            self.assertTrue(hasattr(schema, "add_instrument_link"))
            if not hasattr(schema, "add_instrument_link"):
                connection.close()
                return

            schema.add_instrument_link(
                connection,
                from_identifier_type="jpx_code",
                from_identifier_value="1306",
                to_identifier_type="yahoo_symbol",
                to_identifier_value="1306.T",
                link_type="explicit_alias",
            )

            link = connection.execute("SELECT link_type FROM instrument_links").fetchone()
            self.assertEqual(link[0], "explicit_alias")
            connection.close()

    def test_same_date_duplicate_is_one_history_point_and_conflict_is_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy",
            ]])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            backdate_sources(connection, "2026-01-02T00:00:00+00:00")
            add_source_record(
                connection, source_name="prices", source_identifier="prices#1", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="110", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:1", parser_version="v1",
            )
            add_source_record(
                connection, source_name="prices", source_identifier="prices#2", retrieved_at="2026-01-03T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="110.0", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:2", parser_version="v1",
            )
            deduplicated = calculate_snapshot(connection, as_of="2026-01-03")
            self.assertEqual(len(deduplicated["portfolio"]["holdings"][0]["price_history"]), 1)

            add_source_record(
                connection, source_name="prices", source_identifier="prices#3", retrieved_at="2026-01-04T00:00:00+00:00",
                observation_date="2026-01-04", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="120", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:3", parser_version="v1",
            )
            add_source_record(
                connection, source_name="prices", source_identifier="prices#4", retrieved_at="2026-01-05T00:00:00+00:00",
                observation_date="2026-01-04", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="121", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:4", parser_version="v1",
            )
            conflicting = calculate_snapshot(connection, as_of="2026-01-05")
            holding = conflicting["portfolio"]["holdings"][0]
            self.assertEqual(holding["price_status"], "conflicting")
            self.assertIsNone(holding["latest_price"])
            self.assertIsNone(holding["market_value"])
            self.assertEqual(holding["price_history"], [])
            connection.close()
    def test_cutoff_excludes_future_records_and_future_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "past"],
                ["2026-03-01", "NISA", "9984", "jpx_code", "Future Stock", "BUY", "2", "200", "0", "JPY", "", "future"],
                ["2026-03-02", "mystery", "6758", "jpx_code", "Future Review", "BUY", "1", "300", "0", "JPY", "", "future warning"],
            ])
            prices = root / "prices.csv"
            write_csv(prices, PRICE_COLUMNS, [
                ["1306", "jpx_code", "2026-01-02", "110", "JPY", "2026-01-03T00:00:00+00:00", "current", "prices:2", "no"],
                ["9984", "jpx_code", "2026-03-02", "220", "JPY", "2026-03-03T00:00:00+00:00", "current", "prices:3", "no"],
                ["TOPIX.BENCHMARK", "other", "2026-03-02", "101", "JPY", "2026-03-03T00:00:00+00:00", "current", "prices:4", "yes"],
            ])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            import_price_fixture(connection, prices, source_name="fixture-prices")
            connection.execute(
                "UPDATE source_records SET retrieved_at = ? WHERE source_url_or_identifier = ?",
                ("2026-01-03T00:00:00+00:00", "broker.csv#row-2"),
            )
            connection.commit()

            snapshot = calculate_snapshot(connection, as_of="2026-01-31")

            self.assertEqual([holding["instrument"] for holding in snapshot["portfolio"]["holdings"]], ["1306"])
            self.assertEqual(snapshot["portfolio"]["market_value"], 110.0)
            self.assertEqual(snapshot["data_cutoffs"]["prices"], "2026-01-02")
            self.assertNotIn("9984", {source["instrument_identifier"] for source in snapshot["sources"]})
            self.assertNotIn("UNKNOWN_ACCOUNT", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()

    def test_overflow_and_blank_required_numeric_rows_are_quarantined(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "strict.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "", "100", "0", "JPY", "", "blank quantity"],
                ["2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "overflow", "unexpected"],
            ])
            connection = new_connection(directory)
            result = import_csv(connection, path, source_name="fixture")

            self.assertEqual(result.accepted_rows, 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_quarantine").fetchone()[0], 2)
            messages = [row[0] for row in connection.execute("SELECT message FROM data_warnings")]
            self.assertTrue(any("required" in message.lower() for message in messages))
            self.assertTrue(any("extra" in message.lower() for message in messages))
            connection.close()

    def test_average_cost_sell_uses_sold_average_basis_and_cash_records_are_contributions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounting.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "10", "1000", "0", "JPY", "", "buy"],
                ["2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "SELL", "5", "2000", "0", "JPY", "", "sell"],
                ["2026-01-03", "NISA", "", "", "", "CASH", "", "3000", "0", "JPY", "", "deposit"],
                ["2026-01-04", "NISA", "", "", "", "CASH", "", "-500", "0", "JPY", "", "withdrawal"],
            ])
            connection = new_connection(directory)
            import_csv(connection, path, source_name="fixture")
            backdate_sources(connection, "2026-01-05T00:00:00+00:00")
            position = connection.execute(
                "SELECT quantity, cost_basis FROM positions"
            ).fetchone()
            snapshot = calculate_snapshot(connection, as_of="2026-01-31")

            self.assertEqual(tuple(position), (5.0, 5000.0))
            self.assertEqual(snapshot["portfolio"]["realized_pl"], 5000.0)
            self.assertEqual(snapshot["portfolio"]["contributions"], 3000.0)
            self.assertEqual(snapshot["portfolio"]["cash_movements"], 2500.0)
            connection.close()

    def test_identifier_type_is_required_and_no_alias_is_invented(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "identifiers.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306.T", "", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "missing type"],
                ["2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "explicit type"],
            ])
            connection = new_connection(directory)
            import_csv(connection, path, source_name="fixture")
            identifiers = [tuple(row) for row in connection.execute(
                "SELECT identifier_type, identifier_value FROM instruments"
            )]

            self.assertEqual(identifiers, [("jpx_code", "1306")])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_quarantine").fetchone()[0], 1)
            connection.close()

    def test_legacy_untyped_transaction_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.csv"
            write_csv(path, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "legacy",
            ]])
            connection = new_connection(directory)
            import_csv(connection, path, source_name="fixture")
            backdate_sources(connection, "2026-01-02T00:00:00+00:00")
            connection.execute(
                "UPDATE source_records SET instrument_identifier_type = NULL WHERE field = 'buy'"
            )
            connection.commit()

            snapshot = calculate_snapshot(connection, as_of="2026-01-31")

            self.assertEqual(snapshot["portfolio"]["holdings"], [])
            connection.close()

    def test_refreshed_source_metadata_creates_a_distinct_auditable_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            first = add_source_record(
                connection, source_name="local", source_identifier="prices#2",
                retrieved_at="2026-08-01T00:00:00+00:00", observation_date="2026-07-31",
                instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="100", unit="price", currency="JPY",
                freshness_status="current", citation_location="prices:2", parser_version="v1",
            )
            second = add_source_record(
                connection, source_name="local", source_identifier="prices#2",
                retrieved_at="2026-08-02T00:00:00+00:00", observation_date="2026-07-31",
                instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="100", unit="price", currency="JPY",
                freshness_status="current", citation_location="prices:2", parser_version="v1",
            )

            self.assertNotEqual(first, second)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)
            connection.close()

    def test_price_fixture_rejects_blank_required_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "prices.csv"
            write_csv(path, PRICE_COLUMNS, [[
                "1306", "jpx_code", "2026-01-01", "", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:2", "no",
            ]])
            connection = new_connection(directory)
            with self.assertRaises(ValueError):
                import_price_fixture(connection, path, source_name="prices")
            connection.close()

    def test_distribution_uses_distribution_field_and_is_cited(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "distribution.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy"],
                ["2026-02-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "DISTRIBUTION", "1", "0", "0", "JPY", "120", "distribution"],
            ])
            connection = new_connection(directory)
            import_csv(connection, path, source_name="fixture")
            backdate_sources(connection, "2026-02-02T00:00:00+00:00")
            source = connection.execute(
                "SELECT field, value FROM source_records WHERE field = 'distribution'"
            ).fetchone()
            snapshot = calculate_snapshot(connection, as_of="2026-02-28")

            self.assertEqual(tuple(source), ("distribution", "120"))
            unit = connection.execute("SELECT unit FROM source_records WHERE field = 'distribution'").fetchone()[0]
            self.assertEqual(unit, "total_cash")
            self.assertEqual(snapshot["portfolio"]["distributions"], 120.0)
            self.assertIn(source[0], {"distribution"})
            report = render_report(
                snapshot, run_screens(connection, snapshot, as_of="2026-02-28"),
                provider="local", template_version="v1",
            )
            self.assertIn("Distributions (total_cash): `120.00`", report)
            connection.close()


class MetricsWatchlistReportAndJournalRepairTests(unittest.TestCase):
    def test_new_undated_watchlist_item_is_current_not_historical(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(connection, "NEW", "other", "New item", "ETF", "NYSE", "USD", None, "current")
            current = current_utc_date()
            self.assertEqual(calculate_snapshot(connection, as_of="2026-01-01")["watchlist"], [])
            self.assertEqual(calculate_snapshot(connection, as_of=current)["watchlist"][0]["identifier_value"], "NEW")
            connection.close()

    def test_watchlist_membership_and_metadata_are_point_in_time_versioned(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF v1", "ETF", "NYSE", "USD", "MSCI World", "old",
                effective_date="2026-02-01", observed_at="2026-02-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            old = calculate_snapshot(connection, as_of="2026-01-31")
            first = calculate_snapshot(connection, as_of="2026-02-28")
            self.assertEqual(old["watchlist"], [])
            self.assertEqual(first["watchlist"][0]["display_name"], "Global ETF v1")

            add_watchlist_item(
                connection, "VT", "other", "Global ETF v2", "ETF", "NASDAQ", "USD", "MSCI ACWI", "edited",
                effective_date="2026-03-01", observed_at="2026-03-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            historical = calculate_snapshot(connection, as_of="2026-02-28")["watchlist"][0]
            current = calculate_snapshot(connection, as_of="2026-03-31")["watchlist"][0]
            self.assertEqual(historical["display_name"], "Global ETF v1")
            self.assertEqual(historical["market"], "NYSE")
            self.assertEqual(current["display_name"], "Global ETF v2")
            self.assertEqual(current["benchmark"], "MSCI ACWI")
            connection.close()

    def test_identical_refresh_uses_latest_retrieval_status_not_source_id_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            broker = Path(directory) / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy",
            ]])
            import_csv(connection, broker, source_name="fixture")
            add_source_record(
                connection, source_name="old", source_identifier="old", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="110", unit="price", currency="JPY", freshness_status="stale",
                citation_location="old", parser_version="v1",
            )
            add_source_record(
                connection, source_name="new", source_identifier="new", retrieved_at="2026-01-03T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="1306", instrument_identifier_type="jpx_code",
                field="price", value="110.0", unit="price", currency="JPY", freshness_status="current",
                citation_location="new", parser_version="v1",
            )
            connection.execute("UPDATE source_records SET retrieved_at = ? WHERE field = 'buy'", ("2026-01-03T00:00:00+00:00",))
            connection.commit()
            holding = calculate_snapshot(connection, as_of="2026-01-03")["portfolio"]["holdings"][0]
            self.assertEqual(holding["price_status"], "current")
            self.assertEqual(holding["price_history"][0]["source_id"], connection.execute(
                "SELECT id FROM source_records WHERE source_name = 'new'"
            ).fetchone()[0])
            connection.close()

    def test_undeclared_benchmark_is_not_inferred_and_typed_benchmark_survives_screen(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            )
            add_watchlist_item(
                connection, "1306", "jpx_code", "TOPIX ETF", "ETF", "JPX", "JPY", "TOPIX.BENCHMARK", "typed",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            for identifier, identifier_type, currency in (("VT", "other", "USD"), ("1306", "jpx_code", "JPY")):
                for day, price in (("2026-01-01", "100"), ("2026-01-02", "110"), ("2026-01-03", "120")):
                    add_source_record(
                        connection, source_name="prices", source_identifier=f"{identifier}#{day}",
                        retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                        instrument_identifier=identifier, instrument_identifier_type=identifier_type,
                        field="price", value=price, unit="price", currency=currency, freshness_status="current",
                        citation_location=f"prices:{identifier}:{day}", parser_version="v1",
                    )
            add_source_record(
                connection, source_name="prices", source_identifier="topix#2026-01-03",
                retrieved_at="2026-01-03T00:00:00+00:00", observation_date="2026-01-03",
                instrument_identifier="TOPIX.BENCHMARK", instrument_identifier_type="other", field="benchmark_price",
                value="105", unit="price", currency="JPY", freshness_status="current", citation_location="topix", parser_version="v1",
            )
            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            by_identifier = {item["identifier"]: item for item in snapshot["watchlist"]}
            self.assertEqual(by_identifier["VT"]["benchmark"], "UNDECLARED")
            self.assertIsNone(by_identifier["VT"]["benchmark_return_pct"])
            self.assertEqual(by_identifier["1306"]["benchmark_identifier_type"], "other")
            candidate = next(candidate for candidate in run_screens(connection, snapshot, as_of="2026-01-03") if candidate["instrument"] == "1306")
            self.assertEqual(candidate["benchmark_identifier_type"], "other")
            self.assertIn("UNDECLARED_BENCHMARK", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()

    def test_portfolio_risk_exposes_common_dates_drawdown_and_never_copies_holding_volatility(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            for identifier, name in (("1306", "TOPIX ETF"), ("9984", "Stock")):
                broker = Path(directory) / f"{identifier}.csv"
                write_csv(broker, BROKER_COLUMNS, [[
                    "2026-01-01", "NISA", identifier, "jpx_code", name, "BUY", "1", "100", "0", "JPY", "", "buy",
                ]])
                import_csv(connection, broker, source_name="fixture")
            prices = {
                "1306": (("2026-01-01", "100"), ("2026-01-02", "120"), ("2026-01-03", "90")),
                "9984": (("2026-01-01", "100"), ("2026-01-03", "110"), ("2026-01-04", "105")),
            }
            for identifier, rows in prices.items():
                for day, price in rows:
                    add_source_record(
                        connection, source_name="prices", source_identifier=f"{identifier}#{day}",
                        retrieved_at=f"{day}T00:00:00+00:00", observation_date=day,
                        instrument_identifier=identifier, instrument_identifier_type="jpx_code", field="price",
                        value=price, unit="price", currency="JPY", freshness_status="current",
                        citation_location=f"prices:{identifier}:{day}", parser_version="v1",
                    )
            connection.execute("UPDATE source_records SET retrieved_at = '2026-01-04T00:00:00+00:00' WHERE field = 'buy'")
            connection.commit()
            portfolio = calculate_snapshot(connection, as_of="2026-01-04")["portfolio"]
            self.assertEqual(portfolio["risk_series"]["dates"], ["2026-01-01", "2026-01-03"])
            self.assertIsNotNone(portfolio["drawdown_pct"])
            self.assertEqual(portfolio["drawdown_pct"], 0.0)
            self.assertIsNone(portfolio["volatility"])
            self.assertIn("two", portfolio["volatility_reason"])
            connection.close()

    def test_report_validation_binds_candidates_aggregates_and_distribution_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            broker = Path(directory) / "report.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy",
            ], [
                "2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "DISTRIBUTION", "1", "0", "0", "JPY", "5", "distribution",
            ]])
            import_csv(connection, broker, source_name="fixture")
            connection.execute("UPDATE source_records SET retrieved_at = '2026-01-03T00:00:00+00:00'")
            connection.commit()
            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            candidates = run_screens(connection, snapshot, as_of="2026-01-03")
            report = render_report(snapshot, candidates, provider="local", template_version="v1")
            altered = report.replace("Reason: Deterministic screen", "Reason: Invented screen", 1)
            with self.assertRaises(ValueError):
                validate_report(
                    altered, snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            altered_total = report.replace("Distributions (total_cash): `5.00`", "Distributions (total_cash): `999.00`")
            with self.assertRaises(ValueError):
                validate_report(
                    altered_total, snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            without_provenance = report.replace("Distribution provenance:", "Removed provenance:")
            with self.assertRaises(ValueError):
                validate_report(
                    without_provenance, snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            connection.close()

    def test_report_rejects_unrelated_candidate_source_and_allows_explanatory_token(self) -> None:
        snapshot = {"as_of": "2026-01-01", "data_cutoffs": {}, "warnings": [], "portfolio": {
            "market_value": None, "cost_basis": None, "contributions": None, "distributions": None,
            "distribution_unit": "total_cash", "holdings": [], "allocation": {}, "concentration": {},
        }, "watchlist": [], "sources": [{"id": "SRC-abcdef123456", "source_name": "local", "source_url_or_identifier": "safe", "retrieved_at": "2026-01-01T00:00:00+00:00", "observation_date": "2026-01-01", "citation_location": "safe"}]}
        candidate = {"instrument": "VT", "identifier_type": "other", "identifier_value": "VT", "account": "watchlist", "label": "WATCH", "evidence_quality": "limited", "metrics": {"current_price": None}, "reason": "token is a field name, not a credential", "risk_counter_evidence": "none", "horizon": "long-term", "invalidation": "new data", "source_ids": ["SRC-000000000000"], "ledger_source_ids": [], "manual_review": "Manual review required; no order can be placed by this tool."}
        with self.assertRaises(ValueError):
            render_report(snapshot, [candidate], provider="local", template_version="v1")

    def test_journal_rejects_altered_candidate_rationale_and_conflicting_outcome_evidence(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            for identifier, field, value in (("VT", "price", "100"), ("MSCI.BENCHMARK", "benchmark_price", "10")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"{identifier}#1", retrieved_at="2026-01-01T00:00:00+00:00",
                    observation_date="2026-01-01", instrument_identifier=identifier, instrument_identifier_type="other",
                    field=field, value=value, unit="price", currency="USD", freshness_status="current", citation_location="p1", parser_version="v1",
                )
            for day, value in (("2026-01-02", "110"), ("2026-01-02", "111")):
                add_source_record(
                    connection, source_name="later", source_identifier=f"VT#{value}", retrieved_at="2026-01-02T00:00:00+00:00",
                    observation_date=day, instrument_identifier="VT", instrument_identifier_type="other", field="price", value=value,
                    unit="price", currency="USD", freshness_status="current", citation_location="later", parser_version="v1",
                )
            benchmark_later = add_source_record(
                connection, source_name="later", source_identifier="MSCI#later", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="MSCI.BENCHMARK", instrument_identifier_type="other", field="benchmark_price", value="11",
                unit="price", currency="USD", freshness_status="current", citation_location="later", parser_version="v1",
            )
            connection.commit()
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
            altered = dict(candidate)
            altered["reason"] = "invented rationale"
            with self.assertRaises(ValueError):
                record_recommendation(connection, altered, data_cutoff="2026-01-01", provider="local", template_version="v1", snapshot=snapshot)
            recommendation_id = record_recommendation(connection, candidate, data_cutoff="2026-01-01", provider="local", template_version="v1", snapshot=snapshot)
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-02", observed_price_source_id=connection.execute(
                        "SELECT id FROM source_records WHERE source_url_or_identifier = 'VT#110'"
                    ).fetchone()[0], benchmark_price_source_id=benchmark_later,
                )
            connection.close()
    def test_watchlist_only_items_are_screened(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            from nisa_quant.watchlist import add_watchlist_item

            add_watchlist_item(connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI World", "watch", effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other")
            for day, price in (("2026-01-01", "100"), ("2026-01-02", "105"), ("2026-01-03", "110")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"vt#{day}", retrieved_at=f"{day}T00:00:00+00:00",
                    observation_date=day, instrument_identifier="VT", instrument_identifier_type="other",
                    field="price", value=price, unit="price", currency="USD", freshness_status="current",
                    citation_location=f"prices:{day}", parser_version="v1",
                )

            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            candidates = run_screens(connection, snapshot, as_of="2026-01-03")

            self.assertEqual([candidate["instrument"] for candidate in candidates], ["VT"])
            self.assertEqual(candidates[0]["benchmark"], "MSCI World")
            connection.close()

    def test_declared_benchmark_is_selected_instead_of_first_global_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy",
            ]])
            prices = root / "prices.csv"
            write_csv(prices, PRICE_COLUMNS, [
                ["1306", "jpx_code", "2026-01-01", "100", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:1", "no"],
                ["1306", "jpx_code", "2026-01-02", "110", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:2", "no"],
                ["TOPIX.BENCHMARK", "other", "2026-01-01", "100", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:3", "yes"],
                ["TOPIX.BENCHMARK", "other", "2026-01-02", "101", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:4", "yes"],
                ["MSCI.BENCHMARK", "other", "2026-01-01", "100", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:5", "yes"],
                ["MSCI.BENCHMARK", "other", "2026-01-02", "120", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:6", "yes"],
            ])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            connection.execute("UPDATE instruments SET benchmark = 'MSCI.BENCHMARK', benchmark_identifier_type = 'other', benchmark_identifier_value = 'MSCI.BENCHMARK' WHERE identifier_value = '1306'")
            connection.commit()
            import_price_fixture(connection, prices, source_name="prices")
            connection.execute(
                "UPDATE source_records SET retrieved_at = ? WHERE source_url_or_identifier = ?",
                ("2026-01-02T00:00:00+00:00", "broker.csv#row-2"),
            )
            connection.commit()

            holding = calculate_snapshot(connection, as_of="2026-01-02")["portfolio"]["holdings"][0]

            self.assertEqual(holding.get("benchmark"), "MSCI.BENCHMARK")
            self.assertEqual(holding.get("benchmark_instrument"), "MSCI.BENCHMARK")
            self.assertAlmostEqual(holding.get("benchmark_return_pct"), 20.0)
            connection.close()

    def test_incomplete_aggregation_is_explicitly_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "incomplete.csv"
            write_csv(broker, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "priced"],
                ["2026-01-01", "NISA", "9984", "jpx_code", "SoftBank", "BUY", "1", "200", "0", "JPY", "", "missing"],
            ])
            prices = root / "one-price.csv"
            write_csv(prices, PRICE_COLUMNS, [[
                "1306", "jpx_code", "2026-01-02", "110", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:1", "no",
            ]])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            import_price_fixture(connection, prices, source_name="prices")
            connection.execute(
                "UPDATE source_records SET retrieved_at = ? WHERE source_url_or_identifier LIKE ?",
                ("2026-01-02T00:00:00+00:00", "incomplete.csv#row-%"),
            )
            connection.commit()

            portfolio = calculate_snapshot(connection, as_of="2026-01-03")["portfolio"]

            self.assertIsNone(portfolio["allocation"]["account_pct"])
            self.assertIsNone(portfolio["concentration"]["largest_holding_pct"])
            self.assertIsNone(portfolio["volatility"])
            self.assertIn("INCOMPLETE_AGGREGATION", {warning["code"] for warning in calculate_snapshot(connection, as_of="2026-01-03")["warnings"]})
            connection.close()
    def test_watchlist_and_local_history_expose_metrics_and_safe_insufficient_data(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            entries = [
                ("1306", "jpx_code", "TOPIX ETF", "ETF", "JPX", "JPY", "TOPIX", "Japanese ETF"),
                ("VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI World", "International ETF"),
                ("9984", "jpx_code", "SoftBank Group", "stock", "JPX", "JPY", "TOPIX", "Japanese stock"),
            ]
            for entry in entries:
                add_watchlist_item(connection, *entry, effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00")
            rows = connection.execute("SELECT identifier_type, display_name, asset_type, market, currency, benchmark FROM watchlist ORDER BY id").fetchall()

            self.assertEqual([tuple(row) for row in rows], [entry[1:7] for entry in entries])
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            self.assertIsNone(snapshot["portfolio"]["volatility"])
            self.assertTrue(any(warning["code"] == "INSUFFICIENT_HISTORY" for warning in snapshot["warnings"]))
            connection.close()

    def test_dated_history_calculates_risk_trend_and_benchmark_relative_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "buy",
            ]])
            prices = root / "prices.csv"
            write_csv(prices, PRICE_COLUMNS, [
                ["1306", "jpx_code", "2026-01-01", "100", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:2", "no"],
                ["1306", "jpx_code", "2026-01-02", "120", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:3", "no"],
                ["1306", "jpx_code", "2026-01-03", "90", "JPY", "2026-01-03T00:00:00+00:00", "current", "prices:4", "no"],
                ["TOPIX.BENCHMARK", "other", "2026-01-01", "100", "JPY", "2026-01-01T00:00:00+00:00", "current", "prices:5", "yes"],
                ["TOPIX.BENCHMARK", "other", "2026-01-02", "110", "JPY", "2026-01-02T00:00:00+00:00", "current", "prices:6", "yes"],
                ["TOPIX.BENCHMARK", "other", "2026-01-03", "100", "JPY", "2026-01-03T00:00:00+00:00", "current", "prices:7", "yes"],
            ])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="fixture")
            import_price_fixture(connection, prices, source_name="prices")
            connection.execute("UPDATE instruments SET benchmark = 'TOPIX.BENCHMARK', benchmark_identifier_type = 'other', benchmark_identifier_value = 'TOPIX.BENCHMARK' WHERE identifier_value = '1306'")
            connection.commit()
            backdate_sources(connection, "2026-01-03T00:00:00+00:00")
            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            holding = snapshot["portfolio"]["holdings"][0]
            candidate = run_screens(connection, snapshot, as_of="2026-01-03")[0]

            self.assertEqual(holding["max_drawdown_pct"], -25.0)
            self.assertIsNotNone(holding["volatility_pct"])
            self.assertAlmostEqual(holding["price_return_pct"], -10.0)
            self.assertEqual(holding["benchmark_return_pct"], 0.0)
            self.assertAlmostEqual(holding["benchmark_relative_pct"], -10.0)
            self.assertEqual(candidate["metrics"]["moving_average_3"], 103.33333333333333)
            self.assertEqual(candidate["metrics"]["trend_context"], "below moving average")
            connection.close()

    def test_report_rejects_unsupported_labels_and_secret_shaped_content(self) -> None:
        base = """# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-08-28`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| 1306 | NISA | {label} | limited | quantity=1 | [SRC-abcdef123456] |
## Source list
- [SRC-abcdef123456] local
Manual review required; no order was placed.
"""
        with self.assertRaises(ValueError):
            validate_report(base.format(label="MAYBE"))
        with self.assertRaises(ValueError):
            validate_report(base.format(label="WATCH") + "token: sk-test-abcdefghijklmnopqrstuvwxyz123456\n")
        with self.assertRaises(ValueError):
            validate_report(base.format(label="WATCH") + "account_number=123456789\n")
        with self.assertRaises(ValueError):
            validate_report(base.format(label="WATCH") + "statement-123456.csv\n")

    def test_report_rejects_unsupported_prose_label_and_invented_source(self) -> None:
        report = """# NISA Quant Assistant Report
## Data cutoffs
- prices: `2026-08-28`
## Data warnings
- None
## Ranked candidates
| Instrument | Account | Label | Evidence | Metrics | Sources |
|---|---|---|---|---|---|
| 1306 | NISA | WATCH | limited | quantity=1 | [SRC-abcdef123456] |
**1306 — WATCH**
- Label: MAYBE
## Source list
- [SRC-abcdef123456] local
Manual review required; no order was placed.
"""
        with self.assertRaises(ValueError):
            validate_report(report)

        invented = report.replace("Label: MAYBE", "Label: WATCH").replace("SRC-abcdef123456", "SRC-000000000000")
        with self.assertRaises(ValueError):
            validate_report(invented, source_records=[])

    def test_journal_requires_later_cited_observations_and_persists_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            from nisa_quant.watchlist import add_watchlist_item

            add_watchlist_item(connection, "1306", "jpx_code", "TOPIX ETF", "ETF", "JPX", "JPY", "TOPIX.BENCHMARK", "watch", effective_date="2026-08-01", observed_at="2026-08-01T00:00:00+00:00", benchmark_identifier_type="other")
            for day, price in (("2026-08-26", "100"), ("2026-08-27", "101"), ("2026-08-28", "102")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"1306#{day}", retrieved_at=f"{day}T00:00:00+00:00",
                    observation_date=day, instrument_identifier="1306", instrument_identifier_type="jpx_code",
                    field="price", value=price, unit="price", currency="JPY", freshness_status="current",
                    citation_location=f"prices:{day}", parser_version="v1",
                )
            for day, price in (("2026-08-26", "10"), ("2026-08-27", "10.5"), ("2026-08-28", "11")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"topix#{day}", retrieved_at=f"{day}T00:00:00+00:00",
                    observation_date=day, instrument_identifier="TOPIX.BENCHMARK", instrument_identifier_type="other",
                    field="benchmark_price", value=price, unit="price", currency="JPY", freshness_status="current",
                    citation_location=f"prices:topix:{day}", parser_version="v1",
                )
            snapshot = calculate_snapshot(connection, as_of="2026-08-28")
            candidate = run_screens(connection, snapshot, as_of="2026-08-28")[0]
            recommendation_id = record_recommendation(
                connection, candidate, data_cutoff="2026-08-28", provider="local", template_version="v1", snapshot=snapshot,
            )
            observed_id = add_source_record(
                connection, source_name="prices", source_identifier="prices#later",
                retrieved_at="2026-09-02T00:00:00+00:00", observation_date="2026-09-01",
                instrument_identifier="1306", instrument_identifier_type="jpx_code", field="price", value="110",
                unit="price", currency="JPY", freshness_status="current", citation_location="prices:later", parser_version="v1",
            )
            benchmark_id = add_source_record(
                connection, source_name="prices", source_identifier="prices#benchmark",
                retrieved_at="2026-09-02T00:00:00+00:00", observation_date="2026-09-01",
                instrument_identifier="TOPIX.BENCHMARK", instrument_identifier_type="other", field="benchmark_price", value="11",
                unit="price", currency="JPY", freshness_status="current", citation_location="prices:benchmark", parser_version="v1",
            )
            too_late_id = add_source_record(
                connection, source_name="prices", source_identifier="prices#too-late",
                retrieved_at="2026-09-04T00:00:00+00:00", observation_date="2026-09-03",
                instrument_identifier="1306", instrument_identifier_type="jpx_code", field="price", value="115",
                unit="price", currency="JPY", freshness_status="current", citation_location="prices:too-late", parser_version="v1",
            )

            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-08-20",
                    observed_price=110, benchmark_price=11,
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-09-02",
                    observed_price=110, benchmark_price=11,
                )
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-09-02",
                    observed_price=115, benchmark_price=11,
                    observed_price_source_id=too_late_id, benchmark_price_source_id=benchmark_id,
                )
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-09-02",
                observed_price=110, benchmark_price=11,
                observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
            )
            recommendation = connection.execute("SELECT snapshot_json, snapshot_hash FROM recommendations WHERE id = ?", (recommendation_id,)).fetchone()
            outcome = connection.execute("SELECT observed_price, benchmark_price, observed_price_source_id FROM recommendation_outcomes").fetchone()
            self.assertEqual(json.loads(recommendation[0]), snapshot)
            self.assertTrue(recommendation[1])
            self.assertEqual(tuple(outcome), (110.0, 11.0, observed_id))
            connection.close()

    def test_journal_rejects_future_cutoff_snapshot_mismatch_and_source_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            from nisa_quant.watchlist import add_watchlist_item

            broker = Path(directory) / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-08-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "held",
            ]])
            import_csv(connection, broker, source_name="fixture")
            add_watchlist_item(connection, "1306", "jpx_code", "TOPIX ETF", "ETF", "JPX", "JPY", "TOPIX.BENCHMARK", "watch", effective_date="2026-08-01", observed_at="2026-08-01T00:00:00+00:00")
            for day, price in (("2026-08-26", "100"), ("2026-08-27", "101"), ("2026-08-28", "102")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"1306#{day}", retrieved_at=f"{day}T00:00:00+00:00",
                    observation_date=day, instrument_identifier="1306", instrument_identifier_type="jpx_code",
                    field="price", value=price, unit="price", currency="JPY", freshness_status="current",
                    citation_location=f"prices:{day}", parser_version="v1",
                )
            snapshot = calculate_snapshot(connection, as_of="2026-08-28")
            candidate = run_screens(connection, snapshot, as_of="2026-08-28")[0]

            with self.assertRaises(ValueError):
                record_recommendation(
                    connection, candidate, data_cutoff="2099-01-01", provider="local", template_version="v1", snapshot=snapshot,
                )
            with self.assertRaises(ValueError):
                record_recommendation(
                    connection, candidate, data_cutoff="2026-08-28", provider="local", template_version="v1",
                    snapshot={**snapshot, "as_of": "2026-08-27"},
                )
            bad_candidate = {**candidate, "source_ids": ["SRC-000000000000"]}
            with self.assertRaises(ValueError):
                record_recommendation(
                    connection, bad_candidate, data_cutoff="2026-08-28", provider="local", template_version="v1", snapshot=snapshot,
                )
            connection.close()

    def test_outcome_rejects_mismatched_currency_benchmark_and_missing_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            from nisa_quant.watchlist import add_watchlist_item

            broker = Path(directory) / "broker.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-08-01", "NISA", "VT", "other", "Global ETF", "BUY", "1", "100", "0", "USD", "", "held",
            ]])
            import_csv(connection, broker, source_name="fixture")
            add_watchlist_item(connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI.BENCHMARK", "watch", effective_date="2026-08-01", observed_at="2026-08-01T00:00:00+00:00", benchmark_identifier_type="other")
            for day, price in (("2026-08-26", "100"), ("2026-08-27", "101"), ("2026-08-28", "102")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"vt#{day}", retrieved_at=f"{day}T00:00:00+00:00",
                    observation_date=day, instrument_identifier="VT", instrument_identifier_type="other",
                    field="price", value=price, unit="price", currency="USD", freshness_status="current",
                    citation_location=f"prices:{day}", parser_version="v1",
                )
            benchmark_id = add_source_record(
                connection, source_name="prices", source_identifier="msci#later", retrieved_at="2026-09-02T00:00:00+00:00",
                observation_date="2026-09-01", instrument_identifier="MSCI.BENCHMARK", instrument_identifier_type="other",
                field="benchmark_price", value="110", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:benchmark", parser_version="v1",
            )
            later_id = add_source_record(
                connection, source_name="prices", source_identifier="vt#later", retrieved_at="2026-09-02T00:00:00+00:00",
                observation_date="2026-09-01", instrument_identifier="VT", instrument_identifier_type="other",
                field="price", value="110", unit="price", currency="USD", freshness_status="current",
                citation_location="prices:later", parser_version="v1",
            )
            wrong_type_id = add_source_record(
                connection, source_name="prices", source_identifier="msci#wrong-type", retrieved_at="2026-09-02T00:00:00+00:00",
                observation_date="2026-09-01", instrument_identifier="MSCI.BENCHMARK", instrument_identifier_type="jpx_code",
                field="benchmark_price", value="110", unit="price", currency="USD", freshness_status="current",
                citation_location="prices:wrong-type", parser_version="v1",
            )
            snapshot = calculate_snapshot(connection, as_of="2026-08-28")
            candidate = run_screens(connection, snapshot, as_of="2026-08-28")[0]
            recommendation_id = record_recommendation(
                connection, candidate, data_cutoff="2026-08-28", provider="local", template_version="v1", snapshot=snapshot,
            )
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-09-02", observed_price_source_id=later_id,
                    benchmark_price_source_id=benchmark_id,
                )
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-09-02", observed_price_source_id=later_id,
                    benchmark_price_source_id=wrong_type_id,
                )
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-09-30", observed_price_source_id=later_id,
                    benchmark_price_source_id=benchmark_id,
                )
            connection.close()

class FinalReviewChronologyTests(unittest.TestCase):
    def _recommendation_fixture(self, directory: str) -> tuple[sqlite3.Connection, int, str, str]:
        from nisa_quant.watchlist import add_watchlist_item

        connection = new_connection(directory)
        add_watchlist_item(
            connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI.BENCHMARK", "watch",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
        )
        for identifier, field, value in (("VT", "price", "100"), ("MSCI.BENCHMARK", "benchmark_price", "10")):
            add_source_record(
                connection, source_name="prices", source_identifier=f"{identifier}#cutoff",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier=identifier, instrument_identifier_type="other", field=field,
                value=value, unit="price", currency="USD", freshness_status="current",
                citation_location=f"prices:{identifier}:cutoff", parser_version="v1",
            )
        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
        recommendation_id = record_recommendation(
            connection, candidate, data_cutoff="2026-01-01", provider="local", template_version="v1", snapshot=snapshot,
        )
        observed_id = add_source_record(
            connection, source_name="prices", source_identifier="VT#later", retrieved_at="2026-01-02T00:00:00+00:00",
            observation_date="2026-01-02", instrument_identifier="VT", instrument_identifier_type="other", field="price",
            value="110", unit="price", currency="USD", freshness_status="current", citation_location="prices:VT:later", parser_version="v1",
        )
        benchmark_id = add_source_record(
            connection, source_name="prices", source_identifier="MSCI#later", retrieved_at="2026-01-02T00:00:00+00:00",
            observation_date="2026-01-02", instrument_identifier="MSCI.BENCHMARK", instrument_identifier_type="other", field="benchmark_price",
            value="11", unit="price", currency="USD", freshness_status="current", citation_location="prices:MSCI:later", parser_version="v1",
        )
        connection.commit()
        return connection, recommendation_id, observed_id, benchmark_id

    def test_recommendation_outcome_rejects_future_and_chronologically_invalid_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection, recommendation_id, observed_id, benchmark_id = self._recommendation_fixture(directory)
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-01",
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )

            connection.execute("UPDATE source_records SET retrieved_at = '2025-12-31T23:00:00+00:00' WHERE id = ?", (observed_id,))
            connection.commit()
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-02",
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )

            connection.execute("UPDATE source_records SET retrieved_at = '2026-01-04T00:00:00+00:00' WHERE id = ?", (observed_id,))
            connection.commit()
            with self.assertRaises(ValueError):
                evaluate_recommendation(
                    connection, recommendation_id, evaluation_date="2026-01-03",
                    observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                )

            connection.execute("UPDATE source_records SET retrieved_at = '2026-01-02T00:00:00+00:00' WHERE id = ?", (observed_id,))
            connection.commit()
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price=110.0, benchmark_price=11.0,
                observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
            )
            connection.close()


class FifthFinalReleaseRepairTests(unittest.TestCase):
    def test_future_watchlist_metadata_never_leaks_into_historical_holding(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "future-metadata.csv"
            prices = root / "future-metadata-prices.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-06-01", "NISA", "VT", "other", "Broker name", "BUY", "1", "100", "0", "JPY", "", "held",
            ]])
            write_csv(prices, PRICE_COLUMNS, [[
                "VT", "other", "2026-06-02", "110", "JPY", "2026-06-02T00:00:00+00:00", "current", "price:2026", "no",
            ]])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="broker")
            import_price_fixture(connection, prices, source_name="prices")
            add_watchlist_item(
                connection, "VT", "other", "Future 2027 Name", "ETF", "FUTURE", "USD", "FUTURE.BENCHMARK", "future",
                effective_date="2027-01-01", observed_at="2027-01-01T00:00:00+00:00",
                benchmark_identifier_type="other",
            )

            holding = calculate_snapshot(connection, as_of="2026-12-31")["portfolio"]["holdings"][0]

            self.assertNotEqual(holding["display_name"], "Future 2027 Name")
            self.assertNotEqual(holding["asset_type"], "ETF")
            self.assertNotEqual(holding["market"], "FUTURE")
            self.assertNotEqual(holding["currency"], "USD")
            self.assertEqual(holding["benchmark"], "UNDECLARED")
            self.assertNotIn("PRICE_CURRENCY_MISMATCH", {warning["code"] for warning in calculate_snapshot(connection, as_of="2026-12-31")["warnings"]})
            connection.close()

    def test_source_facts_require_metadata_and_recognized_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            common = dict(
                connection=connection, source_name="prices", source_identifier="test",
                retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
                instrument_identifier="VT", instrument_identifier_type="other", field="price", value="100",
                unit="price", currency="USD", freshness_status="current", citation_location="test:1", parser_version="v1",
            )
            with self.assertRaises(ValueError):
                add_source_record(**{**common, "unit": ""})
            with self.assertRaises(ValueError):
                add_source_record(**{**common, "currency": ""})
            with self.assertRaises(ValueError):
                add_source_record(**{**common, "freshness_status": "fresh-ish"})
            with self.assertRaises(ValueError):
                add_source_record(**{**common, "parser_version": "unknown"})
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
            connection.close()

    def test_stale_distribution_and_benchmark_do_not_produce_usable_metrics(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            add_source_record(
                connection, source_name="prices", source_identifier="vt-price", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="VT", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="USD", freshness_status="current", citation_location="price", parser_version="v1",
            )
            add_source_record(
                connection, source_name="prices", source_identifier="msci-stale", retrieved_at="2026-01-03T00:00:00+00:00",
                observation_date="2026-01-03", instrument_identifier="MSCI", instrument_identifier_type="other", field="benchmark_price",
                value="10", unit="price", currency="USD", freshness_status="stale", citation_location="benchmark", parser_version="v1",
            )
            add_source_record(
                connection, source_name="distributions", source_identifier="vt-dist-stale", retrieved_at="2026-01-04T00:00:00+00:00",
                observation_date="2026-01-04", instrument_identifier="VT", instrument_identifier_type="other", field="distribution",
                value="1", unit="per_unit", currency="USD", freshness_status="stale", citation_location="distribution", parser_version="v1",
            )

            item = calculate_snapshot(connection, as_of="2026-01-05")["watchlist"][0]

            self.assertIsNone(item["benchmark_price"])
            self.assertIsNone(item["benchmark_return_pct"])
            self.assertIsNone(item["distribution_amount"])
            self.assertIsNone(item["distribution_yield_pct"])
            warning_codes = {warning["code"] for warning in calculate_snapshot(connection, as_of="2026-01-05")["warnings"]}
            self.assertIn("STALE_BENCHMARK", warning_codes)
            self.assertIn("STALE_DISTRIBUTION", warning_codes)
            self.assertIsNone(calculate_snapshot(connection, as_of="2026-03-01")["watchlist"][0]["latest_price"])
            connection.close()

    def test_reverse_ordered_buy_and_sell_are_applied_chronologically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reverse.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "SELL", "5", "200", "0", "JPY", "", "later sell"],
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "10", "100", "0", "JPY", "", "earlier buy"],
            ])
            connection = new_connection(directory)

            result = import_csv(connection, path, source_name="reverse")
            position = connection.execute("SELECT quantity, cost_basis FROM positions").fetchone()
            snapshot = calculate_snapshot(connection, as_of="2026-12-31")

            self.assertEqual(result.accepted_rows, 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_quarantine").fetchone()[0], 0)
            self.assertEqual(tuple(position), (5.0, 500.0))
            self.assertEqual(snapshot["portfolio"]["realized_pl"], 500.0)
            connection.close()

    def test_structured_report_rejects_uncited_facts_and_extra_candidate_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "report.csv"
            prices = root / "report-prices.csv"
            write_csv(broker, BROKER_COLUMNS, [[
                "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "held",
            ]])
            write_csv(prices, PRICE_COLUMNS, [[
                "1306", "jpx_code", "2026-01-02", "110", "JPY", "2026-01-02T00:00:00+00:00", "current", "price:1", "no",
            ]])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="broker")
            import_price_fixture(connection, prices, source_name="prices")
            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            candidates = run_screens(connection, snapshot, as_of="2026-01-03")
            report = render_report(snapshot, candidates, provider="local", template_version="v1")

            injected = report.replace("## Source list", "X distribution amount is 999 and investors should accumulate.\n\n## Source list")
            with self.assertRaises(ValueError):
                validate_report(
                    injected, snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            with self.assertRaises(ValueError):
                validate_report(
                    report.replace("## Source list", "This section is explanatory and makes no additional factual claim.\n\n## Source list"),
                    snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            extra_row = report.replace(
                "## Source list",
                "| EXTRA | watchlist | WATCH | limited | current_price=1 | unavailable |\n\n## Source list",
            )
            with self.assertRaises(ValueError):
                validate_report(
                    extra_row, snapshot=snapshot, candidates=candidates,
                    provider="local", template_version="v1",
                )
            connection.close()

    def test_undated_warning_is_excluded_from_historical_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            connection.execute(
                "INSERT INTO data_warnings(warning_code, message, created_at, observation_date) VALUES (?, ?, ?, ?)",
                ("UNDATED", "current-only warning", "2026-01-01T00:00:00+00:00", None),
            )
            connection.execute(
                "INSERT INTO data_warnings(warning_code, message, created_at, observation_date) VALUES (?, ?, ?, ?)",
                ("DATED", "historical warning", "2026-01-01T00:00:00+00:00", "2026-01-01"),
            )
            connection.commit()

            warnings = {warning["code"] for warning in calculate_snapshot(connection, as_of="2026-01-02")["warnings"]}

            self.assertNotIn("UNDATED", warnings)
            self.assertIn("DATED", warnings)
            connection.close()

    def test_currency_breakdowns_preserve_cost_basis_and_realized_pl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "currency-accounting.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "JPY1", "other", "JPY asset", "BUY", "10", "100", "0", "JPY", "", "buy jpy"],
                ["2026-01-02", "NISA", "JPY1", "other", "JPY asset", "SELL", "5", "120", "0", "JPY", "", "sell jpy"],
                ["2026-01-01", "NISA", "USD1", "other", "USD asset", "BUY", "10", "200", "0", "USD", "", "buy usd"],
                ["2026-01-02", "NISA", "USD1", "other", "USD asset", "SELL", "5", "250", "0", "USD", "", "sell usd"],
            ])
            connection = new_connection(directory)
            import_csv(connection, path, source_name="currency")

            portfolio = calculate_snapshot(connection, as_of="2026-12-31")["portfolio"]

            self.assertEqual(portfolio["cost_basis_by_currency"], {"JPY": 500.0, "USD": 1000.0})
            self.assertEqual(portfolio["realized_pl_by_currency"], {"JPY": 100.0, "USD": 250.0})
            self.assertIsNone(portfolio["cost_basis"])
            self.assertIsNone(portfolio["realized_pl"])
            connection.close()

    def test_same_typed_instrument_mixed_transaction_currencies_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            broker = root / "mixed-instrument-currencies.csv"
            write_csv(broker, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "10", "100", "0", "JPY", "", "buy jpy"],
                ["2026-01-02", "NISA", "1306", "jpx_code", "TOPIX ETF", "SELL", "5", "120", "0", "JPY", "", "sell jpy"],
                ["2026-01-03", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "10", "200", "0", "USD", "", "buy usd"],
                ["2026-01-04", "NISA", "1306", "jpx_code", "TOPIX ETF", "SELL", "5", "250", "0", "USD", "", "sell usd"],
            ])
            connection = new_connection(directory)
            import_csv(connection, broker, source_name="mixed-currency")
            backdate_sources(connection, "2026-01-05T00:00:00+00:00")
            add_source_record(
                connection, source_name="prices", source_identifier="1306#2026-01-05",
                retrieved_at="2026-01-05T00:00:00+00:00", observation_date="2026-01-05",
                instrument_identifier="1306", instrument_identifier_type="jpx_code", field="price",
                value="110", unit="price", currency="JPY", freshness_status="current",
                citation_location="prices:1306", parser_version="v1",
            )
            connection.commit()

            snapshot = calculate_snapshot(connection, as_of="2026-01-05")
            portfolio = snapshot["portfolio"]

            self.assertEqual(portfolio["cost_basis_by_currency"], {"JPY": 500.0, "USD": 1000.0})
            self.assertEqual(portfolio["realized_pl_by_currency"], {"JPY": 100.0, "USD": 250.0})
            self.assertIsNone(portfolio["cost_basis"])
            self.assertIsNone(portfolio["realized_pl"])
            self.assertIsNone(portfolio["holdings"][0]["cost_basis"])
            self.assertIsNone(portfolio["market_value"])
            self.assertIsNone(portfolio["unrealized_pl"])
            self.assertEqual(snapshot["currency_context"]["currencies"], ["JPY", "USD"])
            self.assertIn("MIXED_CURRENCY_NO_FX", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()

    def test_outcome_date_uses_only_cited_price_and_benchmark_records(self) -> None:
        from nisa_quant.watchlist import add_watchlist_item

        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "MSCI", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            for identifier, field, value in (("VT", "price", "100"), ("MSCI", "benchmark_price", "10")):
                add_source_record(
                    connection, source_name="prices", source_identifier=f"{identifier}-cutoff",
                    retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                    instrument_identifier=identifier, instrument_identifier_type="other", field=field, value=value,
                    unit="price", currency="USD", freshness_status="current", citation_location="cutoff", parser_version="v1",
                )
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
            recommendation_id = record_recommendation(
                connection, candidate, data_cutoff="2026-01-01", provider="local", template_version="v1", snapshot=snapshot,
            )
            price_id = add_source_record(
                connection, source_name="prices", source_identifier="vt-jan2", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="VT", instrument_identifier_type="other", field="price",
                value="110", unit="price", currency="USD", freshness_status="current", citation_location="jan2", parser_version="v1",
            )
            benchmark_id = add_source_record(
                connection, source_name="prices", source_identifier="msci-jan2", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-02", instrument_identifier="MSCI", instrument_identifier_type="other", field="benchmark_price",
                value="11", unit="price", currency="USD", freshness_status="current", citation_location="jan2", parser_version="v1",
            )
            add_source_record(
                connection, source_name="distributions", source_identifier="unrelated-jan10", retrieved_at="2026-01-10T00:00:00+00:00",
                observation_date="2026-01-10", instrument_identifier="VT", instrument_identifier_type="other", field="distribution",
                value="1", unit="per_unit", currency="USD", freshness_status="current", citation_location="jan10", parser_version="v1",
            )

            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02", observed_price=110.0, benchmark_price=11.0,
                observed_price_source_id=price_id, benchmark_price_source_id=benchmark_id,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0],
                1,
            )
            connection.close()


if __name__ == "__main__":
    unittest.main()
