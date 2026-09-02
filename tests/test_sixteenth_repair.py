import sqlite3
import sys
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import nisa_quant.recommendation_journal as journal
from nisa_quant.recommendation_journal import record_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import render_report, validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.candidate_screening import run_screens
from nisa_quant.source_records import import_price_fixture
from nisa_quant.broker_csv_import import import_csv


SOURCE_ID = "SRC-abcdef123456"
FIXTURES = Path(__file__).parent / "fixtures"


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


class SixteenthRepairReportSafetyTests(unittest.TestCase):
    def test_adversarial_identifier_and_credential_obfuscations_are_rejected(self) -> None:
        forbidden = (
            "a.c.c.o.u.n.t I.D.: ABC123",
            "a/c/c/o/u/n/t / I.D.: ABC123",
            "c.u.s.t.o.m.e.r n.o.: CLIENTABC",
            "b.r.o.k.e.r r.e.f: BROKER123",
            "口座番号: ABC",
            "password hunter2",
            "AWS_SECRET_ACCESS_KEY hunter2",
            "A W S _ S E C R E T _ A C C E S S _ K E Y hunter2",
            "Authorization: Basic YWJj",
            "a\u200bc\u200bc\u200bo\u200bu\u200bn\u200bt I\u200bD ABC123",
            "broker ref BROKER123",
            "customer no CLIENTABC",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_safe_filename_and_explanatory_token_text_remain_allowed(self) -> None:
        validate_report(
            minimal_report("price-history.csv is safe; token is explanatory text"),
            source_records=[SOURCE_ID],
        )

    def test_invisible_source_metadata_is_rejected_before_binding(self) -> None:
        with self.assertRaises(ValueError):
            validate_report(
                minimal_report("stable evidence"),
                source_records=[{"id": SOURCE_ID, "source_name": "local\u200bsource"}],
            )


class SixteenthRepairStructuredReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect_database(":memory:")
        initialize_database(self.connection)
        with patch("nisa_quant.broker_csv_import.utc_now", return_value="2026-08-28T00:00:00+00:00"):
            import_csv(self.connection, FIXTURES / "synthetic_broker.csv", source_name="synthetic-broker")
        import_price_fixture(
            self.connection,
            FIXTURES / "synthetic_prices.csv",
            source_name="synthetic-prices",
        )
        self.as_of = "2026-08-30"
        self.snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        self.candidates = run_screens(self.connection, self.snapshot, as_of=self.as_of)
        self.report = render_report(
            self.snapshot,
            self.candidates,
            provider="local-deterministic",
            template_version="report-v1",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def validate(self, report: str) -> None:
        validate_report(
            report,
            snapshot=self.snapshot,
            candidates=self.candidates,
            provider="local-deterministic",
            template_version="report-v1",
        )

    def test_exact_renderer_output_is_valid(self) -> None:
        self.validate(self.report)

    def test_structured_report_rejects_whitespace_and_invisible_mutations(self) -> None:
        mutations = (
            self.report.replace("## Ranked candidates", "## Ranked candidates ", 1),
            self.report.replace("## Ranked candidates\n", "## Ranked candidates\n   \n", 1),
            self.report + "\n",
            self.report.replace("**", "** ", 1),
            self.report.replace("synthetic-broker", "synthetic-\u200bbroker", 1),
            self.report.replace("Manual review required", "Manual review requ\u200bired", 1),
        )
        for mutated in mutations:
            with self.subTest(mutated=repr(mutated[-100:])):
                with self.assertRaises(ValueError):
                    self.validate(mutated)

    def test_marker_mutations_fail_closed_and_exact_markers_remain_bound(self) -> None:
        mutations = (
            self.report.replace("Provider/model identifier: `local-deterministic`", "Provider/model identifier: `other`", 1),
            self.report.replace("Provider/model identifier:", "Provider/model identifier :", 1),
            self.report.replace("Report contract fingerprint:", "Report contract finger print:", 1),
            self.report + "Report generated from local snapshot as of `2026-08-30`.\n",
        )
        for mutated in mutations:
            with self.subTest(mutated=repr(mutated[-120:])):
                with self.assertRaises(ValueError):
                    self.validate(mutated)


class SixteenthRepairJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect_database(":memory:")
        initialize_database(self.connection)
        with patch("nisa_quant.broker_csv_import.utc_now", return_value="2026-08-28T00:00:00+00:00"):
            import_csv(self.connection, FIXTURES / "synthetic_broker.csv", source_name="synthetic-broker")
        self.connection.execute(
            "UPDATE instruments SET benchmark = 'TOPIX.BENCHMARK', benchmark_identifier_type = 'other', "
            "benchmark_identifier_value = 'TOPIX.BENCHMARK' WHERE identifier_value = '1306'"
        )
        self.connection.commit()
        import_price_fixture(
            self.connection,
            FIXTURES / "synthetic_prices.csv",
            source_name="synthetic-prices",
        )
        self.as_of = "2026-08-30"
        self.snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        self.candidate = run_screens(self.connection, self.snapshot, as_of=self.as_of)[0]

    def tearDown(self) -> None:
        self.connection.close()

    def test_recommendation_future_cutoff_uses_utc_calendar_date(self) -> None:
        real_date = date
        real_datetime = datetime

        class LocalCalendarDate(date):
            @classmethod
            def today(cls) -> date:
                return real_date(2026, 8, 29)

            @classmethod
            def fromisoformat(cls, value: str) -> date:
                return real_date.fromisoformat(value)

        class UtcBoundaryClock:
            @classmethod
            def now(cls, tz: object = None) -> datetime:
                return real_datetime(2026, 8, 30, 0, 30, tzinfo=timezone.utc)

        with patch.object(journal, "date", LocalCalendarDate), patch.object(journal, "datetime", UtcBoundaryClock):
            record_recommendation(
                self.connection,
                self.candidate,
                data_cutoff="2026-08-30",
                provider="local",
                template_version="v1",
                snapshot=self.snapshot,
            )


class SixteenthRepairWarningTests(unittest.TestCase):
    def test_malformed_persisted_warning_date_is_not_projected_or_used(self) -> None:
        connection = connect_database(":memory:")
        initialize_database(connection)
        self.addCleanup(connection.close)
        connection.execute(
            "INSERT INTO data_warnings(import_id, warning_code, message, row_number, created_at, observation_date) "
            "VALUES (NULL, 'MALFORMED_DATE', 'must not publish', 1, ?, '0000')",
            ("2026-01-02T00:00:00+00:00",),
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-03")

        self.assertNotIn("MALFORMED_DATE", {warning["code"] for warning in snapshot["warnings"]})
        self.assertNotIn("0000", {warning.get("observation_date") for warning in snapshot["warnings"]})


if __name__ == "__main__":
    unittest.main()
