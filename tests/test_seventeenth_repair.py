import ast
import json
import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import nisa_quant.candidate_screening as screens
from nisa_quant.recommendation_journal import evaluate_recommendation
from nisa_quant.portfolio_metrics import calculate_snapshot
from nisa_quant.database_schema import connect_database, initialize_database
from nisa_quant.source_records import add_source_record


def new_connection() -> sqlite3.Connection:
    connection = connect_database(":memory:")
    initialize_database(connection)
    return connection


def add_overflow_holding(
    connection: sqlite3.Connection,
    *,
    identifier: str,
    row_number: int,
) -> None:
    account_id = connection.execute(
        "SELECT id FROM accounts WHERE account_type = 'NISA'"
    ).fetchone()[0]
    instrument_id = connection.execute(
        """
        INSERT INTO instruments(
            identifier_type, identifier_value, display_name, asset_type, market, currency
        ) VALUES ('other', ?, ?, 'stock', 'TEST', 'JPY')
        RETURNING id
        """,
        (identifier, identifier),
    ).fetchone()[0]
    import_id = connection.execute(
        """
        INSERT INTO imports(file_hash, source_name, source_identifier, imported_at)
        VALUES (?, 'repair17', ?, '2026-01-01T00:00:00+00:00')
        RETURNING id
        """,
        (f"hash-{identifier}", f"source-{identifier}"),
    ).fetchone()[0]
    source_id = add_source_record(
        connection,
        source_name="repair17",
        source_identifier=f"buy-{identifier}",
        retrieved_at="2026-01-01T00:00:00+00:00",
        observation_date="2026-01-01",
        instrument_identifier=identifier,
        instrument_identifier_type="other",
        field="buy",
        value="1e308",
        unit="price",
        currency="JPY",
        freshness_status="current",
        citation_location=f"repair17:{identifier}:buy",
        parser_version="repair17-v1",
    )
    add_source_record(
        connection,
        source_name="repair17",
        source_identifier=f"price-{identifier}",
        retrieved_at="2026-01-01T00:00:00+00:00",
        observation_date="2026-01-01",
        instrument_identifier=identifier,
        instrument_identifier_type="other",
        field="price",
        value="1e308",
        unit="price",
        currency="JPY",
        freshness_status="current",
        citation_location=f"repair17:{identifier}:price",
        parser_version="repair17-v1",
    )
    connection.execute(
        """
        INSERT INTO transactions(
            import_id, account_id, instrument_id, trade_date, transaction_type,
            quantity, price, fee, currency, distribution, source_record_id, row_hash
        ) VALUES (?, ?, ?, '2026-01-01', 'BUY', 1, 1e308, 0, 'JPY', 0, ?, ?)
        """,
        (import_id, account_id, instrument_id, source_id, f"row-{row_number}"),
    )


def add_outcome_sources(connection: sqlite3.Connection) -> tuple[str, str]:
    observed_id = add_source_record(
        connection,
        source_name="repair17",
        source_identifier="outcome-price",
        retrieved_at="2026-01-02T00:00:00+00:00",
        observation_date="2026-01-02",
        instrument_identifier="ASSET",
        instrument_identifier_type="other",
        field="price",
        value="110",
        unit="price",
        currency="JPY",
        freshness_status="current",
        citation_location="repair17:outcome-price",
        parser_version="repair17-v1",
    )
    benchmark_id = add_source_record(
        connection,
        source_name="repair17",
        source_identifier="outcome-benchmark",
        retrieved_at="2026-01-02T00:00:00+00:00",
        observation_date="2026-01-02",
        instrument_identifier="BENCH",
        instrument_identifier_type="other",
        field="benchmark_price",
        value="11",
        unit="price",
        currency="JPY",
        freshness_status="current",
        citation_location="repair17:outcome-benchmark",
        parser_version="repair17-v1",
    )
    return observed_id, benchmark_id


