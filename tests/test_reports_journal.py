import tempfile
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.imports import import_csv
from nisa_quant.journal import evaluate_recommendation, record_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import render_report, validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import import_price_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_broker.csv"


class ReportsAndJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.tempdir.name) / "portfolio.sqlite")
        initialize_database(self.connection)
        import_csv(self.connection, FIXTURE, source_name="synthetic-broker")
        import_price_fixture(
            self.connection,
            Path(__file__).parent / "fixtures" / "synthetic_prices.csv",
            source_name="synthetic-prices",
        )
        self.snapshot = calculate_snapshot(self.connection, as_of="2026-08-28")
        self.candidates = run_screens(self.connection, self.snapshot, as_of="2026-08-28")

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_report_contains_citations_cutoffs_warnings_and_no_order_statement(self) -> None:
        report = render_report(
            self.snapshot,
            self.candidates,
            provider="local-deterministic",
            template_version="report-v1",
        )
        validate_report(report)
        self.assertIn("Data cutoffs", report)
        self.assertIn("Source list", report)
        self.assertIn("Data warnings", report)
        self.assertIn("Manual review required; no order was placed", report)
        self.assertIn("[SRC-", report)

    def test_journal_preserves_original_and_appends_benchmark_outcome(self) -> None:
        recommendation_id = record_recommendation(
            self.connection,
            self.candidates[0],
            data_cutoff="2026-08-28",
            provider="local-deterministic",
            template_version="report-v1",
        )
        evaluate_recommendation(
            self.connection,
            recommendation_id,
            evaluation_date="2026-09-30",
            observed_price=1100.0,
            benchmark_price=105.0,
        )
        original = self.connection.execute(
            "SELECT label, data_cutoff, provider FROM recommendations WHERE id = ?",
            (recommendation_id,),
        ).fetchone()
        outcome = self.connection.execute(
            "SELECT benchmark_return, observed_return FROM recommendation_outcomes WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        self.assertEqual(tuple(original), (self.candidates[0]["label"], "2026-08-28", "local-deterministic"))
        self.assertIsNotNone(outcome[0])
        self.assertIsNotNone(outcome[1])


if __name__ == "__main__":
    unittest.main()
