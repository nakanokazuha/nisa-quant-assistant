import copy
import hashlib
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from nisa_quant.journal import evaluate_recommendation, record_recommendation
from nisa_quant.metrics import calculate_snapshot
from nisa_quant.reports import validate_report
from nisa_quant.schema import connect_database, initialize_database
from nisa_quant.screens import run_screens
from nisa_quant.sources import add_source_record
from nisa_quant.watchlist import add_watchlist_item, current_materialization_date, materialize_current, watchlist_as_of


SOURCE_ID = "SRC-abcdef123456"


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


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


def canonical_hash(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def recommendation_fixture() -> tuple[sqlite3.Connection, int, str, str]:
    connection = new_connection()
    add_watchlist_item(
        connection, "ASSET", "other", "Asset", "ETF", "JPX", "JPY", "BENCH", "watch",
        effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00", benchmark_identifier_type="other",
        benchmark_identifier_value="BENCH",
    )
    add_source_record(
        connection, source_name="repair18", source_identifier="asset-before",
        retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
        instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
        value="100", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair18:asset-before", parser_version="repair18-v1",
    )
    add_source_record(
        connection, source_name="repair18", source_identifier="bench-before",
        retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
        instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
        value="10", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair18:bench-before", parser_version="repair18-v1",
    )
    snapshot = calculate_snapshot(connection, as_of="2026-01-01")
    candidate = run_screens(connection, snapshot, as_of="2026-01-01")[0]
    recommendation_id = record_recommendation(
        connection, candidate, data_cutoff="2026-01-01", provider="local", template_version="v1",
        snapshot=snapshot,
    )
    observed_id = add_source_record(
        connection, source_name="repair18", source_identifier="asset-after",
        retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
        instrument_identifier="ASSET", instrument_identifier_type="other", field="price",
        value="110", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair18:asset-after", parser_version="repair18-v1",
    )
    benchmark_id = add_source_record(
        connection, source_name="repair18", source_identifier="bench-after",
        retrieved_at="2026-01-02T00:00:00+00:00", observation_date="2026-01-02",
        instrument_identifier="BENCH", instrument_identifier_type="other", field="benchmark_price",
        value="11", unit="price", currency="JPY", freshness_status="current",
        citation_location="repair18:bench-after", parser_version="repair18-v1",
    )
    return connection, recommendation_id, observed_id, benchmark_id


class EighteenthRepairWatchlistTests(unittest.TestCase):
    def test_historical_materialization_does_not_create_a_future_metadata_override(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_watchlist_item(
            connection, "ASSET", "other", "OLD", "ETF", "JPX", "JPY", None, "old",
            effective_date="2026-01-01", observed_at="2026-01-01T00:00:00+00:00",
        )
        add_watchlist_item(
            connection, "ASSET", "other", "NEW", "ETF", "NYSE", "USD", None, "new",
            effective_date="2026-02-01", observed_at="2026-02-01T00:00:00+00:00",
        )

        materialize_current(connection, as_of="2026-01-15")
        materialize_current(connection, as_of="2026-01-15")

        historical = watchlist_as_of(connection, as_of="2026-01-15")
        self.assertEqual([(row["display_name"], row["currency"]) for row in historical], [("OLD", "JPY")])
        current_projection = connection.execute(
            "SELECT display_name, currency FROM watchlist WHERE identifier_value = 'ASSET'"
        ).fetchone()
        self.assertEqual(tuple(current_projection), ("OLD", "JPY"))
        self.assertFalse(connection.execute(
            "SELECT 1 FROM watchlist_projection_overrides WHERE display_name = 'NEW' AND effective_from <= '2026-01-15'"
        ).fetchone())

        materialize_current(connection)
        self.assertEqual(
            tuple(connection.execute(
                "SELECT display_name, currency FROM watchlist WHERE identifier_value = 'ASSET'"
            ).fetchone()),
            ("NEW", "USD"),
        )
        self.assertEqual(watchlist_as_of(connection, as_of=current_materialization_date())[0]["display_name"], "NEW")


class EighteenthRepairOverflowTests(unittest.TestCase):
    def test_overflowed_currency_accumulator_stays_unavailable_for_following_holdings(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        account_id = connection.execute(
            "SELECT id FROM accounts WHERE account_type = 'NISA'"
        ).fetchone()[0]
        import_id = connection.execute(
            "INSERT INTO imports(file_hash, source_name, source_identifier, imported_at) VALUES ('repair18', 'repair18', 'repair18', '2026-01-01T00:00:00+00:00') RETURNING id"
        ).fetchone()[0]
        for number, identifier, price in ((1, "BIG-A", "1e308"), (2, "BIG-B", "1e308"), (3, "SMALL-C", "1")):
            instrument_id = connection.execute(
                "INSERT INTO instruments(identifier_type, identifier_value, display_name, asset_type, market, currency) VALUES ('other', ?, ?, 'stock', 'TEST', 'JPY') RETURNING id",
                (identifier, identifier),
            ).fetchone()[0]
            source_id = add_source_record(
                connection, source_name="repair18", source_identifier=f"buy-{identifier}",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier=identifier, instrument_identifier_type="other", field="buy",
                value=price, unit="price", currency="JPY", freshness_status="current",
                citation_location=f"repair18:{identifier}:buy", parser_version="repair18-v1",
            )
            add_source_record(
                connection, source_name="repair18", source_identifier=f"price-{identifier}",
                retrieved_at="2026-01-01T00:00:00+00:00", observation_date="2026-01-01",
                instrument_identifier=identifier, instrument_identifier_type="other", field="price",
                value=price, unit="price", currency="JPY", freshness_status="current",
                citation_location=f"repair18:{identifier}:price", parser_version="repair18-v1",
            )
            connection.execute(
                "INSERT INTO transactions(import_id, account_id, instrument_id, trade_date, transaction_type, quantity, price, fee, currency, distribution, source_record_id, row_hash) VALUES (?, ?, ?, '2026-01-01', 'BUY', 1, ?, 0, 'JPY', 0, ?, ?)",
                (import_id, account_id, instrument_id, float(price), source_id, f"repair18-row-{number}"),
            )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        portfolio = snapshot["portfolio"]

        self.assertIsNone(portfolio["market_value_by_currency"])
        self.assertIsNone(portfolio["market_value"])
        self.assertIn("NONFINITE_DERIVED_VALUE", {warning["code"] for warning in snapshot["warnings"]})
        json.dumps(snapshot, allow_nan=False)


class EighteenthRepairJournalTests(unittest.TestCase):
    def test_valid_persisted_snapshot_still_allows_an_outcome(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        self.addCleanup(connection.close)
        evaluate_recommendation(
            connection, recommendation_id, evaluation_date="2026-01-02",
            observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
        )
        self.assertEqual(connection.execute("SELECT COUNT(*) FROM recommendation_outcomes").fetchone()[0], 1)
        json.loads(connection.execute("SELECT snapshot_json FROM recommendation_outcomes").fetchone()[0])

    def test_persisted_snapshot_requires_strict_shape_and_types(self) -> None:
        connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
        original = json.loads(connection.execute(
            "SELECT snapshot_json FROM recommendations WHERE id = ?", (recommendation_id,)
        ).fetchone()[0])
        connection.close()
        mutations = []
        for mutate in (
            lambda value: value["portfolio"].__setitem__("market_value", "not-a-number"),
            lambda value: value["portfolio"].__setitem__("holdings", {"bad": "shape"}),
            lambda value: value["portfolio"]["risk_series"].__setitem__("values", ["not-a-number"]),
            lambda value: value["portfolio"].__setitem__("unsafe", "unknown"),
        ):
            mutated = copy.deepcopy(original)
            mutate(mutated)
            mutations.append((json.dumps(mutated, allow_nan=False), canonical_hash(mutated)))

        for snapshot_json, snapshot_hash in mutations:
            with self.subTest(snapshot_json=snapshot_json):
                connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
                connection.execute(
                    "UPDATE recommendations SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?",
                    (snapshot_json, snapshot_hash, recommendation_id),
                )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                    )
                connection.execute("DELETE FROM recommendation_outcomes")
                connection.close()

    def test_persisted_snapshot_rejects_hash_gaps_missing_hash_and_nonfinite_json(self) -> None:
        payloads = (
            ("valid", "hash-gap"),
            ("valid", "missing-hash"),
            ("{\"portfolio\": {\"market_value\": NaN}}", "nonfinite-nan"),
            ("{\"portfolio\": {\"market_value\": Infinity}}", "nonfinite-infinity"),
        )
        for snapshot_json, snapshot_hash in payloads:
            with self.subTest(snapshot_hash=snapshot_hash):
                connection, recommendation_id, observed_id, benchmark_id = recommendation_fixture()
                if snapshot_json == "valid":
                    connection.execute(
                        "UPDATE recommendations SET snapshot_hash = ? WHERE id = ?",
                        ("" if snapshot_hash == "missing-hash" else "0" * 64, recommendation_id),
                    )
                else:
                    connection.execute(
                        "UPDATE recommendations SET snapshot_json = ?, snapshot_hash = ? WHERE id = ?",
                        (snapshot_json, "0" * 64, recommendation_id),
                    )
                connection.commit()
                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection, recommendation_id, evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id, benchmark_price_source_id=benchmark_id,
                    )
                connection.close()


class EighteenthRepairReportSafetyTests(unittest.TestCase):
    def test_bare_account_customer_broker_and_portfolio_identifiers_are_rejected(self) -> None:
        for phrase in ("account ABC123", "broker BROKER123", "customer CLIENT123", "portfolio RETIREMENT123"):
            with self.subTest(phrase=phrase):
                with self.assertRaises(ValueError):
                    validate_report(minimal_report(phrase), source_records=[SOURCE_ID])

        for phrase in ("account category", "broker research", "customer records", "portfolio snapshot"):
            with self.subTest(phrase=phrase):
                validate_report(minimal_report(phrase), source_records=[SOURCE_ID])


if __name__ == "__main__":
    unittest.main()
