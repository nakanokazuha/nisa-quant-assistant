import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class CliTests(unittest.TestCase):
    REFERENCE_AS_OF = "2026-09-04"
    REFERENCE_UNIVERSE = "phase2_reference_universe.csv"
    REFERENCE_MARKET = "phase2_reference_market.csv"

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
            report = str(Path(directory) / "nisa-report.md")
            commands = (
                ("init-db", "--db", database),
                ("watchlist-add", "--db", database, "--identifier-value", "1306", "--identifier-type", "jpx_code", "--display-name", "TOPIX ETF", "--asset-type", "ETF", "--market", "JPX", "--currency", "JPY", "--benchmark", "TOPIX.BENCHMARK", "--benchmark-identifier-type", "other", "--benchmark-identifier-value", "TOPIX.BENCHMARK", "--notes", "synthetic", "--effective-date", "2026-08-01", "--observed-at", "2026-08-01T00:00:00+00:00"),
                ("import-csv", "--db", database, "--csv", str(root / "fixtures" / "synthetic_broker.csv"), "--retrieved-at", "2026-08-30T00:00:00+00:00"),
                ("import-prices", "--db", database, "--csv", str(root / "fixtures" / "synthetic_prices.csv")),
                ("import-distributions", "--db", database, "--csv", str(root / "fixtures" / "synthetic_distributions.csv")),
                ("snapshot", "--db", database, "--as-of", "2026-08-30"),
                ("screens", "--db", database, "--as-of", "2026-08-30"),
                ("report", "--db", database, "--as-of", "2026-08-30", "--output", report),
            )
            for command in commands:
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

    def test_phase2_fixture_refresh_and_evidence_inspection_are_read_only(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "phase2.sqlite")
            refresh = self.run_cli(
                "phase2-refresh-fixtures", "--db", database,
                "--universe-csv", str(root / "fixtures" / self.REFERENCE_UNIVERSE),
                "--market-csv", str(root / "fixtures" / self.REFERENCE_MARKET),
                "--as-of", self.REFERENCE_AS_OF, "--request-id", "cli-phase2-a",
            )
            self.assertEqual(refresh.returncode, 0, refresh.stderr)
            self.assertIn('"accepted_universe_members": 2', refresh.stdout)
            self.assertIn('"accepted_market_observations": 40', refresh.stdout)
            evidence = self.run_cli(
                "phase2-evidence", "--db", database, "--as-of", self.REFERENCE_AS_OF,
            )

        self.assertEqual(evidence.returncode, 0, evidence.stderr)
        self.assertIn('"phase": "phase2-evidence"', evidence.stdout)
        for forbidden in ("BUY", "HOLD", "SELL", "ORDER", "BROKER"):
            self.assertNotIn(forbidden, evidence.stdout.upper())

    def test_help_lists_phase2_read_only_commands(self) -> None:
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("phase2-refresh-fixtures", result.stdout)
        self.assertIn("phase2-evidence", result.stdout)

    def test_phase2_failed_refresh_has_deterministic_nonzero_exit(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "phase2.sqlite")
            bad_sec = Path(directory) / "bad-sec.json"
            bad_sec.write_text("not-json", encoding="utf-8")
            result = self.run_cli(
                "phase2-refresh-fixtures", "--db", database,
                "--universe-csv", str(root / "fixtures" / self.REFERENCE_UNIVERSE),
                "--market-csv", str(root / "fixtures" / self.REFERENCE_MARKET),
                "--sec-json", str(bad_sec), "--sec-ticker", "AAPL", "--sec-cik", "0000320193",
                "--as-of", self.REFERENCE_AS_OF, "--request-id", "cli-phase2-failed",
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('"status": "failed"', result.stdout)

    def test_phase2_zero_active_fixture_refresh_has_nonzero_exit(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            universe = Path(directory) / "empty-universe.csv"
            market = Path(directory) / "market.csv"
            database = Path(directory) / "phase2.sqlite"
            universe.write_text(",".join((
                "universe_id", "effective_date", "membership_status", "ticker", "cik",
                "issuer_name", "exchange", "source_url", "source_version", "retrieved_at",
                "lookahead_bias_status", "survivorship_bias_status",
            )) + "\n", encoding="utf-8")
            market.write_text((root / "fixtures" / self.REFERENCE_MARKET).read_text(encoding="utf-8"), encoding="utf-8")
            result = self.run_cli(
                "phase2-refresh-fixtures", "--db", str(database),
                "--universe-csv", str(universe), "--market-csv", str(market),
                "--as-of", self.REFERENCE_AS_OF, "--request-id", "cli-zero-active",
            )

        self.assertEqual(result.returncode, 2)
        self.assertIn('"status": "failed"', result.stdout)

    def test_phase2_configured_refresh_with_no_provider_has_nonzero_exit(self) -> None:
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            database = str(Path(directory) / "phase2.sqlite")
            config = Path(directory) / "phase2.json"
            config.write_text('{"market":{"enabled":false},"sec":{"enabled":false},"rss":{"enabled":false}}', encoding="utf-8")
            result = self.run_cli(
                "phase2-refresh-config", "--db", database, "--config", str(config),
                "--universe-csv", str(root / "fixtures" / self.REFERENCE_UNIVERSE),
                "--as-of", self.REFERENCE_AS_OF, "--request-id", "cli-no-provider",
            )
        self.assertEqual(result.returncode, 2)
        self.assertIn('"status": "failed"', result.stdout)


if __name__ == "__main__":
    unittest.main()
