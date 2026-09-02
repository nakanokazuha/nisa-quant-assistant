import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.broker_csv_import import import_csv
from nisa_quant.recommendation_journal import evaluate_recommendation, record_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.report_rendering import render_report, validate_report
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.candidate_screening import run_screens
from nisa_quant.source_records import import_price_fixture


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_broker.csv"


class ReportsAndJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.connection = connect_database(Path(self.tempdir.name) / "portfolio.sqlite")
        initialize_database(self.connection)
        # The fixture is historical; freeze importer provenance before the
        # historical cutoff so this test does not depend on the wall clock.
        with patch("nisa_quant.broker_csv_import.utc_now", return_value="2026-08-28T00:00:00+00:00"):
            import_csv(self.connection, FIXTURE, source_name="synthetic-broker")
        self.connection.execute("UPDATE instruments SET benchmark = 'TOPIX.BENCHMARK', benchmark_identifier_type = 'other', benchmark_identifier_value = 'TOPIX.BENCHMARK' WHERE identifier_value = '1306'")
        self.connection.commit()
        import_price_fixture(
            self.connection,
            Path(__file__).parent / "fixtures" / "synthetic_prices.csv",
            source_name="synthetic-prices",
        )
        self.as_of = "2026-08-30"
        self.snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        self.candidates = run_screens(self.connection, self.snapshot, as_of=self.as_of)

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
        validate_report(
            report, snapshot=self.snapshot, candidates=self.candidates,
            provider="local-deterministic", template_version="report-v1",
        )
        self.assertIn("Data cutoffs", report)
        self.assertIn("Source list", report)
        self.assertIn("Data warnings", report)
        self.assertIn("Manual review required; no order was placed", report)
        self.assertIn("[SRC-", report)

    def test_structured_report_rejects_unbound_prose_and_filenames(self) -> None:
        report = render_report(
            self.snapshot,
            self.candidates,
            provider="local-deterministic",
            template_version="report-v1",
        )
        adversaries = (
            "Management has secured a durable competitive moat.",
            "X distribution amount is 999 and investors should accumulate.",
            "taro-yamada-portfolio.csv",
            "account-summary.csv",
        )
        for adversary in adversaries:
            with self.subTest(adversary=adversary):
                if adversary.endswith(".csv"):
                    altered = report.replace("## Source list\n", f"## Source list\n- {adversary}\n", 1)
                else:
                    altered = report.replace("## Ranked candidates\n", f"## Ranked candidates\n{adversary}\n", 1)
                with self.assertRaises(ValueError):
                    validate_report(
                        altered, snapshot=self.snapshot, candidates=self.candidates,
                        provider="local-deterministic", template_version="report-v1",
                    )

        validate_report(
            report, snapshot=self.snapshot, candidates=self.candidates,
            provider="local-deterministic", template_version="report-v1",
        )

    def test_journal_preserves_original_and_appends_benchmark_outcome(self) -> None:
        recommendation_id = record_recommendation(
            self.connection,
            self.candidates[0],
            data_cutoff=self.as_of,
            provider="local-deterministic",
            template_version="report-v1",
        )
        evaluate_recommendation(
            self.connection,
            recommendation_id,
            evaluation_date="2026-09-02",
            observed_price=1100.0,
            benchmark_price=105.0,
            observed_price_source_id=self.connection.execute(
                "SELECT id FROM source_records WHERE field = 'price' AND observation_date = '2026-09-01'"
            ).fetchone()[0],
            benchmark_price_source_id=self.connection.execute(
                "SELECT id FROM source_records WHERE field = 'benchmark_price' AND observation_date = '2026-09-01'"
            ).fetchone()[0],
        )
        original = self.connection.execute(
            "SELECT label, data_cutoff, provider FROM recommendations WHERE id = ?",
            (recommendation_id,),
        ).fetchone()
        outcome = self.connection.execute(
            "SELECT benchmark_return, observed_return FROM recommendation_outcomes WHERE recommendation_id = ?",
            (recommendation_id,),
        ).fetchone()
        self.assertEqual(tuple(original), (self.candidates[0]["label"], self.as_of, "local-deterministic"))
        self.assertIsNotNone(outcome[0])
        self.assertIsNone(outcome[1])


if __name__ == "__main__":
    unittest.main()
