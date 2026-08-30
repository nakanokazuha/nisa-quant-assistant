"""Regression tests for the twenty-fifth release-review repair."""

from __future__ import annotations

import unittest

from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.sources import add_source_record


class TwentyFifthSourceChronologyTests(unittest.TestCase):
    def test_direct_source_ingestion_rejects_missing_observation_date_without_persisting(self) -> None:
        connection = connect_database(":memory:")
        initialize_database(connection)
        self.addCleanup(connection.close)

        with self.assertRaisesRegex(ValueError, "observation date"):
            add_source_record(
                connection,
                source_name="repair25",
                source_identifier="missing-observation",
                retrieved_at="2026-01-01T00:00:00+00:00",
                observation_date=None,
                instrument_identifier="ASSET",
                instrument_identifier_type="other",
                field="price",
                value="100",
                unit="price",
                currency="JPY",
                freshness_status="current",
                citation_location="repair25:missing-observation",
                parser_version="repair25-v1",
            )

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM source_records").fetchone()[0],
            0,
        )
