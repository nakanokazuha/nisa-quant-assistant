import csv
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.imports import import_csv
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import render_report, validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item, watchlist_as_of


ROOT = Path(__file__).parent.parent
BROKER_COLUMNS = (
    "取引日", "口座区分", "銘柄コード", "銘柄コード種別", "銘柄名", "取引区分",
    "数量", "単価", "手数料", "通貨", "分配金", "備考",
)
MARKER = "Historical research / deferred — not active implementation instructions"
RESEARCH_ARTIFACTS = (
    "NISA_AI_INTEGRATION_REPORT.md",
    "NISA_QUANT_ASSISTANT_PLAN.md",
    "NISA_QUANT_ASSISTANT_SPEC.md",
    "deleg3-broker-workflows.md",
    "deleg3-full-tail.md",
    "deleg3-sources.txt",
    "subagent1-frameworks-data.md",
    "subagent2-signals-mcp.md",
    "subagent3-brokers-jquants.md",
    "verified-repos.md",
)


def write_csv(path: Path, rows: list[list[str]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(BROKER_COLUMNS)
        writer.writerows(rows)


def new_connection(directory: str) -> sqlite3.Connection:
    connection = connect_database(Path(directory) / "ledger.sqlite")
    initialize_database(connection)
    return connection


class StructuredReportRepairTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = new_connection(self.tempdir.name)
        broker = Path(self.tempdir.name) / "broker.csv"
        write_csv(broker, [[
            "2026-01-01", "NISA", "1306", "jpx_code", "TOPIX ETF", "BUY", "1", "100", "0", "JPY", "", "held",
        ]])
        import_csv(self.connection, broker, source_name="broker")
        add_source_record(
            self.connection, source_name="prices", source_identifier="price-1",
            retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
            instrument_identifier="1306", instrument_identifier_type="jpx_code", field="price",
            value="110", unit="price", currency="JPY", freshness_status="current",
            citation_location="prices:1", parser_version="v1",
        )
        self.connection.execute(
            "UPDATE source_records SET retrieved_at = ? WHERE field = 'buy'",
            ("2026-01-02T00:00:00+00:00",),
        )
        self.connection.commit()
        self.snapshot = calculate_snapshot(self.connection, as_of="2026-01-03")
        self.candidates = []
        self.report = render_report(self.snapshot, self.candidates, provider="local", template_version="v1")

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_structured_validation_rejects_every_global_unbound_injection(self) -> None:
        injections = {
            "header prose": self.report.replace(
                "# NISA Quant Assistant Report\n",
                "# NISA Quant Assistant Report\nInvented portfolio commentary.\n",
                1,
            ),
            "provider alteration": self.report.replace(
                "Provider/model identifier: `local`; template: `v1`.",
                "Provider/model identifier: `invented`; template: `v1`.",
                1,
            ),
            "cutoff duplicate": self.report.replace(
                "- portfolio: `2026-01-03`",
                "- portfolio: `2026-01-03`\n- portfolio: `2099-01-01`",
                1,
            ),
            "warning prose": self.report.replace(
                "## Data warnings\n\n",
                "## Data warnings\n\n- Invented warning: 999 JPY.\n\n",
                1,
            ),
            "aggregate duplicate": self.report.replace(
                "- Market value: `110.00`",
                "- Market value: `999.00`\n- Market value: `110.00`",
                1,
            ),
            "financial fact": self.report.replace(
                "## Ranked candidates\n",
                "## Ranked candidates\nUnbound earnings are 999.\n",
                1,
            ),
            "unsupported action": self.report.replace(
                "Manual review required; no order was placed.",
                "BUY NOW; no order was placed.",
                1,
            ),
            "source citation": self.report.replace(
                "## Source list\n",
                "## Source list\n- [SRC-000000000000] — invented citation\n",
                1,
            ),
            "source duplicate": self.report.replace(
                "## Source list\n",
                "## Source list\n" + self.report.split("## Source list\n", 1)[1].split("\n\nManual", 1)[0] + "\n",
                1,
            ),
            "footer prose": self.report + "Additional portfolio total: 999 JPY.\n",
            "account prose": self.report.replace(
                "# NISA Quant Assistant Report\n",
                "# NISA Quant Assistant Report\naccount identifier: personal-account\n",
                1,
            ),
            "secret-shaped value": self.report.replace(
                "# NISA Quant Assistant Report\n",
                "# NISA Quant Assistant Report\napi_key: sk-test-abcdefghijklmnopqrstuvwxyz123456\n",
                1,
            ),
            "PII filename": self.report.replace(
                "## Source list\n",
                "## Source list\n- taro-yamada-portfolio.csv\n- account-summary.csv\n",
                1,
            ),
        }
        for name, altered in injections.items():
            with self.subTest(injection=name):
                with self.assertRaises(ValueError):
                    validate_report(
                        altered, snapshot=self.snapshot, candidates=self.candidates,
                        provider="local", template_version="v1",
                    )

    def test_structured_validation_accepts_canonical_renderer_output(self) -> None:
        validate_report(
            self.report, snapshot=self.snapshot, candidates=self.candidates,
            provider="local", template_version="v1",
        )


class CurrencyRepairTests(unittest.TestCase):
    def test_cross_currency_sell_is_quarantined_without_false_realized_pl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "cross-currency.csv"
            write_csv(path, [
                ["2026-01-01", "NISA", "ASSET", "other", "Asset", "BUY", "1", "100", "0", "USD", "", "buy"],
                ["2026-01-02", "NISA", "ASSET", "other", "Asset", "SELL", "1", "120", "0", "JPY", "", "wrong currency"],
            ])

            result = import_csv(connection, path, source_name="broker")
            snapshot = calculate_snapshot(connection, as_of="2026-12-31")

            self.assertEqual(result.accepted_rows, 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM review_quarantine").fetchone()[0], 1)
            self.assertEqual(snapshot["portfolio"]["realized_pl_by_currency"], {})
            self.assertEqual(snapshot["portfolio"]["cost_basis_by_currency"], {"USD": 100.0})
            self.assertIn("MIXED_CURRENCY_NO_FX", {warning["code"] for warning in snapshot["warnings"]})
            connection.close()

    def test_mixed_overall_currency_makes_scalar_cash_and_distribution_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "mixed-totals.csv"
            write_csv(path, [
                ["2026-01-01", "NISA", "JPY-ASSET", "other", "JPY asset", "BUY", "1", "100", "0", "JPY", "", "asset"],
                ["2026-01-02", "NISA", "", "", "", "CASH", "", "50", "0", "USD", "", "cash"],
                ["2026-01-03", "NISA", "JPY-ASSET", "other", "JPY asset", "DISTRIBUTION", "1", "0", "0", "JPY", "5", "distribution"],
            ])

            import_csv(connection, path, source_name="broker")
            portfolio = calculate_snapshot(connection, as_of="2026-12-31")["portfolio"]

            self.assertIsNone(portfolio["contributions"])
            self.assertIsNone(portfolio["cash_movements"])
            self.assertIsNone(portfolio["distributions"])
            self.assertIsNone(portfolio["realized_pl"])
            self.assertEqual(portfolio["contributions_by_currency"], {"USD": 50.0})
            self.assertEqual(portfolio["cash_movements_by_currency"], {"USD": 50.0})
            self.assertEqual(portfolio["distributions_by_currency"], {"JPY": 5.0})
            connection.close()

    def test_same_currency_sell_remains_reconciled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            path = Path(directory) / "same-currency.csv"
            write_csv(path, [
                ["2026-01-01", "NISA", "ASSET", "other", "Asset", "BUY", "2", "100", "0", "USD", "", "buy"],
                ["2026-01-02", "NISA", "ASSET", "other", "Asset", "SELL", "1", "120", "0", "USD", "", "sell"],
            ])

            import_csv(connection, path, source_name="broker")
            portfolio = calculate_snapshot(connection, as_of="2026-12-31")["portfolio"]

            self.assertEqual(portfolio["realized_pl_by_currency"], {"USD": 20.0})
            self.assertEqual(portfolio["cost_basis_by_currency"], {"USD": 100.0})
            connection.close()


class WatchlistAndSourceRepairTests(unittest.TestCase):
    def test_watchlist_observed_at_normalizes_to_utc_for_cutoff_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "OFFSET", "other", "Offset item", "ETF", "NYSE", "USD", None, "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T23:30:00-12:00",
            )

            self.assertEqual(watchlist_as_of(connection, as_of="2026-01-01"), [])
            self.assertEqual(
                [row["identifier_value"] for row in watchlist_as_of(connection, as_of="2026-01-02")],
                ["OFFSET"],
            )
            self.assertEqual(
                connection.execute("SELECT observed_at FROM watchlist_versions").fetchone()[0],
                "2026-01-02T11:30:00+00:00",
            )
            with self.assertRaises(ValueError):
                add_watchlist_item(
                    connection, "NAIVE", "other", "Naive item", "ETF", "NYSE", "USD", None, "watch",
                    effective_date="2026-01-01", observed_at="2026-01-01T12:00:00",
                )
            connection.close()

    def test_unusable_newer_refresh_is_excluded_from_price_benchmark_distribution_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", "BENCH", "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
            )
            values = [
                ("price", "VT", "100", "price", "USD", "price-old", "2026-01-02T00:00:00+00:00", "current"),
                ("price", "VT", "100", "price", "USD", "price-new", "2026-01-03T00:00:00+00:00", "stale"),
                ("benchmark_price", "BENCH", "10", "price", "USD", "bench-old", "2026-01-02T00:00:00+00:00", "current"),
                ("benchmark_price", "BENCH", "10", "price", "USD", "bench-new", "2026-01-03T00:00:00+00:00", "stale"),
                ("distribution", "VT", "1", "per_unit", "USD", "dist-old", "2026-01-02T00:00:00+00:00", "current"),
                ("distribution", "VT", "1", "per_unit", "USD", "dist-new", "2026-01-03T00:00:00+00:00", "stale"),
            ]
            source_ids: dict[str, str] = {}
            for field, identifier, value, unit, currency, source_key, retrieved_at, freshness in values:
                source_ids[source_key] = add_source_record(
                    connection, source_name="local", source_identifier=source_key, retrieved_at=retrieved_at,
                    observation_date="2026-01-01", instrument_identifier=identifier,
                    instrument_identifier_type="other", field=field, value=value, unit=unit,
                    currency=currency, freshness_status=freshness, citation_location=source_key,
                    parser_version="v1",
                )

            item = calculate_snapshot(connection, as_of="2026-01-04")["watchlist"][0]

            self.assertEqual(item["price_status"], "current")
            self.assertEqual(item["price_history"], [{"date": "2026-01-01", "price": 100.0, "source_id": source_ids["price-old"]}])
            self.assertNotIn(source_ids["price-new"], item["source_ids"])
            self.assertEqual(item["benchmark_source_ids"], [source_ids["bench-old"]])
            self.assertEqual(item["distribution_source_ids"], [source_ids["dist-old"]])
            self.assertEqual(item["distribution_history"], [{"date": "2026-01-01", "amount": 1.0, "unit": "per_unit", "source_id": source_ids["dist-old"]}])
            self.assertIn(source_ids["price-new"], {source["id"] for source in calculate_snapshot(connection, as_of="2026-01-04")["sources"]})
            connection.close()

    def test_invalid_metadata_newer_refresh_is_excluded_from_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            connection = new_connection(directory)
            add_watchlist_item(
                connection, "VT", "other", "Global ETF", "ETF", "NYSE", "USD", None, "watch",
                effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
            )
            old_id = add_source_record(
                connection, source_name="local", source_identifier="old", retrieved_at="2026-01-02T00:00:00+00:00",
                observation_date="2026-01-01", instrument_identifier="VT", instrument_identifier_type="other",
                field="price", value="100", unit="price", currency="USD", freshness_status="current",
                citation_location="old", parser_version="v1",
            )
            invalid_id = add_source_record(
                connection, source_name="local", source_identifier="new", retrieved_at="2026-01-03T00:00:00+00:00",
                observation_date="2026-01-01", instrument_identifier="VT", instrument_identifier_type="other",
                field="price", value="999", unit="price", currency="USD", freshness_status="current",
                citation_location="new", parser_version="v1",
            )
            connection.execute("UPDATE source_records SET unit = NULL WHERE id = ?", (invalid_id,))
            connection.commit()

            item = calculate_snapshot(connection, as_of="2026-01-04")["watchlist"][0]

            self.assertEqual(item["price_history"][0]["source_id"], old_id)
            self.assertNotIn(invalid_id, item["source_ids"])
            connection.close()


class ResearchArtifactBoundaryTests(unittest.TestCase):
    def test_every_checked_in_research_artifact_has_explicit_deferred_marker(self) -> None:
        for name in RESEARCH_ARTIFACTS:
            with self.subTest(name=name):
                self.assertIn(MARKER, (ROOT / name).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
