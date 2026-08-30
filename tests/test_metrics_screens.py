import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.imports import import_csv
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import import_price_fixture
from tests.test_time_helpers import current_utc_date


FIXTURE = Path(__file__).parent / "fixtures" / "synthetic_broker.csv"


class MetricsAndScreensTests(unittest.TestCase):
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
        self.as_of = current_utc_date()

    def tearDown(self) -> None:
        self.connection.close()
        self.tempdir.cleanup()

    def test_snapshot_has_deterministic_value_and_quality_warnings(self) -> None:
        snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        self.assertIsNone(snapshot["portfolio"]["market_value"])
        self.assertEqual(snapshot["portfolio"]["contributions"], 0.0)
        self.assertEqual(snapshot["portfolio"]["distributions"], 120.0)
        warning_codes = {warning["code"] for warning in snapshot["warnings"]}
        self.assertIn("UNKNOWN_ACCOUNT", warning_codes)
        self.assertIn("STALE_PRICE", warning_codes)
        self.assertIn("CONFLICTING_PRICE", warning_codes)

    def test_stale_or_conflicting_required_data_is_non_directional(self) -> None:
        snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        candidates = run_screens(self.connection, snapshot, as_of=self.as_of)
        labels = {candidate["instrument"]: candidate["label"] for candidate in candidates}
        self.assertEqual(labels["1306"], "NO ACTION / INSUFFICIENT DATA")
        self.assertIn(labels["9984"], {"WATCH", "NO ACTION / INSUFFICIENT DATA"})

    def test_missing_price_does_not_become_zero(self) -> None:
        self.connection.execute("DELETE FROM source_records WHERE field = 'price'")
        self.connection.commit()
        snapshot = calculate_snapshot(self.connection, as_of=self.as_of)
        self.assertIsNone(snapshot["portfolio"]["market_value"])
        self.assertTrue(any(w["code"] == "MISSING_PRICE" for w in snapshot["warnings"]))


if __name__ == "__main__":
    unittest.main()
