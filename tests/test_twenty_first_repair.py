"""Regression tests for the twenty-first release-review repair."""

from __future__ import annotations

import json
import unittest

from tests.test_nineteenth_repair import minimal_report, recommendation_fixture

from nisa_quant.journal import evaluate_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import validate_report
from nisa_quant.screens import run_screens
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item


SOURCE_ID = "SRC-abcdef123456"


def _source_ids_in_provenance(snapshot: dict[str, object]) -> set[str]:
    portfolio = snapshot["portfolio"]
    assert isinstance(portfolio, dict)
    provenance = portfolio["provenance"]
    assert isinstance(provenance, dict)
    return {
        source_id
        for item in provenance.values()
        for source_id in item["source_ids"]
    }


class TwentyFirstRepairConflictTests(unittest.TestCase):
    def test_conflicting_price_rows_remain_audit_only_and_cannot_bind_recommendations(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        original_price_id = connection.execute(
            "SELECT id FROM source_records WHERE field = 'price' AND instrument_identifier = 'ASSET'"
        ).fetchone()[0]
        conflicting_price_id = add_source_record(
            connection,
            source_name="repair21",
            source_identifier="conflicting-price",
            retrieved_at="2026-01-01T00:00:00+00:00",
            observation_date="2026-01-01",
            instrument_identifier="ASSET",
            instrument_identifier_type="other",
            field="price",
            value="101",
            unit="price",
            currency="JPY",
            freshness_status="current",
            citation_location="repair21:conflicting-price",
            parser_version="repair21-v1",
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        self.assertEqual(connection.execute(
            "SELECT COUNT(*) FROM source_records WHERE id IN (?, ?)",
            (original_price_id, conflicting_price_id),
        ).fetchone()[0], 2)
        published_ids = {source["id"] for source in snapshot["sources"]}
        self.assertNotIn(original_price_id, published_ids)
        self.assertNotIn(conflicting_price_id, published_ids)
        self.assertIn("CONFLICTING_PRICE", {warning["code"] for warning in snapshot["warnings"]})
        self.assertIsNone(snapshot["watchlist"][0]["latest_price"])
        self.assertEqual(snapshot["watchlist"][0]["source_ids"], [])
        self.assertNotIn(original_price_id, _source_ids_in_provenance(snapshot))
        self.assertNotIn(conflicting_price_id, _source_ids_in_provenance(snapshot))
        self.assertIsNone(snapshot["data_cutoffs"]["prices"])

        candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
        self.assertNotIn(original_price_id, candidate["source_ids"])
        self.assertNotIn(conflicting_price_id, candidate["source_ids"])

        from nisa_quant.journal import record_recommendation

        safe_recommendation_id = record_recommendation(
            connection,
            candidate,
            data_cutoff="2026-01-01",
            provider="local",
            template_version="v1",
            snapshot=snapshot,
        )
        persisted_source_ids = set(json.loads(connection.execute(
            "SELECT source_ids FROM recommendations WHERE id = ?", (safe_recommendation_id,)
        ).fetchone()[0]))
        self.assertNotIn(original_price_id, persisted_source_ids)
        self.assertNotIn(conflicting_price_id, persisted_source_ids)

        with self.assertRaisesRegex(ValueError, "generated snapshot"):
            evaluate_recommendation(
                connection,
                recommendation_id,
                evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0)


class TwentyFirstRepairReportSafetyTests(unittest.TestCase):
    def test_standalone_action_gerunds_and_variants_are_rejected(self) -> None:
        forbidden = (
            "BUYING now",
            "SELLING shares",
            "Accumulating units",
            "Purchasing tomorrow",
            "Holding shares",
            "Watching earnings",
            "b.u.y.i.n.g now",
            "S\u200bE\u200bL\u200bL\u200bI\u200bN\u200bG shares",
        )
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_action_gerunds_are_scanned_in_structured_metadata_and_controls_remain_safe(self) -> None:
        for value in (
            "Watching earnings",
            "Accumulating units",
            "Purchasing tomorrow",
            "Holding shares",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_report(
                        minimal_report("stable evidence"),
                        source_records=[{"id": SOURCE_ID, "source_name": value}],
                    )

        validate_report(
            minimal_report("stable evidence"),
            source_records=[{"id": SOURCE_ID, "source_name": "local prices.csv"}],
        )


class TwentyFirstRepairIdentifierSafetyTests(unittest.TestCase):
    def test_all_separator_delimited_identifiers_are_rejected(self) -> None:
        forbidden = (
            "account\nABC123",
            "customer\r\nCLIENT99",
            "broker\u2028ABC123",
            "account•ABC123",
            "a\u200bc\u200bc\u200bo\u200bu\u200bn\u200bt\u200bABC123",
        )
        for phrase in forbidden:
            with self.subTest(phrase=repr(phrase)):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

    def test_person_like_non_ascii_filenames_are_rejected_but_generic_controls_are_safe(self) -> None:
        for filename in ("山田太郎.csv", "田中花子.json"):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(f"See {filename}"), source_records=[SOURCE_ID])

        for filename in ("prices.csv", "価格.csv"):
            with self.subTest(filename=filename):
                validate_report(minimal_report(f"See {filename}"), source_records=[SOURCE_ID])


if __name__ == "__main__":
    unittest.main()