def add_extreme_history_holding(connection: sqlite3.Connection) -> None:
    account_id = connection.execute(
        "SELECT id FROM accounts WHERE account_type = 'NISA'"
    ).fetchone()[0]
    instrument_id = connection.execute(
        """
        INSERT INTO instruments(
            identifier_type, identifier_value, display_name, asset_type, market, currency
        ) VALUES ('other', 'EXTREME', 'EXTREME', 'stock', 'TEST', 'JPY')
        RETURNING id
        """
    ).fetchone()[0]
    import_id = connection.execute(
        """
        INSERT INTO imports(file_hash, source_name, source_identifier, imported_at)
        VALUES ('extreme-history', 'repair17', 'extreme-history', '2026-01-01T00:00:00+00:00')
        RETURNING id
        """
    ).fetchone()[0]
    buy_id = add_source_record(
        connection,
        source_name="repair17",
        source_identifier="extreme-buy",
        retrieved_at="2026-01-01T00:00:00+00:00",
        observation_date="2026-01-01",
        instrument_identifier="EXTREME",
        instrument_identifier_type="other",
        field="buy",
        value="1",
        unit="price",
        currency="JPY",
        freshness_status="current",
        citation_location="repair17:extreme-buy",
        parser_version="repair17-v1",
    )
    connection.execute(
        """
        INSERT INTO transactions(
            import_id, account_id, instrument_id, trade_date, transaction_type,
            quantity, price, fee, currency, distribution, source_record_id, row_hash
        ) VALUES (?, ?, ?, '2026-01-01', 'BUY', 1, 1, 0, 'JPY', 0, ?, 'extreme-buy-row')
        """,
        (import_id, account_id, instrument_id, buy_id),
    )
    for day, value in (("2026-01-01", "1e-308"), ("2026-01-02", "1e308")):
        add_source_record(
            connection,
            source_name="repair17",
            source_identifier=f"extreme-price-{day}",
            retrieved_at=f"{day}T00:00:00+00:00",
            observation_date=day,
            instrument_identifier="EXTREME",
            instrument_identifier_type="other",
            field="price",
            value=value,
            unit="price",
            currency="JPY",
            freshness_status="current",
            citation_location=f"repair17:extreme-price-{day}",
            parser_version="repair17-v1",
        )


def add_realized_overflow_holding(
    connection: sqlite3.Connection,
    *,
    identifier: str,
) -> str:
    account_id = connection.execute(
        "SELECT id FROM accounts WHERE account_type = 'NISA'"
    ).fetchone()[0]
    instrument_id = connection.execute(
        """
        INSERT INTO instruments(
            identifier_type, identifier_value, display_name, asset_type, market, currency
        ) VALUES ('other', ?, ?, 'stock', 'TEST', 'JPY')
        RETURNING id
        """,
        (identifier, identifier),
    ).fetchone()[0]
    import_id = connection.execute(
        """
        INSERT INTO imports(file_hash, source_name, source_identifier, imported_at)
        VALUES (?, 'repair17', ?, '2026-01-01T00:00:00+00:00')
        RETURNING id
        """,
        (f"realized-{identifier}", f"realized-{identifier}"),
    ).fetchone()[0]
    source_ids: dict[str, str] = {}
    for transaction_type, value, day in (
        ("buy", "1", "2026-01-01"),
        ("sell", "1e308", "2026-01-02"),
    ):
        source_ids[transaction_type] = add_source_record(
            connection,
            source_name="repair17",
            source_identifier=f"realized-{identifier}-{transaction_type}",
            retrieved_at=f"{day}T00:00:00+00:00",
            observation_date=day,
            instrument_identifier=identifier,
            instrument_identifier_type="other",
            field=transaction_type,
            value=value,
            unit="price",
            currency="JPY",
            freshness_status="current",
            citation_location=f"repair17:{identifier}:{transaction_type}",
            parser_version="repair17-v1",
        )
        connection.execute(
            """
            INSERT INTO transactions(
                import_id, account_id, instrument_id, trade_date, transaction_type,
                quantity, price, fee, currency, distribution, source_record_id, row_hash
            ) VALUES (?, ?, ?, ?, ?, 1, ?, 0, 'JPY', 0, ?, ?)
            """,
            (
                import_id, account_id, instrument_id, day, transaction_type.upper(),
                value, source_ids[transaction_type],
                f"realized-{identifier}-{transaction_type}-row",
            ),
        )
    return source_ids["sell"]


def insert_recommendation(
    connection: sqlite3.Connection,
    *,
    metrics_json: str,
    snapshot_json: str = "{}",
) -> None:
    connection.execute(
        """
        INSERT INTO recommendations(
            created_at, data_cutoff, provider, template_version, source_ids, instrument,
            label, metrics_json, reason, risk, horizon, invalidation, snapshot_json,
            snapshot_hash, identifier_type, identifier_value, benchmark_identifier_type,
            benchmark_identifier_value, currency, freshness_status
        ) VALUES ('2026-01-01T00:00:00+00:00', '2026-01-01', 'local', 'v1', '[]',
            'ASSET', 'WATCH', ?, 'reason', 'risk', 'horizon', 'invalid', ?, 'hash',
            'other', 'ASSET', 'other', 'BENCH', 'JPY', 'current')
        """,
        (metrics_json, snapshot_json),
    )
    connection.commit()


