import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(Path(__file__).parent.parent / "src")
        return subprocess.run(
            [sys.executable, "-m", "nisa_quant", *arguments],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
        )

    def test_help_is_available_from_documented_src_layout_command(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("import-csv", result.stdout)
        self.assertIn("report", result.stdout)
        self.assertIn("watchlist-add", result.stdout)

    def test_record_recommendation_reports_empty_candidates_as_a_clear_cli_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "fresh.sqlite")
            initialized = self.run_cli("init-db", "--db", database)
            self.assertEqual(initialized.returncode, 0, initialized.stderr)

            result = self.run_cli(
                "record-recommendation", "--db", database, "--as-of", "2026-08-30",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no candidates", result.stderr.lower())

    def test_fresh_database_documented_fixture_path_records_a_candidate(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "fresh.sqlite")
            for command in (
                ("init-db", "--db", database),
                ("import-csv", "--db", database, "--csv", str(root / "fixtures" / "synthetic_broker.csv")),
                ("import-prices", "--db", database, "--csv", str(root / "fixtures" / "synthetic_prices.csv")),
            ):
                result = self.run_cli(*command)
                self.assertEqual(result.returncode, 0, result.stderr)
            result = self.run_cli(
                "record-recommendation", "--db", database, "--as-of", "2026-08-30",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.strip().isdigit())

    def test_watchlist_add_exposes_typed_benchmark_through_existing_api(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "watchlist.sqlite")
            result = self.run_cli(
                "watchlist-add", "--db", database,
                "--identifier-value", "ASSET", "--identifier-type", "other",
                "--display-name", "Synthetic Asset", "--asset-type", "ETF",
                "--market", "JPX", "--currency", "JPY", "--benchmark", "BENCH",
                "--benchmark-identifier-type", "other", "--benchmark-identifier-value", "BENCH",
                "--notes", "synthetic", "--effective-date", "2026-01-01",
                "--observed-at", "2026-01-01T00:00:00+00:00",
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("watchlist version", result.stdout.lower())

    def test_watchlist_add_rejects_an_incomplete_typed_benchmark(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_cli(
                "watchlist-add", "--db", str(Path(directory) / "watchlist.sqlite"),
                "--identifier-value", "ASSET", "--identifier-type", "other",
                "--display-name", "Synthetic Asset", "--asset-type", "ETF",
                "--market", "JPX", "--currency", "JPY", "--benchmark", "BENCH",
                "--benchmark-identifier-value", "BENCH", "--effective-date", "2026-01-01",
                "--observed-at", "2026-01-01T00:00:00+00:00",
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("benchmark identifier type and value", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
