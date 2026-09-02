import csv
import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.broker_csv_import import import_csv
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import _report_contract, render_report, validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.source_records import add_source_record, import_price_fixture
from nisa_quant.watchlist import add_watchlist_item


ROOT = Path(__file__).parent.parent
BROKER_COLUMNS = (
    "取引日", "口座区分", "銘柄コード", "銘柄コード種別", "銘柄名", "取引区分",
    "数量", "単価", "手数料", "通貨", "分配金", "備考",
)
PRICE_COLUMNS = (
    "identifier", "identifier_type", "observation_date", "price", "currency",
    "retrieved_at", "freshness_status", "citation_location", "benchmark",
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


class EighthRepairReportTests(unittest.TestCase):
    def test_structured_validation_requires_and_binds_provider_template_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_source_record(
                connection, source_name="local", source_identifier="report-source",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="VT", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="report", parser_version="v1",
            )
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            report = render_report(snapshot, [], provider="local", template_version="v1")
            with self.assertRaises(ValueError):
                validate_report(report, snapshot=snapshot, candidates=[])

            canonical = render_report(snapshot, [], provider="local", template_version="v1")
            altered = canonical.replace(
                "Provider/model identifier: `local`; template: `v1`.",
                "Provider/model identifier: `attacker`; template: `v2`.",
            )
            altered = altered.replace(
                _report_contract(snapshot, [], provider="local", template_version="v1"),
                _report_contract(snapshot, [], provider="attacker", template_version="v2"),
            )
            with self.assertRaises(ValueError):
                validate_report(
                    altered, snapshot=snapshot, candidates=[],
                    provider="local", template_version="v1",
                )
            validate_report(canonical, snapshot=snapshot, candidates=[], provider="local", template_version="v1")
            connection.close()

    def test_unstructured_and_structured_safety_vocabulary_and_filenames_fail_closed(self) -> None:
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
        forbidden = (
            "PURCHASE NOW", "BUY NOW", "STRONG BUY", "STRONG SELL", "SELL NOW",
            "ACCUMULATE", "GUARANTEED RETURN", "please buy", "must sell",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(base.format(prose=phrase), source_records=["SRC-abcdef123456"])
        for filename in ("taro-yamada.csv", "account-summary.csv", "taro-yamada-portfolio.csv"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    validate_report(base.format(prose=f"See {filename}"), source_records=["SRC-abcdef123456"])
        validate_report(
            base.format(prose="The token field is explanatory and contains no credential. See price-history.csv."),
            source_records=["SRC-abcdef123456"],
        )
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_source_record(
                connection, source_name="local", source_identifier="structured-source",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier="VT", instrument_identifier_type="other", field="price",
                value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="structured", parser_version="v1",
            )
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            canonical = render_report(snapshot, [], provider="local", template_version="v1")
            for phrase in forbidden:
                with self.subTest(structured_phrase=phrase):
                    with self.assertRaises(ValueError):
                        validate_report(
                            canonical.replace("Provider/model identifier:", f"{phrase}\nProvider/model identifier:", 1),
                            snapshot=snapshot, candidates=[], provider="local", template_version="v1",
                        )
            with self.assertRaises(ValueError):
                validate_report(
                    canonical.replace("## Source list", "See taro-yamada.csv\n\n## Source list", 1),
                    snapshot=snapshot, candidates=[], provider="local", template_version="v1",
                )
            connection.close()


class EighthRepairLedgerTests(unittest.TestCase):
    def test_mixed_currency_positions_store_unavailable_scalar_and_retain_audit_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "mixed.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "ASSET", "other", "Asset", "BUY", "1", "100", "0", "USD", "", "usd"],
                ["2026-01-02", "NISA", "ASSET", "other", "Asset", "BUY", "1", "200", "0", "JPY", "", "jpy"],
            ])
            result = import_csv(connection, path, source_name="broker")
            position = connection.execute(
                "SELECT quantity, cost_basis FROM positions"
            ).fetchone()

            self.assertEqual(result.accepted_rows, 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)
            self.assertEqual(position[0], 2.0)
            self.assertIsNone(position[1])
            connection.close()

    def test_cross_currency_sell_keeps_accepted_source_audit_but_not_transaction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "cross-currency.csv"
            write_csv(path, BROKER_COLUMNS, [
                ["2026-01-01", "NISA", "ASSET", "other", "Asset", "BUY", "1", "100", "0", "USD", "", "buy"],
                ["2026-01-02", "NISA", "ASSET", "other", "Asset", "SELL", "1", "120", "0", "JPY", "", "wrong currency"],
            ])
            result = import_csv(connection, path, source_name="broker")

            self.assertEqual(result.accepted_rows, 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM transactions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT cost_basis FROM positions").fetchone()[0], 100.0)
            connection.close()


class EighthRepairCutoffTests(unittest.TestCase):
    def test_cutoffs_only_use_accepted_usable_observations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "BENCH", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            ids = {}
            for field, identifier, value, unit in (
                ("price", "VT", "100", "price"),
                ("benchmark_price", "BENCH", "10", "price"),
                ("distribution", "VT", "1", "per_unit"),
            ):
                ids[(field, "old")] = add_source_record(
                    connection, source_name="local", source_identifier=f"{field}-old",
                    retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-01",
                    instrument_identifier=identifier, instrument_identifier_type="other", field=field,
                    value=value, unit=unit, currency="USD", freshness_status="current",
                    citation_location=f"{field}:old", parser_version="v1",
                )
                ids[(field, "new")] = add_source_record(
                    connection, source_name="local", source_identifier=f"{field}-new",
                    retrieved_at="2026-01-03T00:00:00+00:00", observation_date="2026-01-02",
                    instrument_identifier=identifier, instrument_identifier_type="other", field=field,
                    value="999", unit=unit, currency="USD", freshness_status="stale",
                    citation_location=f"{field}:new", parser_version="v1",
                )
            connection.execute(
                "UPDATE source_records SET unit = NULL WHERE id = ?",
                (ids[("benchmark_price", "new")],),
            )
            connection.execute(
                "UPDATE source_records SET currency = NULL WHERE id = ?",
                (ids[("distribution", "new")],),
            )
            connection.commit()

            snapshot = calculate_snapshot(connection, as_of="2026-01-03")
            item = snapshot["watchlist"][0]

            self.assertEqual(snapshot["data_cutoffs"]["prices"], "2026-01-01")
            self.assertEqual(snapshot["data_cutoffs"]["benchmark"], "2026-01-01")
            self.assertEqual(snapshot["data_cutoffs"]["distributions"], "2026-01-01")
            self.assertEqual(item["source_ids"], [ids[("price", "old")]])
            self.assertEqual(item["benchmark_source_ids"], [ids[("benchmark_price", "old")]])
            self.assertEqual(item["distribution_source_ids"], [ids[("distribution", "old")]])
            self.assertEqual(
                {source["id"] for source in snapshot["sources"]},
                {
                    ids[("price", "old")], ids[("price", "new")],
                    ids[("benchmark_price", "old")], ids[("distribution", "old")],
                },
            )
            self.assertNotIn(ids[("benchmark_price", "new")], {source["id"] for source in snapshot["sources"]})
            self.assertNotIn(ids[("distribution", "new")], {source["id"] for source in snapshot["sources"]})
            self.assertIsNotNone(connection.execute(
                "SELECT 1 FROM source_records WHERE id = ?", (ids[("benchmark_price", "new")],)
            ).fetchone())
            connection.close()


class EighthRepairPositivityTests(unittest.TestCase):
    def test_source_records_and_metrics_reject_non_positive_price_facts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            for field in ("price", "benchmark_price"):
                for value in ("0", "-1"):
                    with self.subTest(field=field, value=value):
                        with self.assertRaises(ValueError):
                            add_source_record(
                                connection, source_name="local", source_identifier=f"{field}-{value}",
                                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                                instrument_identifier="VT", instrument_identifier_type="other", field=field,
                                value=value, unit="price", currency="USD", freshness_status="current",
                                citation_location="test", parser_version="v1",
                            )
            add_source_record(
                connection, source_name="local", source_identifier="valid", retrieved_at="2026-01-01T00:00:00+00:00",
                observation_date="2026-01-01", instrument_identifier="VT", instrument_identifier_type="other",
                field="price", value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="test", parser_version="v1",
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 1)
            connection.close()

    def test_price_fixture_rejects_zero_and_negative_prices(self) -> None:
        for value, benchmark in (("0", "no"), ("-1", "no"), ("0", "yes"), ("-1", "yes")):
            with self.subTest(value=value, benchmark=benchmark), tempfile.TemporaryDirectory() as directory:
                connection = new_connection(directory)
                path = Path(directory) / "prices.csv"
                write_csv(path, PRICE_COLUMNS, [[
                    "VT", "other", "2026-01-01", value, "USD",
                    "2026-01-01T00:00:00+00:00", "current", "prices:1", benchmark,
                ]])
                with self.assertRaises(ValueError):
                    import_price_fixture(connection, path, source_name="prices")
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0], 0)
                connection.close()

    def test_legacy_non_positive_price_and_benchmark_rows_are_unusable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "BENCH", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            for identifier, field in (("VT", "price"), ("BENCH", "benchmark_price")):
                connection.execute(
                    """INSERT INTO source_records(
                        id, source_name, source_url_or_identifier, retrieved_at, observation_date,
                        instrument_identifier, instrument_identifier_type, field, value, unit, currency,
                        freshness_status, citation_location, parser_version
                    ) VALUES (?, 'legacy', ?, '2026-01-01T00:00:00+00:00', '2026-01-01', ?, 'other', ?, ?, 'price', 'USD', 'current', 'legacy', 'v1')""",
                    (f"SRC-{hashlib.sha256(f'{identifier}{field}'.encode()).hexdigest()[:12]}", f"{identifier}-{field}", identifier, field, "0" if field == "price" else "-1"),
                )
            connection.commit()
            snapshot = calculate_snapshot(connection, as_of="2026-01-01")
            item = snapshot["watchlist"][0]

            self.assertIsNone(item["latest_price"])
            self.assertIsNone(item["benchmark_price"])
            self.assertIn("MISSING_PRICE", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()


if __name__ == "__main__":
    unittest.main()