class SeventeenthRepairOverflowTests(unittest.TestCase):
    def test_same_currency_overflow_never_publishes_partial_aggregates(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_overflow_holding(connection, identifier="ASSET-A", row_number=1)
        add_overflow_holding(connection, identifier="ASSET-B", row_number=2)
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        portfolio = snapshot["portfolio"]

        self.assertIsNone(portfolio["market_value_by_currency"])
        self.assertIsNone(portfolio["cost_basis_by_currency"])
        self.assertIsNone(portfolio["market_value"])
        self.assertIsNone(portfolio["cost_basis"])
        self.assertIsNone(portfolio["unrealized_pl"])
        self.assertIsNone(portfolio["allocation"]["account_pct"])
        self.assertIsNone(portfolio["concentration"]["largest_holding_pct"])
        self.assertIsNone(portfolio["volatility"])
        self.assertIn(
            "NONFINITE_DERIVED_VALUE",
            {warning["code"] for warning in snapshot["warnings"]},
        )
        json.dumps(snapshot, allow_nan=False)

    def test_extreme_return_overflow_is_unavailable_with_warning(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        add_extreme_history_holding(connection)
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        holding = snapshot["portfolio"]["holdings"][0]

        self.assertIsNone(holding["price_return_pct"])
        self.assertIsNone(holding["max_drawdown_pct"])
        self.assertIsNone(holding["volatility_pct"])
        self.assertIn(
            "NONFINITE_DERIVED_VALUE",
            {warning["code"] for warning in snapshot["warnings"]},
        )
        self.assertTrue(
            any(
                warning["code"] == "NONFINITE_DERIVED_VALUE"
                and warning.get("instrument") == "EXTREME"
                for warning in snapshot["warnings"]
            )
        )
        json.dumps(snapshot, allow_nan=False)

    def test_realized_overflow_does_not_claim_skipped_sell_provenance(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        first_sell = add_realized_overflow_holding(
            connection, identifier="REALIZED-A",
        )
        second_sell = add_realized_overflow_holding(
            connection, identifier="REALIZED-B",
        )
        connection.commit()

        snapshot = calculate_snapshot(connection, as_of="2026-01-02")
        portfolio = snapshot["portfolio"]
        cited_ledger_ids = set(portfolio["provenance"]["realized_pl_by_currency"]["source_ids"])
        cited_ledger_ids.update(
            source_id
            for holding in portfolio["holdings"]
            for source_id in holding["ledger_source_ids"]
        )

        self.assertIsNone(portfolio["realized_pl_by_currency"])
        self.assertIn(first_sell, cited_ledger_ids)
        self.assertNotIn(second_sell, cited_ledger_ids)


class SeventeenthRepairJournalTests(unittest.TestCase):
    def test_nonfinite_or_malformed_persisted_metrics_fail_closed(self) -> None:
        payloads = (
            '{"current_price": NaN, "benchmark_price": 100}',
            '{"current_price": Infinity, "benchmark_price": 100}',
            '{"current_price": -Infinity, "benchmark_price": 100}',
            '{"current_price": 100, "benchmark_price": 100, "nested": {"bad": NaN}}',
            '{"current_price": "100", "benchmark_price": 100}',
            '[]',
        )
        for metrics_json in payloads:
            with self.subTest(metrics_json=metrics_json):
                connection = new_connection()
                observed_id, benchmark_id = add_outcome_sources(connection)
                insert_recommendation(connection, metrics_json=metrics_json)

                with self.assertRaises(ValueError):
                    evaluate_recommendation(
                        connection,
                        1,
                        evaluation_date="2026-01-02",
                        observed_price_source_id=observed_id,
                        benchmark_price_source_id=benchmark_id,
                    )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM recommendation_outcomes"
                    ).fetchone()[0],
                    0,
                )
                connection.close()

    def test_nonfinite_persisted_snapshot_fails_closed(self) -> None:
        connection = new_connection()
        self.addCleanup(connection.close)
        observed_id, benchmark_id = add_outcome_sources(connection)
        insert_recommendation(
            connection,
            metrics_json='{"current_price": 100, "benchmark_price": 100}',
            snapshot_json='{"portfolio": {"market_value": NaN}}',
        )

        with self.assertRaises(ValueError):
            evaluate_recommendation(
                connection,
                1,
                evaluation_date="2026-01-02",
                observed_price_source_id=observed_id,
                benchmark_price_source_id=benchmark_id,
            )
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM recommendation_outcomes"
            ).fetchone()[0],
            0,
        )


class SeventeenthRepairScreenTests(unittest.TestCase):
    def test_candidate_builder_has_one_reachable_return_path(self) -> None:
        tree = ast.parse(Path(screens.__file__).read_text(encoding="utf-8"))
        builders = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_candidate_from_record"
        ]
        self.assertEqual(len(builders), 1)
        self.assertEqual(
            len([node for node in ast.walk(builders[0]) if isinstance(node, ast.Return)]),
            1,
        )


if __name__ == "__main__":
    unittest.main()
