"""Regression tests for the twenty-fourth release-review repair."""

from __future__ import annotations

import sqlite3
import unittest

from tests.test_nineteenth_repair import recommendation_fixture

from nisa_quant.journal import evaluate_recommendation, record_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.schema import add_instrument_link, connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def add_instrument(
    connection: sqlite3.Connection,
    identifier_type: str,
    identifier_value: str,
    *,
    asset_type: str = "security",
    currency: str = "JPY",
) -> None:
    connection.execute(
        """
        INSERT INTO instruments(
            identifier_type, identifier_value, display_name, asset_type, currency
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (identifier_type, identifier_value, identifier_value, asset_type, currency),
    )
    connection.commit()


def add_fact(
    connection: sqlite3.Connection,
    *,
    source_identifier: str,
    identifier_type: str,
    identifier_value: str,
    field: str,
    value: str,
    observation_date: str = "2026-01-01",
    retrieved_at: str = "2026-01-01T00:00:00+00:00",
    unit: str = "price",
    currency: str = "JPY",
) -> str:
    return add_source_record(
        connection,
        source_name="repair24",
        source_identifier=source_identifier,
        retrieved_at=retrieved_at,
        observation_date=observation_date,
        instrument_identifier=identifier_value,
        instrument_identifier_type=identifier_type,
        field=field,
        value=value,
        unit=unit,
        currency=currency,
        freshness_status="current",
        citation_location=f"repair24:{source_identifier}",
        parser_version="repair24-v1",
    )


def add_watch_item(
    connection: sqlite3.Connection,
    *,
    identifier: str = "ASSET",
    benchmark: str | None = None,
    benchmark_type: str | None = None,
    benchmark_value: str | None = None,
) -> None:
    add_watchlist_item(
        connection,
        identifier_value=identifier,
        identifier_type="other",
        display_name=identifier,
        asset_type="ETF",
        market="JPX",
        currency="JPY",
        benchmark=benchmark,
        notes="repair24",
        effective_date="2026-01-01",
        observed_at="2026-01-01T00:00:00+00:00",
        benchmark_identifier_type=benchmark_type,
        benchmark_identifier_value=benchmark_value,
    )


def documented_outcome_fixture() -> tuple[sqlite3.Connection, int, str, str]:
    connection = new_connection()
    add_watch_item(
        connection, identifier="ASSET", benchmark="BENCH",
        benchmark_type="other", benchmark_value="BENCH",
    )
    add_fact(
        connection, source_identifier="asset-cutoff", identifier_type="other",
        identifier_value="ASSET", field="price", value="100",
        observation_date="2026-08-30", retrieved_at="2026-08-30T00:00:00+00:00",
    )
    add_fact(
        connection, source_identifier="benchmark-cutoff", identifier_type="other",
        identifier_value="BENCH", field="benchmark_price", value="10",
        observation_date="2026-08-30", retrieved_at="2026-08-30T00:00:00+00:00",
    )
    snapshot = calculate_snapshot(connection, as_of="2026-08-30")
    candidate = run_screens(connection, snapshot, as_of="2026-08-30")[0]
    recommendation_id = record_recommendation(
        connection, candidate, data_cutoff="2026-08-30", provider="local", template_version="v1",
        snapshot=snapshot,
    )
    observed_id = add_fact(
        connection, source_identifier="asset-evaluation", identifier_type="other",
        identifier_value="ASSET", field="price", value="110",
        observation_date="2026-09-01", retrieved_at="2026-09-02T00:00:00+00:00",
    )
    benchmark_id = add_fact(
        connection, source_identifier="benchmark-evaluation", identifier_type="other",
        identifier_value="BENCH", field="benchmark_price", value="11",
        observation_date="2026-09-01", retrieved_at="2026-09-02T00:00:00+00:00",
    )
    connection.commit()
    return connection, recommendation_id, observed_id, benchmark_id


class TwentyFourthLinkedFactTests(unittest.TestCase):
    def test_linked_instrument_price_conflict_is_unavailable_and_audit_only(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(connection)
        add_instrument(connection, "yahoo_symbol", "ASSET.T")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ASSET.T",
            link_type="explicit_alias",
        )
        asset_id = add_fact(
            connection, source_identifier="asset-price", identifier_type="other",
            identifier_value="ASSET", field="price", value="100",
        )
        alias_id = add_fact(
            connection, source_identifier="alias-price", identifier_type="yahoo_symbol",
            identifier_value="ASSET.T", field="price", value="101",
            retrieved_at="2026-01-01T01:00:00+00:00",
        )

        snapshot = calculate_snapshot(connection, as_of="2026-01-01")
        item = snapshot["watchlist"][0]

        self.assertEqual(item["price_status"], "conflicting")
        self.assertIsNone(item["latest_price"])
        self.assertNotIn(asset_id, {source["id"] for source in snapshot["sources"]})
        self.assertNotIn(alias_id, {source["id"] for source in snapshot["sources"]})

    def test_same_value_linked_instrument_control_remains_usable(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(connection)
        add_instrument(connection, "yahoo_symbol", "ASSET.T")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ASSET.T",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="asset-price", identifier_type="other",
            identifier_value="ASSET", field="price", value="100",
        )
        alias_id = add_fact(
            connection, source_identifier="alias-price", identifier_type="yahoo_symbol",
            identifier_value="ASSET.T", field="price", value="100.0",
            retrieved_at="2026-01-01T01:00:00+00:00",
        )

        item = calculate_snapshot(connection, as_of="2026-01-01")["watchlist"][0]

        self.assertEqual(item["latest_price"], 100.0)
        self.assertEqual(item["price_history"], [{"date": "2026-01-01", "price": 100.0, "source_id": alias_id}])

    def test_unrelated_typed_identity_does_not_conflict(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(connection)
        add_instrument(connection, "other", "UNRELATED")
        add_fact(
            connection, source_identifier="asset-price", identifier_type="other",
            identifier_value="ASSET", field="price", value="100",
        )
        add_fact(
            connection, source_identifier="unrelated-price", identifier_type="other",
            identifier_value="UNRELATED", field="price", value="999",
        )

        item = calculate_snapshot(connection, as_of="2026-01-01")["watchlist"][0]

        self.assertEqual(item["price_status"], "current")
        self.assertEqual(item["latest_price"], 100.0)

    def test_connected_link_component_reaches_a_third_typed_identity(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(connection)
        add_instrument(connection, "yahoo_symbol", "ASSET.T")
        add_instrument(connection, "isin", "ASSET-ISIN")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ASSET.T",
            link_type="explicit_alias",
        )
        add_instrument_link(
            connection,
            from_identifier_type="yahoo_symbol", from_identifier_value="ASSET.T",
            to_identifier_type="isin", to_identifier_value="ASSET-ISIN",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="asset-price", identifier_type="other",
            identifier_value="ASSET", field="price", value="100",
        )
        add_fact(
            connection, source_identifier="third-alias-price", identifier_type="isin",
            identifier_value="ASSET-ISIN", field="price", value="101",
        )

        item = calculate_snapshot(connection, as_of="2026-01-01")["watchlist"][0]

        self.assertEqual(item["price_status"], "conflicting")

    def test_linked_benchmark_conflict_is_unavailable(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(
            connection, benchmark="BENCH", benchmark_type="other", benchmark_value="BENCH",
        )
        add_instrument(connection, "other", "BENCH", asset_type="benchmark")
        add_instrument(connection, "yahoo_symbol", "BENCH.T", asset_type="benchmark")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="BENCH",
            to_identifier_type="yahoo_symbol", to_identifier_value="BENCH.T",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="bench-price", identifier_type="other",
            identifier_value="BENCH", field="benchmark_price", value="10",
        )
        add_fact(
            connection, source_identifier="bench-alias-price", identifier_type="yahoo_symbol",
            identifier_value="BENCH.T", field="benchmark_price", value="11",
        )

        item = calculate_snapshot(connection, as_of="2026-01-01")["watchlist"][0]

        self.assertIsNone(item["benchmark_price"])
        self.assertIn("CONFLICTING_BENCHMARK", {warning["code"] for warning in calculate_snapshot(connection, as_of="2026-01-01")["warnings"]})

    def test_same_value_linked_benchmark_control_is_one_history_point(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watch_item(
            connection, benchmark="BENCH", benchmark_type="other", benchmark_value="BENCH",
        )
        add_instrument(connection, "other", "BENCH", asset_type="benchmark")
        add_instrument(connection, "yahoo_symbol", "BENCH.T", asset_type="benchmark")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="BENCH",
            to_identifier_type="yahoo_symbol", to_identifier_value="BENCH.T",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="bench-price", identifier_type="other",
            identifier_value="BENCH", field="benchmark_price", value="10",
        )
        alias_id = add_fact(
            connection, source_identifier="bench-alias-price", identifier_type="yahoo_symbol",
            identifier_value="BENCH.T", field="benchmark_price", value="10.0",
            retrieved_at="2026-01-01T01:00:00+00:00",
        )

        item = calculate_snapshot(connection, as_of="2026-01-01")["watchlist"][0]

        self.assertEqual(item["benchmark_price"], 10.0)
        self.assertEqual(item["benchmark_source_ids"], [alias_id])

    def test_linked_ledger_fact_conflict_is_skipped_during_replay(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_instrument(connection, "other", "ASSET")
        add_instrument(connection, "yahoo_symbol", "ASSET.T")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ASSET.T",
            link_type="explicit_alias",
        )
        account_id = connection.execute(
            "SELECT id FROM accounts WHERE account_type = 'NISA'",
        ).fetchone()[0]
        import_id = connection.execute(
            "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('repair24', 'repair24', 'ledger', '2026-01-01T00:00:00+00:00') RETURNING id",
        ).fetchone()[0]
        source_id = add_fact(
            connection, source_identifier="asset-buy", identifier_type="other",
            identifier_value="ASSET", field="buy", value="100",
        )
        connection.execute(
            """
            INSERT INTO transactions(
                import_id, account_id, instrument_id, trade_date, transaction_type,
                quantity, price, fee, currency, distribution, source_record_id, row_hash
            ) VALUES (?, ?, (SELECT id FROM instruments WHERE identifier_type = 'other' AND identifier_value = 'ASSET'), '2026-01-01', 'BUY', 1, 100, 0, 'JPY', 0, ?, 'repair24-row')
            """,
            (import_id, account_id, source_id),
        )
        add_fact(
            connection, source_identifier="alias-buy", identifier_type="yahoo_symbol",
            identifier_value="ASSET.T", field="buy", value="101",
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-01")

        self.assertEqual(snapshot["portfolio"]["holdings"], [])
        self.assertIn("CONFLICTING_SOURCE_FACT", {warning["code"] for warning in snapshot["warnings"]})


class TwentyFourthOutcomeTests(unittest.TestCase):
    def test_documented_later_evaluation_accepts_cited_evidence(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = documented_outcome_fixture()
        self.addCleanup(connection.close)

        evaluate_recommendation(
            connection, recommendation_id, evaluation_date="2026-09-30",
            observed_price=110.0, benchmark_price=11.0,
            observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
        )

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0],
            1,
        )

    def test_linked_asset_conflict_independently_blocks_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        add_instrument(connection, "yahoo_symbol", "ALT")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ALT",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="asset-alt-conflict", identifier_type="yahoo_symbol",
            identifier_value="ALT", field="price", value="111",
            observation_date="2026-01-02", retrieved_at="2026-01-02T00:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0)

    def test_linked_benchmark_conflict_independently_blocks_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        add_instrument(connection, "other", "BENCH", asset_type="benchmark")
        add_instrument(connection, "yahoo_symbol", "ALT-BENCH", asset_type="benchmark")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="BENCH",
            to_identifier_type="yahoo_symbol", to_identifier_value="ALT-BENCH",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="benchmark-alt-conflict", identifier_type="yahoo_symbol",
            identifier_value="ALT-BENCH", field="benchmark_price", value="12",
            observation_date="2026-01-02", retrieved_at="2026-01-02T00:00:00+00:00",
        )

        with self.assertRaisesRegex(ValueError, "conflicts"):
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0)

    def test_linked_outcome_conflict_does_not_insert_an_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        add_instrument(connection, "yahoo_symbol", "ASSET.T")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="ASSET",
            to_identifier_type="yahoo_symbol", to_identifier_value="ASSET.T",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="linked-outcome-price", identifier_type="yahoo_symbol",
            identifier_value="ASSET.T", field="price", value="111",
            observation_date="2026-01-02", retrieved_at="2026-01-02T00:00:00+00:00",
        )
        add_instrument(connection, "other", "BENCH", asset_type="benchmark")
        add_instrument(connection, "yahoo_symbol", "ALT-BENCH")
        add_instrument_link(
            connection,
            from_identifier_type="other", from_identifier_value="BENCH",
            to_identifier_type="yahoo_symbol", to_identifier_value="ALT-BENCH",
            link_type="explicit_alias",
        )
        add_fact(
            connection, source_identifier="linked-outcome-benchmark", identifier_type="yahoo_symbol",
            identifier_value="ALT-BENCH", field="benchmark_price", value="12",
            observation_date="2026-01-02", retrieved_at="2026-01-02T00:00:00+00:00",
        )

        with self.assertRaises(ValueError):
            evaluate_recommendation(
                connection, recommendation_id, evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
